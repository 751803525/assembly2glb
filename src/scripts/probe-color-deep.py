"""
全面探测 STEP 里的颜色信息

用法：
    python probe-color-deep.py <STEP文件>

尝试多个途径：
    1. XCAFDoc_ColorTool.GetColor（shape color）
    2. XCAFDoc_ColorTool.GetInstanceColor（instance color）
    3. XCAFDoc_VisMaterialTool（新材质系统）
    4. 遍历 label 上所有 attribute，找含 "Color" / "Material" 的
    5. STEP 文本里搜 COLOUR 相关实体
"""

import sys
from collections import Counter

from OCC.Core.STEPCAFControl import STEPCAFControl_Reader
from OCC.Core.TDocStd import TDocStd_Document
from OCC.Core.XCAFDoc import XCAFDoc_DocumentTool
from OCC.Core.TDF import (
    TDF_Label,
    TDF_LabelSequence,
    TDF_AttributeIterator,
    TDF_Tool,
)
from OCC.Core.TCollection import TCollection_AsciiString
from OCC.Core.Quantity import Quantity_Color


def entry_of(label):
    s = TCollection_AsciiString()
    TDF_Tool.Entry(label, s)
    return s.ToCString()


def name_of(label):
    try:
        return str(label.GetLabelName() or "")
    except Exception:
        return ""


def main(step_path):
    print("=" * 70)
    print(f"探测文件: {step_path}")
    print("=" * 70)

    # ---------- STEP 文本里搜颜色实体 ----------
    print("\n[1] STEP 文本里的颜色相关实体（前 20 行）")
    try:
        hits = Counter()
        with open(step_path, "rb") as f:
            for lineno, raw in enumerate(f, 1):
                line = raw.decode("utf-8", errors="ignore")
                for kw in (
                    "COLOUR_RGB",
                    "COLOUR_SPECIFICATION",
                    "DRAUGHTING_PRE_DEFINED_COLOUR",
                    "STYLED_ITEM",
                    "PRESENTATION_STYLE_ASSIGNMENT",
                    "SURFACE_STYLE_FILL_AREA",
                    "FILL_AREA_STYLE_COLOUR",
                ):
                    if kw in line:
                        hits[kw] += 1
                        if hits[kw] <= 3:
                            print(f"  行{lineno}: {line.strip()[:120]}")
        print(f"  命中统计: {dict(hits)}")
    except Exception as e:
        print(f"  文本搜索失败: {e}")

    # ---------- OCCT 侧遍历 ----------
    print("\n[2] OCCT 侧：载入 STEP 并遍历所有 label")
    doc = TDocStd_Document("MDTV-XCAF")
    reader = STEPCAFControl_Reader()
    reader.SetColorMode(True)
    reader.SetNameMode(True)
    reader.SetLayerMode(True)
    reader.SetMatMode(True)
    reader.SetGDTMode(True)
    if reader.ReadFile(step_path) != 1:
        print("  ReadFile 失败")
        return
    reader.Transfer(doc)

    shape_tool = XCAFDoc_DocumentTool.ShapeTool(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool(doc.Main())

    # 尝试拿 VisMaterialTool（不同版本 API 名可能不同）
    vis_mat_tool = None
    try:
        vis_mat_tool = XCAFDoc_DocumentTool.VisMaterialTool(doc.Main())
        print("  VisMaterialTool: 可用")
    except Exception as e:
        print(f"  VisMaterialTool: 不可用 ({e})")

    free = TDF_LabelSequence()
    shape_tool.GetFreeShapes(free)

    counter = {
        "total": 0,
        "shape_color_hit": 0,
        "inst_color_hit": 0,
        "name_attr_hit": 0,
    }
    color_values = Counter()
    attr_types = Counter()

    def dump_attrs(label):
        """遍历 label 上所有 attribute，统计类型"""
        it = TDF_AttributeIterator(label)
        while it.More():
            a = it.Value()
            try:
                tn = a.DynamicType().Name()
                if hasattr(tn, "ToCString"):
                    tn = tn.ToCString()
                tn = str(tn)
                attr_types[tn] += 1
            except Exception:
                pass
            it.Next()

    def walk(label, depth=0):
        counter["total"] += 1
        nm = name_of(label)
        en = entry_of(label)

        dump_attrs(label)

        # --- 试 shape color 三种类型 ---
        hit_shape = False
        for t in (0, 1, 2):
            c = Quantity_Color()
            try:
                if color_tool.GetColor(label, t, c):
                    r, g, b = c.Red(), c.Green(), c.Blue()
                    if depth <= 2:
                        print(
                            f"  {'  '*depth}[{nm}] SHAPE_COLOR t={t} = ({r:.3f},{g:.3f},{b:.3f})"
                        )
                    counter["shape_color_hit"] += 1
                    color_values[(round(r, 3), round(g, 3), round(b, 3))] += 1
                    hit_shape = True
                    break
            except Exception:
                pass

        # --- 试 instance color（需要 shape）---
        if not hit_shape and not shape_tool.IsAssembly(label):
            try:
                shape = shape_tool.GetShape(label)
                if not shape.IsNull():
                    for t in (0, 1, 2):
                        c = Quantity_Color()
                        try:
                            if color_tool.GetInstanceColor(shape, t, c):
                                r, g, b = c.Red(), c.Green(), c.Blue()
                                if depth <= 2:
                                    print(
                                        f"  {'  '*depth}[{nm}] INST_COLOR t={t} = ({r:.3f},{g:.3f},{b:.3f})"
                                    )
                                counter["inst_color_hit"] += 1
                                color_values[
                                    (round(r, 3), round(g, 3), round(b, 3))
                                ] += 1
                                hit_shape = True
                                break
                        except Exception:
                            pass
            except Exception:
                pass

        # --- VisMaterialTool ---
        if vis_mat_tool is not None:
            try:
                mat = vis_mat_tool.GetMaterial(label)
                if mat is not None:
                    if depth <= 2:
                        print(f"  {'  '*depth}[{nm}] VIS_MATERIAL = {mat}")
            except Exception:
                pass

        # --- 递归子节点 ---
        if shape_tool.IsAssembly(label):
            comps = TDF_LabelSequence()
            shape_tool.GetComponents(label, comps)
            for i in range(1, comps.Length() + 1):
                comp = comps.Value(i)
                ref = TDF_Label()
                if shape_tool.GetReferredShape(comp, ref):
                    walk(ref, depth + 1)

    for i in range(1, free.Length() + 1):
        walk(free.Value(i))

    # ---------- 汇总 ----------
    print("\n[3] 统计结果")
    print(f"  遍历标签总数: {counter['total']}")
    print(f"  SHAPE_COLOR 命中: {counter['shape_color_hit']}")
    print(f"  INST_COLOR 命中: {counter['inst_color_hit']}")

    print("\n[4] 出现过的颜色值（top 20）")
    for (r, g, b), n in color_values.most_common(20):
        print(f"  ({r},{g},{b}) × {n}")

    print("\n[5] 所有 label 上出现过的 attribute 类型")
    for tn, n in attr_types.most_common():
        print(f"  {tn}: {n}")

    print("\n探测完成")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python probe-color-deep.py <STEP文件>")
        sys.exit(1)
    main(sys.argv[1])
