import { PipelineConfig } from './types.js';
import { fileUtils } from '@/utils/file.js';
import { spawn } from '@/utils/child-process-utils.js';
import { merge_glb } from '@/scripts/index.js';

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
  await fileUtils.emptyDir(outputDir);
  // 2  合并glb
  await spawn(pythonPath, [merge_glb, inputPath, outputDir, fileName], {}, TAG);
  if (config.mode == 'all') {
    fileUtils.copy(inputPath, outputDir);
  }
  return config.outputDir;
}
