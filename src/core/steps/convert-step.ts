import { logger } from '@/utils/logger.js';
import path from 'path';
import { convertFileEncodingStream, detectNonAsciiEncoding } from '@/utils/encoding-utils.js';
import { PipelineConfig } from './types.js';
import { cad_splitter } from '@/scripts/py-path.js';
import { spawn } from '@/utils/child-process-utils.js';
import { fileUtils } from '@/utils/file.js';

const TAG = 'convert';
/**
 * 步骤2：解析 STEP 文件，输出 tree.json 和 叶子节点文件 GLB
 */
export async function convertStep(config: PipelineConfig, pythonPath: string): Promise<string> {
  const { inputPath, outputDir, precision } = config;
  const ext = path.extname(inputPath).toLocaleLowerCase();
  // 1 转码
  const encodding = await detectNonAsciiEncoding(inputPath);
  let encodingPath = inputPath;
  if (encodding !== 'UTF-8' && encodding != 'ASCII') {
    logger.info(TAG, `转码${encodding} -> utf-8,原文件路径：${inputPath}`);
    encodingPath = await convertFileEncodingStream(
      inputPath,
      path.join(outputDir, 'convert-step-encoding', `temp${ext}`),
      encodding,
      'utf-8'
    );
    logger.info(TAG, `转码文件暂存路径：${encodingPath}`);
  }

  // 2 抽取结构树与 glb
  logger.info(TAG, `抽取结构树`);
  const splitDir = path.join(outputDir, 'convert-split-part');
  await fileUtils.emptyDir(splitDir);
  await spawn(
    pythonPath,
    [cad_splitter, encodingPath, path.join(splitDir, 'assembly-tree.json'), precision.toString()],
    {},
    TAG
  );
  return splitDir;
}
