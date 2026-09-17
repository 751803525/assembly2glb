import os
import sys
import json
import re
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
from OCC.Core.TopAbs import TopAbs_FACE
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

# 文件名里"可读部分"的最大长度，防止路径过长触发 Windows 260 限制
FILENAME_NAME_MAX_LEN = 80
# entry hash 的短长度，保证同名零件不冲突
FILENAME_HASH_LEN = 8

# 兜底换算系数：探测失败时假设 STEP 是 mm，glTF 是米
FALLBACK_UNIT_TO_METER = 0.001

# SI 词头 → 米
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

# 常见英制单位 → 米
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
    """
    从 STEP 文件解析长度单位

    读取策略：
        STEP 的 LENGTH_UNIT 声明可能出现在文件头部（罕见）或尾部（常见，
        通常与 GLOBAL_UNIT_ASSIGNED_CONTEXT 一起出现在文件末尾）。
        因此同时读取头部和尾部各 1MB 进行匹配。

    返回：1 个模型单位等于多少米；无法识别时返回 None
    """
    CHUNK = 1024 * 1024  # 1 MB

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

    # ---- 形式 1：长度 SI 单位 ----
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

    # ---- 形式 2：长度英制单位 ----
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
    通过 TDF_Label.GetLabelName() 直接读取名字

    说明：
        在 pythonocc-core 的部分构建中，TDataStd_Name.Get 的 SWIG 绑定缺失，
        导致常规方式无法读出零件名。但 pythonocc-core 在 TDF_Label 上提供了
        GetLabelName() 扩展方法，底层直接调 C++ 侧 TDataStd_Name::Get()，
        不受 Python 绑定缺失影响。
    """
    try:
        name = lbl.GetLabelName()
        if name is None:
            return ""
        s = str(name).strip()
        return s
    except Exception as e:
        logger_err(f"[name-label] {e}")
        return ""


def get_label_name(
    label: TDF_Label,
    fallback_label: Optional[TDF_Label] = None,
    default_name: str = DEFAULT_NODE_NAME,
) -> str:
    """实例标签优先，读不到回退到定义标签"""
    name = _read_label_name(label)

    if (
        (not name or name == default_name)
        and fallback_label
        and fallback_label != label
    ):
        fb = _read_label_name(fallback_label)
        if fb:
            name = fb

    return name if name else default_name


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

        # 探测 STEP 单位，失败则用 mm 兜底
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
        """
        把名字清洗成安全的文件名：
          - 替换 Windows 保留字符 \\ / : * ? " < > |
          - 去掉控制字符
          - 折叠连续下划线
          - 去掉首尾空白和点
        """
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
        """
        分解 gp_Trsf 为 position / quaternion / scale

        注意：
            position 按 self.unit_scale 换算到米，与顶点坐标一致；
            scale 是无量纲比例，不换算。
        """
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
    # GLB 导出
    # -----------------------------------------------------------------

    def _export_shape_to_glb(self, shape, output_glb_path: str) -> bool:
        """
        把单个 shape 三角化并写成 GLB

        顶点坐标按 self.unit_scale 换算，去重 key 用换算后的值。
        """
        mesh = BRepMesh_IncrementalMesh(shape, self.deflection, False, 0.5, True)
        mesh.Perform()

        vertex_map = {}
        vertices: List[float] = []
        indices: List[int] = []

        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        while explorer.More():
            face = topods.Face(explorer.Current())
            loc = TopLoc_Location()
            triangulation = BRep_Tool.Triangulation(face, loc)

            if triangulation:
                trsf = loc.Transformation()
                local_to_global_idx = {}

                for i in range(1, triangulation.NbNodes() + 1):
                    p = triangulation.Node(i)
                    if not loc.IsIdentity():
                        p.Transform(trsf)

                    # 换算到米
                    px = p.X() * self.unit_scale
                    py = p.Y() * self.unit_scale
                    pz = p.Z() * self.unit_scale

                    key = (round(px, 4), round(py, 4), round(pz, 4))
                    if key not in vertex_map:
                        vertex_map[key] = len(vertices) // 3
                        vertices.extend([px, py, pz])
                    local_to_global_idx[i] = vertex_map[key]

                for i in range(1, triangulation.NbTriangles() + 1):
                    tri = triangulation.Triangle(i)
                    n1, n2, n3 = tri.Get()
                    indices.extend(
                        [
                            local_to_global_idx[n1],
                            local_to_global_idx[n2],
                            local_to_global_idx[n3],
                        ]
                    )

            explorer.Next()

        if not vertices or not indices:
            return False

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

        # 索引缓冲：根据顶点数选 USHORT / UINT
        use_uint32 = num_vertices > 65535
        idx_component_type = 5125 if use_uint32 else 5123
        idx_fmt = "<I" if use_uint32 else "<H"

        i_bytes = bytearray()
        for idx in indices:
            i_bytes.extend(struct.pack(idx_fmt, idx))

        # 4 字节对齐
        while len(v_bytes) % 4 != 0:
            v_bytes.extend(b"\x00")
        while len(i_bytes) % 4 != 0:
            i_bytes.extend(b"\x00")

        bin_buffer = v_bytes + i_bytes

        # glTF JSON 元数据
        gltf_dict = {
            "asset": {
                "version": "2.0",
                "generator": "CADLite Pure Mesh Exporter",
            },
            "scene": 0,
            "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
            "buffers": [{"byteLength": len(bin_buffer)}],
            "bufferViews": [
                {
                    "buffer": 0,
                    "byteOffset": 0,
                    "byteLength": len(v_bytes),
                    "target": 34962,
                },
                {
                    "buffer": 0,
                    "byteOffset": len(v_bytes),
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
                    "componentType": idx_component_type,
                    "count": len(indices),
                    "type": "SCALAR",
                },
            ],
        }

        json_bytes = json.dumps(gltf_dict, separators=(",", ":")).encode("utf-8")
        while len(json_bytes) % 4 != 0:
            json_bytes += b" "

        # 写 GLB
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
        导出单个零件 GLB，命中缓存则复用

        文件名格式：{清洗后的零件名}__{entry短hash}.glb
          - 可读部分：便于人工识别与调试
          - hash 部分：保证同名零件不冲突
        """
        label_entry = self._get_label_entry_str(label)

        if label_entry in self.asset_cache:
            return self.asset_cache[label_entry]

        shape = self.shape_tool.GetShape(label)
        if shape.IsNull():
            return None

        # 构造文件名
        raw_name = (display_name or "").strip()
        if not raw_name:
            try:
                raw_name = str(label.GetLabelName() or "").strip()
            except Exception:
                raw_name = ""

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
    ) -> Optional[Dict[str, Any]]:
        target_label = instance_label if instance_label else label
        node_id = self._get_label_entry_str(target_label)
        node_name = get_label_name(target_label, fallback_label=label)

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
                        referred_label, instance_label=comp_label
                    )
                    if child_node:
                        children.append(child_node)

            # 单子节点且无自身变换 → 折叠
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
            node = self._extract_tree_node(free_shapes.Value(i))
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
