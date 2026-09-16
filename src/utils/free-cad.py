import os
import sys

try:
    import FreeCAD
    import FreeCADGui
    import Import
    import Part
except ImportError as e:
    print("错误: 无法导入 FreeCAD 模块！请确保使用了 FreeCAD 自带的 python.exe")
    print(f"详细错误: {e}")
    sys.exit(1)


def parse_step_visibility_and_names(file_path):
    if not os.path.exists(file_path):
        print(f"错误: 找不到文件 -> {file_path}")
        return

    print(f"正在加载文件: {file_path} ...\n")

    # 初始化无头 GUI 环境
    FreeCADGui.setupWithoutGUI()

    doc_name = "StepImportDoc"
    if FreeCAD.getDocument(doc_name):
        FreeCAD.closeDocument(doc_name)

    doc = FreeCAD.newDocument(doc_name)

    try:
        Import.insert(file_path, doc.Name)
    except Exception as e:
        print(f"导入 STEP 文件失败: {e}")
        FreeCAD.closeDocument(doc.Name)
        return

    # 遍历文档对象并打印结果
    print("-" * 60)
    print(f"{'对象名称 (Label)':<35} | {'显示状态 (Visibility)':<15}")
    print("-" * 60)

    count = 0
    for obj in doc.Objects:
        name = obj.Label
        is_visible = "未知"
        if hasattr(obj, "ViewObject") and obj.ViewObject:
            is_visible = (
                "显示 (Visible)" if obj.ViewObject.Visibility else "隐藏 (Hidden)"
            )
        print(f"{name:<35} | {is_visible:<15}")
        count += 1

    print("-" * 60)
    print(f"解析完成！共处理了 {count} 个对象。")

    FreeCAD.closeDocument(doc.Name)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使用方法错误！")
        print(
            '正确示例: "C:\\Program Files\\FreeCAD 0.22\\bin\\python.exe"'
            " get_step_status.py xxx.step"
        )
        sys.exit(1)

    step_file_path = sys.argv[1]
    parse_step_visibility_and_names(step_file_path)
