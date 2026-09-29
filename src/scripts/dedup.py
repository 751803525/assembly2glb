"""
装配体零件 glTF + BIN 去重脚本（独立工具）

用途：
    cad-splitter.py / glb-splitter.py 会为每个 label 导出一份 glTF（+ 同名 .bin）。
    同一几何在不同 label 上会出现重复导出（例如同型号螺钉被实例化多次）。
    本脚本对 meshs/ 目录下的 glTF + BIN 做多级去重，并同步更新结构树 JSON 里的
    asset 字段。

    去重分两级：
        一级（内容指纹）：
            阶段 A —— 直接对"同名 .bin"内容做 sha1。不打开 gltf。
            阶段 B —— 只对 bin 指纹相同的组，再做 gltf JSON 规范化指纹。
        二级（几何指纹）：
            顶点排序 + 索引数，跨 bin 布局差异判定几何是否相同。

    贴图处理：
        gltf 里 images[*].uri 引用外部图片文件（tex_xxx.png 等），
        复制 canonical 时同步把引用的图片一起复制过去，同名去重。

用法：
    python dedup.py <输入目录> <输出目录> [去重等级 1|2]
"""

import os
import sys
import copy
import json
import glob
import base64
import struct
import shutil
import hashlib
from typing import Dict, Any, List, Optional, Tuple, Callable


def log(msg: str) -> None:
    print(msg, flush=True)


def log_err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


MESHS_SUBDIR = "meshs"
TREE_JSON_NAME = "assembly-tree.json"
REPORT_JSON_NAME = "dedup-report.json"

GEO_ROUND_DIGITS = 4
CHUNK_SIZE = 64 * 1024

URI_PLACEHOLDER = "$BIN"
GEN_PLACEHOLDER = "$GEN"

FINGERPRINT_VERSION = b"V1"


def choose_first(files: List[str]) -> str:
    return files[0]


def choose_shortest_name(files: List[str]) -> str:
    return min(files, key=lambda x: (len(x), x))


def choose_lexicographic(files: List[str]) -> str:
    return min(files)


STRATEGIES: Dict[str, Callable[[List[str]], str]] = {
    "first": choose_first,
    "shortest": choose_shortest_name,
    "lexicographic": choose_lexicographic,
}

CURRENT_CHOOSE_CANONICAL: Callable[[List[str]], str] = choose_shortest_name


def _same_name_bin_path(gltf_path: str) -> Optional[str]:
    base, _ = os.path.splitext(gltf_path)
    cand = base + ".bin"
    if os.path.isfile(cand):
        return cand
    return None


