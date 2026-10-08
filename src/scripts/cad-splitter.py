"""
cad-splitter.py — STEP/IGES → glTF + BIN 拆分 + 结构树 JSON 导出
                 手工写 glTF（含材质，外置 .bin）
                 顶点合并：float32 位模式聚类 + 动态法线均值 + 面积加权
                 命名：原始名优先；匿名层用最近有意义祖先 + entity
                 去重：同名节点 entity 短者保留原名，其余加后缀
                 导出：深拷贝 shape 后三角化 + Clean，避免破坏 XCAF 内部共享数据
                 原子导出按 referred_label 去重，同一 label 的多个实例只导一次
                 多格式 reader 分派
                 多体零件自动合并（SOLID/SHELL/COMPOUND 子节点）

用法:
    python cad-splitter.py <input> <output.json> [deflection_mm]

    deflection_mm 省略时按整模型 bbox 对角线动态计算：
        deflection = max(diag * 1e-5, 0.1)  mm
"""

import os
import sys
import json
import re
import math
import struct
import hashlib
import warnings
from array import array
from collections import defaultdict
from typing import Dict, Any, List, Optional, Tuple

warnings.filterwarnings("ignore")

from OCC.Core.STEPCAFControl import STEPCAFControl_Reader
from OCC.Core.TDocStd import TDocStd_Document
from OCC.Core.XCAFDoc import (
    XCAFDoc_DocumentTool,
    XCAFDoc_ColorGen,
    XCAFDoc_ColorSurf,
    XCAFDoc_ColorCurv,
)
from OCC.Core.TDF import TDF_Label, TDF_Tool, TDF_LabelSequence
from OCC.Core.TCollection import TCollection_AsciiString
from OCC.Core.Quantity import Quantity_Color

from OCC.Core.gp import gp_Trsf
from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
from OCC.Core.BRepTools import breptools
from OCC.Core.BRep import BRep_Tool
from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCC.Core.Bnd import Bnd_Box
from OCC.Core.BRepBndLib import brepbndlib
from OCC.Core.TopoDS import topods
from OCC.Core.TopExp import TopExp_Explorer
from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_REVERSED
from OCC.Core.TopLoc import TopLoc_Location

# =====================================================================
# 常量
# =====================================================================

MESHS_SUBDIR = "meshs"

SMOOTH_ANGLE_DEG = 30.0
SMOOTH_ANGLE_COS = math.cos(math.radians(SMOOTH_ANGLE_DEG))

CASCADE_MM_TO_METER = 0.001

AUTO_DEFLECTION_RATIO = 1e-5
MIN_DEFLECTION = 0.1

ASSEMBLY_ROOT_NAME = "Assembly_Root"

_PLACEHOLDER_NAME_PREFIXES = ("=>", "=[")
_BODY_TYPE_NAMES = {"SOLID", "SHELL", "COMPOUND", "COMPSOLID"}


def log(msg: str):
    print(msg, flush=True)


def log_err(msg: str):
    print(msg, file=sys.stderr, flush=True)


# =====================================================================
# 名字工具
# =====================================================================


def _read_label_name(lbl: TDF_Label) -> str:
    try:
        name = lbl.GetLabelName()
        if name is None:
            return ""
        s = str(name).strip()
        if not s:
            return ""
        for prefix in _PLACEHOLDER_NAME_PREFIXES:
            if s.startswith(prefix):
                return ""
        return s
    except Exception:
        return ""


# =====================================================================
# 材质
# =====================================================================


def _material_id(mat: dict) -> str:
    key = json.dumps(mat, sort_keys=True, ensure_ascii=False)
    return "mat_" + hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def read_material_from_label(color_tool, mat_tool, label: TDF_Label) -> Optional[dict]:
    if mat_tool is not None:
        try:
            h = mat_tool.GetShapeMaterial(label)
            if h is not None and not h.IsNull():
                c = h.BaseColor()
                result = {
                    "source": "pbr",
                    "base_color": [
                        round(c.Red(), 4),
                        round(c.Green(), 4),
                        round(c.Blue(), 4),
                        round(c.Alpha(), 4),
                    ],
                    "metallic": 0.0,
                    "roughness": 0.5,
                }
                try:
                    result["metallic"] = round(float(h.Metallic()), 4)
                except Exception:
                    pass
                try:
                    result["roughness"] = round(float(h.Roughness()), 4)
                except Exception:
                    pass
                return result
        except Exception:
            pass

    c = Quantity_Color()
    for ct in (XCAFDoc_ColorGen, XCAFDoc_ColorSurf, XCAFDoc_ColorCurv):
        try:
            if color_tool.GetColor(label, ct, c):
                return {
                    "source": "color",
                    "base_color": [
                        round(c.Red(), 4),
                        round(c.Green(), 4),
                        round(c.Blue(), 4),
                        1.0,
                    ],
                    "metallic": 0.0,
                    "roughness": 0.5,
                }
        except Exception:
            continue
    return None


# =====================================================================
# 主转换器
# =====================================================================


