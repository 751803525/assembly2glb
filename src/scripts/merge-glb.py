"""
装配体 GLB 合并脚本（独立工具，流式合并版）
...（其余文档字符串不变）
"""

import os
import sys
import re
import json
import glob
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


class GlbMerger:
    """把多个零件 GLB 按结构树合并成一个装配 GLB（流式，低内存）"""

    def __init__(
        self,
        input_dir: str,
        output_dir: str,
        output_filename: Optional[str] = None,
    ):
        # 与 splitter/dedup 保持一致：Node 传来的就是短路径 ADMINI~1，
        # 全流程统一用短路径，避免写短读长的不一致
        self.input_dir = os.path.abspath(input_dir)
        self.output_dir = os.path.abspath(output_dir)
        self.raw_output_filename = (output_filename or "").strip() or None

        self.tree_json_path: Optional[str] = None
        self.output_filename_base: str = DEFAULT_OUTPUT_FILENAME

        self.nodes: List[Dict[str, Any]] = []
        self.meshes: List[Dict[str, Any]] = []
        self.accessors: List[Dict[str, Any]] = []
        self.buffer_views: List[Dict[str, Any]] = []

        self.temp_bin_path: Optional[str] = None
        self.temp_bin_file = None
        self.bin_length = 0

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
        return name[:-4] if name.lower().endswith(".glb") else name

    # -----------------------------------------------------------------
    # 流式读取子 GLB
    # -----------------------------------------------------------------

    def _stream_sub_glb(self, glb_abs: str) -> Tuple[Dict[str, Any], int]:
        bin_start = self.bin_length

        with open(glb_abs, "rb") as f:
            header = f.read(12)
            if len(header) < 12:
                raise ValueError(f"GLB 文件过短: {glb_abs}")

            magic, version, length = struct.unpack("<4sII", header)
            if magic != b"glTF":
                raise ValueError(f"不是 GLB 文件: {glb_abs}")
            if version != 2:
                raise ValueError(f"不支持的 GLB 版本: {version}")

            json_chunk: Optional[Dict[str, Any]] = None

            while True:
                chunk_hdr = f.read(8)
                if len(chunk_hdr) < 8:
                    break

                chunk_len, chunk_type = struct.unpack("<I4s", chunk_hdr)

                if chunk_type == b"JSON":
                    json_chunk = json.loads(f.read(chunk_len).decode("utf-8"))
                elif chunk_type == b"BIN\x00":
                    remaining = chunk_len
                    while remaining > 0:
                        buf = f.read(min(COPY_CHUNK_SIZE, remaining))
                        if not buf:
                            break
                        self.temp_bin_file.write(buf)
                        remaining -= len(buf)
                    self.bin_length += chunk_len
                else:
                    f.seek(chunk_len, 1)

            if json_chunk is None:
                raise ValueError(f"GLB 里没有 JSON chunk: {glb_abs}")

        pad = (-self.bin_length) % 4
        if pad:
            self.temp_bin_file.write(b"\x00" * pad)
            self.bin_length += pad

        return json_chunk, bin_start

    # -----------------------------------------------------------------
    # 合并单个子 GLB
    # -----------------------------------------------------------------

    def _merge_sub_glb(self, glb_rel_path: str) -> int:
        if glb_rel_path in self.mesh_cache:
            return self.mesh_cache[glb_rel_path]

        glb_abs = os.path.normpath(os.path.join(self.input_dir, glb_rel_path))
        sub_json, bin_start = self._stream_sub_glb(glb_abs)

        bv_offset = len(self.buffer_views)
        acc_offset = len(self.accessors)
        mesh_offset = len(self.meshes)

        for bv in sub_json.get("bufferViews", []):
            new_bv = dict(bv)
            new_bv["buffer"] = 0
            new_bv["byteOffset"] = bv.get("byteOffset", 0) + bin_start
            self.buffer_views.append(new_bv)

        for acc in sub_json.get("accessors", []):
            new_acc = dict(acc)
            if "bufferView" in new_acc:
                new_acc["bufferView"] += bv_offset
            self.accessors.append(new_acc)

        for mesh in sub_json.get("meshes", []):
            new_mesh: Dict[str, Any] = {"primitives": []}
            for prim in mesh.get("primitives", []):
                new_prim: Dict[str, Any] = {"attributes": {}}
                for attr_name, acc_idx in prim.get("attributes", {}).items():
                    new_prim["attributes"][attr_name] = acc_idx + acc_offset
                if "indices" in prim:
                    new_prim["indices"] = prim["indices"] + acc_offset
                if "material" in prim:
                    new_prim["material"] = prim["material"]
                if "mode" in prim:
                    new_prim["mode"] = prim["mode"]
                new_mesh["primitives"].append(new_prim)
            self.meshes.append(new_mesh)

        merged_index = mesh_offset
        self.mesh_cache[glb_rel_path] = merged_index
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
        scale = trsf.get("scale") or [1.0, 1.0, 1.0]

        if pos != [0.0, 0.0, 0.0]:
            node["translation"] = [float(v) for v in pos]
        if quat != [0.0, 0.0, 0.0, 1.0]:
            node["rotation"] = [float(v) for v in quat]
        if scale != [1.0, 1.0, 1.0]:
            node["scale"] = [float(v) for v in scale]

        my_index = len(self.nodes)
        self.nodes.append(node)

        if tree_node.get("type") == "part":
            asset = tree_node.get("asset")
            if asset:
                try:
                    node["mesh"] = self._merge_sub_glb(asset)
                except Exception as e:
                    logger_err(f"加载子 GLB 失败: {asset}: {e}")

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
                "generator": "assembly2glb Assembled GLB Merger (stream)",
            },
            "scene": 0,
            "scenes": [{"nodes": root_indices}],
            "nodes": self.nodes,
            "meshes": self.meshes,
            "accessors": self.accessors,
            "bufferViews": self.buffer_views,
            "buffers": [{"byteLength": self.bin_length}],
        }

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
        print("用法: python merge-glb.py <输入目录> <输出目录> [输出文件名]")
        print("说明:")
        print("  <输入目录>   含 *.json 结构树 + glbs/ 子目录")
        print("  <输出目录>   合并结果输出目录")
        print("  [输出文件名] 可选，如 'demo' 或 'demo.glb'")
        print("               不传时依次回退到 JSON 顶级 name → 'assembly'")
        sys.exit(1)

    input_dir = sys.argv[1]
    output_dir = sys.argv[2]
    output_filename = sys.argv[3] if len(sys.argv) >= 4 else None

    try:
        GlbMerger(input_dir, output_dir, output_filename).run()
    except Exception as e:
        logger_err(f"[merge-glb] 合并失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
