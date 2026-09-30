import path from 'path';
import fs from 'fs/promises';
import { NodeIO } from '@gltf-transform/core';
import { KHRDracoMeshCompression } from '@gltf-transform/extensions';
import { draco } from '@gltf-transform/functions';
import draco3d from 'draco3dgltf';

import { PipelineConfig } from './types.js';
import { fileUtils } from '@/utils/file-utils.js';

const TAG = 'compress';

// Draco WASM 编解码模块只初始化一次，跨文件复用
let _ioPromise: Promise<NodeIO> | null = null;

async function getIO(): Promise<NodeIO> {
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
// ---------------------------------------------------------------------
// 单文件压缩
// ---------------------------------------------------------------------

async function compressOne(inputFile: string, outputFile: string): Promise<boolean> {
  try {
    const io = await getIO();
    const document = await io.read(inputFile);

    await document.transform(
      draco({
        method: 'edgebreaker',
        // 量化位数用默认值：
        //   quantizePosition: 14, quantizeNormal: 10,
        //   quantizeTexcoord: 12, quantizeColor: 8, quantizeGeneric: 12
      })
    );

    await fs.mkdir(path.dirname(outputFile), { recursive: true });
    await io.write(outputFile, document);
    return true;
  } catch {
    fileUtils.copy(inputFile, outputFile);
    if (path.extname(inputFile) == '.gltf') {
      fileUtils.copy(inputFile.replace(/gltf$/, 'bin'), outputFile.replace(/gltf$/, 'bin'));
    }
    return false;
  }
}

/**
 * Draco 压缩
 *
 * 输入：<inputPath>/ 下的整个目录树
 *   - 顶层可能有 xxx.glb（merge 合并后的装配体）
 *   - mode=all 时还有 assembly-tree.json + meshs/*.gltf + .bin + tex_*.png
 *
 * 输出：<outputDir>/ 保持目录结构
 *   - .glb / .gltf 被 Draco 压缩
 *   - 其它文件（tree json、贴图等）原样复制
 *   - 原 .bin 不复制（每个 gltf 被 draco 重写后会生成新 bin）
 */
export async function compressGlb(
  config: PipelineConfig & { inputPath: string; outputDir: string }
): Promise<string> {
  const { inputPath, outputDir } = config;
  if (!(await fileUtils.exists(inputPath))) {
    throw new Error(`输入目录不存在: ${inputPath}`);
  }
  await fileUtils.prepareEmptyDir(outputDir);
  // ---- 1. 先把所有非 glb/gltf 文件复制过去（保留目录结构） ----
  await fileUtils.copy(inputPath, outputDir, {
    filter: (from: string, _to: string) => {
      const extname = path.extname(from).toLocaleLowerCase();
      return extname == '.glb' || extname == '.gltf' || extname == '.bin';
    },
  });
  // ---- 2. 遍历所有 glb/gltf 并压缩 ----
  const targets = await fileUtils.readdir(inputPath, '.glb', '.gltf');
  if (targets.length === 0) {
    console.info(TAG, `未找到 glb/gltf 文件，跳过压缩`);
    return outputDir;
  }
  console.info(TAG, `待压缩文件数: ${targets.length}`);
  let successCount = 0;
  const allList: Promise<boolean>[] = [];
  // --- 构建 压缩的异步 Promise
  targets.forEach((form) => {
    const to = form.replace(inputPath, outputDir);
    allList.push(compressOne(form, to));
  });
  // --- 等待异步压缩 函数执行完毕 并统计成功数量
  const result = await Promise.all(allList);
  result.forEach((r) => {
    if (r) {
      successCount++;
    }
  });
  console.info(TAG, `成功压缩: ${successCount}/${targets.length}`);
  if (successCount != targets.length) {
    console.info(TAG, '压缩失败的将输出原始模型');
  }
  return outputDir;
}
