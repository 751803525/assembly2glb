import path from 'path';
import { NodeIO } from '@gltf-transform/core';
import { weld, simplify } from '@gltf-transform/functions';
import { MeshoptSimplifier, MeshoptDecoder } from 'meshoptimizer';

import { logger } from '@/utils/logger.js';
import { PipelineConfig } from './types.js';
import { fileUtils } from '@/utils/file.js';

const TAG = 'simplify';

/**
 * 对单个 gltf + 同名 bin 做二次简化与拓扑重构。
 *
 * 输入 .gltf 会引用同名 .bin，gltf-transform 的 NodeIO 会自动解析外部资源；
 * 写出时也会自动生成新的同名 .bin。
 */
async function simplifyKeepEdges(
  inputGltf: string,
  outputGltf: string,
  options: { ratio: number; errorTolerance: number; lockBorder: boolean }
): Promise<void> {
  const { ratio = 0.3, errorTolerance = 0.001, lockBorder = true } = options;

  // 1. 初始化 WebAssembly 模块并注册解码器
  await Promise.all([MeshoptSimplifier.ready, MeshoptDecoder.ready]);

  // 注册 MeshoptDecoder，防止读取带压缩的模型时卡死
  const io = new NodeIO().registerDependencies({
    'meshopt.decoder': MeshoptDecoder,
  });

  const doc = await io.read(inputGltf);

  // 记录简化前统计数据
  let originalFaces = 0;
  for (const mesh of doc.getRoot().listMeshes()) {
    for (const prim of mesh.listPrimitives()) {
      const indices = prim.getIndices();
      if (indices) originalFaces += indices.getCount() / 3;
    }
  }

  // 2. 执行管线化重构与简化
  await doc.transform(
    weld(),
    simplify({
      simplifier: MeshoptSimplifier,
      ratio,
      error: errorTolerance,
      lockBorder,
    })
  );

  // 记录简化后统计数据
  let simplifiedFaces = 0;
  for (const mesh of doc.getRoot().listMeshes()) {
    for (const prim of mesh.listPrimitives()) {
      const indices = prim.getIndices();
      if (indices) simplifiedFaces += indices.getCount() / 3;
    }
  }

  // 3. 写出 .gltf（gltf-transform 会自动生成同名 .bin）
  await io.write(outputGltf, doc);

  logger.info(
    TAG,
    `[${path.basename(inputGltf)}] 简化完成: 三角面数 ${originalFaces} -> ${simplifiedFaces} (${((simplifiedFaces / (originalFaces || 1)) * 100).toFixed(1)}%)`
  );
}

/**
 * 步骤：逐个对 meshs/ 下的所有 gltf 做减面
 *
 * 约定（与 cad-splitter / glb-splitter 输出对齐）：
 *   <inputPath>/assembly-tree.json
 *   <inputPath>/meshs/xxx__hash.gltf + xxx__hash.bin
 *
 * 输出：
 *   <outputDir>/assembly-tree.json        （原样复制）
 *   <outputDir>/meshs/xxx__hash.gltf + bin（减面后的）
 */
export async function simplifyGlb(
  config: PipelineConfig & { inputPath: string; outputDir: string }
): Promise<string> {
  const { simplify, inputPath, outputDir } = config;
  logger.info(TAG, `开始网格二次简化与拓扑重构`);

  const meshsIn = path.join(inputPath, 'meshs');
  const meshsOut = path.join(outputDir, 'meshs');
  await fileUtils.emptyDir(meshsOut);

  const gltfFiles = await fileUtils.readdir(meshsIn, 'gltf');

  // 使用 for...of 正确等待每一个异步简化任务完成
  for (const item of gltfFiles) {
    const name = path.basename(item);
    try {
      await simplifyKeepEdges(item, path.join(meshsOut, name), {
        ratio: simplify / 100,
        errorTolerance: 0.001,
        lockBorder: true,
      });
    } catch (error) {
      logger.error(TAG, `处理文件失败 [${item}]:`, error);
    }
  }

  // 复制结构树
  const treeFiles = await fileUtils.readdir(inputPath, 'json');
  for (const item of treeFiles) {
    const name = path.basename(item);
    await fileUtils.copy(item, path.join(outputDir, name));
  }

  logger.info(TAG, `全部网格减面完成！`);
  return outputDir;
}
