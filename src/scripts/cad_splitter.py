import os
import sys
import json
import faulthandler
import struct
from typing import Dict, Any, List, Optional, Tuple

faulthandler.enable()

from OCC.Core.STEPCAFControl import STEPCAFControl_Reader
from OCC.Core.TDocStd import TDocStd_Document
from OCC.Core.XCAFDoc import XCAFDoc_DocumentTool
from OCC.Core.TDF import TDF_Label, TDF_Tool, TDF_LabelSequence
from OCC.Core.TCollection import TCollection_AsciiString
from OCC.Core.TDataStd import TDataStd_Name
from OCC.Core.gp import gp_Trsf
from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
from OCC.Core.BRepTools import breptools
from OCC.Core.TopExp import TopExp_Explorer
from OCC.Core.TopAbs import TopAbs_FACE
from OCC.Core.TopoDS import topods
from OCC.Core.BRep import BRep_Tool
from OCC.Core.TopLoc import TopLoc_Location


def logger_info(msg: str):
    print(msg, flush=True)


def logger_err(msg: str):
    print(msg, file=sys.stderr, flush=True)


def get_label_entry_str(label: TDF_Label) -> str:
    entry_str = TCollection_AsciiString()
    TDF_Tool.Entry(label, entry_str)
    return entry_str.ToCString()


def get_label_name(label: TDF_Label, default_name: str = "Node") -> str:
    try:
        name_attr = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID(), name_attr):
            ext_str = name_attr.Get()
            ascii_str = TCollection_AsciiString(ext_str)
            name_str = ascii_str.ToCString()
            if name_str and name_str.strip():
                return name_str.strip()
    except Exception:
        pass
    return default_name


def decompose_trsf(trsf: gp_Trsf) -> Tuple[List[float], List[float], List[float]]:
    t = trsf.TranslationPart()
    position = [t.X(), t.Y(), t.Z()]

    q = trsf.GetRotation()
    quaternion = [q.X(), q.Y(), q.Z(), q.W()]

    s = trsf.ScaleFactor()
    scale = [s, s, s]

    return position, quaternion, scale


def export_shape_to_glb_pure_python(
    shape, output_glb_path: str, deflection: float = 0.2
) -> bool:
    # 1. 离散化网格
    mesh = BRepMesh_IncrementalMesh(shape, deflection, False, 0.5, True)
    mesh.Perform()

    vertex_map = {}  # 坐标去重: (x, y, z) -> global_index
    vertices = []
    indices = []

    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = topods.Face(explorer.Current())
        loc = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation(face, loc)

        if triangulation:
            trsf = loc.Transformation()
            local_to_global_idx = {}

            # 顶点映射与去重 (保留 4 位小数，约 0.1 微米级精度)
            for i in range(1, triangulation.NbNodes() + 1):
                p = triangulation.Node(i)
                if not loc.IsIdentity():
                    p.Transform(trsf)

                coord_key = (round(p.X(), 4), round(p.Y(), 4), round(p.Z(), 4))
                if coord_key not in vertex_map:
                    new_idx = len(vertices) // 3
                    vertex_map[coord_key] = new_idx
                    vertices.extend([p.X(), p.Y(), p.Z()])

                local_to_global_idx[i] = vertex_map[coord_key]

            # 映射三角面索引
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

    # 2. 打包顶点数据 (Float32 / 12 字节每点)
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

    # 3. 动态判断并打包索引数据 (关键修复：对齐 componentType)
    # 顶点数 <= 65535 使用 UNSIGNED_SHORT (5123 / 2字节)，否则使用 UNSIGNED_INT (5125 / 4字节)
    use_uint32 = num_vertices > 65535
    idx_component_type = 5125 if use_uint32 else 5123
    idx_fmt = "<I" if use_uint32 else "<H"

    i_bytes = bytearray()
    for idx in indices:
        i_bytes.extend(struct.pack(idx_fmt, idx))

    # 4. 4 字节边界对齐
    while len(v_bytes) % 4 != 0:
        v_bytes.extend(b"\x00")
    while len(i_bytes) % 4 != 0:
        i_bytes.extend(b"\x00")

    bin_buffer = v_bytes + i_bytes

    # 5. 构建 glTF JSON 元数据结构
    gltf_dict = {
        "asset": {"version": "2.0", "generator": "CADLite Pure Mesh Exporter"},
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
                "target": 34962,  # ARRAY_BUFFER (POSITION)
            },
            {
                "buffer": 0,
                "byteOffset": len(v_bytes),
                "byteLength": len(i_bytes),
                "target": 34963,  # ELEMENT_ARRAY_BUFFER (INDICES)
            },
        ],
        "accessors": [
            {
                "bufferView": 0,
                "byteOffset": 0,
                "componentType": 5126,  # FLOAT
                "count": num_vertices,
                "type": "VEC3",
                "max": max_pos,
                "min": min_pos,
            },
            {
                "bufferView": 1,
                "byteOffset": 0,
                "componentType": idx_component_type,  # 5123(USHORT) 或 5125(UINT)
                "count": len(indices),
                "type": "SCALAR",
            },
        ],
    }

    # JSON 区域按 4 字节对齐 (空格填充)
    json_bytes = json.dumps(gltf_dict, separators=(",", ":")).encode("utf-8")
    while len(json_bytes) % 4 != 0:
        json_bytes += b" "

    # 6. 计算文件头部与 Chunk 长度并写入 GLB
    # Header 12 字节 + JSON Header 8 字节 + JSON Data + BIN Header 8 字节 + BIN Data
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


