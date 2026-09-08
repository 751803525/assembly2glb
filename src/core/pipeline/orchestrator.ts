import type { PipelineConfig } from '../types.js';
import { convertStep } from '../steps/convert-step.js';
import { logger } from '../../cli/logger.js';
import { tempDir } from '../../utils/temp-path.js';
import path from 'path';
import { simplifyGlb } from '../steps/simplify-glb.js';
import { dedupGlb } from '../steps/dedup-glb.js';
import { fileUtils } from '../../utils/file.js';

export async function runPipeline(config: PipelineConfig) {
  const { inputPath, outputDir, simplify, dedup, mode, keepTemp } = config;

  logger.info('Pipeline', `解析:${inputPath}`);
  let cacheDir = await convertStep({
    ...config,
    outputDir: path.join(tempDir, 'convert'),
  });

  logger.info('Pipeline', `解析结果:${cacheDir}`);
  // 开始减面
  if (simplify > 0) {
    logger.info('Pipeline', `减面:${cacheDir}`);
    cacheDir = await simplifyGlb({
      ...config,
      inputPath: cacheDir,
      outputDir: path.join(tempDir, 'simplify'),
    });
    logger.info('Pipeline', `减面结果:${cacheDir}`);
  }
  logger.info('Pipeline', `去重:${cacheDir}`);
  if (dedup) {
    // 去重分析
    cacheDir = await dedupGlb({
      ...config,
      inputPath: cacheDir,
      outputDir: path.join(tempDir, 'simplify'),
    });
  }
  logger.info('Pipeline', `去重结果:${cacheDir}`);

  if (mode == 'merged') {
  } else if (mode == 'both') {
  }

  await fileUtils.copy(cacheDir, outputDir);
  if (!keepTemp) {
    await fileUtils.remove(tempDir);
  }
}
