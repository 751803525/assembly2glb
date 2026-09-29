import { logger } from '@/utils/logger.js';
import { fileUtils } from '@/utils/file-utils.js';
import { tempDir } from '@/utils/temp-path.js';
import path from 'path';
import { checkLocalEnvironment } from '@/core/steps/env-check.js';
import { convert } from '@/core/steps/convert.js';
import { dedupGlb } from '@/core/steps/dedup-glb.js';
import { simplifyGlb } from '@/core/steps/simplify-glb.js';
import { PipelineConfig } from '@/core/steps/types.js';
import { merge } from '../steps/merge.js';

const TAG = 'pipeline';

async function runStepAndCleanup<T extends PipelineConfig>(
  label: string,
  stepFn: (config: T) => Promise<string>,
  config: T
): Promise<string> {
  logger.info(TAG, `开始${label}`);
  const out = await stepFn(config);
  logger.info(TAG, `${label}完成`);
  if (!config.keepTemp) {
    try {
      await fileUtils.remove(config.inputPath);
    } catch (e) {
      logger.warn(TAG, `清理${label}输入目录失败: ${e}`);
    }
  }
  return out;
}

export async function runPipeline(
  config: PipelineConfig
): Promise<{ code: number; message?: string }> {
  const result = await checkLocalEnvironment();
  if (!result.success) {
    logger.error(TAG, result.message);
    return { code: 1, message: result.message };
  }

  const { inputPath, outputDir, simplify, dedup, mode } = config;

  logger.info(TAG, `解析:${inputPath}`);
  let cacheDir = await convert({
    ...config,
    env: result.data,
    outputDir: path.join(tempDir, 'convert'),
  });

  logger.info(TAG, `解析完成`);
  // 开始减面
  if (simplify > 0 && simplify < 100) {
    cacheDir = await runStepAndCleanup('减面', simplifyGlb, {
      ...config,
      inputPath: cacheDir,
      outputDir: path.join(tempDir, 'simplify'),
    });
  }
  if (dedup) {
    cacheDir = await runStepAndCleanup('去重', dedupGlb, {
      ...config,
      inputPath: cacheDir,
      outputDir: path.join(tempDir, 'dedup'),
      env: result.data,
    });
  }
  if (mode != 'split') {
    logger.info(TAG, `开始合并`);
    cacheDir = await runStepAndCleanup('合并', merge, {
      ...config,
      inputPath: cacheDir,
      outputDir: path.join(tempDir, 'merge'),
      env: result.data,
      fileName: path.parse(inputPath).name,
    });
  }
  logger.info(TAG, '开始写入，准备写入到输出目录');
  await fileUtils.prepareEmptyDir(outputDir);
  await fileUtils.copy(cacheDir, outputDir);
  if (!config.keepTemp) {
    await fileUtils.remove(cacheDir);
  }

  logger.info(TAG, `操作完成，输出目录:${outputDir}`);
  return {
    code: 0,
    message: 'ok',
  };
}
