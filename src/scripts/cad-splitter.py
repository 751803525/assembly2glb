import os
import sys
import json
import re
import math
import faulthandler
import struct
import hashlib
from typing import Dict, Any, List, Optional, Tuple

faulthandler.enable()

from OCC.Core.STEPCAFControl import STEPCAFControl_Reader
from OCC.Core.TDocStd import TDocStd_Document
from OCC.Core.XCAFDoc import XCAFDoc_DocumentTool
from OCC.Core.TDF import TDF_Label, TDF_Tool, TDF_LabelSequence
from OCC.Core.TCollection import TCollection_AsciiString

from OCC.Core.gp import gp_Trsf
from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
from OCC.Core.BRepTools import breptools
from OCC.Core.TopExp import TopExp_Explorer
from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_REVERSED
from OCC.Core.TopoDS import topods
from OCC.Core.BRep import BRep_Tool
from OCC.Core.TopLoc import TopLoc_Location

# =====================================================================
# 模块级：日志 + 常量
# =====================================================================


def logger_info(msg: str):
    print(msg, flush=True)


def logger_err(msg: str):
    print(msg, file=sys.stderr, flush=True)


DEFAULT_DEFLECTION = 0.2
DEFAULT_NODE_NAME = "Node"
GLB_SUBDIR = "glbs"

FILENAME_NAME_MAX_LEN = 80
FILENAME_HASH_LEN = 8

FALLBACK_UNIT_TO_METER = 0.001

SMOOTH_ANGLE_DEG = 30.0
SMOOTH_ANGLE_COS = math.cos(math.radians(SMOOTH_ANGLE_DEG))

