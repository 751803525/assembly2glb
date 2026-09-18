# assembly2glb

> 工业 CAD 装配体 → glTF/GLB 轻量化流水线

[![npm version](https://img.shields.io/npm/v/@chanleeeee/assembly2glb)](https://www.npmjs.com/package/@chanleeeee/assembly2glb)
[![node](https://img.shields.io/badge/node-%3E%3D18-brightgreen)](https://nodejs.org/)

把工业级 **STEP** 装配体转换为 Web 3D 可直接使用的 **glTF/GLB** 资源，同时产出**完整的结构树 JSON**。

针对大型工业装配体（数百 MB STEP、上万零件）做了如下优化：

- **流式处理**：内存占用与模型规模解耦，不炸内存
- **几何实例去重**：同一 PRODUCT 定义的多次实例化只导出一次
- **字节级去重**：内容完全相同的零件自动合并
- **单位探测**：自动识别 mm / inch 单位，坐标转换到米（glTF 规范）
- **平滑法线**：从解析曲面逐顶点计算，带硬边保持
- **离线运行**：解析阶段完全本地执行，不依赖任何云服务

---

## 特性

| 特性 | 说明 |
|---|---|
| 📥 输入格式 | STEP (`.step` / `.stp`) |
| 📤 输出格式 | glTF 2.0 二进制（`.glb`）+ 结构树 `.json` |
| 🧠 解析引擎 | OpenCASCADE 7.7.2（via pythonocc-core） |
| ⚡ 减面 | 基于 `meshoptimizer` 的高性能网格简化 |
| 🧩 去重 | 字节级指纹（sha1）自动合并重复零件 |
| 🎯 精度控制 | 可调 deflection 参数 |
| 🌊 流式合并 | 常量级内存，支持 GB 级装配 |
| 🔒 环境隔离 | 自动通过 Conda 创建独立的 `cad_env` |

---

## 安装

### 前置依赖

| 依赖 | 版本要求 | 说明 |
|---|---|---|
| Node.js | ≥ 18 | 运行 CLI |
| Conda | Miniconda / Anaconda 任一 | 首次运行时会自动创建 `cad_env` 环境并安装 pythonocc-core 7.7.2 |
| 磁盘 | ≥ 3 GB 空闲 | Conda 环境占用 |

### 安装 assembly2glb

```bash
npm install -g assembly2glb
```

或者用 pnpm：

```bash
pnpm add -g assembly2glb
```

### 首次运行

首次执行时，会自动：

1. 检测系统 Conda
2. 创建 `cad_env` 环境（Python 3.11）
3. 安装 `pythonocc-core==7.7.2` + `occt==7.6.3`
4. 校验 OCC 功能完整性

**这一步约需 3~10 分钟**，取决于网络。之后的运行会复用环境。

---

## 快速开始

### 完整流水线（解析 + 去重 + 合并）

```bash
assembly2glb process \
  -i ./middle-frame.stp \
  -o ./output \
  -m all \
  -d
```

### 输出目录结构

```
output/
├──SH1_904_403891210001_ASM.glb   # 完整装配 GLB
├── assembly-tree.json      # 结构树
└── glbs/                   # 每个零件的独立 GLB
    ├── 中间台体__9a957632.glb
    ├── 螺钉_M6__a1b2c3d4.glb
    └── ...
```

---

## CLI 命令

### `assembly2glb process`

工业 CAD 轻量化流水线（默认仅执行解析，通过 `-s` / `-d` / `-m` 添加更多步骤）。

```
Usage: assembly2glb process [options]

Options:
  -i, --input <path>        输入 STEP 文件路径
  -o, --output <dir>        输出目录（默认: ./output）
  -p, --precision [number]  导出模型精度（deflection，默认 0.2）
  -s, --simplify [number]   执行减面操作（比例 0~1，例如 0.5 表示保留 50% 三角面）
  -d, --dedup               执行去重操作
  -m, --merge [string]      模型合并输出（可选值：split / merge / all）
  --keep-temp               保留临时文件（调试用）
  -h, --help                显示帮助
```

参数详解：

| 参数 | 说明 |
|---|---|
| `-i` | STEP 文件路径，支持绝对/相对路径 |
| `-o` | 输出根目录，所有产物都在此目录下 |
| `-p` | 三角化精度。数值越小越精细（三角形越多、文件越大）。默认 0.2 |
| `-s` | 减面比例。`0.3` 表示减到 30%。不传则不减面 |
| `-d` | 是否执行去重（同一内容零件合并） |
| `-m` | 合并模式：`split`=仅拆分、`merge`=仅合并、`all`=全部 |

---

## 使用示例

### 示例 1：只解析 STEP，导出零件集合

```bash
assembly2glb process -i ./assembly.stp -o ./out -p 0.1
```

产物：`out/convert/convert-split-part/assembly-tree.json` + `glbs/` 目录。

### 示例 2：解析 + 去重 + 合并

```bash
assembly2glb process \
  -i ./assembly.stp \
  -o ./out \
  -p 0.1 \
  -d \
  -m all
```

产物：`out/merge/{顶层零件名}.glb`，可直接拖进 three.js / Babylon.js / 模型查看器。

### 示例 3：只合并已有结果（不重新解析）

如果你的 `dedup/` 或 `convert-split-part/` 已经存在：

```bash
assembly2glb process -i ./assembly.stp -o ./out -m merge
```

---

## 结构树 JSON 格式

```json
{
  "id": "0:1:1:1",
  "name": "SH1_904_403891210001_ASM",
  "type": "assembly",
  "transform": {
    "position": [0.0, 0.0, 0.0],
    "quaternion": [0.0, 0.0, 0.0, 1.0],
    "scale": [1.0, 1.0, 1.0]
  },
  "children": [
    {
      "id": "0:1:1:1:1",
      "name": "114040型材1190001110104-0105",
      "type": "part",
      "transform": {},
      "children": [],
      "asset": "glbs/114040型材1190001110104-0105__9a957632.glb"
    }
  ],
  "asset": null
}
```

| 字段 | 说明 |
|---|---|
| `id` | OCC 内部 label entry，全局唯一 |
| `name` | 零件名（STEP 里 PRODUCT 的名称；无名字时退化为 `{父名}_{index}`） |
| `type` | `assembly` 或 `part` |
| `transform` | 相对父节点的 TRS 变换（已换算到米） |
| `children` | 子节点（`part` 类型的 `children` 恒为空） |
| `asset` | 零件 GLB 的相对路径（`assembly` 类型为 `null`） |

前端加载建议：

```typescript
const tree = await fetch('assembly-tree.json').then(r => r.json());

// 每个 part 节点的 asset 是相对路径，可以据此加载对应的 GLB
function collectAssets(node: any): string[] {
  if (node.type === 'part' && node.asset) return [node.asset];
  return (node.children ?? []).flatMap(collectAssets);
}
```

---

## 环境管理

### `cad_env` 在哪？

默认位置：

- Windows：`{conda_base}\envs\cad_env` 或 `%USERPROFILE%\.conda\envs\cad_env`
- macOS / Linux：`~/miniconda3/envs/cad_env`

### 手动重建环境

如果环境损坏（比如 OCC 版本不对），删掉重跑即可：

```bash
conda env remove -n cad_env -y
assembly2glb process -i ./x.stp -o ./out   # 会自动重建
```

### 切换 Python / OCC 版本

环境检测逻辑在 `src/core/steps/env-check.ts`。如果你需要不同版本，改这两个常量：

```typescript
const PYTHON_VERSION = '3.11';
const PYTHONOCC_VERSION = '7.7.2';
```

注意：`pythonocc-core` 的 SWIG 绑定质量参差不齐。**7.7.2 是当前经过验证的稳定版本**，7.9.0 及以上存在 `TDataStd_Name.Get` 缺失的问题（详见 FAQ Q2）。

---

## 常见问题

### Q1. 首次运行卡在"创建环境"很久

Conda 装包需要拉 Python + OCCT 等几十个大包。用国内镜像可以加速：

```bash
conda config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge/
conda config --set channel_priority strict
```

### Q2. 报错 `TDataStd_Name.Get 方法缺失`

这是因为环境里装的是 pythonocc-core ≥ 7.9.0，SWIG 绑定有回归。执行：

```bash
conda env remove -n cad_env -y
```

然后再跑一次 `assembly2glb`，会自动装 7.7.2。

### Q3. 输出的 GLB 在浏览器里显示特别大/特别小

正常情况下会自动把坐标换算成米。如果你看到模型尺寸明显不对（大了/小了 1000 倍），说明 STEP 文件里没有写单位声明，走了 mm 兜底。请在项目里开 issue 附上 STEP 文件头 200 行。

### Q4. Windows 下报 `Errno 2` 或 `EBUSY`

这是 Windows 文件系统的目录项缓存延迟 + 杀软扫描导致的。已经做了以下防御：

- 读写路径统一使用 `abspath`（不用 `realpath`，避免短/长路径混用）
- 写入后 `fsync` 强制落盘
- 目录清理使用 `rmSync` 同步调用（防止异步竞态）

如果依然遇到，尝试给 `%TEMP%\assembly2glb\` 加杀软白名单。

### Q5. 输出的 GLB 比 FreeCAD 导出的大/小

- FreeCAD 默认 deflection 更小（约 0.05），三角形数量更多，文件更大
- assembly2glb 默认 `0.2`，用 `-p 0.05` 可对齐
- 会做几何实例复用，文件通常比 FreeCAD 更小

### Q6. 一个零件都没导出 / 全叫 `Node`

说明 STEP 里没有 PRODUCT name。会退化为 `{父名}_{index}` 命名，保证不重名。

### Q7. 支持 IGES / BREP 吗？

当前只支持 STEP。IGES、BREP 在 OCC 层面也能读（`IGESCAFControl_Reader` / `BRepTools`），后续会陆续支持。

---

## 已知限制

- 仅支持 STEP：IGES / BREP 尚未接入
- 材质/颜色：STEP 里有材质会被读取，但很多 CAD 导出时会覆盖成单一材质
- 无 UV：当前不导出纹理坐标（工业 CAD 一般也不需要）
- 无 Draco 压缩：如果需要更小的文件，可用 [gltfpack](https://github.com/zeux/meshoptimizer#gltfpack) 后处理

---

## 开发

```bash
# 安装依赖
pnpm install

# 开发模式（tsx 直跑 TS）
pnpm dev process -i ./test.stp -o ./out

# 构建
pnpm run build

# 跑构建产物
pnpm run start process -i ./test.stp -o ./out
```

### 构建产物

```
dist/
├── index.js                    # CLI 入口
├── core/                       # 业务逻辑
│   ├── pipeline/
│   └── steps/
├── utils/
└── scripts/                    # Python 脚本（打包时一起复制）
    ├── cad-splitter.py
    ├── dedup.py
    ├── merge-glb.py
    └── verify-occ.py
```

Python 脚本以原文件形式随包分发，运行时由 `cad_env` 里的 Python 执行。

---

## 贡献

欢迎提 issue 和 PR。提交前请确保：

- `pnpm run lint` 通过
- `pnpm run build` 通过

---

## License

[MIT](./LICENSE) © chanleeeee