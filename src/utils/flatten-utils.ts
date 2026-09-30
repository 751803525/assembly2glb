/**
 * flatten-utils.ts — glTF 拍平工具（"能看就好"模式）
 *
 * 目标：把分层装配 glb 烘焙成"给外部看的单体匿名 glb"。
 * 产物不用于生产，只用于快速预览形状——因此默认：
 *   - 清空所有材质 / 贴图 / 图像
 *   - 剔除 TANGENT / TEXCOORD_* / COLOR_* / JOINTS / WEIGHTS 等纯视觉属性
 *   - 只保留 POSITION + NORMAL
 *   - 清 name / extras / generator
 *   - 合并为 1 个 node + 1 个 mesh + 1 个 primitive
 *
 * 关键顺序：先统一材质和属性，再 join()。
 * 否则 join() 会因为"材质不同 / 属性集合不同"拒绝合并，
 * 导致 Babylon.js / three.js 侧看到多个 sub-mesh。
 *
 * 真要看细节请走 MERGE / SPLIT 模式，或显式关闭下面的开关。
 */

import { NodeIO, Document, Node } from '@gltf-transform/core';
import { KHRDracoMeshCompression } from '@gltf-transform/extensions';
import { flatten, join } from '@gltf-transform/functions';
import draco3d from 'draco3dgltf';
import fs from 'fs/promises';

// =====================================================================
// Draco 模块单例
// =====================================================================

let _ioPromise: Promise<NodeIO> | null = null;

/**
 * 获取注册了 Draco 编解码器的 NodeIO 实例（单例）。
 * draco3d 的 WASM 初始化很慢，必须复用。
 */
export function getFlattenIO(): Promise<NodeIO> {
  if (!_ioPromise) {
    _ioPromise = (async () => {
      const [encoder, decoder] = await Promise.all([
        draco3d.createEncoderModule(),
        draco3d.createDecoderModule(),
      ]);
      return new NodeIO().registerExtensions([KHRDracoMeshCompression]).registerDependencies({
        'draco3d.encoder': encoder,
        'draco3d.decoder': decoder,
      });
    })();
  }
  return _ioPromise;
}

// =====================================================================
// 清空材质
// =====================================================================

/**
 * 移除文档中所有材质及其贴图引用：
 *   1. 断开每个 primitive 的 material 引用
 *   2. dispose 所有 Material
 *   3. dispose 所有 Texture（Image 通过 Texture 间接释放）
 *
 * 返回被移除的材质数量。
 * 产出为纯几何 glb，外观信息全部丢失——符合"能看就好"的定位。
 *
 * 注意：必须在 join() 之前调用，否则 join() 会因为材质不同而不合并。
 */
export function stripMaterials(document: Document): number {
  const root = document.getRoot();
  const count = root.listMaterials().length;

  for (const mesh of root.listMeshes()) {
    for (const prim of mesh.listPrimitives()) {
      prim.setMaterial(null);
    }
  }
  for (const mat of root.listMaterials()) mat.dispose();
  for (const tex of root.listTextures()) tex.dispose();

  return count;
}

// =====================================================================
// 剔除视觉细节属性
// =====================================================================

/** 只保留的顶点属性 */
const KEEP_SEMANTICS = new Set(['POSITION', 'NORMAL']);

/**
 * 从所有 primitive 上剔除 TANGENT / TEXCOORD_* / COLOR_* / JOINTS / WEIGHTS 等。
 *
 * 无贴图时 TANGENT / TEXCOORD 完全无意义；
 * 无蒙皮时 JOINTS / WEIGHTS 也无意义。
 * 只保留 POSITION + NORMAL —— 有法线才有明暗，能看出形状。
 *
 * 返回被剔除的属性总数。
 *
 * 注意：必须在 join() 之前调用，否则属性集合不同的图元不会被合并。
 */
export function stripVisualAttributes(document: Document): number {
  const root = document.getRoot();
  let removed = 0;

  for (const mesh of root.listMeshes()) {
    for (const prim of mesh.listPrimitives()) {
      for (const semantic of prim.listSemantics()) {
        if (KEEP_SEMANTICS.has(semantic)) continue;
        prim.setAttribute(semantic, null);
        removed++;
      }
    }
  }

  // 蒙皮对象一并不需要（拍平后不再有骨骼动画）
  for (const skin of root.listSkins()) skin.dispose();

  return removed;
}

// =====================================================================
// 匿名化
// =====================================================================

/**
 * 清掉所有可能暴露来源的元数据：
 *   - scene / node / mesh 的 name
 *   - 所有对象的 extras（空对象，writer 不会序列化）
 *   - asset.generator
 */
export function anonymize(document: Document): void {
  const root = document.getRoot();
  const empty: Record<string, unknown> = {};

  for (const scene of root.listScenes()) {
    scene.setName('');
    scene.setExtras(empty);
  }
  for (const node of root.listNodes()) {
    node.setName('');
    node.setExtras(empty);
  }
  for (const mesh of root.listMeshes()) {
    mesh.setName('');
    mesh.setExtras(empty);
  }
  for (const mat of root.listMaterials()) {
    mat.setName('');
    mat.setExtras(empty);
  }

  root.getAsset().generator = '';
}

// =====================================================================
// 单节点归并
// =====================================================================

/**
 * 保证场景只剩 1 个 node。
 *
 * join() 之后一般已只剩 1 个带 mesh 的节点，
 * 这里主要是防御性兜底：多余的空节点直接 dispose，
 * 并清掉 survivor 上可能残留的 transform。
 */
