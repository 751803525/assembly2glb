import path from 'path';
import fs from 'fs/promises';

import { PipelineConfig, hasMode, MODE_SPLIT, MODE_MERGE, MODE_FLATTEN } from './types.js';
import { fileUtils } from '@/utils/file-utils.js';
import { spawn } from '@/utils/child-process-utils.js';
import { merge_glb } from '@/scripts/index.js';
import { flattenGlbFile } from '@/utils/flatten-utils.js';

const TAG = 'merge';

/**
 * 在 outputDir 下找分层装配 glb（排除 flatten 的产物）
 */
async function findStagedGlb(outputDir: string, excludeName: string): Promise<string | null> {
  const allGlb = await fileUtils.readdir(outputDir, 'glb');
  const candidates = allGlb.filter((p) => path.basename(p) !== excludeName);
  if (candidates.length === 0) return null;

  // 取最大的作为装配 glb（防止目录里混入意外小文件）
  let largest = candidates[0];
  let maxSize = 0;
  for (const p of candidates) {
    const st = await fs.stat(p);
    if (st.size > maxSize) {
      maxSize = st.size;
      largest = p;
    }
  }
  return largest;
}

/**
 * 生成合并对象
 *
 * 按 mode 位依次执行（互不依赖的组合也支持）：
 *   MODE_SPLIT    → copy dedup 输出（tree + meshs/）到 outputDir
 *   MODE_MERGE    → spawn merge_glb 生成 {name}.glb
 *   MODE_FLATTEN  → 复用/补跑 merge_glb，读 glb 拍平 → {name}_flatten.glb
 */
export async function merge(
  config: PipelineConfig & {
    env: string;
    fileName?: string;
  }
): Promise<string> {
  const { inputPath, outputDir, env, fileName, mode } = config;

  await fileUtils.prepareEmptyDir(outputDir);

  // flatten 输出文件名：{name}_flatten.glb
  const baseName = (fileName || 'assembly').trim() || 'assembly';
  const flattenName = `${baseName}_flatten.glb`;

  // ---- 1. SPLIT ----
  if (hasMode(mode, MODE_SPLIT)) {
    console.info(TAG, `[split] 拷贝结构树与零件 gltf`);
    await fileUtils.copy(inputPath, outputDir);
  }

  // ---- 2. MERGE ----
  if (hasMode(mode, MODE_MERGE)) {
    console.info(TAG, `[merge] 生成分层装配 glb`);
    await spawn(env, [merge_glb, inputPath, outputDir, fileName], {}, TAG);
  }

  // ---- 3. FLATTEN ----
  if (hasMode(mode, MODE_FLATTEN)) {
    console.info(TAG, `[flatten] 开始拍平`);

    // 3.1 找分层 glb；没有就补跑 merge_glb
    let stagedGlb = await findStagedGlb(outputDir, flattenName);
    if (!stagedGlb) {
      console.info(TAG, `[flatten] 未找到装配 glb，补跑 merge_glb`);
      await spawn(env, [merge_glb, inputPath, outputDir, fileName], {}, TAG);
      stagedGlb = await findStagedGlb(outputDir, flattenName);
      if (!stagedGlb) {
        throw new Error(`merge_glb 执行后仍未产出 glb`);
      }
    } else {
      console.info(TAG, `[flatten] 复用已有装配 glb: ${path.basename(stagedGlb)}`);
    }

    // 3.2 拍平
    const stagedSize = (await fs.stat(stagedGlb)).size;
    const outPath = path.join(outputDir, flattenName);

    await flattenGlbFile(stagedGlb, outPath);

    const outSize = (await fs.stat(outPath)).size;
    console.info(
      TAG,
      `[flatten] ${(stagedSize / 1024 / 1024).toFixed(2)} MB → ` +
        `${(outSize / 1024 / 1024).toFixed(2)} MB (${flattenName})`
    );

    // 3.3 没开 MERGE 就删掉中间产物
    if (!hasMode(mode, MODE_MERGE)) {
      try {
        await fs.unlink(stagedGlb);
        console.info(TAG, `[flatten] 已删除中间装配 glb: ${path.basename(stagedGlb)}`);
      } catch (e) {
        console.warn(TAG, `[flatten] 删除中间 glb 失败: ${stagedGlb}: ${e}`);
      }
    }
  }

  return config.outputDir;
}
