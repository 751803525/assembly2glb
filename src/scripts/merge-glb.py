"""
装配体 GLB 合并脚本（独立工具，流式合并版）

把 cad-splitter 输出的多个零件 glTF + BIN 按结构树合并成一个装配 GLB
（流式：BIN 数据直接搬运，不整体进内存）

输入：一个目录，包含
    - 一个结构树 *.json（cad-splitter 的输出）
    - meshs/ 子目录，里面是每个零件的 xxx__hash.gltf + 同名 .bin

输出：合并后的单个 .glb

兼容字段：
    - type: "mesh" / "part"       → 有几何
    - type: "node" / "assembly"   → 纯装配
    - assets / asset              → 关联的子 glTF 路径
    - transform.scale             → 单值 float 或 [x,y,z] 数组
"""

import os
import sys
import re
import json
import glob
import base64
import struct
import shutil
import tempfile
from typing import Dict, Any, List, Optional, Tuple


def logger_info(msg: str) -> None:
    print(msg, flush=True)


def logger_err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


DEFAULT_OUTPUT_FILENAME = "assembly"
COPY_CHUNK_SIZE = 4 * 1024 * 1024


def _norm_scale(v, default=None) -> List[float]:
    """
    把单值 / 数组 / None 统一成 3 元 float 数组。
    cad-splitter 旧版输出 [s, s, s]，新版输出 s（单值 float）。
    """
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


