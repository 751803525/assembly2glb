import path from 'path';
import { convertFileEncodingStream, detectNonAsciiEncoding } from '@/utils/encoding-utils.js';
import { PipelineConfig } from './types.js';
import { cad_splitter, glb_splitter } from '@/scripts/index.js';
import { spawn } from '@/utils/child-process-utils.js';
import { fileUtils } from '@/utils/file-utils.js';
import { glbUsesDraco, gltfUsesDraco, decompressDracoGlb } from '@/utils/draco-utils.js';

const TAG = 'splitter';

const STEP_EXT = new Set(['.step', '.stp', '.iges', '.igs']);
const GLB_EXT = new Set(['.glb', '.gltf']);
const BINARY_CONTAINER_EXT = new Set(['.glb']);

export async function splitter(config: PipelineConfig & { env: string }): Promise<string> {
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
  let encodingPath = inputPath;
  if (!BINARY_CONTAINER_EXT.has(ext)) {
    const encodding = await detectNonAsciiEncoding(inputPath);
    if (encodding !== 'UTF-8' && encodding !== 'ASCII') {
      console.info(TAG, `转码${encodding} -> utf-8,原文件路径：${inputPath}`);
      encodingPath = await convertFileEncodingStream(
        inputPath,
        path.join(outputDir, 'convert-step-encoding', `temp${ext}`),
        encodding,
        'utf-8'
      );
      console.info(TAG, `转码文件暂存路径：${encodingPath}`);
    }
  }

  // 3. Draco 预处理（仅 .glb / .gltf）
  let finalInputPath = encodingPath;
  if (ext === '.glb' || ext === '.gltf') {
    const usesDraco =
      ext === '.glb' ? await glbUsesDraco(encodingPath) : await gltfUsesDraco(encodingPath);

    if (usesDraco) {
      const dracoDir = path.join(outputDir, 'convert-draco');
      await fileUtils.prepareEmptyDir(dracoDir);
      const dracoFreePath = path.join(outputDir, 'convert-draco', `draco-free${ext}`);
      await decompressDracoGlb(encodingPath, dracoFreePath);
      finalInputPath = dracoFreePath;
    }
  }

  // 4. 抽取结构树与零件网格
  console.info(TAG, `抽取结构树（${path.basename(splitter)}）`);
  const splitDir = path.join(outputDir, 'convert-split-part');
  await fileUtils.prepareEmptyDir(splitDir);

  await spawn(
    env,
    [splitter, finalInputPath, path.join(splitDir, 'assembly-tree.json'), precision?.toString()],
    {},
    TAG
  );

  return splitDir;
}
