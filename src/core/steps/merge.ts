import { logger } from '@/utils/logger.js';
import { PipelineConfig } from './types.js';
import path from 'path';
import { fileUtils } from '@/utils/file.js';
import { spawn } from '@/utils/child-process-utils.js';
import { merge_glb } from '@/scripts/index.js';
import { emptyDir } from 'fs-extra';

const TAG = 'merge';
/**
 * 费分析去重复
 */
export async function merge(
  config: PipelineConfig,
  pythonPath: string,
  fileName?: string
): Promise<string> {
  const { inputPath, outputDir } = config;
  emptyDir(outputDir);
  // 2  合并glb
  logger.info(TAG, `抽取结构树`);
  const splitDir = path.join(outputDir, 'convert-split-part');
  await fileUtils.emptyDir(splitDir);
  await spawn(pythonPath, [merge_glb, inputPath, outputDir, fileName], {}, TAG);
  if (config.mode == 'all') {
    fileUtils.copy(inputPath, outputDir);
  }
  return config.outputDir;
}
