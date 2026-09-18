"""
装配体零件 GLB 去重脚本（独立工具）

用途：
    cad-splitter.py 会为每个 label 导出一份 GLB。同一几何在不同 label 上
    会出现重复导出（例如同型号螺钉被实例化多次）。本脚本对 glbs/ 目录下
    的 GLB 做多级去重，并同步更新 assembly-tree.json 里的 asset 字段。

    输出目录与输入目录结构完全一致，便于下游 merge-glb.py 无缝衔接。

用法：
    python dedup.py <输入目录> <输出目录> [去重等级 1|2]

    去重等级：
        1 = 仅一级去重（字节级 sha1）
        2 = 一级 + 二级（几何指纹）级联去重

输入目录结构：
    <输入目录>/
        ├─ assembly-tree.json
        └─ glbs/
            ├─ xxx__a1b2c3d4.glb
            └─ yyy__e5f6g7h8.glb

输出目录结构（与输入一致）：
    <输出目录>/
        ├─ assembly-tree.json
        ├─ dedup-report.json
        └─ glbs/

依赖：
    仅标准库
"""

import os
import sys
import json
import glob
import struct
import shutil
import hashlib
from typing import Dict, Any, List, Optional, Tuple, Callable


def logger_info(msg: str) -> None:
    print(msg, flush=True)


def logger_err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


GLB_SUBDIR = "glbs"
TREE_JSON_NAME = "assembly-tree.json"
REPORT_JSON_NAME = "dedup-report.json"

GEO_ROUND_DIGITS = 4
CHUNK_SIZE = 64 * 1024


# =====================================================================
# canonical 选择策略
# =====================================================================


def choose_first(files: List[str]) -> str:
    """保留首现文件（最稳定，可复现）"""
    return files[0]


def choose_shortest_name(files: List[str]) -> str:
    """保留文件名最短的（更可读）"""
    return min(files, key=lambda x: (len(x), x))


def choose_lexicographic(files: List[str]) -> str:
    """保留字典序最小的（纯稳定排序，便于跨机器一致）"""
    return min(files)


STRATEGIES: Dict[str, Callable[[List[str]], str]] = {
    "first": choose_first,
    "shortest": choose_shortest_name,
    "lexicographic": choose_lexicographic,
}

CURRENT_CHOOSE_CANONICAL: Callable[[List[str]], str] = choose_shortest_name


# =====================================================================
# GLB 解析 & 指纹函数
# =====================================================================


def _parse_glb(path: str) -> Tuple[Dict[str, Any], bytes]:
    with open(path, "rb") as f:
        data = f.read()

    if len(data) < 12:
        raise ValueError(f"GLB 文件过短: {path}")

    magic, version, length = struct.unpack("<4sII", data[:12])
    if magic != b"glTF":
        raise ValueError(f"不是 GLB 文件: {path}")
    if version != 2:
        raise ValueError(f"不支持的 GLB 版本: {version}")

    offset = 12
    json_chunk: Optional[Dict[str, Any]] = None
    bin_chunk = b""

    while offset < length:
        if offset + 8 > len(data):
            break
        chunk_len, chunk_type = struct.unpack("<I4s", data[offset : offset + 8])
        chunk_data = data[offset + 8 : offset + 8 + chunk_len]

        if chunk_type == b"JSON":
            json_chunk = json.loads(chunk_data.decode("utf-8"))
        elif chunk_type == b"BIN\x00":
            bin_chunk = chunk_data

        offset += 8 + chunk_len

    if json_chunk is None:
        raise ValueError(f"GLB 里没有 JSON chunk: {path}")

    return json_chunk, bin_chunk


def fingerprint_bytes(path: str) -> str:
    """一级指纹：文件字节的 sha1"""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def fingerprint_geometry(path: str) -> str:
    """二级指纹：几何指纹（顶点排序 + 索引数）"""
    json_dict, bin_data = _parse_glb(path)

    positions: List[Tuple[float, float, float]] = []
    total_indices = 0

    for mesh in json_dict.get("meshes", []):
        for prim in mesh.get("primitives", []):
            pos_acc_idx = prim.get("attributes", {}).get("POSITION")
            if pos_acc_idx is None:
                continue

            acc = json_dict["accessors"][pos_acc_idx]
            if acc.get("componentType") != 5126 or acc.get("type") != "VEC3":
                continue

            bv_idx = acc.get("bufferView")
            if bv_idx is None:
                continue

            bv = json_dict["bufferViews"][bv_idx]
            base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
            count = acc["count"]

            for i in range(count):
                offset = base + i * 12
                if offset + 12 > len(bin_data):
                    break
                x, y, z = struct.unpack_from("<fff", bin_data, offset)
                positions.append(
                    (
                        round(x, GEO_ROUND_DIGITS),
                        round(y, GEO_ROUND_DIGITS),
                        round(z, GEO_ROUND_DIGITS),
                    )
                )

            if "indices" in prim:
                idx_acc = json_dict["accessors"][prim["indices"]]
                total_indices += idx_acc["count"]

    positions.sort()

    h = hashlib.sha1()
    for p in positions:
        h.update(struct.pack("<fff", *p))
    h.update(struct.pack("<I", total_indices))
    return h.hexdigest()