class CadToGlbConverter:
    SUPPORTED_EXT = {".step", ".stp", ".iges", ".igs"}

    def __init__(
        self,
        input_path: str,
        json_path: str,
        deflection: Optional[float] = None,
    ):
        self.input_path = os.path.abspath(input_path)
        self.json_path = os.path.abspath(json_path)
        self.output_dir = os.path.dirname(self.json_path)
        self.meshs_dir = os.path.join(self.output_dir, MESHS_SUBDIR)

        ext = os.path.splitext(self.input_path)[1].lower()
        if ext not in self.SUPPORTED_EXT:
            raise ValueError(f"不支持的文件类型: {ext}")
        self.input_ext = ext

        if array("f").itemsize != 4:
            raise RuntimeError(f"平台 float32 itemsize={array('f').itemsize}，需要 4")
        if array("I").itemsize != 4:
            raise RuntimeError(f"平台 uint32 itemsize={array('I').itemsize}，需要 4")
        if array("H").itemsize != 2:
            raise RuntimeError(f"平台 uint16 itemsize={array('H').itemsize}，需要 2")

        self.unit_scale = CASCADE_MM_TO_METER

        self.deflection_arg = deflection
        self.deflection: float = 0.0

        self.doc = TDocStd_Document("MDTV-XCAF")
        self.shape_tool = None
        self.color_tool = None
        self.mat_tool = None

        self.asset_cache: Dict[str, str] = {}
        self.materials_table: Dict[str, dict] = {}
        self._mat_cache: Dict[str, Optional[dict]] = {}

    # ---------------- 基础工具 ----------------

    @staticmethod
    def _get_label_entry_str(label: TDF_Label) -> str:
        s = TCollection_AsciiString()
        TDF_Tool.Entry(label, s)
        return s.ToCString()

    @staticmethod
    def _sanitize_name(name: str) -> str:
        if not name:
            return ""
        s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
        s = "".join(c for c in s if c.isprintable())
        s = re.sub(r"_+", "_", s)
        return s.strip(" ._")

    def _decompose_trsf(self, trsf: gp_Trsf) -> Tuple[List[float], List[float], float]:
        t = trsf.TranslationPart()
        position = [
            t.X() * self.unit_scale,
            t.Y() * self.unit_scale,
            t.Z() * self.unit_scale,
        ]
        q = trsf.GetRotation()
        quaternion = [q.X(), q.Y(), q.Z(), q.W()]
        scale = trsf.ScaleFactor()
        return position, quaternion, scale

    def _read_material(
        self, label: TDF_Label, fallback_label: Optional[TDF_Label] = None
    ) -> Optional[dict]:
        key = self._get_label_entry_str(label)
        if key in self._mat_cache:
            return self._mat_cache[key]

        result = read_material_from_label(self.color_tool, self.mat_tool, label)

        if result is None and fallback_label is not None and fallback_label != label:
            fb_key = self._get_label_entry_str(fallback_label)
            if fb_key in self._mat_cache:
                result = self._mat_cache[fb_key]
            else:
                result = read_material_from_label(
                    self.color_tool, self.mat_tool, fallback_label
                )
                self._mat_cache[fb_key] = result

        self._mat_cache[key] = result
        return result

    # ---------------- 名字 / 结构判据 ----------------

    @staticmethod
    def _is_identity_tfm(tfm: Optional[dict]) -> bool:
        if not tfm:
            return True
        pos = tfm.get("position", [0, 0, 0])
        quat = tfm.get("quaternion", [0, 0, 0, 1])
        scl = tfm.get("scale", 1.0)
        if any(abs(v) > 1e-9 for v in pos):
            return False
        if abs(quat[3] - 1.0) > 1e-9 or any(abs(v) > 1e-9 for v in quat[:3]):
            return False
        if abs(scl - 1.0) > 1e-9:
            return False
        return True

    @staticmethod
    def _is_placeholder_name(name: str) -> bool:
        n = (name or "").strip()
        if not n:
            return True
        return n.upper() in _BODY_TYPE_NAMES

    def _read_real_name(
        self, label: TDF_Label, fallback_label: Optional[TDF_Label] = None
    ) -> str:
        name = _read_label_name(label)
        if name and not self._is_placeholder_name(name):
            return name
        if fallback_label is not None and fallback_label != label:
            name = _read_label_name(fallback_label)
            if name and not self._is_placeholder_name(name):
                return name
        return ""

    def _is_body_container(self, children: List[dict]) -> bool:
        if not children:
            return False
        for c in children:
            if c.get("type") != "mesh":
                return False
            if c.get("_subtree_has_raw_name", False):
                return False
            if not self._is_identity_tfm(c.get("transform")):
                return False
        return True

    # ---------------- 三角化 + 法线 ----------------

    def _triangulate_with_surface_normals(self, shape):
        mesh = BRepMesh_IncrementalMesh(shape, self.deflection, False, 0.5, True)
        mesh.Perform()

        faces_payload = []
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        face_id = 0
        while explorer.More():
            face = topods.Face(explorer.Current())
            face_id += 1
            is_reversed = face.Orientation() == TopAbs_REVERSED

            loc = TopLoc_Location()
            triangulation = BRep_Tool.Triangulation(face, loc)
            if triangulation is None:
                explorer.Next()
                continue

            trsf = loc.Transformation()
            n_nodes = triangulation.NbNodes()

            face_verts = []
            for i in range(1, n_nodes + 1):
                p = triangulation.Node(i)
                if not loc.IsIdentity():
                    p.Transform(trsf)
                face_verts.append(
                    (
                        p.X() * self.unit_scale,
                        p.Y() * self.unit_scale,
                        p.Z() * self.unit_scale,
                    )
                )

            # 曲面解析法线（用三角化自带的 UV 参数域，仅用于算法线，不输出 UV）
            face_normals = [None] * n_nodes
            has_uv_nodes = False
            try:
                has_uv_nodes = triangulation.HasUVNodes()
            except Exception:
                has_uv_nodes = False

            if has_uv_nodes:
                surface = None
                try:
                    surface = BRep_Tool.Surface(face)
                except Exception:
                    surface = None

                if surface is not None:
                    for i in range(1, n_nodes + 1):
                        try:
                            uv = triangulation.UVNode(i)
                            d = surface.Normal(uv.X(), uv.Y())
                            nx, ny, nz = d.X(), d.Y(), d.Z()
                            length = math.sqrt(nx * nx + ny * ny + nz * nz)
                            if length > 1e-12:
                                n = (nx / length, ny / length, nz / length)
                                if is_reversed:
                                    n = (-n[0], -n[1], -n[2])
                                face_normals[i - 1] = n
                        except Exception:
                            pass

            # 三角形（过滤退化）
            triangles = []
            for ti in range(1, triangulation.NbTriangles() + 1):
                t = triangulation.Triangle(ti)
                a, b, c = t.Get()
                a, b, c = a - 1, b - 1, c - 1
                if a == b or b == c or a == c:
                    continue
                v0 = face_verts[a]
                v1 = face_verts[b]
                v2 = face_verts[c]
                e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
                e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
                cx = e1[1] * e2[2] - e1[2] * e2[1]
                cy = e1[2] * e2[0] - e1[0] * e2[2]
                cz = e1[0] * e2[1] - e1[1] * e2[0]
                cl = math.sqrt(cx * cx + cy * cy + cz * cz)
                if cl < 1e-20:
                    continue
                triangles.append((a, b, c))

            # 缺失法线补全：面积加权平均
            if any(n is None for n in face_normals):
                node_acc = [None] * n_nodes
                for a, b, c in triangles:
                    v0 = face_verts[a]
                    v1 = face_verts[b]
                    v2 = face_verts[c]
                    e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
                    e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
                    cx = e1[1] * e2[2] - e1[2] * e2[1]
                    cy = e1[2] * e2[0] - e1[0] * e2[2]
                    cz = e1[0] * e2[1] - e1[1] * e2[0]
                    cl = math.sqrt(cx * cx + cy * cy + cz * cz)
                    if cl < 1e-20:
                        continue
                    area = 0.5 * cl
                    nx, ny, nz = cx / cl, cy / cl, cz / cl
                    if is_reversed:
                        nx, ny, nz = -nx, -ny, -nz
                    for vi in (a, b, c):
                        if node_acc[vi] is None:
                            node_acc[vi] = [0.0, 0.0, 0.0]
                        node_acc[vi][0] += nx * area
                        node_acc[vi][1] += ny * area
                        node_acc[vi][2] += nz * area
                for i in range(n_nodes):
                    if face_normals[i] is not None:
                        continue
                    acc = node_acc[i]
                    if acc is None:
                        face_normals[i] = (0.0, 0.0, 1.0)
                        continue
                    ln = math.sqrt(acc[0] ** 2 + acc[1] ** 2 + acc[2] ** 2)
                    if ln < 1e-12:
                        face_normals[i] = (0.0, 0.0, 1.0)
                    else:
                        face_normals[i] = (acc[0] / ln, acc[1] / ln, acc[2] / ln)

            # 顶点面积（法线加权用）
            node_area = [0.0] * n_nodes
            for a, b, c in triangles:
                v0 = face_verts[a]
                v1 = face_verts[b]
                v2 = face_verts[c]
                e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
                e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
                cx = e1[1] * e2[2] - e1[2] * e2[1]
                cy = e1[2] * e2[0] - e1[0] * e2[2]
                cz = e1[0] * e2[1] - e1[1] * e2[0]
                cl = math.sqrt(cx * cx + cy * cy + cz * cz)
                if cl < 1e-20:
                    continue
                area = 0.5 * cl
                node_area[a] += area
                node_area[b] += area
                node_area[c] += area

            faces_payload.append(
                {
                    "face_id": face_id,
                    "is_reversed": is_reversed,
                    "verts": face_verts,
                    "normals": face_normals,
                    "triangles": triangles,
                    "node_area": node_area,
                }
            )
            explorer.Next()

        if not faces_payload:
            return None

        def _make_key(p):
            x, y, z = p
            if x == 0.0:
                x = 0.0
            if y == 0.0:
                y = 0.0
            if z == 0.0:
                z = 0.0
            return struct.pack("<fff", x, y, z)

        buckets: Dict[bytes, List[dict]] = {}
        node_to_cluster: Dict[Tuple[int, int], dict] = {}
        all_clusters: List[dict] = []

        for fp in faces_payload:
            fid = fp["face_id"]
            verts = fp["verts"]
            normals = fp["normals"]
            areas = fp["node_area"]

            for ni in range(len(verts)):
                p = verts[ni]
                n = normals[ni] if normals[ni] is not None else (0.0, 0.0, 1.0)
                w = areas[ni] if areas[ni] > 0.0 else 1e-12

                key = _make_key(p)
                blist = buckets.setdefault(key, [])

                matched = None
                for cl in blist:
                    acc = cl["acc_normal"]
                    ln = math.sqrt(acc[0] ** 2 + acc[1] ** 2 + acc[2] ** 2)
                    if ln < 1e-12:
                        rn = (0.0, 0.0, 1.0)
                    else:
                        rn = (acc[0] / ln, acc[1] / ln, acc[2] / ln)
                    if n[0] * rn[0] + n[1] * rn[1] + n[2] * rn[2] >= SMOOTH_ANGLE_COS:
                        matched = cl
                        break

                if matched is None:
                    cl = {
                        "pos": p,
                        "acc_normal": [n[0] * w, n[1] * w, n[2] * w],
                        "acc_weight": w,
                        "vertex_idx": -1,
                    }
                    blist.append(cl)
                    all_clusters.append(cl)
                    matched = cl
                else:
                    acc = matched["acc_normal"]
                    acc[0] += n[0] * w
                    acc[1] += n[1] * w
                    acc[2] += n[2] * w
                    matched["acc_weight"] += w

                node_to_cluster[(fid, ni)] = matched

        vertices: List[float] = []
        out_normals: List[float] = []

        for cl in all_clusters:
            acc = cl["acc_normal"]
            ln = math.sqrt(acc[0] ** 2 + acc[1] ** 2 + acc[2] ** 2)
            if ln < 1e-12:
                n = (0.0, 0.0, 1.0)
            else:
                n = (acc[0] / ln, acc[1] / ln, acc[2] / ln)

            cl["vertex_idx"] = len(vertices) // 3
            vertices.extend(cl["pos"])
            out_normals.extend(n)

        indices: List[int] = []
        for fp in faces_payload:
            fid = fp["face_id"]
            is_rev = fp["is_reversed"]
            for a, b, c in fp["triangles"]:
                if is_rev:
                    b, c = c, b
                i0 = node_to_cluster[(fid, a)]["vertex_idx"]
                i1 = node_to_cluster[(fid, b)]["vertex_idx"]
                i2 = node_to_cluster[(fid, c)]["vertex_idx"]
                indices.extend([i0, i1, i2])

        if not vertices or not indices:
            return None

        return vertices, out_normals, indices

    # ---------------- 材质 JSON ----------------

    @staticmethod
    def _build_material_json(mat_dict: dict, idx: int) -> dict:
        bc = mat_dict["base_color"]
        r, g, b = float(bc[0]), float(bc[1]), float(bc[2])
        a = float(bc[3]) if len(bc) > 3 else 1.0
        mt = float(mat_dict.get("metallic", 0.0))
        rg = float(mat_dict.get("roughness", 0.5))

        m = {
            "name": f"Material_{idx}",
            "pbrMetallicRoughness": {
                "baseColorFactor": [r, g, b, a],
                "metallicFactor": mt,
                "roughnessFactor": rg,
            },
            "doubleSided": False,
        }
        if a < 1.0:
            m["alphaMode"] = "BLEND"
        return m

    # ---------------- 多 part glTF 导出（深拷贝 + 流式写盘） ----------------

    def _export_gltf(
        self,
        parts: List[Tuple[TDF_Label, Optional[dict]]],
        output_gltf_path: str,
        output_name: str,
    ):
        buffer_views: List[dict] = []
        accessors: List[dict] = []
        primitives: List[dict] = []
        materials: List[dict] = []
        mat_index_map: Dict[str, int] = {}
        offset = 0
        little_endian = sys.byteorder == "little"

        gltf_basename = os.path.basename(output_gltf_path)
        if gltf_basename.lower().endswith(".gltf"):
            bin_basename = gltf_basename[:-5] + ".bin"
        else:
            bin_basename = gltf_basename + ".bin"
        output_bin_path = os.path.join(os.path.dirname(output_gltf_path), bin_basename)

        with open(output_bin_path, "wb") as fbin:
            for label, mat_dict in parts:
                label_entry = self._get_label_entry_str(label)

                src = self.shape_tool.GetShape(label)
                if src.IsNull():
                    raise RuntimeError(f"shape 为空: {label_entry}")

                copier = BRepBuilderAPI_Copy(src)
                local_shape = copier.Shape()

                try:
                    result = self._triangulate_with_surface_normals(local_shape)
                    if result is None:
                        raise RuntimeError(f"三角化失败或无有效几何: {label_entry}")
                    vertices, normals, indices = result

                    num_vertices = len(vertices) // 3
                    if num_vertices == 0 or not indices:
                        raise RuntimeError(
                            f"零件无有效几何: {label_entry}, "
                            f"vertices={num_vertices}, indices={len(indices)}"
                        )

                    # ---- 顶点 ----
                    if not all(map(math.isfinite, vertices)):
                        for i, v in enumerate(vertices):
                            if not math.isfinite(v):
                                raise RuntimeError(
                                    f"顶点含非法数值 ({label_entry}): "
                                    f"index={i}, value={v}"
                                )

                    xs = vertices[0::3]
                    ys = vertices[1::3]
                    zs = vertices[2::3]
                    min_pos = [min(xs), min(ys), min(zs)]
                    max_pos = [max(xs), max(ys), max(zs)]

                    v_arr = array("f", vertices)
                    if not little_endian:
                        v_arr.byteswap()
                    v_bytes = v_arr.tobytes()

                    fbin.write(v_bytes)
                    buffer_views.append(
                        {
                            "buffer": 0,
                            "byteOffset": offset,
                            "byteLength": len(v_bytes),
                            "target": 34962,
                        }
                    )
                    v_acc = len(accessors)
                    accessors.append(
                        {
                            "bufferView": len(buffer_views) - 1,
                            "byteOffset": 0,
                            "componentType": 5126,
                            "count": num_vertices,
                            "type": "VEC3",
                            "max": max_pos,
                            "min": min_pos,
                        }
                    )
                    offset += len(v_bytes)

                    # ---- 法线 ----
                    if not all(map(math.isfinite, normals)):
                        for i, v in enumerate(normals):
                            if not math.isfinite(v):
                                raise RuntimeError(
                                    f"法线含非法数值 ({label_entry}): "
                                    f"index={i}, value={v}"
                                )

                    n_arr = array("f", normals)
                    if not little_endian:
                        n_arr.byteswap()
                    n_bytes = n_arr.tobytes()

                    fbin.write(n_bytes)
                    buffer_views.append(
                        {
                            "buffer": 0,
                            "byteOffset": offset,
                            "byteLength": len(n_bytes),
                            "target": 34962,
                        }
                    )
                    n_acc = len(accessors)
                    accessors.append(
                        {
                            "bufferView": len(buffer_views) - 1,
                            "byteOffset": 0,
                            "componentType": 5126,
                            "count": num_vertices,
                            "type": "VEC3",
                        }
                    )
                    offset += len(n_bytes)

                    attributes = {"POSITION": v_acc, "NORMAL": n_acc}

                    # ---- 索引 ----
                    use_uint32 = num_vertices > 65535
                    i_min = min(indices)
                    i_max = max(indices)
                    if i_min < 0 or i_max > num_vertices - 1:
                        raise RuntimeError(
                            f"索引越界 ({label_entry}): "
                            f"[{i_min}, {i_max}], 顶点数={num_vertices}"
                        )

                    i_count = len(indices)
                    idx_type = 5125 if use_uint32 else 5123
                    i_arr = array("I" if use_uint32 else "H", indices)
                    if not little_endian:
                        i_arr.byteswap()
                    i_bytes = i_arr.tobytes()

                    fbin.write(i_bytes)
                    padding = (4 - len(i_bytes) % 4) % 4
                    if padding:
                        fbin.write(b"\x00" * padding)

                    buffer_views.append(
                        {
                            "buffer": 0,
                            "byteOffset": offset,
                            "byteLength": len(i_bytes) + padding,
                            "target": 34963,
                        }
                    )
                    i_acc = len(accessors)
                    accessors.append(
                        {
                            "bufferView": len(buffer_views) - 1,
                            "byteOffset": 0,
                            "componentType": idx_type,
                            "count": i_count,
                            "type": "SCALAR",
                        }
                    )
                    offset += len(i_bytes) + padding

                    # ---- primitive + 材质 ----
                    prim = {"attributes": attributes, "indices": i_acc}
                    if mat_dict and mat_dict.get("base_color"):
                        key = json.dumps(mat_dict, sort_keys=True, ensure_ascii=False)
                        if key not in mat_index_map:
                            mat_index_map[key] = len(materials)
                            materials.append(
                                self._build_material_json(mat_dict, mat_index_map[key])
                            )
                        prim["material"] = mat_index_map[key]

                    primitives.append(prim)

                finally:
                    try:
                        breptools.Clean(local_shape)
                    except Exception:
                        pass

        if not primitives:
            part_entries = [self._get_label_entry_str(lbl) for lbl, _ in parts]
            raise RuntimeError(f"所有 part 均无有效 primitive: {part_entries}")

        gltf_dict = {
            "asset": {"version": "2.0", "generator": "cad-splitter glTF Exporter"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0, "name": output_name}],
            "meshes": [{"name": output_name, "primitives": primitives}],
            "buffers": [{"byteLength": offset, "uri": bin_basename}],
            "bufferViews": buffer_views,
            "accessors": accessors,
        }
        if materials:
            gltf_dict["materials"] = materials

        with open(output_gltf_path, "w", encoding="utf-8") as fj:
            json.dump(gltf_dict, fj, separators=(",", ":"))

    # ---------------- 单零件 / 多 body 导出 ----------------

    def _export_atomic_gltf_with_cache(
        self,
        label: TDF_Label,
        mat_ids: Optional[List[str]] = None,
    ) -> str:
        label_entry = self._get_label_entry_str(label)
        if label_entry in self.asset_cache:
            return self.asset_cache[label_entry]

        mat_dict = None
        if mat_ids:
            mat_dict = self.materials_table.get(mat_ids[0])

        safe_filename = label_entry.replace(":", "_") + ".gltf"
        gltf_path = os.path.join(self.meshs_dir, safe_filename)

        self._export_gltf([(label, mat_dict)], gltf_path, output_name=label_entry)

        log(f"[mesh] {safe_filename}")
        rel = f"{MESHS_SUBDIR}/{safe_filename}"
        self.asset_cache[label_entry] = rel
        return rel

    def _export_merged_gltf(
        self,
        bodies: List[Tuple[TDF_Label, Optional[dict]]],
        cache_key: str,
    ) -> str:
        full_key = f"M|{cache_key}"
        if full_key in self.asset_cache:
            return self.asset_cache[full_key]

        safe_filename = cache_key.replace(":", "_") + ".gltf"
        gltf_path = os.path.join(self.meshs_dir, safe_filename)

        self._export_gltf(bodies, gltf_path, output_name=cache_key)

        log(f"[mesh] {safe_filename}")
        rel = f"{MESHS_SUBDIR}/{safe_filename}"
        self.asset_cache[full_key] = rel
        return rel

    # ---------------- 材质表 ----------------

    def _register_material(self, mat_dict: dict) -> str:
        mid = _material_id(mat_dict)
        if mid not in self.materials_table:
            self.materials_table[mid] = mat_dict
        return mid

    # ---------------- 结构树 ----------------

    def _extract_tree_node(
        self,
        label: TDF_Label,
        instance_label: Optional[TDF_Label] = None,
        index: int = 0,
    ) -> Optional[Dict[str, Any]]:
        target_label = instance_label if instance_label else label
        entity = self._get_label_entry_str(target_label)

        real_name = self._read_real_name(target_label, label)
        has_raw = bool(real_name)

        is_asm = self.shape_tool.IsAssembly(label)

        position = [0.0, 0.0, 0.0]
        quaternion = [0.0, 0.0, 0.0, 1.0]
        scale = 1.0
        if instance_label:
            try:
                loc = self.shape_tool.GetLocation(instance_label)
                if not loc.IsIdentity():
                    position, quaternion, scale = self._decompose_trsf(
                        loc.Transformation()
                    )
            except Exception as e:
                log_err(f"[warn] 读取实例变换失败 ({entity}): {e}")

        transform_dict = {
            "position": position,
            "quaternion": quaternion,
            "scale": scale,
        }

        if is_asm:
            components = TDF_LabelSequence()
            self.shape_tool.GetComponents(label, components)

            children = []
            for i in range(1, components.Length() + 1):
                comp_label = components.Value(i)
                referred_label = TDF_Label()
                if self.shape_tool.GetReferredShape(comp_label, referred_label):
                    child = self._extract_tree_node(
                        referred_label,
                        instance_label=comp_label,
                        index=i,
                    )
                    if child:
                        children.append(child)

            subtree_has_raw = has_raw or any(
                c.get("_subtree_has_raw_name", False) for c in children
            )

            # 决策 1：子树里有原始有意义名 → 保留层级
            if subtree_has_raw:
                return {
                    "type": "node",
                    "transform": transform_dict,
                    "children": children,
                    "_raw_name": real_name,
                    "_entity": entity,
                    "_subtree_has_raw_name": True,
                }

            # 决策 2：单子件 + 单位变换 → 塌缩
            if len(children) == 1 and self._is_identity_tfm(transform_dict):
                return children[0]

            # 决策 3：全是无名 mesh → 合并为多 part
            if self._is_body_container(children):
                bodies: List[Tuple[TDF_Label, Optional[dict]]] = []
                for c in children:
                    if "_merged_bodies" in c:
                        bodies.extend(c["_merged_bodies"])
                    elif "_label_ref" in c:
                        lbl = c["_label_ref"]
                        mat_ids = c.get("_material_ids", [])
                        mat_dict = (
                            self.materials_table.get(mat_ids[0]) if mat_ids else None
                        )
                        bodies.append((lbl, mat_dict))

                if bodies:
                    for c in children:
                        c.pop("_label_ref", None)
                        c.pop("_instance_label", None)
                        c.pop("_merged_bodies", None)

                    return {
                        "type": "mesh",
                        "transform": transform_dict,
                        "_raw_name": "",
                        "_entity": entity,
                        "_subtree_has_raw_name": False,
                        "_merged_bodies": bodies,
                    }

            # 决策 4：全无名但不能合并 → 保留匿名层级
            return {
                "type": "node",
                "transform": transform_dict,
                "children": children,
                "_raw_name": "",
                "_entity": entity,
                "_subtree_has_raw_name": False,
            }

        # mesh 叶子
        mat_src = instance_label if instance_label else label
        mat_dict = self._read_material(
            mat_src, fallback_label=label if instance_label else None
        )

        mat_ids: List[str] = []
        if mat_dict:
            mat_ids.append(self._register_material(mat_dict))

        node: Dict[str, Any] = {
            "type": "mesh",
            "transform": transform_dict,
            "_raw_name": real_name,
            "_entity": entity,
            "_subtree_has_raw_name": has_raw,
            "_label_ref": label,
        }
        if mat_ids:
            node["_material_ids"] = mat_ids
        return node

    def _generate_gltfs_recursive(self, node):
        if node["type"] == "mesh":
            if "_merged_bodies" in node:
                bodies = node.pop("_merged_bodies")
                node["asset"] = self._export_merged_gltf(
                    bodies, cache_key=node["_entity"]
                )
            elif "_label_ref" in node:
                label = node.pop("_label_ref")
                node.pop("_instance_label", None)
                mat_ids = node.get("_material_ids", [])
                node["asset"] = self._export_atomic_gltf_with_cache(
                    label,
                    mat_ids=mat_ids,
                )
        else:
            node.pop("_instance_label", None)

        for child in node.get("children", []):
            self._generate_gltfs_recursive(child)

    # ---------------- 命名阶段 ----------------

    @staticmethod
    def _ensure_root_name(root):
        if not root.get("_raw_name"):
            root["_raw_name"] = ASSEMBLY_ROOT_NAME
        if not root.get("_entity"):
            root["_entity"] = "root"

    def _assign_draft_names(self, n, parent_meaningful: str):
        raw = n.get("_raw_name", "")
        entity = n.get("_entity", "") or "unknown"
        entity_safe = entity.replace(":", "_")

        if raw:
            draft = self._sanitize_name(raw)
            if draft:
                next_meaningful = draft
            else:
                draft = f"{parent_meaningful}_{entity_safe}"
                next_meaningful = parent_meaningful
        else:
            draft = f"{parent_meaningful}_{entity_safe}"
            next_meaningful = parent_meaningful

        n["_final_name"] = draft

        for c in n.get("children", []):
            self._assign_draft_names(c, next_meaningful)

    @staticmethod
    def _collect_all_nodes(root) -> List[dict]:
        all_nodes: List[dict] = []

        def walk(n):
            all_nodes.append(n)
            for c in n.get("children", []):
                walk(c)

        walk(root)
        return all_nodes

    @staticmethod
    def _dedupe_names(all_nodes: List[dict]):
        grouped: Dict[str, List[dict]] = defaultdict(list)
        for n in all_nodes:
            grouped[n["_final_name"]].append(n)

        for name, nodes in grouped.items():
            if len(nodes) <= 1:
                continue
            nodes.sort(key=lambda x: (len(x.get("_entity", "")), x.get("_entity", "")))
            for n in nodes[1:]:
                entity_safe = (n.get("_entity", "") or "unknown").replace(":", "_")
                n["_final_name"] = f"{name}_{entity_safe}"

    def _build_asset_groups(self, all_nodes: List[dict]):
        asset_to_a: Dict[str, List[str]] = defaultdict(list)
        asset_to_b: Dict[str, List[str]] = defaultdict(list)
        for n in all_nodes:
            asset = n.get("asset", "")
            if not asset:
                continue
            raw = n.get("_raw_name", "")
            final = n.get("_final_name", "")
            if raw and self._sanitize_name(raw) == final:
                asset_to_a[asset].append(final)
            else:
                asset_to_b[asset].append(final)
        return dict(asset_to_a), dict(asset_to_b)

    @staticmethod
    def _cleanup_node(n):
        name = n.get("_final_name", "")
        node_type = n.get("type", "node")
        tfm = n.get("transform") or {
            "position": [0.0, 0.0, 0.0],
            "quaternion": [0.0, 0.0, 0.0, 1.0],
            "scale": 1.0,
        }
        mat_ids = n.get("_material_ids", [])
        ast = n.get("asset", "")
        children = n.get("children", [])

        n.clear()
        n["id"] = name
        n["name"] = name
        n["type"] = node_type
        if mat_ids:
            n["material"] = mat_ids
        n["transform"] = tfm
        if ast:
            n["asset"] = ast
        if children:
            n["children"] = children

    def _finalize_tree(self, root):
        self._ensure_root_name(root)
        self._assign_draft_names(root, ASSEMBLY_ROOT_NAME)

        all_nodes = self._collect_all_nodes(root)
        self._dedupe_names(all_nodes)

        asset_to_a, asset_to_b = self._build_asset_groups(all_nodes)

        for n in all_nodes:
            self._cleanup_node(n)

        root["materials"] = self.materials_table

        return all_nodes, asset_to_a, asset_to_b

    # ---------------- 改名阶段 ----------------

    def _rewrite_gltf_internal(
        self, gltf_path: str, mesh_name: str, bin_basename: Optional[str] = None
    ):
        with open(gltf_path, "r", encoding="utf-8") as f:
            g = json.load(f)

        if g.get("nodes"):
            g["nodes"][0]["name"] = mesh_name
        if g.get("meshes"):
            g["meshes"][0]["name"] = mesh_name
        if bin_basename is not None and g.get("buffers"):
            g["buffers"][0]["uri"] = bin_basename

        with open(gltf_path, "w", encoding="utf-8") as f:
            json.dump(g, f, separators=(",", ":"))

    def _rename_assets(self, all_nodes, asset_to_a, asset_to_b):
        all_assets = set(asset_to_a.keys()) | set(asset_to_b.keys())
        asset_to_rep: Dict[str, str] = {}
        used_filenames = set()

        for asset in sorted(all_assets):
            a = asset_to_a.get(asset, [])
            b = asset_to_b.get(asset, [])
            candidates = a if a else b
            if not candidates:
                continue
            rep = min(candidates, key=lambda s: (len(s), s))
            if not rep:
                raise RuntimeError(f"代表名为空: asset={asset}")

            candidate_file = f"{rep}.gltf"
            if candidate_file in used_filenames:
                raise RuntimeError(f"文件名冲突: {candidate_file}")
            used_filenames.add(candidate_file)
            asset_to_rep[asset] = rep

        asset_to_new: Dict[str, str] = {}
        for old_asset, rep in asset_to_rep.items():
            old_path = os.path.join(self.output_dir, old_asset)
            new_filename = f"{rep}.gltf"
            new_path = os.path.join(self.meshs_dir, new_filename)
            new_asset = f"{MESHS_SUBDIR}/{new_filename}"

            old_stem, _ = os.path.splitext(old_path)
            new_stem, _ = os.path.splitext(new_path)
            old_bin = old_stem + ".bin"
            new_bin = new_stem + ".bin"

            if old_path == new_path:
                self._rewrite_gltf_internal(
                    new_path,
                    mesh_name=rep,
                    bin_basename=os.path.basename(new_bin),
                )
                asset_to_new[old_asset] = new_asset
                continue

            os.rename(old_path, new_path)
            os.rename(old_bin, new_bin)

            self._rewrite_gltf_internal(
                new_path,
                mesh_name=rep,
                bin_basename=os.path.basename(new_bin),
            )

            asset_to_new[old_asset] = new_asset
            log(f"[rename] {old_asset} -> {new_asset}")

        for n in all_nodes:
            old = n.get("asset", "")
            if old in asset_to_new:
                n["asset"] = asset_to_new[old]

    # ---------------- reader 分派 ----------------

    def _create_reader(self):
        ext = self.input_ext
        if ext in (".step", ".stp"):
            r = STEPCAFControl_Reader()
            r.SetColorMode(True)
            r.SetNameMode(True)
            r.SetLayerMode(True)
            r.SetMatMode(True)
            return r
        if ext in (".iges", ".igs"):
            try:
                from OCC.Core.IGESCAFControl import IGESCAFControl_Reader
            except ImportError as e:
                raise RuntimeError(f"当前 OCC 环境不支持 IGES: {e}")
            r = IGESCAFControl_Reader()
            r.SetColorMode(True)
            r.SetNameMode(True)
            r.SetLayerMode(True)
            return r
        raise ValueError(f"不支持的文件类型: {ext}")

    # ---------------- 主流程 ----------------

    def _load(self):
        log(f"[doc] 正在载入 {os.path.basename(self.input_path)}")
        reader = self._create_reader()
        status = reader.ReadFile(self.input_path)
        if status != 1:
            raise RuntimeError(f"文件解析失败: status={status}")
        if not reader.Transfer(self.doc):
            raise RuntimeError("数据转移到 XCAF 失败")

        self.shape_tool = XCAFDoc_DocumentTool.ShapeTool(self.doc.Main())
        self.color_tool = XCAFDoc_DocumentTool.ColorTool(self.doc.Main())
        try:
            self.mat_tool = XCAFDoc_DocumentTool.VisMaterialTool(self.doc.Main())
        except Exception:
            self.mat_tool = None
        log("[doc] 载入完成")

    def _compute_auto_deflection(self) -> float:
        free_shapes = TDF_LabelSequence()
        self.shape_tool.GetFreeShapes(free_shapes)

        bbox = Bnd_Box()
        for i in range(1, free_shapes.Length() + 1):
            shape = self.shape_tool.GetShape(free_shapes.Value(i))
            if shape.IsNull():
                continue
            try:
                brepbndlib.Add(shape, bbox)
            except Exception:
                continue

        if bbox.IsVoid():
            return MIN_DEFLECTION

        xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()
        diag = math.sqrt((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2)
        return max(diag * AUTO_DEFLECTION_RATIO, MIN_DEFLECTION)

    def _build_root_tree(self) -> Dict[str, Any]:
        free_shapes = TDF_LabelSequence()
        self.shape_tool.GetFreeShapes(free_shapes)
        if free_shapes.Length() == 0:
            raise RuntimeError("未找到有效的根拓扑模型")

        if free_shapes.Length() == 1:
            root = self._extract_tree_node(free_shapes.Value(1))
            if root is None:
                raise RuntimeError("根节点为空")
            return root

        children = []
        for i in range(1, free_shapes.Length() + 1):
            n = self._extract_tree_node(free_shapes.Value(i), index=i)
            if n:
                children.append(n)

        return {
            "type": "node",
            "transform": {
                "position": [0.0, 0.0, 0.0],
                "quaternion": [0.0, 0.0, 0.0, 1.0],
                "scale": 1.0,
            },
            "children": children,
            "_raw_name": ASSEMBLY_ROOT_NAME,
            "_entity": "root",
            "_subtree_has_raw_name": True,
        }

    def run(self):
        self._load()

        if self.deflection_arg is None:
            self.deflection = self._compute_auto_deflection()
            log(f"[deflection] 自动: {self.deflection:.6f} mm")
        else:
            self.deflection = self.deflection_arg
            log(f"[deflection] 指定: {self.deflection} mm")

        root = self._build_root_tree()

        os.makedirs(self.meshs_dir, exist_ok=True)
        log("[mesh] 开始导出 glTF ...")
        self._generate_gltfs_recursive(root)
        log(f"[mesh] 完成，共 {len(self.asset_cache)} 个 glTF")

        all_nodes, asset_to_a, asset_to_b = self._finalize_tree(root)

        log("[rename] 重命名 glTF ...")
        self._rename_assets(all_nodes, asset_to_a, asset_to_b)

        os.makedirs(self.output_dir, exist_ok=True)
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(root, f, ensure_ascii=False, indent=2)
        log(f"[output] {self.json_path}")
        log(f"[output] {self.meshs_dir}")


# =====================================================================
# 入口
# =====================================================================


def main():
    if len(sys.argv) < 3:
        print("用法: python cad-splitter.py <input> <out.json> [deflection_mm]")
        sys.exit(1)

    input_path = sys.argv[1]
    json_output = sys.argv[2]

    deflection_val: Optional[float] = None
    if len(sys.argv) >= 4:
        try:
            deflection_val = float(sys.argv[3])
        except ValueError:
            log(f"[warn] deflection '{sys.argv[3]}' 无效，改用自动计算")
            deflection_val = None

    try:
        CadToGlbConverter(input_path, json_output, deflection=deflection_val).run()
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
