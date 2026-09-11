import { logger } from '@/cli/logger.js';
import path from 'path';
import { fileEncoding } from '@/utils/encoding-utils.js';
import { PipelineConfig } from '../types.js';
import { cad_splitter } from '@/py/py-path.js';
import { spawn } from '@/utils/child-process-utils.js';
import { fileUtils } from '@/utils/file.js';

const TAG = 'convert';
/**
 * 步骤2：解析 STEP 文件，输出 tree.json 和零件文件（STL 中间格式，随后转为 GLB）
 */
export async function convertStep(config: PipelineConfig, pythonPath: string): Promise<string> {
  const { inputPath, outputDir } = config;
  logger.info('convert', `解析: ${inputPath}`);
  const ext = path.extname(inputPath).toLocaleLowerCase();
  // 1 转码
  logger.info('convert', `转码`);
  let encodingPath = await fileEncoding(
    inputPath,
    path.join(outputDir, 'convert-step-encoding', `temp${ext}`)
  );
  logger.info('convert', `转码文件暂存路径：${encodingPath}`);
  // 2 抽取结构树
  logger.info('convert', `抽取结构树`);
  const startTime = new Date();
  const splitDir = path.join(outputDir, 'convert-split-part');
  await fileUtils.emptyDir(splitDir);
  await spawn(
    pythonPath,
    [cad_splitter, encodingPath, path.join(splitDir, 'assembly-tree.json')],
    {},
    TAG
  );
  logger.info(TAG, `开始时间：${startTime.toISOString()}`);
  logger.info(TAG, `结束时间：${new Date().toISOString()}`);
  // logger.info('convert', `查到的零件数量: ${totalMeshesCount}`);
  // const tempOut = path.join(outputDir, 'convert-split-part');
  // await fileUtils.writeFile(path.resolve(tempOut, 'assembly-tree.json'), tree);
  // await exportPartGlbAll(encodingPath, tree, tempOut);
  return outputDir;
}
