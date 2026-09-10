import inquirer from 'inquirer';
import { logger } from '@/cli/logger.js';
import { runPipeline } from '@/core/pipeline/orchestrator.js';
import type { PipelineConfig } from '@/core/types.js';

interface ProcessOptions {
  input?: string;
  output?: string;
  simplify?: string | boolean;
  dedup?: boolean;
  mode?: 'merged' | 'split' | 'both';
  keepTemp?: boolean;
}

export async function processCommand(options: ProcessOptions): Promise<void> {
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
  // 3. 减面参数
  let simplify = -1;
  if (options.simplify) {
    // 命令行明确指定了步骤
    if (typeof options.simplify == 'string') {
      simplify = parseInt(options.simplify);
    } else {
      const answer = await inquirer.prompt({
        type: 'input',
        name: 'output',
        message: '减面目标面数:',
        default: '5000',
      });
      simplify = parseInt(answer.output);
    }
  }
  let dedup = options.dedup == true;

  let mode: 'merged' | 'split' | 'both' = 'both';

  if (options.mode) {
    // 命令行明确指定了步骤
    if (typeof options.mode == 'string') {
      mode = options.mode;
    } else {
      const answer = await inquirer.prompt({
        type: 'list', // 改为单选列表类型
        name: 'mode', // 对应的返回字段名
        message: '请选择输出模式:',
        choices: [
          { name: '合并模式 (merged)', value: 'merged' },
          { name: '拆分模式 (split)', value: 'split' },
          { name: '同时输出 (both)', value: 'both' },
        ],
        default: 'both', // 默认选中项的 value
      });
      mode = answer.mode;
    }
  }

  let keepTemp = options.keepTemp || false;

  // 6. 构建并执行流水线
  const config: PipelineConfig = {
    inputPath,
    outputDir,
    simplify,
    dedup,
    mode,
    keepTemp,
  };
  logger.info('process', config);
  const result = await runPipeline(config);
  if (result.code == 0) {
    process.exit(0);
  }
}
