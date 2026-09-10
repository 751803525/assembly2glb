import type { PipelineContext } from '../types.js';
import { logger } from '../../cli/logger.js';
import fs from 'fs/promises';
import path from 'path';
import { spawn } from 'child_process';
import { fileURLToPath } from 'url';
import { convertFileEncoding } from '../../utils/encoding-utils.js';

import os from 'os';
import { cleanupDir, fileExists } from '../../utils/file.js';
import { findFreeCADCmd } from '../../utils/find-free-CAD-cmd.js';

interface BatchTaskItem {
  step: string;
  glb: string;
  size: number;
}
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

async function splitStep(
  exe: string,
  py: string,
  inputPath: string,
  outputDir: string
): Promise<void> {
  /**
   * 清除上次可能存在的残留文件
   */
  await cleanupDir(outputDir);
  return new Promise((resolve, reject) => {
    const args = [py, inputPath, outputDir];
    logger.info(`执行命令: ${exe} ${args.join(' ')}`);
    const child = spawn(exe, args, {
      shell: false,
      env: {
        ...process.env,
        // 强制 Python 标准输出为 UTF-8，防止 Windows 控制台打印中文零件名乱码
        PYTHONIOENCODING: 'utf-8',
      },
    });

    // 4. 解决 Windows CMD 下可能出现的 GBK/UTF8 跨进程编码截断
    child.stdout.on('data', (data: Buffer) => {
      const lines = data.toString('utf-8').trim()?.split('\n');
      lines?.forEach((line) => {
        logger.info(`[FreeCAD]: ${line}`);
      });
    });

    child.stderr.on('data', (data: Buffer) => {
      const lines = data.toString('utf-8').trim().split('\n');
      // 忽略部分 freecad 启动时吐出的非致命 console 警告
      lines?.forEach((line) => {
        logger.info(`[FreeCAD]: ${line}`);
      });
    });

    child.on('close', (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`freecadcmd 进程异常退出，退出码: ${code}`));
      }
    });

    child.on('error', (err) => {
      logger.error(`无法启动 freecadcmd 进程，请检查路径是否存在: ${exe}`);
      reject(err);
    });
  });
}

/**
 * 递归更新树结构中的文件名 (.step -> .glb)
 */
function updateTreeMapping(node: any) {
  if (
    node.fileName &&
    typeof node.fileName === 'string' &&
    node.fileName.toLowerCase().endsWith('.step')
  ) {
    node.fileName = node.fileName.replace(/\.step$/i, '.glb');
  }
  if (node.children && Array.isArray(node.children)) {
    node.children.forEach(updateTreeMapping);
  }
}

/**
 * 控制并发量的 Promise Pool
 */
async function asyncPool<T>(
  concurrency: number,
  iterable: T[],
  iteratorFn: (item: T) => Promise<any>
) {
  const ret: Promise<any>[] = [];
  const executing: Promise<any>[] = [];
  for (const item of iterable) {
    const p = Promise.resolve().then(() => iteratorFn(item));
    ret.push(p);
    if (concurrency <= iterable.length) {
      const e: Promise<any> = p.then(() => executing.splice(executing.indexOf(e), 1));
      executing.push(e);
      if (executing.length >= concurrency) {
        await Promise.race(executing);
      }
    }
  }
  return Promise.all(ret);
}

/**
 * 按文件体积容量 (MB) 动态切分任务批次
 */
async function buildDynamicBatches(
  stepFiles: string[],
  inputDir: string,
  outputDir: string,
  maxBatchSizeBytes: number
): Promise<BatchTaskItem[][]> {
  const batches: BatchTaskItem[][] = [];
  let currentBatch: BatchTaskItem[] = [];
  let currentBatchSize = 0;

  for (const name of stepFiles) {
    const stepPath = path.join(inputDir, name);
    const glbPath = path.join(outputDir, name.replace(/\.step$/i, '.glb'));
    try {
      const stats = await fs.stat(stepPath);

      const fileSize = stats.size;
      // 超过累计大小阈值且当前批次非空，则推入新批次
      if (currentBatchSize + fileSize > maxBatchSizeBytes && currentBatch.length > 0) {
        batches.push(currentBatch);
        currentBatch = [];
        currentBatchSize = 0;
      }
      currentBatch.push({ step: stepPath, glb: glbPath, size: fileSize });
      currentBatchSize += fileSize;
    } catch (e: any) {
      // 忽略文件读取异常
      logger.error(e);
    }
  }

  if (currentBatch.length > 0) {
    batches.push(currentBatch);
  }

  return batches;
}

/**
 * 执行单批次 Python 转换子进程
 */
