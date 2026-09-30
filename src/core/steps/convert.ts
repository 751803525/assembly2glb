import { logger } from '@/utils/logger.js';
import path from 'path';
import { convertFileEncodingStream, detectNonAsciiEncoding } from '@/utils/encoding-utils.js';
import { PipelineConfig } from './types.js';
import { cad_splitter, glb_splitter } from '@/scripts/index.js';
import { spawn } from '@/utils/child-process-utils.js';
import { fileUtils } from '@/utils/file-utils.js';

const TAG = 'convert';

// STEP/IGES 走 cad-splitter；GLB/GLTF 走 glb-splitter
const STEP_EXT = new Set(['.step', '.stp', '.iges', '.igs']);
const GLB_EXT = new Set(['.glb', '.gltf']);

// 唯一真正是二进制容器、不能做字符集转码的格式
const BINARY_CONTAINER_EXT = new Set(['.glb']);

export async function convert(config: PipelineConfig & { env: string }): Promise<string> {
  const { inputPath, outputDir, precision, env } = config;
  const ext = path.extname(inputPath).toLocaleLowerCase();

  await fileUtils.prepareEmptyDir(outputDir);

  // 1. 选择 splitter
  let splitter: string;
  if (STEP_EXT.has(ext)) {
    splitter = cad_splitter;
  } else if (GLB_EXT.has(ext)) {
    splitter = glb_splitter;
  } else {
    throw new Error(`不支持的输入格式: ${ext}`);
  }

  // 2. 编码对齐
  //   - .glb 是二进制容器，不做字符集检测（内部的 JSON chunk 若不合规只能报错）
  //   - .gltf 是文本 JSON，可能遇到非 UTF-8（不合规但存在），做检测
  //   - .step/.stp/.iges/.igs 是 ISO-10303 / 文本，做检测
  let encodingPath = inputPath;
  if (!BINARY_CONTAINER_EXT.has(ext)) {
    const encodding = await detectNonAsciiEncoding(inputPath);
    if (encodding !== 'UTF-8' && encodding !== 'ASCII') {
      logger.info(TAG, `转码${encodding} -> utf-8,原文件路径：${inputPath}`);
      encodingPath = await convertFileEncodingStream(
        inputPath,
        path.join(outputDir, 'convert-step-encoding', `temp${ext}`),
        encodding,
        'utf-8'
      );
      logger.info(TAG, `转码文件暂存路径：${encodingPath}`);
    }
  }

  // 3. 抽取结构树与零件网格
  logger.info(TAG, `抽取结构树（${path.basename(splitter)}）`);
  const splitDir = path.join(outputDir, 'convert-split-part');
  await fileUtils.prepareEmptyDir(splitDir);

  await spawn(
    env,
    [splitter, encodingPath, path.join(splitDir, 'assembly-tree.json'), precision.toString()],
    {},
    TAG
  );
  return splitDir;
}
