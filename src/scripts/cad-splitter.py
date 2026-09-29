"""
cad-splitter.py — STEP/IGES → glTF + BIN 拆分 + 结构树 JSON 导出
                 手工三角化 + 手工 glTF 写入（含材质，外置 .bin）
                 顶点合并：位置量化 + 法线角度聚类 + 面积加权平均
                 多格式 reader 分派
                 多体零件自动合并（SOLID/SHELL/COMPOUND 子节点）

用法:
    python cad-splitter.py <input> <output.json> [deflection_mm]

输出:
    <output.json>              结构树（文件名由命令行指定）
    meshs/xxx__hash.gltf       每个零件的 glTF
    meshs/xxx__hash.bin        同名外置 buffer
"""

import os
import sys
import json
import re
import math
import struct
import hashlib
import warnings
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
from OCC.Core.Quantity import Quantity_Color, Quantity_TOC_RGB

from OCC.Core.gp import gp_Trsf
from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
from OCC.Core.BRepTools import breptools
from OCC.Core.BRep import BRep_Builder, BRep_Tool
from OCC.Core.TopoDS import topods, TopoDS_Compound
from OCC.Core.TopExp import TopExp_Explorer
from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_REVERSED
from OCC.Core.TopLoc import TopLoc_Location

# =====================================================================
# 常量
# =====================================================================

DEFAULT_DEFLECTION = 0.1
DEFAULT_NODE_NAME = "Node"

MESHS_SUBDIR = "meshs"

FILENAME_NAME_MAX_LEN = 80
FILENAME_HASH_LEN = 8

FALLBACK_UNIT_TO_METER = 0.001

SMOOTH_ANGLE_DEG = 30.0
SMOOTH_ANGLE_COS = math.cos(math.radians(SMOOTH_ANGLE_DEG))

VERTEX_POS_GRID = 1e-5

_PLACEHOLDER_NAME_PREFIXES = ("=>", "=[")

_BODY_TYPE_NAMES = {"SOLID", "SHELL", "COMPOUND", "COMPSOLID"}

_SI_PREFIX_TO_METER = {
    "": 1.0,
    "$": 1.0,
    "MILLI": 0.001,
    "CENTI": 0.01,
    "DECI": 0.1,
    "DEKA": 10.0,
    "HECTO": 100.0,
    "KILO": 1000.0,
    "MICRO": 1e-6,
    "NANO": 1e-9,
}

_NAMED_UNIT_TO_METER = {
    "INCH": 0.0254,
    "FOOT": 0.3048,
    "YARD": 0.9144,
    "MILE": 1609.344,
    "MIL": 2.54e-5,
    "THOU": 2.54e-5,
}


def log(msg: str):
    print(msg, flush=True)


def log_err(msg: str):
    print(msg, file=sys.stderr, flush=True)


# =====================================================================
# STEP 单位探测
# =====================================================================