# =====================================================================
# 单级去重：按指纹分组 → 展平
# =====================================================================


def _dedup_by_fingerprint(
    files: List[str],
    glbs_dir: str,
    fingerprint_fn: Callable[[str], str],
    choose_canonical: Callable[[List[str]], str],
) -> Tuple[List[str], Dict[str, str]]:
    """通用去重：对 files 按指纹分组，每组选一个 canonical"""
    group_map: Dict[str, List[str]] = {}
    total = len(files)

    for i, fname in enumerate(files):
        fpath = os.path.join(glbs_dir, fname)
        try:
            h = fingerprint_fn(fpath)
        except Exception as e:
            logger_err(f"指纹计算失败 {fname}: {e}")
            h = f"__err__:{fname}"

        if h in group_map:
            group_map[h].append(fname)
        else:
            group_map[h] = [fname]

        if total >= 100 and (i + 1) % 100 == 0:
            logger_info(f"指纹进度 {i + 1}/{total}")

    canonicals: List[str] = []
    asset_mapping: Dict[str, str] = {}
    for _h, group in group_map.items():
        c = choose_canonical(group)
        canonicals.append(c)
        for f in group:
            asset_mapping[f] = c

    canonicals.sort()
    return canonicals, asset_mapping


# =====================================================================
# 映射叠加
# =====================================================================


def compose_mappings(
    mapping_prev: Dict[str, str],
    mapping_curr: Dict[str, str],
) -> Dict[str, str]:
    """把两级映射叠加为一级（函数复合 f → c_prev → c_curr）"""
    composed: Dict[str, str] = {}
    for f, c_prev in mapping_prev.items():
        composed[f] = mapping_curr.get(c_prev, c_prev)
    return composed


# =====================================================================
# 分级去重入口
# =====================================================================


def dedup_level_1(
    files: List[str],
    glbs_dir: str,
    choose_canonical: Callable[[List[str]], str] = CURRENT_CHOOSE_CANONICAL,
) -> Tuple[List[str], Dict[str, str]]:
    """一级去重：字节指纹（sha1）"""
    return _dedup_by_fingerprint(files, glbs_dir, fingerprint_bytes, choose_canonical)


def dedup_level_2(
    files: List[str],
    glbs_dir: str,
    choose_canonical: Callable[[List[str]], str] = CURRENT_CHOOSE_CANONICAL,
) -> Tuple[List[str], Dict[str, str]]:
    """二级去重：几何指纹（当前为占位实现）"""
    return list(files), {f: f for f in files}


# =====================================================================
# JSON 清洗
# =====================================================================


def rewrite_assets(node: Any, asset_mapping: Dict[str, str]) -> None:
    """递归重写节点树里的 asset 字段"""
    if isinstance(node, dict):
        asset = node.get("asset")
        if isinstance(asset, str) and asset:
            normalized = asset.replace("\\", "/")
            if "/" in normalized:
                prefix, basename = normalized.rsplit("/", 1)
            else:
                prefix, basename = GLB_SUBDIR, normalized

            if basename in asset_mapping:
                node["asset"] = f"{prefix}/{asset_mapping[basename]}"

        for child in node.get("children", []) or []:
            rewrite_assets(child, asset_mapping)

    elif isinstance(node, list):
        for item in node:
            rewrite_assets(item, asset_mapping)


# =====================================================================
# 主流程
# =====================================================================


