import { logger } from '@/utils/logger.js';
import { fileUtils } from '@/utils/file.js';
import { tempDir } from '@/utils/temp-path.js';
import path from 'path';
import { checkLocalEnvironment } from '@/core/steps/check-occ-env.js';
import { convertStep } from '@/core/steps/convert-step.js';
import { dedupGlb } from '@/core/steps/dedup-glb.js';
import { simplifyGlb } from '@/core/steps/simplify-glb.js';
import { PipelineConfig } from '@/core/steps/types.js';

const TAG = 'pipeline';

export async function runPipeline(
  config: PipelineConfig
): Promise<{ code: number; message?: string }> {
  const result = await checkLocalEnvironment();
  if (!result.success) {
    logger.error(TAG, result.message);
    return { code: 1, message: result.message };
  }

  const { inputPath, outputDir, simplify, dedup, keepTemp } = config;

  logger.info(TAG, `解析:${inputPath}`);
  let cacheDir = await convertStep(
    {
      ...config,
      outputDir: path.join(tempDir, 'convert'),
    },
    result.data
  );

  logger.info(TAG, `解析结果:${cacheDir}`);
  // 开始减面
  if (simplify > 0 && simplify < 100) {
    logger.info(TAG, `减面:${cacheDir}`);
    cacheDir = await simplifyGlb({
      ...config,
      inputPath: cacheDir,
      outputDir: path.join(tempDir, 'simplify'),
    });
    logger.info(TAG, `减面结果:${cacheDir}`);
  }
  if (dedup) {
    logger.info(TAG, `去重:${cacheDir}`);
    // 去重分析
    cacheDir = await dedupGlb({
      ...config,
      inputPath: cacheDir,
      outputDir: path.join(tempDir, 'dedup'),
    });
    logger.info(TAG, `去重结果:${cacheDir}`);
  }
  await fileUtils.emptyDir(outputDir);
  logger.info(TAG, '导出完成，准备写入到输出目录');
  await fileUtils.copy(cacheDir, outputDir);
  if (!keepTemp) {
    await fileUtils.remove(tempDir);
  }
  logger.info(TAG, `操作完成，输出到目录:${outputDir}`);
  return {
    code: 0,
    message: 'ok',
  };
}
