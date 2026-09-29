"""
glb-splitter.py — GLB → glTF + BIN 拆分 + 结构树 JSON 导出
                  保留原始 node 树结构
                  mesh 内容去重：字节级/浮点量化级相同的 mesh 只导出一份 gltf+bin
                  顶点合并：位置量化 + 法线角度聚类 + 面积加权平均
                  材质：PBR 常量 + 贴图
                        - 保持原始图片格式（PNG/JPG/WebP/... 不转换）
                        - 支持三种来源：外部文件 / data URI (base64) / bufferView
                        - 落盘为 meshs/tex_<内容hash>.<ext>，内容哈希去重
                  IO 走 mmap + array.array，1G+ GLB 不会爆内存

用法:
    python glb-splitter.py <input.glb> <output.json> [<ignored>]

输出:
    <output.json>              结构树（材质表引用相对路径的贴图）
    meshs/xxx__hash.gltf       每个带 mesh 的 node 一份 glTF
    meshs/xxx__hash.bin        同名外置 buffer
    meshs/tex_<hash>.<ext>     贴图（内容哈希去重）
"""

import os
import sys
import re
import json
import math
import struct
import hashlib
import mmap
import array
import base64
from typing import Dict, Any, List, Optional, Tuple

# =====================================================================
# 脚本级配置
# =====================================================================

MESHS_SUBDIR = "meshs"

VERTEX_MERGE_MODE = "normalize"
KEEP_UV = False

SMOOTH_ANGLE_DEG = 30.0
SMOOTH_ANGLE_COS = math.cos(math.radians(SMOOTH_ANGLE_DEG))

COMPUTE_NORMALS = True
VERTEX_POS_GRID_METER = 1e-5

FILENAME_NAME_MAX_LEN = 80
FILENAME_HASH_LEN = 8
IMAGE_HASH_LEN = 16


COMPONENT_TYPE_SIZE = {
    5120: 1,
    5121: 1,
    5122: 2,
    5123: 2,
    5125: 4,
    5126: 4,
}
COMPONENT_TYPE_FMT = {
    5120: "b",
    5121: "B",
    5122: "h",
    5123: "H",
    5125: "I",
    5126: "f",
}
TYPE_DIM = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


def log(msg: str) -> None:
    print(msg, flush=True)


def log_err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _sniff_image_ext(raw: bytes, mime_hint: str = "") -> str:
    if len(raw) >= 8 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if len(raw) >= 3 and raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if len(raw) >= 6 and raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if len(raw) >= 2 and raw[:2] == b"BM":
        return ".bmp"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if len(raw) >= 12 and raw[:12] == b"\xabKTX 20\xbb\r\n\x1a\n":
        return ".ktx2"
    if len(raw) >= 4 and raw[:4] == b"DDS ":
        return ".dds"
    if len(raw) >= 4 and raw[:4] == b"\x76\x2f\x31\x01":
        return ".exr"

    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "image/ktx2": ".ktx2",
        "image/vnd-ms.dds": ".dds",
        "image/x-exr": ".exr",
    }.get((mime_hint or "").lower(), ".bin")


def _material_id(mat: dict) -> str:
    key = json.dumps(mat, sort_keys=True, ensure_ascii=False)
    return "mat_" + hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


class GlbContainer:

    def __init__(self, path: str):
        self.path = path
        self.file = None
        self.mm = None
        self.gltf: Optional[Dict[str, Any]] = None
        self.bin_offset = 0
        self.bin_length = 0

        try:
            self.file = open(path, "rb")
            self.mm = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)

            if len(self.mm) < 12:
                raise ValueError(f"GLB 文件过短: {path}")

            magic, version, length = struct.unpack_from("<4sII", self.mm, 0)
            if magic != b"glTF":
                raise ValueError(f"不是 GLB 文件: {path}")
            if version != 2:
                raise ValueError(f"不支持的 GLB 版本: {version}")

            offset = 12
            while offset + 8 <= length:
                chunk_len, chunk_type = struct.unpack_from("<I4s", self.mm, offset)
                chunk_start = offset + 8
                chunk_end = chunk_start + chunk_len

                if chunk_type == b"JSON":
                    raw = bytes(self.mm[chunk_start:chunk_end])
                    self.gltf = json.loads(raw.decode("utf-8"))
                elif chunk_type == b"BIN\x00":
                    self.bin_offset = chunk_start
                    self.bin_length = chunk_len

                offset = chunk_end

            if self.gltf is None:
                raise ValueError(f"GLB 里没有 JSON chunk: {path}")

        except Exception:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self.mm is not None:
                self.mm.close()
        except Exception:
            pass
        self.mm = None
        try:
            if self.file is not None:
                self.file.close()
        except Exception:
            pass
        self.file = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def read_bin_view(self, offset: int, length: int) -> memoryview:
        start = self.bin_offset + offset
        end = start + length
        return memoryview(self.mm)[start:end]


