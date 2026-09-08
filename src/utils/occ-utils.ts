import fs from 'fs-extra';
import path from 'path';
import initocctimportjs from 'occt-import-js';
import { Document, NodeIO } from '@gltf-transform/core';
import { simplify, dedup, prune } from '@gltf-transform/functions';
import { MeshoptSimplifier } from 'meshoptimizer';

const io = new NodeIO();

// 存储全局初始化过的 occt 实例，避免重复加载 WASM
let occtInstance: any = null;

async function getOcct() {
  if (!occtInstance) {
    occtInstance = await initocctimportjs();
  }
  return occtInstance;
}

export interface Vector3 {
  x: number;
  y: number;
  z: number;
}

export interface Quaternion {
  x: number;
  y: number;
  z: number;
  w: number;
}

export interface AssemblyNode {
  id: string; // 唯一节点标识
  name: string | null; // 节点名称，AP203 无名称时回退为 null 或兜底名
  meshIndex: number | null; // 对应离散几何体索引（用于第二步导出 GLB），非叶子节点为 null
  position: Vector3; // 平移
  rotation: Quaternion; // 旋转四元数
  scale: Vector3; // 缩放
  matrix: number[]; // 16位原生 4x4 变换矩阵
  children: AssemblyNode[];
}

/**
 * 提取结构树和 位置/旋转/缩放 信息 (Extract Assembly Tree)
 * @param stepPath STEP 文件路径
 * @returns tree 数组，保证顶层统一为 [ Node1, Node2 ... ] 格式
 */
export async function extractAssemblyTree(stepPath: string): Promise<{
  tree: AssemblyNode[];
  totalMeshesCount: number;
}> {
  const occt = await getOcct();
  const fileBuffer = await fs.readFile(stepPath);

  // 解析 STEP 文件
  const result = occt.ReadStepFile(fileBuffer, {
    linearUnit: 'mm',
    linearDeflection: 5.0, // 放宽到 2.0mm 甚至 5.0mm（超大装配体推荐 5.0）
    angularDeflection: 1.0, // 放宽角度偏差
  });

  if (!result || !result.success) {
    throw new Error(`解析 STEP 文件失败或文件损坏: ${stepPath}`);
  }

  let autoNodeIdCounter = 0;

  /**
   * 校验节点是否可见
   * 兼容 AP203/AP214/AP242 不同协议下的可见性标识
   */
  function isNodeVisible(occtNode: any): boolean {
    if (occtNode.visible !== undefined && occtNode.visible === false) return false;
    if (occtNode.isVisible !== undefined && occtNode.isVisible === false) return false;
    return true;
  }

  /**
   * 递归解析单个节点
   */
  function parseNode(occtNode: any): AssemblyNode | null {
    // 1. 过滤不可见节点
    if (!isNodeVisible(occtNode)) {
      return null;
    }

    // 2. 变换矩阵解析与分解（AP203 缺省时赋予单位矩阵）
    const matrix =
      Array.isArray(occtNode.matrix) && occtNode.matrix.length === 16
        ? occtNode.matrix
        : [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];

    const transform = decomposeMatrix(matrix);

    // 3. AP203 属性兼容（处理空名称与 ID 生成）
    const rawName = occtNode.name ? String(occtNode.name).trim() : '';
    const nodeName = rawName.length > 0 ? rawName : null;
    const nodeId = occtNode.uuid || `node_${autoNodeIdCounter++}`;

    // 4. Mesh 几何数据索引绑定
    const meshIndex =
      typeof occtNode.mesh === 'number' && occtNode.mesh >= 0 ? occtNode.mesh : null;

    // 5. 递归处理子节点
    const children: AssemblyNode[] = [];
    if (Array.isArray(occtNode.children) && occtNode.children.length > 0) {
      for (const child of occtNode.children) {
        const parsedChild = parseNode(child);
        if (parsedChild) {
          children.push(parsedChild);
        }
      }
    }

    return {
      id: nodeId,
      name: nodeName,
      meshIndex: meshIndex,
      position: transform.position,
      rotation: transform.rotation,
      scale: transform.scale,
      matrix: matrix,
      children: children,
    };
  }

  // 统一输出为数组形式
  const rootNodes: AssemblyNode[] = [];

  if (result.root) {
    const parsedRoot = parseNode(result.root);
    if (parsedRoot) rootNodes.push(parsedRoot);
  } else if (Array.isArray(result.roots)) {
    for (const rootItem of result.roots) {
      const parsedRoot = parseNode(rootItem);
      if (parsedRoot) rootNodes.push(parsedRoot);
    }
  }

  return {
    tree: rootNodes,
    totalMeshesCount: result.meshes ? result.meshes.length : 0,
  };
}

/**
 * 根据 Mesh ID 提取并导出原始未减面的 GLB (Export Raw GLB)
 * @param stepPath STEP 文件路径
 * @param meshId 第一步结构树中对应的 meshId
 * @param outputPath 输出的 .glb 文件路径
 */