def detect_step_length_unit(step_path: str) -> Optional[float]:
    CHUNK = 1024 * 1024
    try:
        file_size = os.path.getsize(step_path)
        with open(step_path, "rb") as f:
            head = f.read(min(CHUNK, file_size))
            tail = b""
            if file_size > CHUNK:
                f.seek(max(0, file_size - CHUNK))
                tail = f.read()
    except Exception as e:
        log_err(f"[unit-detect] 读取失败: {e}")
        return None

    text = (head + b"\n" + tail).decode("utf-8", errors="ignore")

    m = re.search(
        r"LENGTH_UNIT\s*\([^)]*\)[^;]*?SI_UNIT\s*\(\s*([^,]*?)\s*,\s*\.METRE\.\s*\)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        prefix = m.group(1).strip().strip(".").upper()
        if prefix in _SI_PREFIX_TO_METER:
            return _SI_PREFIX_TO_METER[prefix]

    m = re.search(
        r"LENGTH_UNIT\s*\([^)]*\)[^;]*?CONVERSION_BASED_UNIT\s*\(\s*'([^']+)'",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        unit_name = m.group(1).strip().upper()
        if unit_name in _NAMED_UNIT_TO_METER:
            return _NAMED_UNIT_TO_METER[unit_name]

    return None


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


def get_label_name(
    label: TDF_Label,
    fallback_label: Optional[TDF_Label] = None,
    parent_name: str = "",
    index: int = 0,
    default_name: str = DEFAULT_NODE_NAME,
) -> str:
    name = _read_label_name(label)
    if name:
        return name
    if fallback_label is not None and fallback_label != label:
        fb_name = _read_label_name(fallback_label)
        if fb_name:
            return fb_name
    if parent_name:
        return f"{parent_name}_{index}" if index > 0 else parent_name
    return f"{default_name}_{index}" if index > 0 else default_name


# =====================================================================
# 材质读取
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
        self, input_path: str, json_path: str, deflection: float = DEFAULT_DEFLECTION
    ):
        self.input_path = os.path.abspath(input_path)
        self.json_path = os.path.abspath(json_path)
        self.output_dir = os.path.dirname(self.json_path)
        self.meshs_dir = os.path.join(self.output_dir, MESHS_SUBDIR)
        self.deflection = deflection

        ext = os.path.splitext(self.input_path)[1].lower()
        if ext not in self.SUPPORTED_EXT:
            raise ValueError(f"不支持的文件类型: {ext}")
        self.input_ext = ext

        if ext in (".step", ".stp"):
            detected = detect_step_length_unit(self.input_path)
            if detected is None:
                self.unit_scale = FALLBACK_UNIT_TO_METER
                log(f"[unit] 未探测到单位，按 mm 兜底 (×{FALLBACK_UNIT_TO_METER})")
            else:
                self.unit_scale = detected
                log(f"[unit] 单位探测成功：1 单位 = {detected} 米")
        else:
            self.unit_scale = FALLBACK_UNIT_TO_METER
            log(f"[unit] 非 STEP，按 mm 兜底 (×{FALLBACK_UNIT_TO_METER})")

        self.doc = TDocStd_Document("MDTV-XCAF")
        self.shape_tool = None
        self.color_tool = None
        self.mat_tool = None

        self.asset_cache: Dict[str, str] = {}
        self.materials_table: Dict[str, dict] = {}

    # ---------------- 基础工具 ----------------

    @staticmethod
    def _get_label_entry_str(label: TDF_Label) -> str:
        s = TCollection_AsciiString()
        TDF_Tool.Entry(label, s)
        return s.ToCString()

    @staticmethod
    def _sanitize_filename(name: str) -> str:
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

    # ---------------- 多体零件合并 ----------------

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
    def _is_unnamed_body(node: dict) -> bool:
        return (
            node.get("type") == "mesh"
            and (node.get("name") or "").strip() in _BODY_TYPE_NAMES
        )

    def _is_body_container(self, children: List[dict]) -> bool:
        if not children or len(children) < 2:
            return False
        for c in children:
            if not self._is_unnamed_body(c):
                return False
            if not self._is_identity_tfm(c.get("transform")):
                return False
        return True

    def _merge_children_shapes(self, children: List[dict]):
        shapes = []
        for c in children:
            lbl = c.get("_label_ref")
            if lbl is None:
                return None
            try:
                s = self.shape_tool.GetShape(lbl)
                if s.IsNull():
                    return None
                shapes.append(s)
            except Exception:
                return None

        if not shapes:
            return None

        builder = BRep_Builder()
        compound = TopoDS_Compound()
        builder.MakeCompound(compound)
        for s in shapes:
            builder.Add(compound, s)
        return compound

    # ---------------- 三角化 + 面积加权平均法线 ----------------

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

            face_normals = [None] * n_nodes
            surface = None
            has_uv = False
            try:
                surface = BRep_Tool.Surface(face)
                if surface is not None:
                    has_uv = bool(triangulation.HasUVNodes())
            except Exception:
                surface = None
                has_uv = False

            if has_uv:
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

            triangles = []
            for ti in range(1, triangulation.NbTriangles() + 1):
                t = triangulation.Triangle(ti)
                a, b, c = t.Get()
                triangles.append((a - 1, b - 1, c - 1))

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
                    cross_len = math.sqrt(cx * cx + cy * cy + cz * cz)
                    if cross_len < 1e-20:
                        continue
                    area = 0.5 * cross_len
                    nx, ny, nz = cx / cross_len, cy / cross_len, cz / cross_len
                    if is_reversed:
                        nx, ny, nz = -nx, -ny, -nz
                    for vi in (a, b, c):
                        if node_acc[vi] is None:
                            node_acc[vi] = [0.0, 0.0, 0.0, 0.0]
                        node_acc[vi][0] += nx * area
                        node_acc[vi][1] += ny * area
                        node_acc[vi][2] += nz * area
                        node_acc[vi][3] += area

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
                cross_len = math.sqrt(cx * cx + cy * cy + cz * cz)
                if cross_len < 1e-20:
                    continue
                area = 0.5 * cross_len
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

        GRID = VERTEX_POS_GRID

        def _quantize(p):
            return (
                int(round(p[0] / GRID)),
                int(round(p[1] / GRID)),
                int(round(p[2] / GRID)),
            )

        buckets: Dict[Tuple[int, int, int], List[dict]] = {}
        node_to_cluster: Dict[Tuple[int, int], dict] = {}
        all_clusters: List[dict] = []

        for fp in faces_payload:
            fid = fp["face_id"]
            verts = fp["verts"]
            normals = fp["normals"]
            areas = fp["node_area"]

            for ni in range(len(verts)):
                p = verts[ni]
                n = normals[ni]
                if n is None:
                    n = (0.0, 0.0, 1.0)
                w = areas[ni]
                if w <= 0.0:
                    w = 1e-12

                key = _quantize(p)
                blist = buckets.setdefault(key, [])

                matched = None
                for cl in blist:
                    rn = cl["ref_normal"]
                    dot = n[0] * rn[0] + n[1] * rn[1] + n[2] * rn[2]
                    if dot >= SMOOTH_ANGLE_COS:
                        matched = cl
                        break

                if matched is None:
                    cl = {
                        "pos": p,
                        "ref_normal": n,
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
            ln = math.sqrt(acc[0] * acc[0] + acc[1] * acc[1] + acc[2] * acc[2])
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

        if not vertices:
            return None

        return vertices, out_normals, indices

    # ---------------- glTF + BIN 写入 ----------------

    def _export_shape_to_gltf(
        self, shape, output_gltf_path: str, material: Optional[dict] = None
    ) -> bool:
        result = self._triangulate_with_surface_normals(shape)
        if result is None:
            return False

        vertices, normals, indices = result
        num_vertices = len(vertices) // 3

        v_bytes = bytearray()
        min_pos = [float("inf")] * 3
        max_pos = [float("-inf")] * 3
        for i in range(0, len(vertices), 3):
            vx, vy, vz = vertices[i], vertices[i + 1], vertices[i + 2]
            v_bytes.extend(struct.pack("<fff", vx, vy, vz))
            min_pos[0] = min(min_pos[0], vx)
            min_pos[1] = min(min_pos[1], vy)
            min_pos[2] = min(min_pos[2], vz)
            max_pos[0] = max(max_pos[0], vx)
            max_pos[1] = max(max_pos[1], vy)
            max_pos[2] = max(max_pos[2], vz)

        n_bytes = bytearray()
        for i in range(0, len(normals), 3):
            n_bytes.extend(
                struct.pack("<fff", normals[i], normals[i + 1], normals[i + 2])
            )

        use_uint32 = num_vertices > 65535
        idx_component_type = 5125 if use_uint32 else 5123
        idx_fmt = "<I" if use_uint32 else "<H"
        i_bytes = bytearray()
        for idx in indices:
            i_bytes.extend(struct.pack(idx_fmt, idx))

        while len(v_bytes) % 4 != 0:
            v_bytes.extend(b"\x00")
        while len(n_bytes) % 4 != 0:
            n_bytes.extend(b"\x00")
        while len(i_bytes) % 4 != 0:
            i_bytes.extend(b"\x00")

        bin_buffer = v_bytes + n_bytes + i_bytes
        v_offset = 0
        n_offset = len(v_bytes)
        i_offset = len(v_bytes) + len(n_bytes)

        primitive = {
            "attributes": {"POSITION": 0, "NORMAL": 1},
            "indices": 2,
        }

        materials_json: List[dict] = []
        if material and material.get("base_color"):
            bc = material["base_color"]
            r, g, b = float(bc[0]), float(bc[1]), float(bc[2])
            a = float(bc[3]) if len(bc) > 3 else 1.0
            mt = float(material.get("metallic", 0.0))
            rg = float(material.get("roughness", 0.5))

            mat_json = {
                "name": "Material_0",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [r, g, b, a],
                    "metallicFactor": mt,
                    "roughnessFactor": rg,
                },
                "doubleSided": False,
            }
            if a < 1.0:
                mat_json["alphaMode"] = "BLEND"
            materials_json.append(mat_json)
            primitive["material"] = 0

        gltf_basename = os.path.basename(output_gltf_path)
        if gltf_basename.lower().endswith(".gltf"):
            bin_basename = gltf_basename[:-5] + ".bin"
        else:
            bin_basename = gltf_basename + ".bin"
        output_bin_path = os.path.join(os.path.dirname(output_gltf_path), bin_basename)

        gltf_dict = {
            "asset": {"version": "2.0", "generator": "cad-splitter glTF Exporter"},
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [primitive]}],
            "buffers": [{"byteLength": len(bin_buffer), "uri": bin_basename}],
            "bufferViews": [
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
                {
                    "buffer": 0,
                    "byteOffset": i_offset,
                    "byteLength": len(i_bytes),
                    "target": 34963,
                },
            ],
            "accessors": [
                {
                    "bufferView": 0,
                    "byteOffset": 0,
                    "componentType": 5126,
                    "count": num_vertices,
                    "type": "VEC3",
                    "max": max_pos,
                    "min": min_pos,
                },
                {
                    "bufferView": 1,
                    "byteOffset": 0,
                    "componentType": 5126,
                    "count": num_vertices,
                    "type": "VEC3",
                },
                {
                    "bufferView": 2,
                    "byteOffset": 0,
                    "componentType": idx_component_type,
                    "count": len(indices),
                    "type": "SCALAR",
                },
            ],
        }
        if materials_json:
            gltf_dict["materials"] = materials_json

        with open(output_bin_path, "wb") as fb:
            fb.write(bin_buffer)

        with open(output_gltf_path, "w", encoding="utf-8") as fj:
            json.dump(gltf_dict, fj, separators=(",", ":"))

        return True

    # ---------------- 单零件 glTF 导出 ----------------

    def _export_atomic_gltf_with_cache(
        self,
        label: TDF_Label,
        display_name: Optional[str] = None,
        material_src_label: Optional[TDF_Label] = None,
    ) -> Optional[str]:
        label_entry = self._get_label_entry_str(label)
        if label_entry in self.asset_cache:
            return self.asset_cache[label_entry]

        shape = self.shape_tool.GetShape(label)
        if shape.IsNull():
            return None

        mat_dict = None
        if material_src_label is not None:
            mat_dict = read_material_from_label(
                self.color_tool, self.mat_tool, material_src_label
            )
        if mat_dict is None:
            mat_dict = read_material_from_label(self.color_tool, self.mat_tool, label)

        raw_name = (display_name or "").strip()
        if not raw_name:
            raw_name = _read_label_name(label) or "part"
        safe_name = self._sanitize_filename(raw_name)[:FILENAME_NAME_MAX_LEN] or "part"

        short_hash = hashlib.sha1(label_entry.encode("utf-8")).hexdigest()[
            :FILENAME_HASH_LEN
        ]
        safe_filename = f"{safe_name}__{short_hash}.gltf"
        gltf_path = os.path.join(self.meshs_dir, safe_filename)

        status = self._export_shape_to_gltf(shape, gltf_path, material=mat_dict)
        breptools.Clean(shape)

        if not status:
            return None

        log(f"[mesh] {safe_filename}")
        rel = f"{MESHS_SUBDIR}/{safe_filename}"
        self.asset_cache[label_entry] = rel
        return rel

    def _export_merged_gltf(
        self,
        shape,
        display_name: str,
        cache_key: str,
        material_ids: Optional[List[str]] = None,
    ) -> Optional[str]:
        if cache_key in self.asset_cache:
            return self.asset_cache[cache_key]

        raw_name = (display_name or "").strip() or "merged"
        safe_name = (
            self._sanitize_filename(raw_name)[:FILENAME_NAME_MAX_LEN] or "merged"
        )
        short_hash = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[
            :FILENAME_HASH_LEN
        ]
        safe_filename = f"{safe_name}__{short_hash}.gltf"
        gltf_path = os.path.join(self.meshs_dir, safe_filename)

        mat_dict = None
        if material_ids:
            mat_dict = self.materials_table.get(material_ids[0])

        status = self._export_shape_to_gltf(shape, gltf_path, material=mat_dict)
        breptools.Clean(shape)

        if not status:
            return None

        log(f"[mesh] {safe_filename}")
        rel = f"{MESHS_SUBDIR}/{safe_filename}"
        self.asset_cache[cache_key] = rel
        return rel

    # ---------------- 结构树 ----------------

    def _register_material(self, mat_dict: dict) -> str:
        mid = _material_id(mat_dict)
        if mid not in self.materials_table:
            self.materials_table[mid] = mat_dict
        return mid

    def _extract_tree_node(
        self,
        label: TDF_Label,
        instance_label: Optional[TDF_Label] = None,
        parent_name: str = "",
        index: int = 0,
    ) -> Optional[Dict[str, Any]]:
        target_label = instance_label if instance_label else label
        node_id = self._get_label_entry_str(target_label)
        node_name = get_label_name(
            target_label,
            fallback_label=label,
            parent_name=parent_name,
            index=index,
        )

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
            except Exception:
                pass

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
                        parent_name=node_name,
                        index=i,
                    )
                    if child:
                        children.append(child)

            if (
                len(children) == 1
                and position == [0.0, 0.0, 0.0]
                and quaternion == [0.0, 0.0, 0.0, 1.0]
            ):
                return children[0]

            if self._is_body_container(children):
                merged_shape = self._merge_children_shapes(children)
                if merged_shape is not None:
                    for c in children:
                        c.pop("_label_ref", None)
                        c.pop("_instance_label", None)

                    mat_dict = read_material_from_label(
                        self.color_tool, self.mat_tool, label
                    )
                    mat_ids: List[str] = []
                    if mat_dict:
                        mat_ids.append(self._register_material(mat_dict))

                    node: Dict[str, Any] = {
                        "id": node_id,
                        "name": node_name,
                        "type": "mesh",
                        "transform": {
                            "position": position,
                            "quaternion": quaternion,
                            "scale": scale,
                        },
                        "children": [],
                        "_merged_shape": merged_shape,
                        "_ref_entry": self._get_label_entry_str(label),
                    }
                    if mat_ids:
                        node["_material_ids"] = mat_ids
                    return node

            return {
                "id": node_id,
                "name": node_name,
                "type": "node",
                "transform": {
                    "position": position,
                    "quaternion": quaternion,
                    "scale": scale,
                },
                "children": children,
            }

        mat_src = instance_label if instance_label else label
        mat_dict = read_material_from_label(self.color_tool, self.mat_tool, mat_src)
        if mat_dict is None and instance_label:
            mat_dict = read_material_from_label(self.color_tool, self.mat_tool, label)

        mat_ids: List[str] = []
        if mat_dict:
            mat_ids.append(self._register_material(mat_dict))

        node: Dict[str, Any] = {
            "id": node_id,
            "name": node_name,
            "type": "mesh",
            "transform": {
                "position": position,
                "quaternion": quaternion,
                "scale": scale,
            },
            "children": [],
            "_label_ref": label,
            "_instance_label": instance_label,
        }
        if mat_ids:
            node["_material_ids"] = mat_ids
        return node

    def _generate_gltfs_recursive(self, node):
        if node["type"] == "mesh":
            if "_merged_shape" in node:
                merged = node.pop("_merged_shape")
                ref_entry = node.pop("_ref_entry", "") or node.get("id", "")
                node["asset"] = self._export_merged_gltf(
                    merged,
                    display_name=node.get("name"),
                    cache_key=ref_entry,
                    material_ids=node.get("_material_ids", []),
                )
            elif "_label_ref" in node:
                label = node.pop("_label_ref")
                inst = node.pop("_instance_label", None)
                node["asset"] = self._export_atomic_gltf_with_cache(
                    label,
                    display_name=node.get("name"),
                    material_src_label=inst,
                )
        else:
            node.pop("_instance_label", None)

        for child in node.get("children", []):
            self._generate_gltfs_recursive(child)

    # ---------------- finalize ----------------

    def _finalize_tree(self, root: dict):
        name_count = {}

        def _count(n):
            nm = n.get("name", "")
            name_count[nm] = name_count.get(nm, 0) + 1
            for c in n.get("children", []):
                _count(c)

        _count(root)

        def _fix(n):
            nm = n.get("name", "")
            entry = n.get("id", "")
            mat_ids = n.get("_material_ids", [])
            ast = n.get("asset", "")
            children = n.get("children", [])
            node_type = n.get("type", "node")
            tfm = n.get("transform") or {
                "position": [0.0, 0.0, 0.0],
                "quaternion": [0.0, 0.0, 0.0, 1.0],
                "scale": 1.0,
            }

            for c in children:
                _fix(c)

            if nm and name_count.get(nm, 0) > 1:
                new_id = f"{nm}_{entry}" if entry else nm
            else:
                new_id = nm or entry

            n.clear()
            n["id"] = new_id
            n["name"] = nm
            n["type"] = node_type
            if mat_ids:
                n["material"] = mat_ids
            n["transform"] = tfm
            if ast:
                n["asset"] = ast
            n["children"] = children

        _fix(root)
        root["materials"] = self.materials_table

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
            from OCC.Core.IGESCAFControl import IGESCAFControl_Reader

            r = IGESCAFControl_Reader()
            r.SetColorMode(True)
            r.SetNameMode(True)
            r.SetLayerMode(True)
            return r
        raise ValueError(f"不支持的文件类型: {ext}")

    # ---------------- 主流程 ----------------

    def _load(self):
        log(f"[doc] 正在载入 {self.input_ext}")
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
            n = self._extract_tree_node(
                free_shapes.Value(i),
                parent_name="Assembly_Root",
                index=i,
            )
            if n:
                children.append(n)
        return {
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

    def run(self):
        self._load()
        log("[dump] 提取模型结构树")
        root = self._build_root_tree()

        os.makedirs(self.meshs_dir, exist_ok=True)
        log("[mesh] 生成叶子节点 glTF ...")
        self._generate_gltfs_recursive(root)
        log(
            f"[mesh] 唯一 glTF {len(self.asset_cache)} 个，材质 {len(self.materials_table)} 个"
        )

        self._finalize_tree(root)

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

    deflection_val = DEFAULT_DEFLECTION
    if len(sys.argv) >= 4:
        try:
            deflection_val = float(sys.argv[3])
        except ValueError:
            log(
                f"[warn  ] deflection '{sys.argv[3]}' 无效，用默认 {DEFAULT_DEFLECTION}"
            )

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