def _load_gltf_json(gltf_path: str) -> Dict[str, Any]:
    with open(gltf_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _iter_buffers(json_dict: Dict[str, Any], gltf_dir: str):
    for buf in json_dict.get("buffers", []):
        declared = int(buf.get("byteLength", 0))
        uri = buf.get("uri", "") or ""

        if uri.startswith("data:"):
            _, _, b64 = uri.partition(",")
            raw = base64.b64decode(b64)
            yield declared, "data", raw
        elif uri:
            bin_path = os.path.normpath(os.path.join(gltf_dir, uri))
            yield declared, "file", bin_path
        else:
            yield declared, "zero", None


def _make_buffer_reader(json_dict: Dict[str, Any], gltf_dir: str):
    infos = []
    for declared, kind, src in _iter_buffers(json_dict, gltf_dir):
        infos.append((declared, kind, src))

    def read(buf_idx: int, offset: int, length: int) -> bytes:
        if buf_idx < 0 or buf_idx >= len(infos):
            return b""
        declared, kind, src = infos[buf_idx]
        if length <= 0:
            return b""
        end = offset + length
        if declared and end > declared:
            end = declared
        real_len = end - offset
        if real_len <= 0:
            return b""

        if kind == "data":
            return src[offset : offset + real_len]
        if kind == "file":
            try:
                with open(src, "rb") as f:
                    f.seek(offset)
                    return f.read(real_len)
            except Exception:
                return b""
        return b"\x00" * real_len

    return read


def _canonicalize_gltf(json_dict: Dict[str, Any]) -> Dict[str, Any]:
    normalized = copy.deepcopy(json_dict)

    asset = normalized.get("asset")
    if isinstance(asset, dict) and "generator" in asset:
        asset["generator"] = GEN_PLACEHOLDER

    for buf in normalized.get("buffers", []):
        if "uri" in buf:
            buf["uri"] = URI_PLACEHOLDER

    return normalized


def _hash_file_into(h: "hashlib._Hash", path: str) -> None:
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
    except Exception:
        pass


def _update_hash_with_buffer(
    h: "hashlib._Hash",
    declared: int,
    kind: str,
    src,
) -> None:
    if kind == "data":
        h.update(src[:declared] if declared else src)
        return

    if kind == "file":
        try:
            with open(src, "rb") as fb:
                if declared <= 0:
                    while True:
                        chunk = fb.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        h.update(chunk)
                else:
                    remaining = declared
                    while remaining > 0:
                        chunk = fb.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            break
                        h.update(chunk)
                        remaining -= len(chunk)
        except Exception:
            pass
        return

    if declared > 0:
        zeros = b"\x00" * min(CHUNK_SIZE, declared)
        left = declared
        while left > 0:
            take = min(len(zeros), left)
            h.update(zeros[:take])
            left -= take


def fingerprint_bins_only(gltf_path: str) -> str:
    h = hashlib.sha1()
    h.update(FINGERPRINT_VERSION)
    h.update(b"|bins|")

    same_name_bin = _same_name_bin_path(gltf_path)
    if same_name_bin is not None:
        _hash_file_into(h, same_name_bin)
        return h.hexdigest()

    try:
        json_dict = _load_gltf_json(gltf_path)
    except Exception:
        return h.hexdigest()
    gltf_dir = os.path.dirname(gltf_path)
    for declared, kind, src in _iter_buffers(json_dict, gltf_dir):
        h.update(b"|buf|")
        h.update(struct.pack("<Q", declared))
        _update_hash_with_buffer(h, declared, kind, src)
    return h.hexdigest()


def fingerprint_json_normalized(gltf_path: str) -> str:
    json_dict = _load_gltf_json(gltf_path)
    normalized = _canonicalize_gltf(json_dict)
    json_bytes = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    h = hashlib.sha1()
    h.update(FINGERPRINT_VERSION)
    h.update(b"|gltf|")
    h.update(struct.pack("<I", len(json_bytes)))
    h.update(json_bytes)
    return h.hexdigest()


def fingerprint_geometry(gltf_path: str) -> str:
    json_dict = _load_gltf_json(gltf_path)
    gltf_dir = os.path.dirname(gltf_path)
    reader = _make_buffer_reader(json_dict, gltf_dir)

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
            buf_idx = int(bv.get("buffer", 0))
            base = int(bv.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
            count = int(acc["count"])

            raw = reader(buf_idx, base, count * 12)
            usable = len(raw) // 12

            for i in range(usable):
                x, y, z = struct.unpack_from("<fff", raw, i * 12)
                positions.append(
                    (
                        round(x, GEO_ROUND_DIGITS),
                        round(y, GEO_ROUND_DIGITS),
                        round(z, GEO_ROUND_DIGITS),
                    )
                )

            if "indices" in prim:
                idx_acc = json_dict["accessors"][prim["indices"]]
                total_indices += int(idx_acc["count"])

    positions.sort()

    h = hashlib.sha1()
    h.update(FINGERPRINT_VERSION)
    h.update(b"|geo|")
    for p in positions:
        h.update(struct.pack("<fff", *p))
    h.update(struct.pack("<I", total_indices))
    return h.hexdigest()


def _dedup_by_fingerprint(
    files: List[str],
    meshs_dir: str,
    fingerprint_fn: Callable[[str], str],
    choose_canonical: Callable[[List[str]], str],
    progress_tag: str = "指纹",
) -> Tuple[List[str], Dict[str, str]]:
    group_map: Dict[str, List[str]] = {}
    total = len(files)

    for i, fname in enumerate(files):
        fpath = os.path.join(meshs_dir, fname)
        try:
            h = fingerprint_fn(fpath)
        except Exception as e:
            log_err(f"{progress_tag} 计算失败 {fname}: {e}")
            h = f"__err__:{fname}"

        group_map.setdefault(h, []).append(fname)

        if total >= 100 and (i + 1) % 100 == 0:
            log(f"{progress_tag} 进度 {i + 1}/{total}")

    canonicals: List[str] = []
    asset_mapping: Dict[str, str] = {}
    for _h, group in group_map.items():
        c = choose_canonical(group)
        canonicals.append(c)
        for f in group:
            asset_mapping[f] = c

    canonicals.sort()
    return canonicals, asset_mapping


def _dedup_bin_first(
    files: List[str],
    meshs_dir: str,
    choose_canonical: Callable[[List[str]], str],
) -> Tuple[List[str], Dict[str, str]]:
    total = len(files)

    bin_groups: Dict[str, List[str]] = {}
    fast_hits = 0
    for i, fname in enumerate(files):
        fpath = os.path.join(meshs_dir, fname)
        if _same_name_bin_path(fpath) is not None:
            fast_hits += 1

        try:
            bh = fingerprint_bins_only(fpath)
        except Exception as e:
            log_err(f"bin 指纹失败 {fname}: {e}")
            bh = f"__err_bin__:{fname}"
        bin_groups.setdefault(bh, []).append(fname)

        if total >= 100 and (i + 1) % 100 == 0:
            log(f"bin 指纹进度 {i + 1}/{total}")

    multi_groups = sum(1 for g in bin_groups.values() if len(g) > 1)
    bin_dup_files = sum(len(g) for g in bin_groups.values() if len(g) > 1)
    log(
        f"bin 指纹阶段: {total} 个文件 → {len(bin_groups)} 组"
        f"（其中 {multi_groups} 组 / {bin_dup_files} 个文件需 JSON 指纹细分）；"
        f"同名 bin 快速路径命中 {fast_hits}/{total}"
    )

    canonicals: List[str] = []
    asset_mapping: Dict[str, str] = {}

    for _bh, group in bin_groups.items():
        if len(group) == 1:
            only = group[0]
            canonicals.append(only)
            asset_mapping[only] = only
            continue

        json_groups: Dict[str, List[str]] = {}
        for fname in group:
            fpath = os.path.join(meshs_dir, fname)
            try:
                jh = fingerprint_json_normalized(fpath)
            except Exception as e:
                log_err(f"json 指纹失败 {fname}: {e}")
                jh = f"__err_json__:{fname}"
            json_groups.setdefault(jh, []).append(fname)

        for _jh, sub in json_groups.items():
            c = choose_canonical(sub)
            canonicals.append(c)
            for f in sub:
                asset_mapping[f] = c

    canonicals.sort()
    return canonicals, asset_mapping


def compose_mappings(
    mapping_prev: Dict[str, str],
    mapping_curr: Dict[str, str],
) -> Dict[str, str]:
    composed: Dict[str, str] = {}
    for f, c_prev in mapping_prev.items():
        composed[f] = mapping_curr.get(c_prev, c_prev)
    return composed


def dedup_level_1(
    files: List[str],
    meshs_dir: str,
    choose_canonical: Callable[[List[str]], str] = CURRENT_CHOOSE_CANONICAL,
) -> Tuple[List[str], Dict[str, str]]:
    return _dedup_bin_first(files, meshs_dir, choose_canonical)


def dedup_level_2(
    files: List[str],
    meshs_dir: str,
    choose_canonical: Callable[[List[str]], str] = CURRENT_CHOOSE_CANONICAL,
) -> Tuple[List[str], Dict[str, str]]:
    return _dedup_by_fingerprint(
        files,
        meshs_dir,
        fingerprint_geometry,
        choose_canonical,
        progress_tag="几何指纹",
    )


def rewrite_asset(node: Any, asset_mapping: Dict[str, str]) -> None:
    """递归重写节点树里的 asset 字段"""
    if isinstance(node, dict):
        asset = node.get("asset")
        if isinstance(asset, str) and asset:
            normalized = asset.replace("\\", "/")
            if "/" in normalized:
                prefix, basename = normalized.rsplit("/", 1)
            else:
                prefix, basename = MESHS_SUBDIR, normalized

            if basename in asset_mapping:
                node["asset"] = f"{prefix}/{asset_mapping[basename]}"

        for child in node.get("children", []) or []:
            rewrite_asset(child, asset_mapping)

    elif isinstance(node, list):
        for item in node:
            rewrite_asset(item, asset_mapping)


def _copy_file(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as fsrc:
        with open(dst, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
            fdst.flush()
            os.fsync(fdst.fileno())
    try:
        shutil.copystat(src, dst)
    except Exception:
        pass


class DedupRunner:

    def __init__(self, input_dir: str, output_dir: str, level: int):
        self.input_dir = os.path.abspath(input_dir)
        self.output_dir = os.path.abspath(output_dir)
        self.level = level

        self.input_meshs_dir = os.path.join(self.input_dir, MESHS_SUBDIR)
        self.input_tree_json = os.path.join(self.input_dir, TREE_JSON_NAME)

        self.output_meshs_dir = os.path.join(self.output_dir, MESHS_SUBDIR)
        self.output_tree_json = os.path.join(self.output_dir, TREE_JSON_NAME)
        self.output_report_json = os.path.join(self.output_dir, REPORT_JSON_NAME)

    def _check_inputs(self) -> None:
        if not os.path.isdir(self.input_dir):
            raise NotADirectoryError(f"输入目录不存在: {self.input_dir}")
        if not os.path.isdir(self.input_meshs_dir):
            raise NotADirectoryError(
                f"输入目录下没有 meshs/ 子目录: {self.input_meshs_dir}"
            )
        if not os.path.isfile(self.input_tree_json):
            raise FileNotFoundError(
                f"输入目录下没有 {TREE_JSON_NAME}: {self.input_tree_json}"
            )

    def _collect_gltfs(self) -> List[str]:
        return sorted(
            os.path.basename(p)
            for p in glob.glob(os.path.join(self.input_meshs_dir, "*.gltf"))
            if os.path.isfile(p)
        )

    def _prepare_output(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        if os.path.isdir(self.output_meshs_dir):
            shutil.rmtree(self.output_meshs_dir)
        os.makedirs(self.output_meshs_dir, exist_ok=True)

    def _copy_canonical_files(self, canonicals: List[str]) -> None:
        total = len(canonicals)
        copied = 0
        fast_hits = 0
        copied_images: Dict[str, bool] = {}

        for c in canonicals:
            src_gltf = os.path.join(self.input_meshs_dir, c)
            dst_gltf = os.path.join(self.output_meshs_dir, c)

            if not os.path.isfile(src_gltf):
                log_err(f"源文件不存在，跳过: {c}")
                continue

            _copy_file(src_gltf, dst_gltf)

            same_name_bin = _same_name_bin_path(src_gltf)
            if same_name_bin is not None:
                fast_hits += 1
                bin_name = os.path.basename(same_name_bin)
                _copy_file(same_name_bin, os.path.join(self.output_meshs_dir, bin_name))
            else:
                try:
                    json_dict = _load_gltf_json(src_gltf)
                except Exception as e:
                    log_err(f"解析 {c} 失败，跳过 bin: {e}")
                    json_dict = None

                if json_dict is not None:
                    gltf_dir = os.path.dirname(src_gltf)
                    for buf in json_dict.get("buffers", []):
                        uri = buf.get("uri", "") or ""
                        if not uri or uri.startswith("data:"):
                            continue
                        bin_src = os.path.normpath(os.path.join(gltf_dir, uri))
                        bin_dst = os.path.normpath(
                            os.path.join(self.output_meshs_dir, uri)
                        )
                        if not os.path.isfile(bin_src):
                            log_err(f"缺失 bin，跳过: {bin_src}")
                            continue
                        _copy_file(bin_src, bin_dst)

            try:
                img_json = _load_gltf_json(src_gltf)
            except Exception:
                img_json = None

            if img_json is not None:
                gltf_dir = os.path.dirname(src_gltf)
                for img in img_json.get("images", []) or []:
                    uri = img.get("uri", "") or ""
                    if not uri or uri.startswith("data:"):
                        continue
                    if uri in copied_images:
                        continue
                    img_src = os.path.normpath(os.path.join(gltf_dir, uri))
                    img_dst = os.path.normpath(os.path.join(self.output_meshs_dir, uri))
                    if not os.path.isfile(img_src):
                        log_err(f"缺失图片，跳过: {img_src}")
                        copied_images[uri] = True
                        continue
                    if not os.path.exists(img_dst):
                        _copy_file(img_src, img_dst)
                    copied_images[uri] = True

            copied += 1

            if total >= 100 and copied % 100 == 0:
                log(f"复制进度 {copied}/{total}")

        log(
            f"复制完成: {copied}/{total} 个 gltf（含各自 bin + 贴图）；"
            f"同名 bin 快速路径命中 {fast_hits}/{copied}；"
            f"贴图 {len(copied_images)} 张"
        )

    def _rewrite_tree(self, asset_mapping: Dict[str, str]) -> None:
        with open(self.input_tree_json, "r", encoding="utf-8") as f:
            tree = json.load(f)

        non_identity = sum(1 for k, v in asset_mapping.items() if k != v)
        rewrite_asset(tree, asset_mapping)

        with open(self.output_tree_json, "w", encoding="utf-8") as f:
            json.dump(tree, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        log(f"重写 asset 引用 {non_identity} 条")

    def run(self) -> None:
        self._check_inputs()

        log(f"输入目录: {self.input_dir}")
        log(f"输出目录: {self.output_dir}")
        log(f"去重等级: {self.level}")

        files = self._collect_gltfs()
        total = len(files)
        if total == 0:
            log_err("meshs/ 目录下没有 .gltf 文件")
            raise RuntimeError("无可处理的 glTF 文件")

        log(f"发现 {total} 个 glTF 文件")

        log("一级去重（bin 优先）")
        canonicals, mapping = dedup_level_1(
            files, self.input_meshs_dir, CURRENT_CHOOSE_CANONICAL
        )
        log(f"一级去重: {total} → {len(canonicals)}")

        if self.level >= 2:
            log("二级去重（几何指纹）")
            canonicals2, mapping2 = dedup_level_2(
                canonicals, self.input_meshs_dir, CURRENT_CHOOSE_CANONICAL
            )
            mapping = compose_mappings(mapping, mapping2)
            canonicals = canonicals2
            log(f"几何去重: {total} → {len(canonicals)}")

        log(f"输出准备：复制 {len(canonicals)} 个文件对 ...")
        self._prepare_output()
        self._copy_canonical_files(canonicals)

        log("清洗结构树")
        self._rewrite_tree(mapping)
        log("清洗结构树完成")


def main():
    if len(sys.argv) < 3:
        log_err(
            f"参数不足：需要至少 2 个参数（输入目录、输出目录），"
            f"实际收到 {max(0, len(sys.argv) - 1)} 个"
        )
        print(
            "用法: python dedup.py <输入目录> <输出目录> [去重等级 1|2]  (gltf+bin 版)"
        )
        print("说明:")
        print("  <输入目录>    含 assembly-tree.json + meshs/ 的目录")
        print("                meshs/ 内为 *.gltf + 同名 *.bin + tex_*.png")
        print("  <输出目录>    去重后的产物目录（结构与输入一致）")
        print("  [去重等级]    可选，1 = bin 优先的内容级（默认）；")
        print("                     2 = 内容级 + 几何指纹级联")
        sys.exit(1)

    input_dir = sys.argv[1]
    output_dir = sys.argv[2]

    level = 1
    if len(sys.argv) >= 4:
        try:
            level = int(sys.argv[3])
        except ValueError:
            log_err(f"去重等级必须是整数 1 或 2，收到: {sys.argv[3]!r}")
            sys.exit(1)

        if level not in (1, 2):
            log_err(f"去重等级只支持 1 或 2，收到: {level}")
            sys.exit(1)

    try:
        DedupRunner(input_dir, output_dir, level).run()
    except Exception as e:
        log_err(f"去重失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
