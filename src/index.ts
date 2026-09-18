#!/usr/bin/env node

import { program } from 'commander';
import { processCommand } from '@/cli/commands/process.js';

program
  .command('process')
  .description('工业CAD轻量化流水线（默认仅执行解析，通过 -s 添加更多步骤）')
  .option('-i, --input <path>', '输入 STEP/IGES 文件路径')
  .option('-o, --output <dir>', '输出目录（默认: ./output）')
  .option('-p, --precision [number]', '导出模型精度')
  .option('-s, --simplify [number]', '执行减面操作')
  .option('-d, --dedup', '执行去重操作')
  .option('-m, --merge [string]', '模型合并输出')
  .option('--keep-temp', '保留临时文件（调试用）')
  .action(async (options) => {
    await processCommand(options);
  });

program.parse();