function runBatchProcess(
  exe: string,
  pyPath: string,
  tasks: BatchTaskItem[],
  batchId: number
): Promise<void> {
  return new Promise(async (resolve, reject) => {
    const tempJsonPath = path.join(os.tmpdir(), `glb_batch_${batchId}_${Date.now()}.json`);
    await fs.writeFile(tempJsonPath, JSON.stringify(tasks, null, 2), 'utf-8');

    const args = [pyPath, tempJsonPath];
    const child = spawn(exe, args, {
      shell: false,
      env: {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
      },
    });

    child.stdout.on('data', (data: Buffer) => {
      const lines = data.toString('utf-8').trim()?.split('\n');
      lines?.forEach((line) => {
        logger.info(`[GLB Batch ${batchId}]: ${line}`);
      });
    });

    child.stderr.on('data', (data: Buffer) => {
      const lines = data.toString('utf-8').trim()?.split('\n');
      lines?.forEach((line) => {
        logger.warn(`[GLB Batch ${batchId} Error]: ${line}`);
      });
    });

    child.on('close', async (code) => {
      await fs.unlink(tempJsonPath).catch(() => {});
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`批次 ${batchId} 在转 GLB 过程中退出异常，退出码: ${code}`));
      }
    });

    child.on('error', async (err) => {
      await fs.unlink(tempJsonPath).catch(() => {});
      reject(err);
    });
  });
}

/**
 * 核心调度方法：将输出目录下拆出的所有 STEP 动态分批转换为 GLB 并更新 tree.json
 */
async function convertStepsToGlbDynamicBatch(
  exe: string,
  pyPath: string,
  inputDir: string,
  outputDir: string,
  maxBatchSizeBytes = 100 * 1024 * 1024, // 默认单批次容量上限 100MB
  concurrency = 2 // 默认开 2 个并发子进程池
): Promise<void> {
  /**
   * 清除上次可能存在的残留文件
   */
  await cleanupDir(outputDir);
  if (!(await fileExists(pyPath))) {
    throw new Error(`缺少批处理 Python 脚本: ${pyPath}`);
  }

  // 1. 检索所有拆出的单体 .step 文件
  const allFiles = await fs.readdir(inputDir);
  const stepFiles = allFiles.filter((f) => f.toLowerCase().endsWith('.step'));

  if (stepFiles.length === 0) {
    logger.warn('未在此目录下找到需要转换的 STEP 文件');
    return;
  }
  // 2. 动态切分批次
  const batches = await buildDynamicBatches(stepFiles, inputDir, outputDir, maxBatchSizeBytes);
  logger.info(
    `共检测到 ${stepFiles.length} 个 STEP 零件，基于 ${(maxBatchSizeBytes / 1024 / 1024).toFixed(2)}MB 阈值已动态划分为 ${batches.length} 个转换批次`
  );

  // 3. 多进程并发运行批次池
  let completedBatchCount = 0;
  await asyncPool(concurrency, batches, async (batchTasks) => {
    completedBatchCount++;
    const currentId = completedBatchCount;
    const batchMB = (batchTasks.reduce((acc, cur) => acc + cur.size, 0) / (1024 * 1024)).toFixed(2);
    logger.info(
      `▶ 开始执行批次 [${currentId}/${batches.length}]: 包含 ${batchTasks.length} 个零件, 累计大小: ${batchMB} MB`
    );

    await runBatchProcess(exe, pyPath, batchTasks, currentId);
  });

  // 4. 更新 tree.json 结构树，将 .step 彻底更名为 .glb
  const fromTreePath = path.join(inputDir, 'tree.json');
  logger.info('正在同步tree.json 映射...');
  const rawContent = await fs.readFile(fromTreePath, 'utf-8');
  const treeData = JSON.parse(rawContent);

  if (treeData.tree && Array.isArray(treeData.tree)) {
    treeData.tree.forEach(updateTreeMapping);
  }
  const toTreePath = path.join(outputDir, 'tree.json');
  await fs.writeFile(toTreePath, JSON.stringify(treeData, null, 2), 'utf-8');
  logger.info(`✅ tree.json 映射替换完成: ${toTreePath}`);
}

/**
 * 步骤2：解析 STEP 文件，输出 tree.json 和零件文件（STL 中间格式，随后转为 GLB）
 */
export async function convertStep(context: PipelineContext): Promise<string> {
  const { config, tempDir } = context;
  const { inputPath } = config;
  logger.info(`解析: ${inputPath}`);
  const ext = path.extname(inputPath).toLocaleLowerCase();
  // 1 转码
  const encodingOutputPath = path.join(tempDir, 'convert-step-encoding', `temp${ext}`);
  await convertFileEncoding(inputPath, encodingOutputPath);

  // 2. 分离装配体、生成 tree.json 并删除 暂存的encoding 文件
  const splitOutputDir = path.join(tempDir, 'convert-step-split');
  const splitPyPath = path.join(__dirname, '../../scripts/step-split.py');
  const { exe } = await findFreeCADCmd();
  await splitStep(exe, splitPyPath, encodingOutputPath, splitOutputDir);
  await cleanupDir(path.dirname(encodingOutputPath));

  // 3. 动态批次调度转换 GLB 并更新 tree.json 并暂存的step 文件
  const convertPyPath = path.join(__dirname, '../../scripts/convert-batch-glb.py');
  const outputDir = path.join(tempDir, 'convert-step');

  logger.info('开始启动动态批次 GLB 转换流程...');
  await convertStepsToGlbDynamicBatch(
    exe,
    convertPyPath,
    splitOutputDir,
    outputDir,
    64 * 1024 * 1024,
    2
  );
  await cleanupDir(splitOutputDir);
  logger.info('全部零件转换 GLB 及 JSON 映射更新成功！');
  return outputDir;
}
