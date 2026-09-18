import { PipelineConfig } from './types.js';
import { fileUtils } from '@/utils/file.js';
import { spawn } from '@/utils/child-process-utils.js';
import { dedup } from '@/scripts/index.js';
const TAG = 'dedup';
/**
 * 费分析去重复
 */
export async function dedupGlb(config: PipelineConfig, pythonPath: string): Promise<string> {
  const { inputPath, outputDir } = config;
  await fileUtils.emptyDir(outputDir);
  // 2  合并glb
  await spawn(pythonPath, [dedup, inputPath, outputDir], {}, TAG);
  if (config.mode == 'all') {
    fileUtils.copy(inputPath, outputDir);
  }
  return config.outputDir;
}