class StepToGlbConverter:
    def __init__(self, step_path: str, json_path: str, deflection: float = 0.2):
        self.step_path = os.path.abspath(step_path)
        self.json_path = os.path.abspath(json_path)

        self.output_dir = os.path.dirname(self.json_path)
        self.glb_dir = os.path.join(self.output_dir, "glbs")
        self.deflection = deflection

        self.doc = TDocStd_Document("MDTV-XCAF")
        self.shape_tool = None

        # 几何体去重缓存哈希表 key: Label Entry -> val: asset 相对路径
        self.asset_cache: Dict[str, str] = {}

    def _export_atomic_glb_with_cache(self, label: TDF_Label) -> Optional[str]:
        label_entry = get_label_entry_str(label)

        # 【去重校验核心步骤】：如果在缓存中找到，直接复用已导出路径
        if label_entry in self.asset_cache:
            return self.asset_cache[label_entry]

        shape = self.shape_tool.GetShape(label)
        if shape.IsNull():
            return None

        # 用 Label 的 Entry 作为绝对唯一文件名 (如 0_1_1_2.glb)
        safe_filename = label_entry.replace(":", "_") + ".glb"
        glb_path = os.path.join(self.glb_dir, safe_filename)

        status = export_shape_to_glb_pure_python(shape, glb_path, self.deflection)
        breptools.Clean(shape)

        if status:
            logger_info(f"导出：{safe_filename}")
            rel_asset_path = f"glbs/{safe_filename}"
            # 记录到去重缓存
            self.asset_cache[label_entry] = rel_asset_path
            return rel_asset_path
        return None

    def _extract_tree_node(
        self, label: TDF_Label, instance_label: Optional[TDF_Label] = None
    ) -> Optional[Dict[str, Any]]:
        target_label = instance_label if instance_label else label
        node_id = get_label_entry_str(target_label)
        node_name = get_label_name(target_label, default_name="Node")
        is_asm = self.shape_tool.IsAssembly(label)

        position = [0.0, 0.0, 0.0]
        quaternion = [0.0, 0.0, 0.0, 1.0]
        scale = [1.0, 1.0, 1.0]

        if instance_label:
            try:
                loc = self.shape_tool.GetLocation(instance_label)
                if not loc.IsIdentity():
                    position, quaternion, scale = decompose_trsf(loc.Transformation())
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
                    child_node = self._extract_tree_node(
                        referred_label, instance_label=comp_label
                    )
                    if child_node:
                        children.append(child_node)

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
        else:
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
            # 调用带去重校验的导出函数
            glb_path = self._export_atomic_glb_with_cache(label)
            node["asset"] = glb_path

        for child in node.get("children", []):
            self._generate_glbs_recursive(child)

    def run(self):
        logger_info(f"正在载入 STEP 文件")
        reader = STEPCAFControl_Reader()
        reader.SetColorMode(True)
        reader.SetNameMode(True)
        if reader.ReadFile(self.step_path) != 1:
            raise RuntimeError("STEP 文件解析失败")

        reader.Transfer(self.doc)
        self.shape_tool = XCAFDoc_DocumentTool.ShapeTool(self.doc.Main())

        free_shapes = TDF_LabelSequence()
        self.shape_tool.GetFreeShapes(free_shapes)

        if free_shapes.Length() == 0:
            raise RuntimeError("未找到有效的根拓扑模型")

        logger_info("提取模型结构树")
        if free_shapes.Length() == 1:
            root_tree = self._extract_tree_node(free_shapes.Value(1))
        else:
            children = []
            for i in range(1, free_shapes.Length() + 1):
                node = self._extract_tree_node(free_shapes.Value(i))
                if node:
                    children.append(node)
            root_tree = {
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

        os.makedirs(self.glb_dir, exist_ok=True)
        logger_info(f"生成叶子节点 GLB 模型")
        self._generate_glbs_recursive(root_tree)
        logger_info(f"模型导出完成！共生成{len(self.asset_cache)}个独立 GLB 文件。")
        os.makedirs(self.output_dir, exist_ok=True)
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(root_tree, f, ensure_ascii=False, indent=2)

        logger_info(f"成功导出结构树文件: {os.path.basename(self.json_path)}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "用法: python cad_splitter.py <STEP文件路径> <JSON输出完整路径> [deflection精度, 默认0.2]"
        )
        sys.exit(1)

    step_input = sys.argv[1]
    json_output = sys.argv[2]

    # 可选参数解析：若传入第 3 个参数则转为 float，否则默认 0.2
    deflection_val = 0.2
    if len(sys.argv) >= 4:
        try:
            deflection_val = float(sys.argv[3])
        except ValueError:
            logger_info(
                f"传入的 deflection 参数 '{sys.argv[3]}' 无效，将使用默认值 0.2"
            )
    converter = StepToGlbConverter(step_input, json_output, deflection=deflection_val)
    converter.run()