class DedupRunner:

    def __init__(self, input_dir: str, output_dir: str, level: int):
        self.input_dir = os.path.abspath(input_dir)
        self.output_dir = os.path.abspath(output_dir)
        self.level = level

        self.input_glbs_dir = os.path.join(self.input_dir, GLB_SUBDIR)
        self.input_tree_json = os.path.join(self.input_dir, TREE_JSON_NAME)

        self.output_glbs_dir = os.path.join(self.output_dir, GLB_SUBDIR)
        self.output_tree_json = os.path.join(self.output_dir, TREE_JSON_NAME)
        self.output_report_json = os.path.join(self.output_dir, REPORT_JSON_NAME)

    # -----------------------------------------------------------------
    # 输入检查
    # -----------------------------------------------------------------

    def _check_inputs(self) -> None:
        if not os.path.isdir(self.input_dir):
            raise NotADirectoryError(f"输入目录不存在: {self.input_dir}")
        if not os.path.isdir(self.input_glbs_dir):
            raise NotADirectoryError(
                f"输入目录下没有 glbs/ 子目录: {self.input_glbs_dir}"
            )
        if not os.path.isfile(self.input_tree_json):
            raise FileNotFoundError(
                f"输入目录下没有 {TREE_JSON_NAME}: {self.input_tree_json}"
            )

    def _collect_glbs(self) -> List[str]:
        return sorted(
            os.path.basename(p)
            for p in glob.glob(os.path.join(self.input_glbs_dir, "*.glb"))
            if os.path.isfile(p)
        )

    # -----------------------------------------------------------------
    # 输出准备
    # -----------------------------------------------------------------

    def _prepare_output(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        if os.path.isdir(self.output_glbs_dir):
            shutil.rmtree(self.output_glbs_dir)
        os.makedirs(self.output_glbs_dir, exist_ok=True)

    # -----------------------------------------------------------------
    # 复制 canonical 文件
    # -----------------------------------------------------------------

    def _copy_canonical_files(self, canonicals: List[str]) -> None:
        total = len(canonicals)
        copied = 0

        for c in canonicals:
            src = os.path.join(self.input_glbs_dir, c)
            dst = os.path.join(self.output_glbs_dir, c)

            if not os.path.isfile(src):
                logger_err(f"源文件不存在，跳过: {c}")
                continue

            # 手工 copy + fsync，确保下游进程能立刻读到文件内容
            # （替代 shutil.copy2 —— 后者不保证数据落盘）
            with open(src, "rb") as fsrc:
                with open(dst, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
                    fdst.flush()
                    os.fsync(fdst.fileno())

            # 复制文件属性（时间戳、权限）
            try:
                shutil.copystat(src, dst)
            except Exception:
                pass

            copied += 1

            if total >= 100 and copied % 100 == 0:
                logger_info(f"复制进度 {copied}/{total}")

        logger_info(f"复制完成: {copied}/{total} 个文件")

    # -----------------------------------------------------------------
    # 清洗 JSON
    # -----------------------------------------------------------------

    def _rewrite_tree(self, asset_mapping: Dict[str, str]) -> None:
        with open(self.input_tree_json, "r", encoding="utf-8") as f:
            tree = json.load(f)

        rewrite_assets(tree, asset_mapping)

        with open(self.output_tree_json, "w", encoding="utf-8") as f:
            json.dump(tree, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())  # 强制落盘，避免下游立刻读时读到旧内容

    # -----------------------------------------------------------------
    # 主流程
    # -----------------------------------------------------------------

    def run(self) -> None:
        self._check_inputs()

        logger_info(f"输入目录: {self.input_dir}")
        logger_info(f"输出目录: {self.output_dir}")
        logger_info(f"去重等级: {self.level}")

        files = self._collect_glbs()
        total = len(files)
        if total == 0:
            logger_err("glbs/ 目录下没有 .glb 文件")
            raise RuntimeError("无可处理的 GLB 文件")

        logger_info(f"发现 {total} 个 GLB 文件")

        # ---------- 一级去重 ----------
        logger_info("字节指纹去重")
        canonicals, mapping = dedup_level_1(
            files, self.input_glbs_dir, CURRENT_CHOOSE_CANONICAL
        )
        logger_info(f"指纹去重: {total} → {len(canonicals)}")

        # ---------- 二级去重（可选） ----------
        if self.level >= 2:
            logger_info("几何指纹去重")
            canonicals2, mapping2 = dedup_level_2(
                canonicals, self.input_glbs_dir, CURRENT_CHOOSE_CANONICAL
            )
            mapping = compose_mappings(mapping, mapping2)
            canonicals = canonicals2

        # ---------- 应用：复制 + 清洗 JSON ----------
        logger_info(f"输出准备：复制 {len(canonicals)} 个文件 ...")
        self._prepare_output()
        self._copy_canonical_files(canonicals)

        logger_info("清洗结构树")
        self._rewrite_tree(mapping)
        logger_info("清洗结构树完成")


def main():
    if len(sys.argv) < 3:
        logger_err(
            f"参数不足：需要至少 2 个参数（输入目录、输出目录），"
            f"实际收到 {max(0, len(sys.argv) - 1)} 个"
        )
        print("用法: python dedup.py <输入目录> <输出目录> [去重等级 1|2]")
        print("说明:")
        print("  <输入目录>    含 assembly-tree.json + glbs/ 的目录")
        print("  <输出目录>    去重后的产物目录（结构与输入一致）")
        print("  [去重等级]    可选，1 = 字节级（默认）；2 = 字节级 + 几何指纹级联")
        sys.exit(1)

    input_dir = sys.argv[1]
    output_dir = sys.argv[2]

    # 去重等级：可选，默认 1
    level = 1
    if len(sys.argv) >= 4:
        try:
            level = int(sys.argv[3])
        except ValueError:
            logger_err(f"去重等级必须是整数 1 或 2，收到: {sys.argv[3]!r}")
            sys.exit(1)

        if level not in (1, 2):
            logger_err(f"去重等级只支持 1 或 2，收到: {level}")
            sys.exit(1)

    try:
        DedupRunner(input_dir, output_dir, level).run()
    except Exception as e:
        logger_err(f"去重失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
