"""
装配体 GLB 合并脚本（独立工具，流式合并版）

把 cad-splitter / glb-splitter 输出的多个零件 glTF + BIN 按结构树合并成一个装配 GLB
（流式：BIN 数据直接搬运，不整体进内存）

输入：一个目录，包含
    - 一个结构树 *.json（cad-splitter / glb-splitter 的输出）
    - meshs/ 子目录，里面是每个零件的 xxx__hash.gltf + 同名 .bin
      （可选）贴图文件 tex_xxx.png 等

输出：合并后的单个 .glb

材质处理：
    - 每个子 gltf 的 materials / textures / samplers / images 全部展开到全局
    - 图片二进制追加到最终 GLB 的 BIN 段
    - 跨 gltf 内容相同去重（图片/材质/sampler/texture 都按内容去重）
    - primitive.material 索引映射到全局材质表

兼容字段：
    - type: "mesh" / "part"       → 有几何
    - type: "node" / "assembly"   → 纯装配
    - asset                       → 关联的子 glTF 路径
    - transform.scale             → 单值 float 或 [x,y,z] 数组
"""

import os
import sys
import re
import copy
import json
import glob
import base64
import struct
import shutil
import tempfile
import hashlib
from typing import Dict, Any, List, Optional, Tuple


def log(msg: str) -> None:
    print(msg, flush=True)


def log_err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


DEFAULT_OUTPUT_FILENAME = "assembly"
COPY_CHUNK_SIZE = 4 * 1024 * 1024


def _norm_scale(v, default=None) -> List[float]:
    if default is None:
        default = [1.0, 1.0, 1.0]
    if v is None:
        return list(default)
    if isinstance(v, (int, float)):
        return [float(v)] * 3
    if isinstance(v, (list, tuple)):
        if len(v) == 3:
            return [float(x) for x in v]
        if len(v) == 1:
            return [float(v[0])] * 3
    return list(default)


def _sniff_image_mime(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".gif": "image/gif",
        ".ktx2": "image/ktx2",
        ".dds": "image/vnd-ms.dds",
    }.get(ext, "image/png")


