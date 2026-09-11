#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
CAD 装配结构树与变换矩阵提取工具 (纯轻量解析版)

功能说明：
1. 读取 STEP 文件，递归解析其层次装配树 (Assembly Tree)。
2. 提取每个节点的元数据：ID、节点名称、类型 (assembly / part)。
3. 提取局部变换矩阵 (Transformation Matrix / 16位 4x4 矩阵)，方便前端渲染引擎 (如 Three.js / Babylon.js) 进行实例化布局。
4. 不触发任何子 STEP / BREP 的几何导出，执行速度极快且高度稳定。
"""

import os
import sys
import json
import faulthandler
from typing import Dict, Any, List, Optional

# 开启 C++ 底层崩溃/段错误堆栈捕获
faulthandler.enable()

# --- OpenCASCADE 依赖导入 ---
from OCC.Core.STEPCAFControl import STEPCAFControl_Reader
from OCC.Core.TDocStd import TDocStd_Document
from OCC.Core.XCAFApp import XCAFApp_Application
from OCC.Core.XCAFDoc import XCAFDoc_ShapeTool
from OCC.Core.XCAFDoc import XCAFDoc_DocumentTool
from OCC.Core.TDF import TDF_Label, TDF_Tool, TDF_LabelSequence
from OCC.Core.TCollection import TCollection_AsciiString
from OCC.Core.TDataStd import TDataStd_Name
from OCC.Core.TopLoc import TopLoc_Location
from OCC.Core.gp import gp_Trsf
from datetime import datetime

# ==============================================================================
# 1. OpenCASCADE 辅助提取函数
# ==============================================================================


def logger_info(message):
    print(message, flush=True)


def logger_err(message):
    print(message, file=sys.stderr, flush=True)


def get_label_entry_str(label: TDF_Label) -> str:
    """获取 TDF_Label 的唯一拓扑路径标识 (例如 '0:1:1:1')"""
    entry_str = TCollection_AsciiString()
    TDF_Tool.Entry(label, entry_str)
    return entry_str.ToCString()


def get_label_name(label: TDF_Label, default_name: str = "Node") -> str:
    """提取 CAD 节点的名称 (Name Attribute)"""
    name_attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID(), name_attr):
        name = name_attr.Get().ToCString()
        if name and name.strip():
            return name.strip()
    return default_name


def extract_matrix_16(trsf: gp_Trsf) -> List[float]:
    """
    将 OpenCASCADE 的 gp_Trsf (3x4 空间变换对象) 转换为标准的 4x4 行优先 (Row-Major) 16位矩阵列表。
    适合前端 Three.js / Babylon.js / WebGL 渲染引擎直接加载使用。
    """
    mat = trsf.VectorialPart()  # 3x3 旋转缩放部分
    tr = trsf.TranslationPart()  # 1x3 平移向量

    # 展开为 4x4 变换矩阵 (Row-Major 行优先)
    return [
        mat.Value(1, 1),
        mat.Value(1, 2),
        mat.Value(1, 3),
        tr.X(),
        mat.Value(2, 1),
        mat.Value(2, 2),
        mat.Value(2, 3),
        tr.Y(),
        mat.Value(3, 1),
        mat.Value(3, 2),
        mat.Value(3, 3),
        tr.Z(),
        0.0,
        0.0,
        0.0,
        1.0,
    ]


# ==============================================================================
# 2. 结构树解析核心类
# ==============================================================================


class CadTreeExtractor:
    def __init__(self, step_path: str, output_json_path: str):
        self.step_path = os.path.abspath(step_path)
        self.output_json_path = os.path.abspath(output_json_path)

        self.doc = None
        self.shape_tool = None

    def load_step(self, step_path: str) -> STEPCAFControl_Reader:
        """读取 STEP 并创建底层 XCAF 文档结构"""

        if not os.path.exists(step_path):
            raise FileNotFoundError(f"找不到输入的 STEP 文件: {step_path}")
        logger_info(f"正在加载主 STEP 文件: {step_path}")
        reader = STEPCAFControl_Reader()
        reader.SetColorMode(True)
        reader.SetNameMode(True)

        status = reader.ReadFile(step_path)
        logger_info(f"STEP ReadFile 状态码: {status}")
        if status != 1:
            raise RuntimeError(f"STEP 文件读取失败，状态码: {status}")
        else:
            return reader

    def get_doc(self) -> TDocStd_Document:
        if self.doc is None:
            self.doc = TDocStd_Document("MDTV-XCAF")
        return self.doc

    def get_shape_tool(self, doc: STEPCAFControl_Reader) -> XCAFDoc_ShapeTool:
        if self.shape_tool is None:
            self.shape_tool = XCAFDoc_DocumentTool.ShapeTool(doc)
        return self.shape_tool

    def build_node_tree(self, free_shapes: TDF_LabelSequence):
        # for(i=0;i<free_shapes.Length();i++) occt 模型遍历从 1 开始 最大到 len
        return []

    def run(self) -> Dict[str, Any]:
        """执行全流程"""
        # 加载 STEP
        reader = self.load_step(self.step_path)
        # 初始化文档对象
        doc = self.get_doc()
        # 将加载的文件转为文档
        logger_info("转为文档")
        reader.Transfer(doc)
        logger_info("转换完成")
        # 初始化 ShapeTool
        shape_tool = self.get_shape_tool(doc.Main())
        # 获取装配整体根节点
        free_shapes = TDF_LabelSequence()
        shape_tool.GetFreeShapes(free_shapes)
        logger_info(f"成功识别装配根节点数量 (FreeShapes): {free_shapes.Length()}")
        if free_shapes.Length() == 0:
            raise RuntimeError("STEP 文件中未提取到任何有效几何根节点！")
        node_tree = self.build_node_tree(free_shapes)
        return {doc, node_tree}


# ==============================================================================
# 3. 命令行主入口
# ==============================================================================

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python cad_splitter.py <输入STEP文件路径> <输出装配树JSON路径>")
        sys.exit(1)

    input_step_arg = sys.argv[1]
    output_json_arg = sys.argv[2]
    try:
        extractor = CadTreeExtractor(
            step_path=input_step_arg, output_json_path=output_json_arg
        )
        extractor.run()
    except Exception as err:
        logger_err(f"执行失败，错误信息: {str(err)}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
