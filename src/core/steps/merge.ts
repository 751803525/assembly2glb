import { PipelineConfig } from './types.js';
import { fileUtils } from '@/utils/file-utils.js';
import { spawn } from '@/utils/child-process-utils.js';
import { merge_glb } from '@/scripts/index.js';

const TAG = 'merge';
/**
 * 费分析去重复
 */
export async function merge(
  config: PipelineConfig & {
    env: string;
    fileName?: string;
  }
): Promise<string> {
  const { inputPath, outputDir, env, fileName } = config;
  await fileUtils.prepareEmptyDir(outputDir);
  // 2  合并glb
  console.log('fileName', fileName);
  await spawn(env, [merge_glb, inputPath, outputDir, fileName], {}, TAG);
  if (config.mode == 'all') {
    fileUtils.copy(inputPath, outputDir);
  }
  return config.outputDir;
}
