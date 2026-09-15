import inquirer from 'inquirer';
import { runPipeline } from '@/core/pipeline/orchestrator.js';
import { PipelineConfig } from '@/core/steps/types.js';
import { logger } from '@/utils/logger.js';

interface ProcessOptions {
  input?: string;
  output?: string;
  simplify?: string | boolean;
  dedup?: boolean;
  precision?: string;
  keepTemp?: boolean;
}

export async function processCommand(options: ProcessOptions): Promise<void> {
  logger.info('process', { options });
  // 1. 处理输入文件路径（参数优先，缺失则交互）
  let inputPath = options.input;
  if (!inputPath) {
    const answer = await inquirer.prompt({
      type: 'input',
      name: 'input',
      message: '请输入 STEP/IGES 文件路径:',
    });
    inputPath = answer.input;
    if (!inputPath) {
      throw new Error('必须提供输入文件路径');
    }
  }
  // 2. 处理输出目录（参数优先，缺失则交互）
  let outputDir = options.output || './output';
  if (!options.output) {
    const answer = await inquirer.prompt({
      type: 'input',
      name: 'output',
      message: '输出目录（默认 ./output）:',
      default: './output',
    });
    outputDir = answer.output;
  }
  // 3. 导出模型精度
  let precision = 0.2;
  if (!options.precision) {
    // 命令行明确指定了步骤
    if (typeof options.precision == 'string') {
      precision = parseFloat(options.precision);
    } else {
      const answer = await inquirer.prompt({
        type: 'input',
        name: 'output',
        message: '模型精度:',
        default: '0.2',
      });
      precision = parseFloat(answer.output);
    }
  }
  // 3. 减面参数
  let simplify = -1;
  if (!options.simplify) {
    // 命令行明确指定了步骤
    if (typeof options.simplify == 'string') {
      simplify = parseInt(options.simplify);
    } else {
      const answer = await inquirer.prompt({
        type: 'input',
        name: 'output',
        message: '减面比例:',
        default: '0',
      });
      simplify = parseInt(answer.output);
    }
  }
  let dedup = options.dedup == true;

  let keepTemp = options.keepTemp || false;

  // 6. 构建并执行流水线
  const config: PipelineConfig = {
    inputPath,
    outputDir,
    simplify,
    dedup,
    precision,
    keepTemp,
  };
  const result = await runPipeline(config);
  if (result.code == 0) {
    process.exit(0);
  }
}