export async function exportPartGlb(
  stepPath: string,
  meshId: number,
  outputPath: string
): Promise<string> {
  const occt = await getOcct();
  const fileBuffer = fs.readFileSync(stepPath);

  const result = occt.ReadStepFile(fileBuffer, {
    linearDeflection: 0.1,
    angularDeflection: 0.1,
  });

  if (!result.success || !result.meshes || !result.meshes[meshId]) {
    throw new Error(`未寻找到 Mesh ID: ${meshId} 对应的几何数据`);
  }

  const meshData = result.meshes[meshId];

  // 构建内存中的 glTF Document
  const doc = new Document();
  const buffer = doc.createBuffer();
  const scene = doc.createScene('default');
  const node = doc.createNode(`Node_Mesh_${meshId}`);
  const mesh = doc.createMesh(meshData.name || `Mesh_${meshId}`);
  const primitive = doc.createPrimitive();

  // 1. 注入 顶点 Positions
  if (meshData.attributes?.position) {
    const posAccessor = doc
      .createAccessor()
      .setArray(new Float32Array(meshData.attributes.position.array))
      .setType('VEC3')
      .setBuffer(buffer);
    primitive.setAttribute('POSITION', posAccessor);
  }

  // 2. 注入 法线 Normals
  if (meshData.attributes?.normal) {
    const normAccessor = doc
      .createAccessor()
      .setArray(new Float32Array(meshData.attributes.normal.array))
      .setType('VEC3')
      .setBuffer(buffer);
    primitive.setAttribute('NORMAL', normAccessor);
  }

  // 3. 注入 顶点索引 Indices
  if (meshData.index) {
    const indexAccessor = doc
      .createAccessor()
      .setArray(new Uint32Array(meshData.index.array))
      .setType('SCALAR')
      .setBuffer(buffer);
    primitive.setIndices(indexAccessor);
  }

  mesh.addPrimitive(primitive);
  node.setMesh(mesh);
  scene.addChild(node);

  // 确保输出目录存在
  const dir = path.dirname(outputPath);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });

  // 写出原始 GLB
  const glbBuffer = await io.writeBinary(doc);
  fs.writeFileSync(outputPath, glbBuffer);

  return outputPath;
}

/**
 * 对指定 GLB 执行网格减面操作 (Reduce / Simplify Mesh)
 */
export async function simplifyGlb(
  inputGlbPath: string,
  outputPath: string,
  ratio: number = 0.3
): Promise<string> {
  if (!fs.existsSync(inputGlbPath)) {
    throw new Error(`输入的 GLB 文件不存在: ${inputGlbPath}`);
  }

  // 确保减面 WASM 初始化完成
  await MeshoptSimplifier.ready;

  // 读取磁盘中的 GLB 文件
  const doc = await io.read(inputGlbPath);

  // 调用优化函数管道：去重顶点 -> 清除冗余元素 -> 二次误差算法减面
  await doc.transform(
    dedup(),
    prune(),
    simplify({
      simplifier: MeshoptSimplifier,
      ratio: ratio,
      error: 0.001, // 几何边界形状容差控制
    })
  );

  // 确保输出目录存在
  const dir = path.dirname(outputPath);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });

  // 导出减面优化后的 GLB
  const optimizedBuffer = await io.writeBinary(doc);
  fs.writeFileSync(outputPath, optimizedBuffer);

  return outputPath;
}

/**
 * 辅助函数：将 4x4 矩阵分解为 Position, Rotation (Quaternion), Scale
 * （完全适配 Three.js 变换解析）
 */
function decomposeMatrix(m: number[]) {
  // m 为 16 位平铺数组
  const position = { x: m[12], y: m[13], z: m[14] };

  // 计算 Scale
  const sx = Math.hypot(m[0], m[1], m[2]);
  const sy = Math.hypot(m[4], m[5], m[6]);
  const sz = Math.hypot(m[8], m[9], m[10]);
  const scale = { x: sx, y: sy, z: sz };

  // 构建旋转矩阵并导出四元数 Quaternion
  const rm00 = m[0] / sx,
    rm01 = m[1] / sx,
    rm02 = m[2] / sx;
  const rm10 = m[4] / sy,
    rm11 = m[5] / sy,
    rm12 = m[6] / sy;
  const rm20 = m[8] / sz,
    rm21 = m[9] / sz,
    rm22 = m[10] / sz;

  const tr = rm00 + rm11 + rm22;
  let qw = 0,
    qx = 0,
    qy = 0,
    qz = 0;

  if (tr > 0) {
    const S = Math.sqrt(tr + 1.0) * 2;
    qw = 0.25 * S;
    qx = (rm12 - rm21) / S;
    qy = (rm20 - rm02) / S;
    qz = (rm01 - rm10) / S;
  } else if (rm00 > rm11 && rm00 > rm22) {
    const S = Math.sqrt(1.0 + rm00 - rm11 - rm22) * 2;
    qw = (rm12 - rm21) / S;
    qx = 0.25 * S;
    qy = (rm01 + rm10) / S;
    qz = (rm20 + rm02) / S;
  } else if (rm11 > rm22) {
    const S = Math.sqrt(1.0 + rm11 - rm00 - rm22) * 2;
    qw = (rm20 - rm02) / S;
    qx = (rm01 + rm10) / S;
    qy = 0.25 * S;
    qz = (rm12 + rm21) / S;
  } else {
    const S = Math.sqrt(1.0 + rm22 - rm00 - rm11) * 2;
    qw = (rm01 - rm10) / S;
    qx = (rm20 + rm02) / S;
    qy = (rm12 + rm21) / S;
    qz = 0.25 * S;
  }

  return { position, rotation: { x: qx, y: qy, z: qz, w: qw }, scale };
}
