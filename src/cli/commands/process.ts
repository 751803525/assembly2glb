import inquirer from 'inquirer';
import { runPipeline } from '@/core/pipeline/orchestrator.js';
import {
  hasMode,
  isValidMode,
  MODE_FLATTEN,
  MODE_MERGE,
  MODE_SPLIT,
  OutputMode,
  PipelineConfig,
} from '@/core/steps/types.js';

interface ProcessOptions {
  input?: string;
  output?: string;
  simplify?: string | boolean;
  dedup?: boolean;
  precision?: string | boolean;
  mode?: boolean | number;
  compress?: boolean;
  keepTemp?: boolean | boolean;
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
  // 3. 导出模型精度
  let precision = 0.1;
  if (options.precision) {
    // 命令行明确指定了步骤
    if (typeof options.precision == 'string') {
      precision = parseFloat(options.precision);
    } else {
      const answer = await inquirer.prompt({
        type: 'input',
        name: 'output',
        message: '模型精度:',
        default: '0.1mm',
      });
      precision = parseFloat(answer.output);
    }
  }
  // 3. 减面参数
  let simplify = 0;
  if (options.simplify) {
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

  // 3. 输出模式参数
  let mode: PipelineConfig['mode'] = MODE_SPLIT; // 默认
  if (options.mode) {
    if (typeof options.mode === 'string') {
      const inputMode = parseInt(options.mode);
      mode = isValidMode(inputMode) ? inputMode : MODE_SPLIT; // 命令行传入
    } else {
      const answer = await inquirer.prompt<{ modes: number[] }>({
        type: 'checkbox',
        name: 'modes',
        message: '请选择输出内容（可多选，空格切换，回车确认）:',
        choices: [
          { name: '离散化（结构树 + 分件 gltf）', value: MODE_SPLIT, checked: true },
          { name: '合并（分层装配 glb）', value: MODE_MERGE },
          { name: '单体（烘焙为不可拆单体 glb）', value: MODE_FLATTEN },
        ],
        validate: (choices) => {
          const anyChecked = (choices as { name: string; value: number; checked?: boolean }[]).some(
            (c) => c.checked
          );
          return anyChecked || '至少选择一项';
        },
      });

      // 把选中的位 OR 起来
      mode = answer.modes.reduce((acc, v) => acc | v, 0) as OutputMode;
    }
  }
  const dedup = options.dedup == true;

  const compress = options.compress || false;
  const keepTemp = options.keepTemp || false;

  // 6. 构建并执行流水线
  const config: PipelineConfig = {
    inputPath,
    outputDir,
    simplify,
    dedup,
    precision,
    mode,
    compress,
    keepTemp,
  };
  const result = await runPipeline(config);
  if (result.code == 0) {
    process.exit(0);
  }
}