class GlbSplitter:

    def __init__(
        self,
        container: GlbContainer,
        output_meshs_dir: str,
        grid_meter: float,
    ):
        self.container = container
        self.gltf = container.gltf
        self.meshs_dir = output_meshs_dir
        self.grid = grid_meter

        self.mesh_cache: Dict[int, str] = {}
        self._mesh_full_sig: Dict[int, bytes] = {}
        self._canonical_by_sig: Dict[bytes, int] = {}
        self._canonical_mesh: Dict[int, int] = {}

        self.material_table: Dict[str, dict] = {}
        self._exported_image_files: Dict[str, str] = {}
        self._image_idx_to_file: Dict[int, str] = {}

        self.keep_uv = True if VERTEX_MERGE_MODE == "preserve" else KEEP_UV

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        if not name:
            return ""
        s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
        s = "".join(c for c in s if c.isprintable())
        s = re.sub(r"_+", "_", s)
        return s.strip(" ._")

    def _read_image_raw(self, image_idx: int) -> Optional[Tuple[str, bytes]]:
        images = self.gltf.get("images", [])
        if image_idx < 0 or image_idx >= len(images):
            return None
        img = images[image_idx]
        mime = (img.get("mimeType") or "").strip()

        uri = img.get("uri")
        if uri:
            if uri.startswith("data:"):
                header, _, b64 = uri.partition(",")
                if not mime:
                    try:
                        mime = header.split(";")[0].split(":", 1)[1]
                    except Exception:
                        mime = ""
                try:
                    raw = base64.b64decode(b64)
                except Exception as e:
                    log_err(f"[tex] base64 解码失败: {e}")
                    return None
                return mime, raw
            else:
                img_path = os.path.join(os.path.dirname(self.container.path), uri)
                try:
                    with open(img_path, "rb") as f:
                        raw = f.read()
                except Exception as e:
                    log_err(f"[tex] 读取外部贴图失败 {img_path}: {e}")
                    return None
                if not mime:
                    ext = os.path.splitext(uri)[1].lower()
                    mime = {
                        ".png": "image/png",
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".webp": "image/webp",
                        ".bmp": "image/bmp",
                        ".gif": "image/gif",
                        ".ktx2": "image/ktx2",
                    }.get(ext, "")
                return mime, raw

        bv_idx = img.get("bufferView")
        if bv_idx is not None:
            bv = self.gltf["bufferViews"][bv_idx]
            base = int(bv.get("byteOffset", 0))
            length = int(bv["byteLength"])
            raw = bytes(self.container.read_bin_view(base, length))
            return mime, raw

        return None

    def _ensure_image_file(self, image_idx: int) -> Optional[str]:
        if image_idx in self._image_idx_to_file:
            return self._image_idx_to_file[image_idx]

        result = self._read_image_raw(image_idx)
        if result is None:
            return None
        mime_hint, raw = result

        ext = _sniff_image_ext(raw, mime_hint)
        content_hash = hashlib.sha1(raw).hexdigest()[:IMAGE_HASH_LEN]
        filename = f"tex_{content_hash}{ext}"

        if content_hash not in self._exported_image_files:
            filepath = os.path.join(self.meshs_dir, filename)
            if not os.path.exists(filepath):
                try:
                    with open(filepath, "wb") as f:
                        f.write(raw)
                except Exception as e:
                    log_err(f"[tex] 写图片失败 {filepath}: {e}")
                    return None
            self._exported_image_files[content_hash] = filename

        self._image_idx_to_file[image_idx] = filename
        return filename

    def _resolve_texture_filename(self, gltf_tex_idx: int) -> Optional[str]:
        textures = self.gltf.get("textures", [])
        if gltf_tex_idx < 0 or gltf_tex_idx >= len(textures):
            return None
        src = textures[gltf_tex_idx].get("source")
        if src is None:
            return None
        return self._ensure_image_file(int(src))

    def register_material(self, mat_idx: int) -> Optional[str]:
        materials = self.gltf.get("materials", [])
        if mat_idx < 0 or mat_idx >= len(materials):
            return None
        src = materials[mat_idx]

        pbr = src.get("pbrMetallicRoughness", {}) or {}
        bc = pbr.get("baseColorFactor", [1.0, 1.0, 1.0, 1.0])
        try:
            r, g, b = float(bc[0]), float(bc[1]), float(bc[2])
            a = float(bc[3]) if len(bc) > 3 else 1.0
        except Exception:
            r = g = b = 1.0
            a = 1.0

        mat: Dict[str, Any] = {
            "source": "pbr",
            "base_color": [round(r, 4), round(g, 4), round(b, 4), round(a, 4)],
            "metallic": round(float(pbr.get("metallicFactor", 1.0)), 4),
            "roughness": round(float(pbr.get("roughnessFactor", 1.0)), 4),
        }

        ef = src.get("emissiveFactor")
        if ef:
            try:
                mat["emissive_factor"] = [round(float(x), 4) for x in ef[:3]]
            except Exception:
                pass

        if src.get("alphaMode") == "BLEND":
            mat["alpha_mode"] = "BLEND"
        if src.get("doubleSided"):
            mat["double_sided"] = True

        tex_map = [
            ("base_color_texture", pbr.get("baseColorTexture")),
            ("metallic_roughness_texture", pbr.get("metallicRoughnessTexture")),
            ("normal_texture", src.get("normalTexture")),
            ("occlusion_texture", src.get("occlusionTexture")),
            ("emissive_texture", src.get("emissiveTexture")),
        ]
        for our_key, tex_info in tex_map:
            if not tex_info:
                continue
            tex_idx = tex_info.get("index")
            if tex_idx is None:
                continue
            filename = self._resolve_texture_filename(int(tex_idx))
            if filename:
                mat[our_key] = filename

        mid = _material_id(mat)
        if mid not in self.material_table:
            self.material_table[mid] = mat
        return mid

    def read_accessor_array(self, acc_idx: int) -> array.array:
        acc = self.gltf["accessors"][acc_idx]
        count = int(acc["count"])
        num_comp = TYPE_DIM[acc["type"]]
        comp_type = int(acc["componentType"])
        typecode = COMPONENT_TYPE_FMT[comp_type]
        total_elems = count * num_comp

        bv_idx = acc.get("bufferView")
        if bv_idx is None:
            return array.array(
                typecode,
                bytes(COMPONENT_TYPE_SIZE[comp_type] * total_elems),
            )

        bv = self.gltf["bufferViews"][bv_idx]
        buf_idx = int(bv.get("buffer", 0))
        if buf_idx != 0:
            raise ValueError(f"不支持多 buffer 引用：buffer={buf_idx}")

        comp_size = COMPONENT_TYPE_SIZE[comp_type]
        elem_size = comp_size * num_comp
        stride = int(bv.get("byteStride") or elem_size)
        base = int(bv.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))

        if count == 0:
            return array.array(typecode)

        if stride == elem_size:
            mv = self.container.read_bin_view(base, total_elems * comp_size)
            arr = array.array(typecode)
            arr.frombytes(mv)
            return arr

        arr = array.array(typecode)
        for i in range(count):
            off = base + i * stride
            mv = self.container.read_bin_view(off, elem_size)
            arr.frombytes(mv)
        return arr

    def _mesh_full_signature(self, mesh_idx: int) -> bytes:
        if mesh_idx in self._mesh_full_sig:
            return self._mesh_full_sig[mesh_idx]

        mesh = self.gltf["meshes"][mesh_idx]
        h = hashlib.sha1()
        inv_grid = 1.0 / self.grid

        for prim in mesh.get("primitives", []):
            h.update(struct.pack("<I", int(prim.get("mode", 4))))

            mat_idx = prim.get("material")
            if mat_idx is not None:
                mid = self.register_material(int(mat_idx))
                if mid:
                    h.update(mid.encode("ascii"))

            attrs = prim.get("attributes", {})
            for k in sorted(attrs.keys()):
                acc_idx = attrs[k]
                acc = self.gltf["accessors"][acc_idx]
                count = int(acc["count"])
                comp_type = int(acc["componentType"])
                type_dim = TYPE_DIM[acc["type"]]

                h.update(k.encode("utf-8"))
                h.update(struct.pack("<III", count, comp_type, type_dim))

                arr = self.read_accessor_array(acc_idx)
                if comp_type == 5126:
                    q = array.array("q")
                    for v in arr:
                        q.append(int(round(v * inv_grid)))
                    h.update(q.tobytes())
                    del q
                else:
                    h.update(arr.tobytes())
                del arr

            idx_idx = prim.get("indices")
            if idx_idx is not None:
                acc = self.gltf["accessors"][idx_idx]
                h.update(struct.pack("<I", int(acc["count"])))
                arr = self.read_accessor_array(idx_idx)
                h.update(arr.tobytes())
                del arr

        sig = h.digest()
        self._mesh_full_sig[mesh_idx] = sig
        return sig

    def resolve_canonical_mesh(self, mesh_idx: int) -> int:
        if mesh_idx in self._canonical_mesh:
            return self._canonical_mesh[mesh_idx]
        full_sig = self._mesh_full_signature(mesh_idx)
        canonical = self._canonical_by_sig.get(full_sig)
        if canonical is None:
            canonical = mesh_idx
            self._canonical_by_sig[full_sig] = mesh_idx
        self._canonical_mesh[mesh_idx] = canonical
        return canonical

    @staticmethod
    def _decompose_matrix(
        m: List[float],
    ) -> Tuple[List[float], List[float], List[float]]:
        tx, ty, tz = m[12], m[13], m[14]
        c0 = (m[0], m[1], m[2])
        c1 = (m[4], m[5], m[6])
        c2 = (m[8], m[9], m[10])

        sx = math.sqrt(c0[0] ** 2 + c0[1] ** 2 + c0[2] ** 2)
        sy = math.sqrt(c1[0] ** 2 + c1[1] ** 2 + c1[2] ** 2)
        sz = math.sqrt(c2[0] ** 2 + c2[1] ** 2 + c2[2] ** 2)

        if sx > 1e-12:
            c0 = (c0[0] / sx, c0[1] / sx, c0[2] / sx)
        if sy > 1e-12:
            c1 = (c1[0] / sy, c1[1] / sy, c1[2] / sy)
        if sz > 1e-12:
            c2 = (c2[0] / sz, c2[1] / sz, c2[2] / sz)

        r00, r10, r20 = c0
        r01, r11, r21 = c1
        r02, r12, r22 = c2

        trace = r00 + r11 + r22
        if trace > 0:
            s = math.sqrt(trace + 1.0) * 2.0
            qw = 0.25 * s
            qx = (r21 - r12) / s
            qy = (r02 - r20) / s
            qz = (r10 - r01) / s
        elif r00 > r11 and r00 > r22:
            s = math.sqrt(1.0 + r00 - r11 - r22) * 2.0
            qw = (r21 - r12) / s
            qx = 0.25 * s
            qy = (r01 + r10) / s
            qz = (r02 + r20) / s
        elif r11 > r22:
            s = math.sqrt(1.0 + r11 - r00 - r22) * 2.0
            qw = (r02 - r20) / s
            qx = (r01 + r10) / s
            qy = 0.25 * s
            qz = (r12 + r21) / s
        else:
            s = math.sqrt(1.0 + r22 - r00 - r11) * 2.0
            qw = (r10 - r01) / s
            qx = (r02 + r20) / s
            qy = (r12 + r21) / s
            qz = 0.25 * s

        return [tx, ty, tz], [qx, qy, qz, qw], [sx, sy, sz]

    def _get_node_transform(self, node: Dict[str, Any]) -> Dict[str, Any]:
        if "matrix" in node:
            t, r, s = self._decompose_matrix(node["matrix"])
        else:
            t = node.get("translation", [0.0, 0.0, 0.0])
            r = node.get("rotation", [0.0, 0.0, 0.0, 1.0])
            s = node.get("scale", [1.0, 1.0, 1.0])

        if abs(s[0] - s[1]) < 1e-9 and abs(s[1] - s[2]) < 1e-9:
            scale_out: Any = float(s[0])
        else:
            scale_out = [float(x) for x in s]

        return {
            "position": [float(x) for x in t],
            "quaternion": [float(x) for x in r],
            "scale": scale_out,
        }

    def _merge_vertices(
        self,
        positions: array.array,
        normals: Optional[array.array],
        uvs: Optional[array.array],
        indices: array.array,
        vertex_offset: int,
    ) -> Tuple[array.array, array.array, Optional[array.array], array.array]:
        num_verts = len(positions) // 3
        num_tris = len(indices) // 3

        empty_f = array.array("f")
        empty_i = array.array("I")

        if num_verts == 0:
            return empty_f, empty_f, (empty_f if self.keep_uv else None), empty_i

        node_area = array.array("f", bytes(4 * num_verts))
        tri_cross = array.array("f", bytes(12 * num_tris))

        for ti in range(num_tris):
            i0 = indices[ti * 3]
            i1 = indices[ti * 3 + 1]
            i2 = indices[ti * 3 + 2]
            v0i = i0 * 3
            v1i = i1 * 3
            v2i = i2 * 3
            v0x = positions[v0i]
            v0y = positions[v0i + 1]
            v0z = positions[v0i + 2]
            v1x = positions[v1i]
            v1y = positions[v1i + 1]
            v1z = positions[v1i + 2]
            v2x = positions[v2i]
            v2y = positions[v2i + 1]
            v2z = positions[v2i + 2]

            e1x = v1x - v0x
            e1y = v1y - v0y
            e1z = v1z - v0z
            e2x = v2x - v0x
            e2y = v2y - v0y
            e2z = v2z - v0z

            cx = e1y * e2z - e1z * e2y
            cy = e1z * e2x - e1x * e2z
            cz = e1x * e2y - e1y * e2x

            ti3 = ti * 3
            tri_cross[ti3] = cx
            tri_cross[ti3 + 1] = cy
            tri_cross[ti3 + 2] = cz
            area = 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)
            node_area[i0] += area
            node_area[i1] += area
            node_area[i2] += area

        if COMPUTE_NORMALS:
            acc = array.array("f", bytes(12 * num_verts))
            for ti in range(num_tris):
                i0 = indices[ti * 3]
                i1 = indices[ti * 3 + 1]
                i2 = indices[ti * 3 + 2]
                ti3 = ti * 3
                cx = tri_cross[ti3]
                cy = tri_cross[ti3 + 1]
                cz = tri_cross[ti3 + 2]
                a0 = i0 * 3
                a1 = i1 * 3
                a2 = i2 * 3
                acc[a0] += cx
                acc[a0 + 1] += cy
                acc[a0 + 2] += cz
                acc[a1] += cx
                acc[a1 + 1] += cy
                acc[a1 + 2] += cz
                acc[a2] += cx
                acc[a2 + 1] += cy
                acc[a2 + 2] += cz

            for vi in range(num_verts):
                v3 = vi * 3
                x = acc[v3]
                y = acc[v3 + 1]
                z = acc[v3 + 2]
                ln = math.sqrt(x * x + y * y + z * z)
                if ln < 1e-12:
                    acc[v3] = 0.0
                    acc[v3 + 1] = 0.0
                    acc[v3 + 2] = 1.0
                else:
                    acc[v3] = x / ln
                    acc[v3 + 1] = y / ln
                    acc[v3 + 2] = z / ln
            vertex_normals = acc
        elif normals is not None and len(normals) >= num_verts * 3:
            vertex_normals = normals
        else:
            vertex_normals = array.array("f", bytes(12 * num_verts))
            for vi in range(num_verts):
                vertex_normals[vi * 3 + 2] = 1.0

        del tri_cross

        inv_grid = 1.0 / self.grid
        MASK32 = 0xFFFFFFFF

        buckets: Dict[int, List[dict]] = {}
        node_to_cluster_idx = array.array("I", bytes(4 * num_verts))
        cluster_positions = array.array("f")
        cluster_normals_acc = array.array("f")
        cluster_uvs = array.array("f") if self.keep_uv else None
        has_uv_global = self.keep_uv and uvs is not None and len(uvs) >= num_verts * 2

        for vi in range(num_verts):
            v3 = vi * 3
            px = positions[v3]
            py = positions[v3 + 1]
            pz = positions[v3 + 2]
            nx = vertex_normals[v3]
            ny = vertex_normals[v3 + 1]
            nz = vertex_normals[v3 + 2]
            w = node_area[vi]
            if w <= 0.0:
                w = 1e-12

            qx = int(round(px * inv_grid))
            qy = int(round(py * inv_grid))
            qz = int(round(pz * inv_grid))
            key = ((qx & MASK32) << 64) | ((qy & MASK32) << 32) | (qz & MASK32)

            blist = buckets.get(key)
            if blist is None:
                blist = []
                buckets[key] = blist

            uv_u = 0.0
            uv_v = 0.0
            has_uv = has_uv_global
            if has_uv:
                uv_u = uvs[vi * 2]
                uv_v = uvs[vi * 2 + 1]

            matched = None
            for cl in blist:
                dot = nx * cl["ref_nx"] + ny * cl["ref_ny"] + nz * cl["ref_nz"]
                if dot < SMOOTH_ANGLE_COS:
                    continue
                if has_uv and cl["ref_uv_valid"]:
                    if (
                        abs(uv_u - cl["ref_uv_u"]) > 1e-5
                        or abs(uv_v - cl["ref_uv_v"]) > 1e-5
                    ):
                        continue
                matched = cl
                break

            if matched is None:
                new_idx = len(cluster_positions) // 3
                cl = {
                    "ref_nx": nx,
                    "ref_ny": ny,
                    "ref_nz": nz,
                    "vertex_idx": new_idx,
                    "ref_uv_valid": has_uv,
                    "ref_uv_u": uv_u,
                    "ref_uv_v": uv_v,
                }
                blist.append(cl)
                cluster_positions.append(px)
                cluster_positions.append(py)
                cluster_positions.append(pz)
                cluster_normals_acc.append(nx * w)
                cluster_normals_acc.append(ny * w)
                cluster_normals_acc.append(nz * w)
                if cluster_uvs is not None:
                    cluster_uvs.append(uv_u)
                    cluster_uvs.append(uv_v)
            else:
                new_idx = matched["vertex_idx"]
                ci3 = new_idx * 3
                cluster_normals_acc[ci3] += nx * w
                cluster_normals_acc[ci3 + 1] += ny * w
                cluster_normals_acc[ci3 + 2] += nz * w

            node_to_cluster_idx[vi] = new_idx

        del buckets
        del node_area
        del vertex_normals

        num_clusters = len(cluster_positions) // 3
        out_normals = array.array("f", bytes(12 * num_clusters))
        for ci in range(num_clusters):
            ci3 = ci * 3
            x = cluster_normals_acc[ci3]
            y = cluster_normals_acc[ci3 + 1]
            z = cluster_normals_acc[ci3 + 2]
            ln = math.sqrt(x * x + y * y + z * z)
            if ln < 1e-12:
                out_normals[ci3] = 0.0
                out_normals[ci3 + 1] = 0.0
                out_normals[ci3 + 2] = 1.0
            else:
                out_normals[ci3] = x / ln
                out_normals[ci3 + 1] = y / ln
                out_normals[ci3 + 2] = z / ln

        del cluster_normals_acc

        new_indices = array.array("I", bytes(4 * len(indices)))
        for j in range(len(indices)):
            new_indices[j] = node_to_cluster_idx[indices[j]] + vertex_offset

        del node_to_cluster_idx

        return cluster_positions, out_normals, cluster_uvs, new_indices

    def export_mesh(self, mesh_idx: int, display_name: str) -> Optional[str]:
        canonical = self.resolve_canonical_mesh(mesh_idx)
        if canonical in self.mesh_cache:
            return self.mesh_cache[canonical]

        mesh = self.gltf["meshes"][canonical]
        mesh_name = mesh.get("name") or display_name or f"mesh_{canonical}"

        all_positions = array.array("f")
        all_normals = array.array("f")
        all_uvs = array.array("f")
        any_uv = False
        vertex_offset = 0

        out_prims: List[Tuple[array.array, Optional[int]]] = []

        for prim in mesh.get("primitives", []):
            mode = int(prim.get("mode", 4))
            if mode != 4:
                log_err(f"[mesh] 跳过非三角形 primitive (mode={mode})")
                continue

            attrs = prim.get("attributes", {})
            pos_acc_idx = attrs.get("POSITION")
            if pos_acc_idx is None:
                continue

            positions = self.read_accessor_array(pos_acc_idx)

            normals: Optional[array.array] = None
            nrm_acc_idx = attrs.get("NORMAL")
            if nrm_acc_idx is not None:
                normals = self.read_accessor_array(nrm_acc_idx)

            uvs: Optional[array.array] = None
            uv_acc_idx = attrs.get("TEXCOORD_0")
            if uv_acc_idx is not None and self.keep_uv:
                uvs = self.read_accessor_array(uv_acc_idx)

            idx_acc_idx = prim.get("indices")
            if idx_acc_idx is not None:
                raw_idx = self.read_accessor_array(idx_acc_idx)
                indices = (
                    raw_idx if raw_idx.typecode == "I" else array.array("I", raw_idx)
                )
            else:
                num_v = len(positions) // 3
                indices = array.array("I", range(num_v))

            if VERTEX_MERGE_MODE == "preserve":
                new_positions = positions
                new_normals = (
                    normals
                    if normals is not None
                    else array.array("f", bytes(len(positions) * 4))
                )
                if normals is None:
                    for vi in range(len(positions) // 3):
                        new_normals[vi * 3 + 2] = 1.0
                new_uvs = uvs
                new_indices = array.array("I", bytes(4 * len(indices)))
                for j in range(len(indices)):
                    new_indices[j] = indices[j] + vertex_offset
            else:
                new_positions, new_normals, new_uvs, new_indices = self._merge_vertices(
                    positions, normals, uvs, indices, vertex_offset
                )

            all_positions.extend(new_positions)
            all_normals.extend(new_normals)
            if self.keep_uv and new_uvs is not None:
                all_uvs.extend(new_uvs)
                any_uv = True
            vertex_offset += len(new_positions) // 3

            src_mat_idx = prim.get("material")
            out_prims.append(
                (
                    new_indices,
                    int(src_mat_idx) if src_mat_idx is not None else None,
                )
            )

        if not out_prims or len(all_positions) == 0:
            return None

        num_verts = len(all_positions) // 3

        out_materials: List[dict] = []
        out_textures: List[dict] = []
        out_images: List[dict] = []
        out_samplers: List[dict] = []

        src_materials = self.gltf.get("materials", [])
        src_textures = self.gltf.get("textures", [])
        src_samplers = self.gltf.get("samplers", [])

        tex_map: Dict[int, int] = {}
        smp_map: Dict[int, int] = {}
        img_map: Dict[int, int] = {}

        def ensure_sampler(si: int) -> Optional[int]:
            if si in smp_map:
                return smp_map[si]
            if si < 0 or si >= len(src_samplers):
                return None
            new_idx = len(out_samplers)
            out_samplers.append(dict(src_samplers[si]))
            smp_map[si] = new_idx
            return new_idx

        def ensure_image(im_idx: int) -> Optional[int]:
            if im_idx in img_map:
                return img_map[im_idx]
            filename = self._ensure_image_file(im_idx)
            if filename is None:
                return None
            new_idx = len(out_images)
            out_images.append({"uri": filename})
            img_map[im_idx] = new_idx
            return new_idx

        def ensure_texture(ti: int) -> Optional[int]:
            if ti in tex_map:
                return tex_map[ti]
            if ti < 0 or ti >= len(src_textures):
                return None
            src_tex = src_textures[ti]
            new_tex: Dict[str, Any] = {}
            src_img = src_tex.get("source")
            if src_img is not None:
                ni = ensure_image(int(src_img))
                if ni is None:
                    return None
                new_tex["source"] = ni
            src_smp = src_tex.get("sampler")
            if src_smp is not None:
                ns = ensure_sampler(int(src_smp))
                if ns is not None:
                    new_tex["sampler"] = ns
            new_idx = len(out_textures)
            out_textures.append(new_tex)
            tex_map[ti] = new_idx
            return new_idx

        def convert_material(src_mat_idx: int) -> Optional[int]:
            if src_mat_idx < 0 or src_mat_idx >= len(src_materials):
                return None
            src = src_materials[src_mat_idx]
            new_mat: Dict[str, Any] = {}

            for k, v in src.items():
                if k == "pbrMetallicRoughness":
                    new_pbr = dict(v)
                    for tk in ("baseColorTexture", "metallicRoughnessTexture"):
                        if tk in new_pbr and new_pbr[tk]:
                            ti = new_pbr[tk].get("index")
                            if ti is not None:
                                nt = ensure_texture(int(ti))
                                if nt is None:
                                    new_pbr.pop(tk, None)
                                else:
                                    new_ti = dict(new_pbr[tk])
                                    new_ti["index"] = nt
                                    new_pbr[tk] = new_ti
                    new_mat["pbrMetallicRoughness"] = new_pbr
                elif k in ("normalTexture", "occlusionTexture", "emissiveTexture"):
                    if v and isinstance(v, dict):
                        ti = v.get("index")
                        if ti is not None:
                            nt = ensure_texture(int(ti))
                            if nt is None:
                                continue
                            new_tex_info = dict(v)
                            new_tex_info["index"] = nt
                            new_mat[k] = new_tex_info
                        else:
                            new_mat[k] = v
                    else:
                        new_mat[k] = v
                else:
                    new_mat[k] = v

            new_idx = len(out_materials)
            out_materials.append(new_mat)
            return new_idx

        src_mat_to_new: Dict[int, int] = {}
        for _, src_mat_idx in out_prims:
            if src_mat_idx is None:
                continue
            if src_mat_idx in src_mat_to_new:
                continue
            nm = convert_material(src_mat_idx)
            if nm is not None:
                src_mat_to_new[src_mat_idx] = nm

        v_bytes = all_positions.tobytes()
        n_bytes = all_normals.tobytes()
        u_bytes: Optional[bytes] = None
        if self.keep_uv and any_uv and len(all_uvs) > 0:
            u_bytes = all_uvs.tobytes()

        def pad4(b: bytes) -> bytes:
            rem = (-len(b)) % 4
            return b if rem == 0 else b + b"\x00" * rem

        v_bytes = pad4(v_bytes)
        n_bytes = pad4(n_bytes)
        if u_bytes is not None:
            u_bytes = pad4(u_bytes)

        prim_idx_bytes: List[bytes] = []
        for indices_arr, _ in out_prims:
            prim_idx_bytes.append(pad4(indices_arr.tobytes()))

        bin_buffer = bytearray()
        v_offset = len(bin_buffer)
        bin_buffer.extend(v_bytes)
        n_offset = len(bin_buffer)
        bin_buffer.extend(n_bytes)
        u_offset: Optional[int] = None
        if u_bytes is not None:
            u_offset = len(bin_buffer)
            bin_buffer.extend(u_bytes)

        i_offsets: List[int] = []
        for ib in prim_idx_bytes:
            i_offsets.append(len(bin_buffer))
            bin_buffer.extend(ib)

        min_pos = [float("inf")] * 3
        max_pos = [float("-inf")] * 3
        for vi in range(num_verts):
            v3 = vi * 3
            for k in range(3):
                v = all_positions[v3 + k]
                if v < min_pos[k]:
                    min_pos[k] = v
                if v > max_pos[k]:
                    max_pos[k] = v

        buffer_views: List[dict] = [
            {
                "buffer": 0,
                "byteOffset": v_offset,
                "byteLength": len(v_bytes),
                "target": 34962,
            },
            {
                "buffer": 0,
                "byteOffset": n_offset,
                "byteLength": len(n_bytes),
                "target": 34962,
            },
        ]
        accessors: List[dict] = [
            {
                "bufferView": 0,
                "byteOffset": 0,
                "componentType": 5126,
                "count": num_verts,
                "type": "VEC3",
                "max": max_pos,
                "min": min_pos,
            },
            {
                "bufferView": 1,
                "byteOffset": 0,
                "componentType": 5126,
                "count": num_verts,
                "type": "VEC3",
            },
        ]
        next_bv = 2
        next_acc = 2

        attributes: Dict[str, int] = {"POSITION": 0, "NORMAL": 1}

        if u_bytes is not None:
            buffer_views.append(
                {
                    "buffer": 0,
                    "byteOffset": u_offset,
                    "byteLength": len(u_bytes),
                    "target": 34962,
                }
            )
            accessors.append(
                {
                    "bufferView": next_bv,
                    "byteOffset": 0,
                    "componentType": 5126,
                    "count": num_verts,
                    "type": "VEC2",
                }
            )
            attributes["TEXCOORD_0"] = next_acc
            next_bv += 1
            next_acc += 1

        prim_indices_accessor: List[int] = []
        for pi, (indices_arr, _) in enumerate(out_prims):
            buffer_views.append(
                {
                    "buffer": 0,
                    "byteOffset": i_offsets[pi],
                    "byteLength": len(prim_idx_bytes[pi]),
                    "target": 34963,
                }
            )
            accessors.append(
                {
                    "bufferView": next_bv,
                    "byteOffset": 0,
                    "componentType": 5125,
                    "count": len(indices_arr),
                    "type": "SCALAR",
                }
            )
            prim_indices_accessor.append(next_acc)
            next_bv += 1
            next_acc += 1

        out_primitives: List[dict] = []
        for pi, (indices_arr, src_mat_idx) in enumerate(out_prims):
            p: Dict[str, Any] = {
                "attributes": attributes,
                "indices": prim_indices_accessor[pi],
                "mode": 4,
            }
            if src_mat_idx is not None and src_mat_idx in src_mat_to_new:
                p["material"] = src_mat_to_new[src_mat_idx]
            out_primitives.append(p)

        safe_name = self._sanitize_filename(mesh_name)[:FILENAME_NAME_MAX_LEN] or "mesh"
        short_hash = hashlib.sha1(str(canonical).encode("utf-8")).hexdigest()[
            :FILENAME_HASH_LEN
        ]
        gltf_filename = f"{safe_name}__{short_hash}.gltf"
        bin_filename = f"{safe_name}__{short_hash}.bin"

        gltf_path = os.path.join(self.meshs_dir, gltf_filename)
        bin_path = os.path.join(self.meshs_dir, bin_filename)

        gltf_dict: Dict[str, Any] = {
            "asset": {"version": "2.0", "generator": "glb-splitter"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": out_primitives}],
            "buffers": [{"byteLength": len(bin_buffer), "uri": bin_filename}],
            "bufferViews": buffer_views,
            "accessors": accessors,
        }
        if out_samplers:
            gltf_dict["samplers"] = out_samplers
        if out_images:
            gltf_dict["images"] = out_images
        if out_textures:
            gltf_dict["textures"] = out_textures
        if out_materials:
            gltf_dict["materials"] = out_materials

        with open(bin_path, "wb") as f:
            f.write(bin_buffer)
        with open(gltf_path, "w", encoding="utf-8") as f:
            json.dump(gltf_dict, f, separators=(",", ":"))

        log(f"[mesh] {gltf_filename}")

        rel = f"{MESHS_SUBDIR}/{gltf_filename}"
        self.mesh_cache[canonical] = rel
        return rel

    def _collect_mesh_mat_ids(self, mesh_idx: int) -> List[str]:
        mesh = self.gltf["meshes"][mesh_idx]
        ids: List[str] = []
        for prim in mesh.get("primitives", []):
            mat_idx = prim.get("material")
            if mat_idx is None:
                continue
            mid = self.register_material(int(mat_idx))
            if mid and mid not in ids:
                ids.append(mid)
        return ids

    def build_node(self, node_idx: int) -> Dict[str, Any]:
        node = self.gltf["nodes"][node_idx]
        name = node.get("name") or f"Node_{node_idx}"
        transform = self._get_node_transform(node)

        child_indices = node.get("children", []) or []
        child_nodes = [self.build_node(c) for c in child_indices]

        mesh_idx = node.get("mesh")

        if mesh_idx is not None:
            asset = self.export_mesh(mesh_idx, name)
            mat_ids = self._collect_mesh_mat_ids(mesh_idx)

            out: Dict[str, Any] = {
                "id": str(node_idx),
                "name": name,
                "type": "mesh",
                "transform": transform,
                "children": child_nodes,
            }
            if asset:
                out["asset"] = asset
            if mat_ids:
                out["material"] = mat_ids
            return out

        return {
            "id": str(node_idx),
            "name": name,
            "type": "node",
            "transform": transform,
            "children": child_nodes,
        }


def split_glb(input_path: str, output_json: str, grid_meter: float) -> None:
    log(f"[doc] 正在载入 {os.path.basename(input_path)}")

    output_dir = os.path.dirname(os.path.abspath(output_json))
    meshs_dir = os.path.join(output_dir, MESHS_SUBDIR)
    os.makedirs(meshs_dir, exist_ok=True)

    with GlbContainer(input_path) as container:
        exporter = GlbSplitter(container, meshs_dir, grid_meter)

        scene_idx = int(container.gltf.get("scene", 0))
        scenes = container.gltf.get("scenes", [])
        if not scenes:
            raise RuntimeError("GLB 里没有 scenes")
        scene = scenes[scene_idx]
        root_indices = scene.get("nodes", []) or []

        if not root_indices:
            raise RuntimeError("scene 里没有任何 node")

        if len(root_indices) == 1:
            tree = exporter.build_node(root_indices[0])
        else:
            log(f"[tree] 多根（{len(root_indices)} 个），自动包一层 Assembly_Root")
            children = [exporter.build_node(i) for i in root_indices]
            tree = {
                "id": "Assembly_Root",
                "name": "Assembly_Root",
                "type": "node",
                "transform": {
                    "position": [0.0, 0.0, 0.0],
                    "quaternion": [0.0, 0.0, 0.0, 1.0],
                    "scale": 1.0,
                },
                "children": children,
            }

        tree["materials"] = exporter.material_table

        unique_meshes = len(
            [k for k in exporter.mesh_cache.keys() if isinstance(k, int)]
        )
        log(
            f"[mesh] 唯一 glTF {unique_meshes} 个，"
            f"材质 {len(exporter.material_table)} 个，"
            f"贴图 {len(exporter._exported_image_files)} 张"
        )

        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(tree, f, ensure_ascii=False, indent=2)
        log(f"[output] {output_json}")
        log(f"[output] {meshs_dir}")


def main() -> None:
    if len(sys.argv) < 3:
        print("用法: python glb-splitter.py <input.glb> <output.json>")
        sys.exit(1)

    input_path = sys.argv[1]
    output_json = sys.argv[2]

    log(
        f"[unit] 顶点量化网格: {VERTEX_POS_GRID_METER} 米 "
        f"(模式={VERTEX_MERGE_MODE})"
    )

    try:
        split_glb(input_path, output_json, VERTEX_POS_GRID_METER)
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
