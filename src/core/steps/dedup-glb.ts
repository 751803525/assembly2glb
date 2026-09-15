import { logger } from '@/utils/logger.js';
import { PipelineConfig } from './types.js';

const TAG = 'dedup';
/**
 * 费分析去重复
 */
export async function dedupGlb(context: PipelineConfig): Promise<string> {
  logger.warn(TAG, 'dedupGlb 尚未实现');
  return context.outputDir;
}