class GltfMerger:

    def __init__(
        self,
        input_dir: str,
        output_dir: str,
        output_filename: Optional[str] = None,
    ):
        self.input_dir = os.path.abspath(input_dir)
        self.output_dir = os.path.abspath(output_dir)
        self.raw_output_filename = (output_filename or "").strip() or None

        self.tree_json_path: Optional[str] = None
        self.output_filename_base: str = DEFAULT_OUTPUT_FILENAME

        self.nodes: List[Dict[str, Any]] = []
        self.meshes: List[Dict[str, Any]] = []
        self.accessors: List[Dict[str, Any]] = []
        self.buffer_views: List[Dict[str, Any]] = []

        self.materials: List[Dict[str, Any]] = []
        self.textures: List[Dict[str, Any]] = []
        self.images: List[Dict[str, Any]] = []
        self.samplers: List[Dict[str, Any]] = []

        self._image_cache: Dict[str, int] = {}
        self._sampler_cache: Dict[str, int] = {}
        self._texture_cache: Dict[Tuple, int] = {}
        self._material_cache: Dict[str, int] = {}

        self.temp_bin_path: Optional[str] = None
        self.temp_bin_file = None
        self.bin_length = 0

        self.mesh_cache: Dict[str, int] = {}

    def _discover_tree_json(self) -> str:
        candidates = [
            p
            for p in glob.glob(os.path.join(self.input_dir, "*.json"))
            if os.path.isfile(p)
        ]
        if not candidates:
            raise FileNotFoundError(f"输入目录里没有找到结构树 JSON: {self.input_dir}")
        if len(candidates) > 1:
            log_err(
                f"[warn] 输入目录里发现多个 JSON，将使用第一个: "
                f"{[os.path.basename(c) for c in candidates]}"
            )
        return candidates[0]

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        if not name:
            return ""
        s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
        s = "".join(c for c in s if c.isprintable())
        s = re.sub(r"_+", "_", s)
        return s.strip(" ._")

    @staticmethod
    def _strip_glb_ext(name: str) -> str:
        lower = name.lower()
        for ext in (".glb", ".gltf"):
            if lower.endswith(ext):
                return name[: -len(ext)]
        return name

    def _stream_sub_gltf(self, gltf_abs: str) -> Tuple[Dict[str, Any], List[int]]:
        with open(gltf_abs, "r", encoding="utf-8") as f:
            sub_json = json.load(f)

        gltf_dir = os.path.dirname(gltf_abs)
        buffers = sub_json.get("buffers", [])
        buffer_starts: List[int] = []

        for buf in buffers:
            start = self.bin_length
            buffer_starts.append(start)

            uri = buf.get("uri", "") or ""

            if uri.startswith("data:"):
                _, _, b64 = uri.partition(",")
                raw = base64.b64decode(b64)
                self.temp_bin_file.write(raw)
                self.bin_length += len(raw)
            elif uri:
                bin_abs = os.path.normpath(os.path.join(gltf_dir, uri))
                if not os.path.isfile(bin_abs):
                    raise FileNotFoundError(f"找不到 bin 文件: {bin_abs}")
                with open(bin_abs, "rb") as fb:
                    while True:
                        chunk = fb.read(COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        self.temp_bin_file.write(chunk)
                        self.bin_length += len(chunk)
            else:
                declared = int(buf.get("byteLength", 0))
                if declared > 0:
                    self.temp_bin_file.write(b"\x00" * declared)
                    self.bin_length += declared

            pad = (-self.bin_length) % 4
            if pad:
                self.temp_bin_file.write(b"\x00" * pad)
                self.bin_length += pad

        return sub_json, buffer_starts

    def _extract_image_bytes(
        self,
        sub_json: Dict[str, Any],
        gltf_abs: str,
        buffer_starts: List[int],
        img: Dict[str, Any],
    ) -> Optional[Tuple[bytes, str, str]]:
        gltf_dir = os.path.dirname(gltf_abs)
        uri = img.get("uri", "") or ""
        mime = img.get("mimeType")

        if uri and not uri.startswith("data:"):
            img_path = os.path.normpath(os.path.join(gltf_dir, uri))
            if not os.path.isfile(img_path):
                log_err(f"[merge] 缺失贴图: {img_path}")
                return None
            with open(img_path, "rb") as f:
                raw = f.read()
            if not mime:
                mime = _sniff_image_mime(img_path)
            return raw, mime, f"file:{img_path}"

        if uri.startswith("data:"):
            header, _, b64 = uri.partition(",")
            if not mime:
                try:
                    mime = header.split(";")[0].split(":", 1)[1]
                except Exception:
                    mime = "image/png"
            raw = base64.b64decode(b64)
            return raw, mime, f"sha1:{hashlib.sha1(raw).hexdigest()}"

        bv_idx = img.get("bufferView")
        if bv_idx is not None:
            bv = sub_json["bufferViews"][bv_idx]
            buf_idx = int(bv.get("buffer", 0))
            base = int(bv.get("byteOffset", 0))
            length = int(bv["byteLength"])

            buffers = sub_json.get("buffers", [])
            if buf_idx < 0 or buf_idx >= len(buffers):
                return None
            buf = buffers[buf_idx]
            buri = buf.get("uri", "") or ""

            raw = b""
            if buri.startswith("data:"):
                _, _, b64 = buri.partition(",")
                full = base64.b64decode(b64)
                raw = full[base : base + length]
            elif buri:
                bin_abs = os.path.normpath(os.path.join(gltf_dir, buri))
                try:
                    with open(bin_abs, "rb") as f:
                        f.seek(base)
                        raw = f.read(length)
                except Exception as e:
                    log_err(f"[merge] 读内嵌贴图失败 {bin_abs}: {e}")
                    return None

            if not mime:
                mime = "image/png"
            return raw, mime, f"sha1:{hashlib.sha1(raw).hexdigest()}"

        return None

    @staticmethod
    def _remap_material_textures(
        mat: Dict[str, Any], tex_idx_map: Dict[int, int]
    ) -> Dict[str, Any]:
        new_mat = copy.deepcopy(mat)

        pbr = new_mat.get("pbrMetallicRoughness")
        if isinstance(pbr, dict):
            for k in ("baseColorTexture", "metallicRoughnessTexture"):
                if k in pbr and isinstance(pbr[k], dict):
                    old_idx = pbr[k].get("index")
                    if old_idx is not None:
                        new_idx = tex_idx_map.get(int(old_idx))
                        if new_idx is not None:
                            pbr[k]["index"] = new_idx
                        else:
                            pbr.pop(k, None)

        for k in ("normalTexture", "occlusionTexture", "emissiveTexture"):
            if k in new_mat and isinstance(new_mat[k], dict):
                old_idx = new_mat[k].get("index")
                if old_idx is not None:
                    new_idx = tex_idx_map.get(int(old_idx))
                    if new_idx is not None:
                        new_mat[k]["index"] = new_idx
                    else:
                        new_mat.pop(k, None)

        return new_mat

    def _merge_sub_gltf(self, gltf_rel_path: str) -> int:
        if gltf_rel_path in self.mesh_cache:
            return self.mesh_cache[gltf_rel_path]

        if os.path.isabs(gltf_rel_path):
            gltf_abs = gltf_rel_path
        else:
            gltf_abs = os.path.normpath(os.path.join(self.input_dir, gltf_rel_path))

        sub_json, buffer_starts = self._stream_sub_gltf(gltf_abs)

        bv_offset = len(self.buffer_views)
        acc_offset = len(self.accessors)
        mesh_offset = len(self.meshes)

        for bv in sub_json.get("bufferViews", []):
            new_bv = dict(bv)
            buf_idx = int(bv.get("buffer", 0))
            base = buffer_starts[buf_idx] if 0 <= buf_idx < len(buffer_starts) else 0
            new_bv["buffer"] = 0
            new_bv["byteOffset"] = int(bv.get("byteOffset", 0)) + base
            self.buffer_views.append(new_bv)

        for acc in sub_json.get("accessors", []):
            new_acc = dict(acc)
            if "bufferView" in new_acc:
                new_acc["bufferView"] = int(new_acc["bufferView"]) + bv_offset
            self.accessors.append(new_acc)

        img_idx_map: Dict[int, int] = {}
        for old_img_idx, img in enumerate(sub_json.get("images", [])):
            result = self._extract_image_bytes(sub_json, gltf_abs, buffer_starts, img)
            if result is None:
                continue
            raw, mime, cache_key = result

            if cache_key in self._image_cache:
                global_img_idx = self._image_cache[cache_key]
            else:
                pad = (-self.bin_length) % 4
                if pad:
                    self.temp_bin_file.write(b"\x00" * pad)
                    self.bin_length += pad

                byte_offset = self.bin_length
                self.temp_bin_file.write(raw)
                self.bin_length += len(raw)

                new_bv_idx = len(self.buffer_views)
                self.buffer_views.append(
                    {
                        "buffer": 0,
                        "byteOffset": byte_offset,
                        "byteLength": len(raw),
                    }
                )

                global_img_idx = len(self.images)
                self.images.append(
                    {
                        "bufferView": new_bv_idx,
                        "mimeType": mime,
                    }
                )
                self._image_cache[cache_key] = global_img_idx

            img_idx_map[old_img_idx] = global_img_idx

        smp_idx_map: Dict[int, int] = {}
        for old_smp_idx, smp in enumerate(sub_json.get("samplers", [])):
            key = json.dumps(smp, sort_keys=True)
            if key in self._sampler_cache:
                global_smp_idx = self._sampler_cache[key]
            else:
                global_smp_idx = len(self.samplers)
                self.samplers.append(dict(smp))
                self._sampler_cache[key] = global_smp_idx
            smp_idx_map[old_smp_idx] = global_smp_idx

        tex_idx_map: Dict[int, int] = {}
        for old_tex_idx, tex in enumerate(sub_json.get("textures", [])):
            src_img = tex.get("source")
            src_smp = tex.get("sampler")
            global_img = img_idx_map.get(int(src_img)) if src_img is not None else None
            global_smp = smp_idx_map.get(int(src_smp)) if src_smp is not None else None

            if global_img is None:
                continue

            key = (global_img, global_smp)
            if key in self._texture_cache:
                global_tex_idx = self._texture_cache[key]
            else:
                new_tex: Dict[str, Any] = {"source": global_img}
                if global_smp is not None:
                    new_tex["sampler"] = global_smp
                global_tex_idx = len(self.textures)
                self.textures.append(new_tex)
                self._texture_cache[key] = global_tex_idx
            tex_idx_map[old_tex_idx] = global_tex_idx

        mat_idx_map: Dict[int, int] = {}
        for old_mat_idx, mat in enumerate(sub_json.get("materials", [])):
            new_mat = self._remap_material_textures(mat, tex_idx_map)
            key = json.dumps(new_mat, sort_keys=True)
            if key in self._material_cache:
                global_mat_idx = self._material_cache[key]
            else:
                global_mat_idx = len(self.materials)
                self.materials.append(new_mat)
                self._material_cache[key] = global_mat_idx
            mat_idx_map[old_mat_idx] = global_mat_idx

        for mesh in sub_json.get("meshes", []):
            new_mesh: Dict[str, Any] = {"primitives": []}
            for prim in mesh.get("primitives", []):
                new_prim: Dict[str, Any] = {"attributes": {}}
                for attr_name, acc_idx in prim.get("attributes", {}).items():
                    new_prim["attributes"][attr_name] = int(acc_idx) + acc_offset
                if "indices" in prim:
                    new_prim["indices"] = int(prim["indices"]) + acc_offset
                if "material" in prim:
                    old_mat_idx = int(prim["material"])
                    if old_mat_idx in mat_idx_map:
                        new_prim["material"] = mat_idx_map[old_mat_idx]
                if "mode" in prim:
                    new_prim["mode"] = int(prim["mode"])
                new_mesh["primitives"].append(new_prim)
            if "name" in mesh:
                new_mesh["name"] = mesh["name"]
            self.meshes.append(new_mesh)

        merged_index = mesh_offset
        self.mesh_cache[gltf_rel_path] = merged_index
        return merged_index

    def _build_node(self, tree_node: Dict[str, Any]) -> int:
        node: Dict[str, Any] = {}
        node["name"] = tree_node.get("name") or "Node"

        trsf = tree_node.get("transform") or {}
        pos = trsf.get("position") or [0.0, 0.0, 0.0]
        quat = trsf.get("quaternion") or [0.0, 0.0, 0.0, 1.0]
        scale_raw = trsf.get("scale")

        if pos != [0.0, 0.0, 0.0]:
            node["translation"] = [float(v) for v in pos]
        if quat != [0.0, 0.0, 0.0, 1.0]:
            node["rotation"] = [float(v) for v in quat]

        scale3 = _norm_scale(scale_raw, [1.0, 1.0, 1.0])
        if scale3 != [1.0, 1.0, 1.0]:
            node["scale"] = scale3

        my_index = len(self.nodes)
        self.nodes.append(node)

        node_type = tree_node.get("type", "")
        if node_type in ("mesh", "part"):
            asset = tree_node.get("asset")
            if asset:
                try:
                    node["mesh"] = self._merge_sub_gltf(asset)
                except Exception as e:
                    log_err(f"加载子 glTF 失败: {asset}: {e}")

        children = tree_node.get("children") or []
        if children:
            node["children"] = [self._build_node(c) for c in children]

        return my_index

    def _write_glb(self, root_indices: List[int], out_path: str) -> None:
        gltf: Dict[str, Any] = {
            "asset": {
                "version": "2.0",
                "generator": "assembly2glb Assembled GLB Merger (gltf+bin stream)",
            },
            "scene": 0,
            "scenes": [{"nodes": root_indices}],
            "nodes": self.nodes,
            "meshes": self.meshes,
            "accessors": self.accessors,
            "bufferViews": self.buffer_views,
            "buffers": [{"byteLength": self.bin_length}],
        }
        if self.materials:
            gltf["materials"] = self.materials
        if self.textures:
            gltf["textures"] = self.textures
        if self.images:
            gltf["images"] = self.images
        if self.samplers:
            gltf["samplers"] = self.samplers

        json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
        while len(json_bytes) % 4 != 0:
            json_bytes += b" "

        total_length = 12 + 8 + len(json_bytes) + 8 + self.bin_length
        header = struct.pack("<4sII", b"glTF", 2, total_length)
        json_hdr = struct.pack("<I4s", len(json_bytes), b"JSON")
        bin_hdr = struct.pack("<I4s", self.bin_length, b"BIN\x00")

        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        self.temp_bin_file.flush()
        self.temp_bin_file.seek(0)

        with open(out_path, "wb") as f:
            f.write(header)
            f.write(json_hdr)
            f.write(json_bytes)
            f.write(bin_hdr)
            shutil.copyfileobj(self.temp_bin_file, f, COPY_CHUNK_SIZE)

    def _resolve_output_basename(self, tree: Any) -> str:
        if self.raw_output_filename:
            name = self._strip_glb_ext(self.raw_output_filename)
            safe = self._sanitize_filename(name)
            if safe:
                return safe

        if isinstance(tree, dict):
            top_name = tree.get("name")
            if top_name:
                safe = self._sanitize_filename(str(top_name))
                if safe:
                    return safe
        return DEFAULT_OUTPUT_FILENAME

    def run(self) -> str:
        if not os.path.isdir(self.input_dir):
            raise NotADirectoryError(f"输入目录不存在: {self.input_dir}")

        os.makedirs(self.output_dir, exist_ok=True)

        fd, self.temp_bin_path = tempfile.mkstemp(
            prefix=".merge-bin-", suffix=".tmp", dir=self.output_dir
        )
        os.close(fd)
        self.temp_bin_file = open(self.temp_bin_path, "w+b")

        try:
            self.tree_json_path = self._discover_tree_json()
            log(f"结构树: {self.tree_json_path}")

            with open(self.tree_json_path, "r", encoding="utf-8") as f:
                tree = json.load(f)

            self.output_filename_base = self._resolve_output_basename(tree)
            log(f"输出文件名: {self.output_filename_base}.glb")

            if isinstance(tree, list):
                root_indices = [self._build_node(t) for t in tree]
            else:
                root_indices = [self._build_node(tree)]

            log(
                f"合并统计: nodes={len(self.nodes)} meshes={len(self.meshes)} "
                f"accessors={len(self.accessors)} "
                f"bufferViews={len(self.buffer_views)} "
                f"materials={len(self.materials)} "
                f"textures={len(self.textures)} "
                f"images={len(self.images)} "
                f"samplers={len(self.samplers)} "
                f"bin={self.bin_length / 1024 / 1024:.2f} MB"
            )

            output_path = os.path.join(
                self.output_dir, f"{self.output_filename_base}.glb"
            )
            log(f"写出 GLB: {output_path}")
            self._write_glb(root_indices, output_path)
            log("合并完成")
            return output_path

        finally:
            try:
                if self.temp_bin_file is not None:
                    self.temp_bin_file.close()
            except Exception:
                pass
            if self.temp_bin_path and os.path.exists(self.temp_bin_path):
                try:
                    os.remove(self.temp_bin_path)
                except Exception:
                    pass


def main():
    if len(sys.argv) < 3:
        print("用法: python merge-glb.py <输入目录> <输出目录> [输出文件名]")
        print("说明:")
        print(
            "  <输入目录>   含 *.json 结构树 + meshs/ 子目录（*.gltf + *.bin + tex_*.png）"
        )
        print("  <输出目录>   合并结果输出目录")
        print("  [输出文件名] 可选，如 'demo' 或 'demo.glb'")
        print("               不传时依次回退到 JSON 顶级 name → 'assembly'")
        sys.exit(1)

    input_dir = sys.argv[1]
    output_dir = sys.argv[2]
    output_filename = sys.argv[3] if len(sys.argv) >= 4 else None

    try:
        GltfMerger(input_dir, output_dir, output_filename).run()
    except Exception as e:
        log_err(f"[merge-glb] 合并失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
