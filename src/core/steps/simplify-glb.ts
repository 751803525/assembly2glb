import path from 'path';
import { NodeIO } from '@gltf-transform/core';
import { weld, simplify } from '@gltf-transform/functions';
import { MeshoptSimplifier, MeshoptDecoder } from 'meshoptimizer'; // 引入 MeshoptDecoder

import { logger } from '@/utils/logger.js';
import { PipelineConfig } from './types.js';
import { fileUtils } from '@/utils/file.js';

const TAG = 'simplify';

/**
 * 保持棱角的二次简化与拓扑重构
 */
async function simplifyKeepEdges(
  inputPath: string,
  outputPath: string,
  options: { ratio: number; errorTolerance: number; lockBorder: boolean }
) {
  try {
    const { ratio = 0.3, errorTolerance = 0.001, lockBorder = true } = options;

    // 1. 初始化 WebAssembly 模块并注册解码器
    await Promise.all([MeshoptSimplifier.ready, MeshoptDecoder.ready]);

    // 注册 MeshoptDecoder，防止读取带压缩的 GLB 时卡死
    const io = new NodeIO().registerDependencies({
      'meshopt.decoder': MeshoptDecoder,
    });
    const doc = await io.read(inputPath);
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
        ratio: ratio,
        error: errorTolerance,
        lockBorder: lockBorder,
      })
      // tangents()
    );

    // 记录简化后统计数据
    let simplifiedVertices = 0;
    let simplifiedFaces = 0;
    for (const mesh of doc.getRoot().listMeshes()) {
      for (const prim of mesh.listPrimitives()) {
        const pos = prim.getAttribute('POSITION');
        const indices = prim.getIndices();
        if (pos) simplifiedVertices += pos.getCount();
        if (indices) simplifiedFaces += indices.getCount() / 3;
      }
    }

    // 3. 保存简化后的 GLB
    await io.write(outputPath, doc);
    logger.info(
      TAG,
      `[${path.basename(inputPath)}] 简化完成: 三角面数 ${originalFaces} -> ${simplifiedFaces} (${((simplifiedFaces / (originalFaces || 1)) * 100).toFixed(1)}%)`
    );
  } catch (error) {
    logger.error(TAG, `处理文件失败 [${inputPath}]:`, error);
  }
}

/**
 * 步骤4：逐个对全部网格进行减面
 */
export async function simplifyGlb(config: PipelineConfig): Promise<string> {
  const { simplify, inputPath, outputDir } = config;
  logger.info(TAG, `开始网格二次简化与拓扑重构`);

  const glbFiles = await fileUtils.readdir(path.join(inputPath, 'glbs'), 'glb');
  const outGlb = path.join(outputDir, 'glbs');
  await fileUtils.emptyDir(outGlb);

  // 使用 for...of 正确等待每一个异步简化任务完成
  for (const item of glbFiles) {
    const name = path.basename(item);
    await simplifyKeepEdges(item, path.join(outGlb, name), {
      ratio: simplify / 100,
      errorTolerance: 0.001,
      lockBorder: true,
    });
  }
  const treeFiles = await fileUtils.readdir(inputPath, 'json');
  for (const item of treeFiles) {
    const name = path.basename(item);
    await fileUtils.copy(item, path.join(outputDir, name));
  }
  logger.info(TAG, `全部 GLB 减面完成！`);
  return config.outputDir;
}