function collapseToSingleNode(document: Document): void {
  const root = document.getRoot();
  const scene = root.getDefaultScene() ?? root.listScenes()[0] ?? null;
  if (!scene) return;

  const children = scene.listChildren();
  if (children.length <= 1) return;

  const survivor: Node | undefined = children.find((n) => n.getMesh());
  if (!survivor) {
    for (let i = 1; i < children.length; i++) children[i].dispose();
    return;
  }

  for (const node of children) {
    if (node === survivor) continue;
    if (node.getMesh()) {
      console.warn(
        '[flatten] collapseToSingleNode 遇到多个带 mesh 的节点，' +
          'join() 未完全合并，可能丢失几何'
      );
    }
    node.setMesh(null);
    node.dispose();
  }

  // 清掉 survivor 上可能残留的 transform（flatten 后应已是 identity）
  survivor.setMatrix([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
  survivor.setTranslation([0, 0, 0]);
  survivor.setRotation([0, 0, 0, 1]);
  survivor.setScale([1, 1, 1]);
}

// =====================================================================
// 拍平主流程
// =====================================================================

export interface FlattenStats {
  sizeBefore: number;
  sizeAfter: number;
  nodeCountBefore: number;
  nodeCountAfter: number;
  meshCountBefore: number;
  meshCountAfter: number;
  primitiveCountAfter: number;
  materialCountBefore: number;
  materialCountAfter: number;
  attributesRemoved: number;
}

export interface FlattenOptions {
  /** 日志前缀，默认 'flatten' */
  tag?: string;
  /** 是否打印详细统计，默认 true */
  verbose?: boolean;
  /**
   * 是否清空所有材质 / 贴图。
   * 默认 true（"能看就好"）。
   */
  stripMaterials?: boolean;
  /**
   * 是否剔除 TANGENT / TEXCOORD_* / COLOR_* / JOINTS / WEIGHTS。
   * 默认 true（"能看就好"）。
   */
  stripVisualAttributes?: boolean;
}

/**
 * 把分层 glb 拍平成"给外部看的单体匿名 glb"。
 *
 * 流程（顺序很关键）：
 *   1. 读入 inputGlbPath
 *   2. flatten()          —— 烘焙所有 node 变换到顶点
 *   3. stripVisualAttributes() —— 剔掉视觉属性（先于 join）
 *   4. stripMaterials()   —— 清空材质（先于 join）
 *   5. join()             —— 此时所有图元属性/材质一致，可彻底合并
 *   6. collapseToSingleNode()  —— 兜底保证 1 node
 *   7. anonymize()        —— 清 name / extras / generator
 *   8. 写出 outputGlbPath
 */
export async function flattenGlbFile(
  inputGlbPath: string,
  outputGlbPath: string,
  options: FlattenOptions = {}
): Promise<FlattenStats> {
  const tag = options.tag ?? 'flatten';
  const verbose = options.verbose ?? true;
  const doStripMat = options.stripMaterials ?? true;
  const doStripAttr = options.stripVisualAttributes ?? true;

  const sizeBefore = (await fs.stat(inputGlbPath)).size;

  const io = await getFlattenIO();
  const document = await io.read(inputGlbPath);

  const root = document.getRoot();
  const nodeCountBefore = root.listNodes().length;
  const meshCountBefore = root.listMeshes().length;
  const materialCountBefore = root.listMaterials().length;

  // ---- 1. 烘焙变换（单独跑，join 稍后再跑） ----
  await document.transform(flatten());

  // ---- 2. 先统一：顶点属性 + 材质 ----
  // 必须放在 join 之前，否则 join 会因"属性集合不同 / 材质不同"拒绝合并，
  // 导致 Babylon.js 加载后看到多个 sub-mesh。
  let attributesRemoved = 0;
  if (doStripAttr) attributesRemoved = stripVisualAttributes(document);
  if (doStripMat) stripMaterials(document);

  // ---- 3. 现在所有图元都"同属性 + 无材质"，join 才能真正合起来 ----
  await document.transform(join({ keepNamed: false }));

  // ---- 4. 单节点归并（兜底） ----
  collapseToSingleNode(document);

  // ---- 5. 匿名化 ----
  anonymize(document);

  // ---- 6. 写文件 ----
  await io.write(outputGlbPath, document);

  const sizeAfter = (await fs.stat(outputGlbPath)).size;

  const finalMesh = root.listMeshes()[0];
  const primitiveCountAfter = finalMesh ? finalMesh.listPrimitives().length : 0;

  const stats: FlattenStats = {
    sizeBefore,
    sizeAfter,
    nodeCountBefore,
    nodeCountAfter: root.listNodes().length,
    meshCountBefore,
    meshCountAfter: root.listMeshes().length,
    primitiveCountAfter,
    materialCountBefore,
    materialCountAfter: root.listMaterials().length,
    attributesRemoved,
  };

  if (verbose) {
    const matPart = doStripMat
      ? `materials ${materialCountBefore} → 0`
      : `materials ${materialCountBefore} (保留)`;
    const attrPart = doStripAttr ? `, attrs -${attributesRemoved}` : '';
    console.info(
      tag,
      `nodes ${nodeCountBefore} → ${stats.nodeCountAfter}, ` +
        `meshes ${meshCountBefore} → ${stats.meshCountAfter}, ` +
        `primitives → ${primitiveCountAfter}, ` +
        `${matPart}${attrPart}, ` +
        `size ${(sizeBefore / 1024 / 1024).toFixed(2)} MB → ` +
        `${(sizeAfter / 1024 / 1024).toFixed(2)} MB`
    );
  }

  return stats;
}
