import path from 'node:path';
import fs from 'node:fs/promises';
import fsSync from 'node:fs';
import { createRequire } from 'node:module';
import { spawn as nodeSpawn } from 'node:child_process';

const TAG = 'draco';
const DRACO_EXT = 'KHR_draco_mesh_compression';

const require = createRequire(import.meta.url);

/**
 * 定位一个 npm 包的目录。
 * 绕过 exports 字段，用 require.resolve.paths + 文件系统查找。
 */
function findPackageDir(pkgName: string): string {
  const searchPaths = require.resolve.paths(pkgName) ?? [];
  for (const base of searchPaths) {
    const candidate = path.join(base, pkgName);
    if (fsSync.existsSync(path.join(candidate, 'package.json'))) {
      return fsSync.realpathSync(candidate);
    }
  }
  throw new Error(
    `找不到包目录: ${pkgName}\n` +
      `搜索路径:\n  ${searchPaths.join('\n  ')}\n` +
      `请执行: npm i @gltf-transform/cli`
  );
}

/**
 * 定位项目本地安装的 gltf-transform CLI 入口。
 */
function resolveGltfTransformBin(): string {
  const pkgDir = findPackageDir('@gltf-transform/cli');
  const pkg = JSON.parse(fsSync.readFileSync(path.join(pkgDir, 'package.json'), 'utf-8'));

  let binRel: string;
  if (typeof pkg.bin === 'string') {
    binRel = pkg.bin;
  } else if (pkg.bin && typeof pkg.bin === 'object') {
    binRel = pkg.bin['gltf-transform'] ?? (Object.values(pkg.bin)[0] as string);
  } else {
    throw new Error('@gltf-transform/cli 的 package.json 缺少 bin 字段');
  }
  return path.resolve(pkgDir, binRel);
}

const GLTF_TRANSFORM_BIN = resolveGltfTransformBin();

if (!fsSync.existsSync(GLTF_TRANSFORM_BIN)) {
  throw new Error(
    `gltf-transform CLI 入口不存在: ${GLTF_TRANSFORM_BIN}\n` + `请执行: npm i @gltf-transform/cli`
  );
}

/**
 * 检测 .glb 是否使用 Draco 压缩。
 */
export async function glbUsesDraco(filePath: string): Promise<boolean> {
  const fh = await fs.open(filePath, 'r');
  try {
    const header = Buffer.alloc(12);
    await fh.read(header, 0, 12, 0);
    if (header.toString('ascii', 0, 4) !== 'glTF') {
      return false;
    }

    const totalLen = header.readUInt32LE(8);
    let offset = 12;
    while (offset < totalLen) {
      const chunkHeader = Buffer.alloc(8);
      await fh.read(chunkHeader, 0, 8, offset);
      const chunkLen = chunkHeader.readUInt32LE(0);
      const chunkType = chunkHeader.toString('ascii', 4, 8);

      if (chunkType === 'JSON') {
        const jsonBuf = Buffer.alloc(chunkLen);
        await fh.read(jsonBuf, 0, chunkLen, offset + 8);
        const gltf = JSON.parse(jsonBuf.toString('utf-8'));
        const used = new Set<string>(gltf.extensionsUsed ?? []);
        const required = new Set<string>(gltf.extensionsRequired ?? []);
        const hasDraco = used.has(DRACO_EXT) || required.has(DRACO_EXT);
        if (hasDraco) {
          console.info(TAG, `检测到 Draco 压缩: ${filePath}`);
        }
        return hasDraco;
      }
      offset += 8 + chunkLen;
    }
    return false;
  } finally {
    await fh.close();
  }
}

/**
 * 检测 .gltf（文本 JSON）是否使用 Draco 压缩。
 */
export async function gltfUsesDraco(filePath: string): Promise<boolean> {
  const text = await fs.readFile(filePath, 'utf-8');
  const gltf = JSON.parse(text);
  const used = new Set<string>(gltf.extensionsUsed ?? []);
  const required = new Set<string>(gltf.extensionsRequired ?? []);
  return used.has(DRACO_EXT) || required.has(DRACO_EXT);
}

/**
 * 用项目本地安装的 gltf-transform 把 Draco 解压为标准 GLB。
 * 跑在 Node 上，不涉及 Python 环境。
 */
export async function decompressDracoGlb(inputPath: string, outputPath: string): Promise<void> {
  console.info(TAG, `解压 Draco: ${inputPath} → ${outputPath}`);

  return new Promise((resolve, reject) => {
    const child = nodeSpawn(process.execPath, [GLTF_TRANSFORM_BIN, 'copy', inputPath, outputPath], {
      stdio: ['ignore', 'inherit', 'inherit'],
    });

    child.on('error', (err) => {
      console.error(TAG, `spawn 失败: ${err.message}`);
      reject(err);
    });

    child.on('close', (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`gltf-transform 退出码 ${code}`));
      }
    });
  });
}