class GltfMerger:
    """把多个零件 glTF（+ 外置 .bin）按结构树合并成一个装配 GLB（流式，低内存）"""

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

        self.temp_bin_path: Optional[str] = None
        self.temp_bin_file = None
        self.bin_length = 0

        # 缓存：相对路径 → 合并后的 mesh 索引
        self.mesh_cache: Dict[str, int] = {}

    # -----------------------------------------------------------------
    # 输入发现 & 文件名工具
    # -----------------------------------------------------------------

    def _discover_tree_json(self) -> str:
        candidates = [
            p
            for p in glob.glob(os.path.join(self.input_dir, "*.json"))
            if os.path.isfile(p)
        ]
        if not candidates:
            raise FileNotFoundError(f"输入目录里没有找到结构树 JSON: {self.input_dir}")
        if len(candidates) > 1:
            logger_err(
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

    # -----------------------------------------------------------------
    # 流式读取子 glTF + BIN
    # -----------------------------------------------------------------

    def _stream_sub_gltf(self, gltf_abs: str) -> Tuple[Dict[str, Any], List[int]]:
        """
        读取子 .gltf，把它的所有 buffer 内容按顺序追加到临时 bin 文件，
        返回 (gltf_json, buffer_starts)。

        buffer_starts[k] = 子 gltf 第 k 个 buffer 在全局 bin 中的起始字节偏移。
        """
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
                # data:application/octet-stream;base64,xxxx
                _, _, b64 = uri.partition(",")
                raw = base64.b64decode(b64)
                self.temp_bin_file.write(raw)
                self.bin_length += len(raw)
            elif uri:
                # 外部 .bin（相对 gltf 所在目录）
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
                # 无 uri：GLB 内嵌 buffer 的情况，理论上不会出现在 .gltf 里
                # 但有 buffer 声明却无数据时，按 byteLength 补零
                declared = int(buf.get("byteLength", 0))
                if declared > 0:
                    self.temp_bin_file.write(b"\x00" * declared)
                    self.bin_length += declared

            # 每个 buffer 之间做 4 字节对齐
            pad = (-self.bin_length) % 4
            if pad:
                self.temp_bin_file.write(b"\x00" * pad)
                self.bin_length += pad

        return sub_json, buffer_starts

    # -----------------------------------------------------------------
    # 合并单个子 glTF
    # -----------------------------------------------------------------

    def _merge_sub_gltf(self, gltf_rel_path: str) -> int:
        if gltf_rel_path in self.mesh_cache:
            return self.mesh_cache[gltf_rel_path]

        # 兼容绝对路径 / 相对路径
        if os.path.isabs(gltf_rel_path):
            gltf_abs = gltf_rel_path
        else:
            gltf_abs = os.path.normpath(os.path.join(self.input_dir, gltf_rel_path))

        sub_json, buffer_starts = self._stream_sub_gltf(gltf_abs)

        bv_offset = len(self.buffer_views)
        acc_offset = len(self.accessors)
        mesh_offset = len(self.meshes)
        mat_offset = len(self.materials)

        # ---- bufferViews：buffer 全部改为 0，byteOffset 加上对应 buffer 起始 ----
        for bv in sub_json.get("bufferViews", []):
            new_bv = dict(bv)
            buf_idx = int(bv.get("buffer", 0))
            base = buffer_starts[buf_idx] if 0 <= buf_idx < len(buffer_starts) else 0
            new_bv["buffer"] = 0
            new_bv["byteOffset"] = int(bv.get("byteOffset", 0)) + base
            self.buffer_views.append(new_bv)

        # ---- accessors：bufferView 索引整体偏移 ----
        for acc in sub_json.get("accessors", []):
            new_acc = dict(acc)
            if "bufferView" in new_acc:
                new_acc["bufferView"] = int(new_acc["bufferView"]) + bv_offset
            self.accessors.append(new_acc)

        # ---- materials：追加到全局材质表 ----
        for mat in sub_json.get("materials", []):
            self.materials.append(dict(mat))

        # ---- meshes：attributes / indices / material 索引整体偏移 ----
        for mesh in sub_json.get("meshes", []):
            new_mesh: Dict[str, Any] = {"primitives": []}
            for prim in mesh.get("primitives", []):
                new_prim: Dict[str, Any] = {"attributes": {}}
                for attr_name, acc_idx in prim.get("attributes", {}).items():
                    new_prim["attributes"][attr_name] = int(acc_idx) + acc_offset
                if "indices" in prim:
                    new_prim["indices"] = int(prim["indices"]) + acc_offset
                if "material" in prim:
                    new_prim["material"] = int(prim["material"]) + mat_offset
                if "mode" in prim:
                    new_prim["mode"] = int(prim["mode"])
                new_mesh["primitives"].append(new_prim)
            # 保留可能存在的 name
            if "name" in mesh:
                new_mesh["name"] = mesh["name"]
            self.meshes.append(new_mesh)

        merged_index = mesh_offset
        self.mesh_cache[gltf_rel_path] = merged_index
        return merged_index

    # -----------------------------------------------------------------
    # 递归构建 node
    # -----------------------------------------------------------------

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

        # scale 可能是单值 float（新格式）或数组（旧格式）
        scale3 = _norm_scale(scale_raw, [1.0, 1.0, 1.0])
        if scale3 != [1.0, 1.0, 1.0]:
            node["scale"] = scale3

        my_index = len(self.nodes)
        self.nodes.append(node)

        node_type = tree_node.get("type", "")
        if node_type in ("mesh", "part"):
            asset = tree_node.get("assets") or tree_node.get("asset")
            if asset:
                try:
                    node["mesh"] = self._merge_sub_gltf(asset)
                except Exception as e:
                    logger_err(f"加载子 glTF 失败: {asset}: {e}")

        children = tree_node.get("children") or []
        if children:
            node["children"] = [self._build_node(c) for c in children]

        return my_index

    # -----------------------------------------------------------------
    # 写出 GLB
    # -----------------------------------------------------------------

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

    # -----------------------------------------------------------------
    # 文件名解析
    # -----------------------------------------------------------------

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

    # -----------------------------------------------------------------
    # 主流程
    # -----------------------------------------------------------------

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
            logger_info(f"结构树: {self.tree_json_path}")

            with open(self.tree_json_path, "r", encoding="utf-8") as f:
                tree = json.load(f)

            self.output_filename_base = self._resolve_output_basename(tree)
            logger_info(f"输出文件名: {self.output_filename_base}.glb")

            if isinstance(tree, list):
                root_indices = [self._build_node(t) for t in tree]
            else:
                root_indices = [self._build_node(tree)]

            logger_info(
                f"合并统计: nodes={len(self.nodes)} meshes={len(self.meshes)} "
                f"accessors={len(self.accessors)} "
                f"bufferViews={len(self.buffer_views)} "
                f"materials={len(self.materials)} "
                f"bin={self.bin_length / 1024 / 1024:.2f} MB"
            )

            output_path = os.path.join(
                self.output_dir, f"{self.output_filename_base}.glb"
            )
            logger_info(f"写出 GLB: {output_path}")
            self._write_glb(root_indices, output_path)
            logger_info("合并完成")
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
        print("用法: python merge-gltf.py <输入目录> <输出目录> [输出文件名]")
        print("说明:")
        print("  <输入目录>   含 *.json 结构树 + meshs/ 子目录（*.gltf + *.bin）")
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
        logger_err(f"[merge-gltf] 合并失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
