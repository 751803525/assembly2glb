"""
关键 API 探测脚本（临时调试用，发布前可删除）

用途：
    锁版本调试阶段，逐个探测业务代码实际依赖的关键 API 是否存在于
    当前 pythonocc-core 版本中。

用法：
    python probe-key-apis.py

输出：
    每行格式 `API_NAME=OK` / `API_NAME=MISSING` / `API_NAME=ERROR(异常类型)`
    退出码始终为 0（探测本身不算失败，方便作为信息收集工具）

依赖：
    仅需标准库 + pythonocc-core
"""
import sys


def probe(tag: str, expr: str) -> None:
    """执行一个表达式，把结果按统一格式打印"""
    try:
        ok = eval(expr)  # noqa: S307 —— 探测脚本，表达式固定可控
        print(f"{tag}={'OK' if ok else 'MISSING'}")
    except Exception as e:
        print(f"{tag}=ERROR({type(e).__name__})")


def main() -> None:
    print("=" * 60)
    print("关键 API 探测 - pythonocc-core 环境诊断")
    print("=" * 60)

    # --- 基本信息 ---
    try:
        import OCC
        print(f"OCC.VERSION={OCC.VERSION}")
    except Exception as e:
        print(f"OCC.VERSION=ERROR({type(e).__name__})")

    try:
        print(f"PYTHON={sys.version.split()[0]}")
    except Exception:
        pass

    print("-" * 60)

    # --- 名字读取链路（cad-splitter.py 核心依赖） ---
    probe("TDataStd_Name.Get", "hasattr(__import__('OCC.Core.TDataStd', fromlist=['TDataStd_Name']).TDataStd_Name, 'Get')")
    probe("TDataStd_Name.GetID_s", "hasattr(__import__('OCC.Core.TDataStd', fromlist=['TDataStd_Name']).TDataStd_Name, 'GetID_s')")
    probe("TDataStd_Name.GetID", "hasattr(__import__('OCC.Core.TDataStd', fromlist=['TDataStd_Name']).TDataStd_Name, 'GetID')")
    probe("TDataStd_GenericExtString.DownCast", "hasattr(__import__('OCC.Core.TDataStd', fromlist=['TDataStd_GenericExtString']).TDataStd_GenericExtString, 'DownCast')")

    # --- XCAF 结构树链路 ---
    probe("XCAFDoc_DocumentTool.ShapeTool", "hasattr(__import__('OCC.Core.XCAFDoc', fromlist=['XCAFDoc_DocumentTool']).XCAFDoc_DocumentTool, 'ShapeTool')")
    probe("TDF_Label.FindAttribute", "hasattr(__import__('OCC.Core.TDF', fromlist=['TDF_Label']).TDF_Label, 'FindAttribute')")
    probe("TDF_LabelSequence.Length", "hasattr(__import__('OCC.Core.TDF', fromlist=['TDF_LabelSequence']).TDF_LabelSequence, 'Length')")
    probe("TDF_AttributeIterator", "hasattr(__import__('OCC.Core.TDF', fromlist=['TDF_AttributeIterator']).TDF_AttributeIterator, 'More')")

    # --- STEP 读取链路 ---
    probe("STEPCAFControl_Reader.ReadFile", "hasattr(__import__('OCC.Core.STEPCAFControl', fromlist=['STEPCAFControl_Reader']).STEPCAFControl_Reader, 'ReadFile')")
    probe("STEPCAFControl_Reader.SetNameMode", "hasattr(__import__('OCC.Core.STEPCAFControl', fromlist=['STEPCAFControl_Reader']).STEPCAFControl_Reader, 'SetNameMode')")
    probe("STEPCAFControl_Reader.Transfer", "hasattr(__import__('OCC.Core.STEPCAFControl', fromlist=['STEPCAFControl_Reader']).STEPCAFControl_Reader, 'Transfer')")

    # --- 三角化 & 导出链路 ---
    probe("BRepMesh_IncrementalMesh.Perform", "hasattr(__import__('OCC.Core.BRepMesh', fromlist=['BRepMesh_IncrementalMesh']).BRepMesh_IncrementalMesh, 'Perform')")
    probe("BRep_Tool.Triangulation", "hasattr(__import__('OCC.Core.BRep', fromlist=['BRep_Tool']).BRep_Tool, 'Triangulation')")
    probe("TopExp_Explorer.More", "hasattr(__import__('OCC.Core.TopExp', fromlist=['TopExp_Explorer']).TopExp_Explorer, 'More')")
    probe("TopoDS.Face", "hasattr(__import__('OCC.Core.TopoDS', fromlist=['topods']).topods, 'Face')")

    # --- 变换链路 ---
    probe("gp_Trsf.TranslationPart", "hasattr(__import__('OCC.Core.gp', fromlist=['gp_Trsf']).gp_Trsf, 'TranslationPart')")
    probe("gp_Trsf.GetRotation", "hasattr(__import__('OCC.Core.gp', fromlist=['gp_Trsf']).gp_Trsf, 'GetRotation')")
    probe("gp_Trsf.ScaleFactor", "hasattr(__import__('OCC.Core.gp', fromlist=['gp_Trsf']).gp_Trsf, 'ScaleFactor')")

    print("=" * 60)
    print("探测完成")
    print("=" * 60)


if __name__ == "__main__":
    main()