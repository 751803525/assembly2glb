import { logger } from '../../cli/logger.js';
import path from 'path';
import { fileEncoding } from '../../utils/encoding-utils.js';
import { PipelineConfig } from '../types.js';
import { extractAssemblyTree } from '../../utils/occ-utils.js';
import { fileUtils } from '../../utils/file.js';

/**
 * 步骤2：解析 STEP 文件，输出 tree.json 和零件文件（STL 中间格式，随后转为 GLB）
 */
export async function convertStep(config: PipelineConfig): Promise<string> {
  const { inputPath, outputDir } = config;
  logger.info('convert', `解析: ${inputPath}`);
  const ext = path.extname(inputPath).toLocaleLowerCase();
  // 1 转码
  let encodingPath = await fileEncoding(
    inputPath,
    path.join(outputDir, 'convert-step-encoding', `temp${ext}`)
  );
  let { tree, totalMeshesCount } = await extractAssemblyTree(encodingPath);
  logger.info('convert', `查到的零件数量: ${totalMeshesCount}`);
  const tempOut = path.join(outputDir, 'convert-split-part');
  await fileUtils.writeFile(path.resolve(tempOut, 'assembly-tree.json'), tree);
  // await exportPartGlbAll(encodingPath, tree, tempOut);
  return tempOut;
}
