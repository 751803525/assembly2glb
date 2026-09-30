import { hasMode, MODE_FLATTEN, MODE_MERGE, MODE_SPLIT, PipelineConfig } from './types.js';
import { fileUtils } from '@/utils/file-utils.js';
import { spawn } from '@/utils/child-process-utils.js';
import { merge_glb } from '@/scripts/index.js';

const TAG = 'merge';
/**
 * 生成合并对象
 */
export async function merge(
  config: PipelineConfig & {
    env: string;
    fileName?: string;
  }
): Promise<string> {
  const { inputPath, outputDir, env, fileName } = config;
  await fileUtils.prepareEmptyDir(outputDir);
  if (hasMode(MODE_SPLIT, config.mode)) {
    await fileUtils.copy(inputPath, outputDir);
  }
  // 2  合并glb
  if (hasMode(MODE_MERGE, config.mode)) {
    await spawn(env, [merge_glb, inputPath, outputDir, fileName], {}, TAG);
  }

  // 3  拍平 glb
  if (hasMode(MODE_FLATTEN, config.mode)) {
    // TODO
  }

  return config.outputDir;
}