# OCC 生成的占位名前缀（如 "=>[0:1:1:66]" / "=[0:1:1:66]"），视为"无名字"
_PLACEHOLDER_NAME_PREFIXES = ("=>", "=[")

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
        logger_err(f"[unit-detect] 读取 STEP 失败: {e}")
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
        logger_err(f"[unit-detect] 未知 SI 词头: {prefix!r}")
        return None

    m = re.search(
        r"LENGTH_UNIT\s*\([^)]*\)[^;]*?CONVERSION_BASED_UNIT\s*\(\s*'([^']+)'",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        unit_name = m.group(1).strip().upper()
        if unit_name in _NAMED_UNIT_TO_METER:
            return _NAMED_UNIT_TO_METER[unit_name]
        logger_err(f"[unit-detect] 未知长度单位: {unit_name!r}")
        return None

    return None


# =====================================================================
# 名字读取工具
# =====================================================================


def _read_label_name(lbl: TDF_Label) -> str:
    """
    读 label 的名字
    - 空 / OCC 占位名（=>[entry] 形式）→ 返回空串，触发 fallback
    - 其他 → 原样返回
    """
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
    except Exception as e:
        logger_err(f"[name-label] {e}")
        return ""


def get_label_name(
    label: TDF_Label,
    fallback_label: Optional[TDF_Label] = None,
    parent_name: str = "",
    index: int = 0,
    default_name: str = DEFAULT_NODE_NAME,
) -> str:
    """
    名字解析：
        1. 主 label 自身名字（非空、非占位名）
        2. fallback label 自身名字（非空、非占位名）
        3. 兜底：{parent_name}_{index}（如 "SH1_904_1_2"）
           无 parent_name 时退化为 {default_name}_{index}
    """
    # 1. 主 label
    name = _read_label_name(label)
    if name:
        return name

    # 2. fallback label
    if fallback_label is not None and fallback_label != label:
        fb_name = _read_label_name(fallback_label)
        if fb_name:
            return fb_name

    # 3. 兜底：{parent_name}_{index}
    if parent_name:
        return f"{parent_name}_{index}" if index > 0 else parent_name
    return f"{default_name}_{index}" if index > 0 else default_name


# =====================================================================
# 主转换器
# =====================================================================


class StepToGlbConverter:
    """STEP → GLB 拆分 + 结构树 JSON 导出"""

    def __init__(
        self,
        step_path: str,
        json_path: str,
        deflection: float = DEFAULT_DEFLECTION,
    ):
        self.step_path = os.path.abspath(step_path)
        self.json_path = os.path.abspath(json_path)
        self.output_dir = os.path.dirname(self.json_path)
        self.glb_dir = os.path.join(self.output_dir, GLB_SUBDIR)

        self.deflection = deflection

        detected = detect_step_length_unit(self.step_path)
        if detected is None:
            self.unit_scale = FALLBACK_UNIT_TO_METER
            logger_info(
                f"未探测到 STEP 单位，按 mm 兜底处理 " f"(× {FALLBACK_UNIT_TO_METER})"
            )
        else:
            self.unit_scale = detected
            logger_info(f"STEP 单位探测成功：1 单位 = {detected} 米")

        self.doc = TDocStd_Document("MDTV-XCAF")
        self.shape_tool = None

        # referred_label 的 entry -> asset 相对路径
        # 同一 PRODUCT 定义的多个实例 → 命中缓存 → 只导出一份
        self.asset_cache: Dict[str, str] = {}

    # -----------------------------------------------------------------
    # 标签工具
    # -----------------------------------------------------------------

    @staticmethod
    def _get_label_entry_str(label: TDF_Label) -> str:
        entry_str = TCollection_AsciiString()
        TDF_Tool.Entry(label, entry_str)
        return entry_str.ToCString()

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        if not name:
            return ""
        s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
        s = "".join(c for c in s if c.isprintable())
        s = re.sub(r"_+", "_", s)
        return s.strip(" ._")

    # -----------------------------------------------------------------
    # 变换工具
    # -----------------------------------------------------------------

    def _decompose_trsf(
        self,
        trsf: gp_Trsf,
    ) -> Tuple[List[float], List[float], List[float]]:
        t = trsf.TranslationPart()
        position = [
            t.X() * self.unit_scale,
            t.Y() * self.unit_scale,
            t.Z() * self.unit_scale,
        ]

        q = trsf.GetRotation()
        quaternion = [q.X(), q.Y(), q.Z(), q.W()]

        s = trsf.ScaleFactor()
        scale = [s, s, s]

        return position, quaternion, scale

    # -----------------------------------------------------------------
    # 三角化 + 顶点法线
    # -----------------------------------------------------------------

    def _triangulate_with_surface_normals(
        self, shape
    ) -> Optional[Tuple[List[float], List[float], List[int]]]:
        mesh = BRepMesh_IncrementalMesh(shape, self.deflection, False, 0.5, True)
        mesh.Perform()

        position_clusters: Dict[Tuple, List[List]] = {}

        vertices: List[float] = []
        indices: List[int] = []

        def find_or_add_vertex(
            pos: Tuple[float, float, float],
            nrm: Tuple[float, float, float],
            face_id: int,
        ) -> int:
            pos_key = (
                round(pos[0], 5),
                round(pos[1], 5),
                round(pos[2], 5),
            )
            entries = position_clusters.setdefault(pos_key, [])

            # 同一个 face 在该位置已有顶点 → 直接复用
            for entry in entries:
                if entry[0] == face_id:
                    return entry[1]

            # 跨 face 按法线夹角聚类
            for entry in entries:
                rx, ry, rz = entry[2], entry[3], entry[4]
                dot = nrm[0] * rx + nrm[1] * ry + nrm[2] * rz
                if dot >= SMOOTH_ANGLE_COS:
                    return entry[1]

            new_idx = len(vertices) // 3
            vertices.extend(pos)
            entries.append([face_id, new_idx, nrm[0], nrm[1], nrm[2]])
            return new_idx

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

            # 顶点世界坐标
            face_verts: List[Tuple[float, float, float]] = []
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

            # 逐顶点法线
            face_normals: List[Optional[Tuple[float, float, float]]] = [None] * n_nodes

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

            # 逐顶点兜底（邻域三角形面法线加权平均）
            if any(n is None for n in face_normals):
                vertex_tris: List[List[int]] = [[] for _ in range(n_nodes)]
                for ti in range(1, triangulation.NbTriangles() + 1):
                    t = triangulation.Triangle(ti)
                    a, b, c = t.Get()
                    vertex_tris[a - 1].append(ti)
                    vertex_tris[b - 1].append(ti)
                    vertex_tris[c - 1].append(ti)

                for i in range(n_nodes):
                    if face_normals[i] is not None:
                        continue

                    acc = [0.0, 0.0, 0.0]
                    for ti in vertex_tris[i]:
                        t = triangulation.Triangle(ti)
                        a, b, c = t.Get()
                        v0 = face_verts[a - 1]
                        v1 = face_verts[b - 1]
                        v2 = face_verts[c - 1]

                        e1x = v1[0] - v0[0]
                        e1y = v1[1] - v0[1]
                        e1z = v1[2] - v0[2]
                        e2x = v2[0] - v0[0]
                        e2y = v2[1] - v0[1]
                        e2z = v2[2] - v0[2]

                        nx = e1y * e2z - e1z * e2y
                        ny = e1z * e2x - e1x * e2z
                        nz = e1x * e2y - e1y * e2x

                        if is_reversed:
                            nx, ny, nz = -nx, -ny, -nz

                        acc[0] += nx
                        acc[1] += ny
                        acc[2] += nz

                    length = math.sqrt(acc[0] ** 2 + acc[1] ** 2 + acc[2] ** 2)
                    if length > 1e-12:
                        face_normals[i] = (
                            acc[0] / length,
                            acc[1] / length,
                            acc[2] / length,
                        )
                    else:
                        face_normals[i] = (0.0, 0.0, 1.0)

            # 遍历三角形
            for ti in range(1, triangulation.NbTriangles() + 1):
                t = triangulation.Triangle(ti)
                a, b, c = t.Get()

                v0 = face_verts[a - 1]
                v1 = face_verts[b - 1]
                v2 = face_verts[c - 1]
                n0 = face_normals[a - 1]
                n1 = face_normals[b - 1]
                n2 = face_normals[c - 1]

                if is_reversed:
                    v1, v2 = v2, v1
                    n1, n2 = n2, n1

                i0 = find_or_add_vertex(v0, n0, face_id)
                i1 = find_or_add_vertex(v1, n1, face_id)
                i2 = find_or_add_vertex(v2, n2, face_id)

                indices.extend([i0, i1, i2])

            explorer.Next()

        if not vertices:
            return None

        # 生成最终法线
        n_verts = len(vertices) // 3
        normals: List[float] = [0.0] * (n_verts * 3)

        for entries in position_clusters.values():
            for entry in entries:
                vidx = entry[1]
                normals[vidx * 3] = entry[2]
                normals[vidx * 3 + 1] = entry[3]
                normals[vidx * 3 + 2] = entry[4]

        return vertices, normals, indices

    # -----------------------------------------------------------------
    # GLB 导出
    # -----------------------------------------------------------------

    def _export_shape_to_glb(self, shape, output_glb_path: str) -> bool:
        result = self._triangulate_with_surface_normals(shape)
        if result is None:
            return False

        vertices, normals, indices = result
        num_vertices = len(vertices) // 3

        # 顶点缓冲
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

        # 法线缓冲
        n_bytes = bytearray()
        for i in range(0, len(normals), 3):
            n_bytes.extend(
                struct.pack(
                    "<fff",
                    normals[i],
                    normals[i + 1],
                    normals[i + 2],
                )
            )

        # 索引缓冲
        use_uint32 = num_vertices > 65535
        idx_component_type = 5125 if use_uint32 else 5123
        idx_fmt = "<I" if use_uint32 else "<H"

        i_bytes = bytearray()
        for idx in indices:
            i_bytes.extend(struct.pack(idx_fmt, idx))

        # 4 字节对齐
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

        gltf_dict = {
            "asset": {
                "version": "2.0",
                "generator": "assembly2glb Pure Mesh Exporter",
            },
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "meshes": [
                {
                    "primitives": [
                        {
                            "attributes": {
                                "POSITION": 0,
                                "NORMAL": 1,
                            },
                            "indices": 2,
                        }
                    ]
                }
            ],
            "buffers": [{"byteLength": len(bin_buffer)}],
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

        json_bytes = json.dumps(gltf_dict, separators=(",", ":")).encode("utf-8")
        while len(json_bytes) % 4 != 0:
            json_bytes += b" "

        total_length = 12 + 8 + len(json_bytes) + 8 + len(bin_buffer)
        header = struct.pack("<4sII", b"glTF", 2, total_length)
        json_chunk_hdr = struct.pack("<I4s", len(json_bytes), b"JSON")
        bin_chunk_hdr = struct.pack("<I4s", len(bin_buffer), b"BIN\x00")

        with open(output_glb_path, "wb") as f:
            f.write(header)
            f.write(json_chunk_hdr)
            f.write(json_bytes)
            f.write(bin_chunk_hdr)
            f.write(bin_buffer)

        return True

    def _export_atomic_glb_with_cache(
        self,
        label: TDF_Label,
        display_name: Optional[str] = None,
    ) -> Optional[str]:
        """
        导出单个零件 GLB

        缓存 key = referred_label 的 entry
            同一 PRODUCT 定义的多个实例 → 命中缓存 → 只导出一份
        """
        label_entry = self._get_label_entry_str(label)

        if label_entry in self.asset_cache:
            return self.asset_cache[label_entry]

        shape = self.shape_tool.GetShape(label)
        if shape.IsNull():
            return None

        raw_name = (display_name or "").strip()
        if not raw_name:
            try:
                raw_name = _read_label_name(label)
            except Exception:
                raw_name = ""

        if not raw_name:
            raw_name = "part"

        safe_name = self._sanitize_filename(raw_name)
        safe_name = safe_name[:FILENAME_NAME_MAX_LEN] if safe_name else "part"

        short_hash = hashlib.sha1(label_entry.encode("utf-8")).hexdigest()[
            :FILENAME_HASH_LEN
        ]

        safe_filename = f"{safe_name}__{short_hash}.glb"
        glb_path = os.path.join(self.glb_dir, safe_filename)

        status = self._export_shape_to_glb(shape, glb_path)
        breptools.Clean(shape)

        if not status:
            return None

        logger_info(f"导出：{safe_filename}")
        rel_asset_path = f"{GLB_SUBDIR}/{safe_filename}"
        self.asset_cache[label_entry] = rel_asset_path
        return rel_asset_path

    # -----------------------------------------------------------------
    # 结构树
    # -----------------------------------------------------------------

    def _extract_tree_node(
        self,
        label: TDF_Label,
        instance_label: Optional[TDF_Label] = None,
        parent_name: str = "",
        index: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """
        递归提取结构树节点

        参数：
            parent_name: 父节点的 node_name，用于无名字节点的兜底命名
            index:       当前节点在父节点 children 里的序号（1-based）
        """
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
        scale = [1.0, 1.0, 1.0]

        if instance_label:
            try:
                loc = self.shape_tool.GetLocation(instance_label)
                if not loc.IsIdentity():
                    position, quaternion, scale = self._decompose_trsf(
                        loc.Transformation()
                    )
            except Exception:
                pass

        # 装配体
        if is_asm:
            components = TDF_LabelSequence()
            self.shape_tool.GetComponents(label, components)

            children = []
            for i in range(1, components.Length() + 1):
                comp_label = components.Value(i)
                referred_label = TDF_Label()
                if self.shape_tool.GetReferredShape(comp_label, referred_label):
                    child_node = self._extract_tree_node(
                        referred_label,
                        instance_label=comp_label,
                        parent_name=node_name,
                        index=i,
                    )
                    if child_node:
                        children.append(child_node)

            # 单子节点且无自身变换 → 折叠为子节点
            if (
                len(children) == 1
                and position == [0.0, 0.0, 0.0]
                and quaternion == [0.0, 0.0, 0.0, 1.0]
            ):
                return children[0]

            return {
                "id": node_id,
                "name": node_name,
                "type": "assembly",
                "transform": {
                    "position": position,
                    "quaternion": quaternion,
                    "scale": scale,
                },
                "children": children,
                "asset": None,
            }

        # 零件
        return {
            "id": node_id,
            "name": node_name,
            "type": "part",
            "transform": {
                "position": position,
                "quaternion": quaternion,
                "scale": scale,
            },
            "children": [],
            "asset": None,
            "_label_ref": label,
        }

    def _generate_glbs_recursive(self, node: Dict[str, Any]):
        if node["type"] == "part" and "_label_ref" in node:
            label = node.pop("_label_ref")
            node["asset"] = self._export_atomic_glb_with_cache(
                label, display_name=node.get("name")
            )

        for child in node.get("children", []):
            self._generate_glbs_recursive(child)

    # -----------------------------------------------------------------
    # 主流程
    # -----------------------------------------------------------------

    def _load_step(self):
        logger_info("正在载入文件")
        reader = STEPCAFControl_Reader()
        reader.SetColorMode(True)
        reader.SetNameMode(True)

        if reader.ReadFile(self.step_path) != 1:
            raise RuntimeError("STEP 文件解析失败")

        reader.Transfer(self.doc)
        self.shape_tool = XCAFDoc_DocumentTool.ShapeTool(self.doc.Main())

    def _build_root_tree(self) -> Dict[str, Any]:
        free_shapes = TDF_LabelSequence()
        self.shape_tool.GetFreeShapes(free_shapes)

        if free_shapes.Length() == 0:
            raise RuntimeError("未找到有效的根拓扑模型")

        logger_info("提取模型结构树")

        # 单根 → 直接以它为根
        if free_shapes.Length() == 1:
            return self._extract_tree_node(free_shapes.Value(1))

        # 多根 → 包一层虚拟 Assembly_Root
        children = []
        for i in range(1, free_shapes.Length() + 1):
            node = self._extract_tree_node(
                free_shapes.Value(i),
                parent_name="Assembly_Root",
                index=i,
            )
            if node:
                children.append(node)

        return {
            "id": "root_0",
            "name": "Assembly_Root",
            "type": "assembly",
            "transform": {
                "position": [0.0, 0.0, 0.0],
                "quaternion": [0.0, 0.0, 0.0, 1.0],
                "scale": [1.0, 1.0, 1.0],
            },
            "children": children,
            "asset": None,
        }

    def _write_json(self, root_tree: Dict[str, Any]):
        os.makedirs(self.output_dir, exist_ok=True)
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(root_tree, f, ensure_ascii=False, indent=2)
        logger_info(f"成功导出结构树文件: {os.path.basename(self.json_path)}")

    def run(self):
        self._load_step()
        root_tree = self._build_root_tree()

        os.makedirs(self.glb_dir, exist_ok=True)
        logger_info("生成叶子节点 GLB 模型")
        self._generate_glbs_recursive(root_tree)
        logger_info(f"模型导出完成！共生成 {len(self.asset_cache)} 个独立 GLB 文件。")

        self._write_json(root_tree)


# =====================================================================
# 入口
# =====================================================================


def main():
    if len(sys.argv) < 3:
        print(
            "用法: python cad_splitter.py <STEP文件路径> <JSON输出完整路径> "
            "[deflection精度, 默认0.2]"
        )
        sys.exit(1)

    step_input = sys.argv[1]
    json_output = sys.argv[2]

    deflection_val = DEFAULT_DEFLECTION
    if len(sys.argv) >= 4:
        try:
            deflection_val = float(sys.argv[3])
        except ValueError:
            logger_info(
                f"传入的 deflection 参数 '{sys.argv[3]}' 无效，将使用默认值 "
                f"{DEFAULT_DEFLECTION}"
            )

    converter = StepToGlbConverter(step_input, json_output, deflection=deflection_val)
    converter.run()


if __name__ == "__main__":
    main()
