import { logger } from '@/cli/logger.js';
import { PipelineConfig } from '../types.js';

/**
 * 费分析去重复
 */
export async function dedupGlb(context: PipelineConfig): Promise<string> {
  logger.warn('simplify', 'dedupGlb 尚未实现');
  return context.outputDir;
}
