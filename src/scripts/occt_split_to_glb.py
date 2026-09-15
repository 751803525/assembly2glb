import sys
import os

# 检查 FreeCAD 核心模块
try:
    import FreeCAD
    import Part
    import Mesh
except ImportError:
    print(
        "[FREECAD_ERROR] 无法加载 FreeCAD / Part / Mesh 模块，请确保在 freecadcmd 环境中运行！"
    )
    sys.exit(1)


def clean_filename(name):
    """净化节点名称，生成安全的文件名"""
    if not name:
        return "unnamed"
    return "".join([c if (c.isalnum() or c in ("_", "-")) else "_" for c in name])


def export_shape_to_glb(
    shape, output_path, linear_deflection=0.1, angular_deflection=0.5
):
    """
    使用 OCCT 算法对 CAD Shape 进行曲面三角化 (Meshing)，并导出为 GLB
    """
    mesh = Mesh.Mesh()
    mesh_data = shape.tessellate(linear_deflection, angular_deflection)

    for vert in mesh_data[0]:
        mesh.addPoint(vert)
    for facet in mesh_data[1]:
        mesh.addFacet(facet[0], facet[1], facet[2])

    mesh.write(output_path)


def main():
    try:
        argv = sys.argv[sys.argv.index("--") + 1 :]
        input_step = argv[0]
        output_dir = argv[1]
    except Exception as e:
        print(f"[FREECAD_ERROR] 参数传递错误: {e}")
        sys.exit(1)

    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    print(f"[OCCT/FreeCAD] 正在加载 CAD 源文件: {input_step}")

    doc = FreeCAD.newDocument("OCCT_Split_Doc")

    try:
        Part.insert(input_step, doc.Name)
        print(f"[OCCT/FreeCAD] 文件解析成功，共有 {len(doc.Objects)} 个顶级节点/组件。")
    except Exception as err:
        print(f"[FREECAD_ERROR] CAD 文件解析失败: {err}")
        sys.exit(1)

    success_count = 0
    fail_count = 0

    for idx, obj in enumerate(doc.Objects):
        if hasattr(obj, "Shape") and not obj.Shape.isNull():
            raw_name = obj.Label if obj.Label else f"part_{idx}"
            safe_name = clean_filename(raw_name)

            out_glb_name = f"{safe_name}_{idx}.glb"
            out_glb_path = os.path.join(output_dir, out_glb_name)

            print(f"[{idx + 1}/{len(doc.Objects)}] 正在处理 OCCT Shape: {raw_name}")

            try:
                export_shape_to_glb(obj.Shape, out_glb_path, linear_deflection=0.1)
                print(f"  └─ ✅ 成功导出 GLB: {out_glb_name}")
                success_count += 1
            except Exception as exp_err:
                print(f"  └─ ❌ 离散化/导出 GLB 失败: {exp_err}")
                fail_count += 1

    FreeCAD.closeDocument(doc.Name)
    print(
        f"\n[OCCT/FreeCAD] 拆分导出完成！成功: {success_count} 个 | 失败: {fail_count} 个"
    )


if __name__ == "__main__":
    main()
