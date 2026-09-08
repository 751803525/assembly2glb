import { logger } from '../../cli/logger.js';
import { PipelineConfig } from '../types.js';

/**
 * 步骤4：逐个对全部网格进行减面
 */
export async function simplifyGlb(context: PipelineConfig): Promise<string> {
  logger.warn('simplify', 'simplifyGlb 尚未实现');
  return context.outputDir;
}
