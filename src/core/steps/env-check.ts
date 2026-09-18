import * as fs from 'fs';
import * as path from 'path';
import { exec, spawn } from '@/utils/child-process-utils.js';
import { logger } from '@/utils/logger.js';
import { verify_occ } from '@/scripts/index.js';

const TAG = 'env-check';
const CAD_ENV_NAME = 'cad_env';

// ============================================================
// 版本锁定
// ------------------------------------------------------------
// 目标环境：python=3.11 + pythonocc-core=7.7.2
// ============================================================
const PYTHON_VERSION = '3.11';
const PYTHONOCC_VERSION = '7.7.2';
const CONDA_CHANNEL = 'conda-forge';

/** 读取 Python 主次版本号（如 "3.11"） */
const GET_PY_VERSION_SCRIPT =
  "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')";

export type EnvCheckResult = { success: true; data: string };
export type EnvCheckError = {
  success: false;
  message: string;
};

// =====================================================================
// 基础工具
// =====================================================================

/** 计算 cad_env 内 python 可执行文件路径 */
const resolveEnvPython = (condaBaseDir: string): string =>
  process.platform === 'win32'
    ? path.join(condaBaseDir, 'envs', CAD_ENV_NAME, 'python.exe')
    : path.join(condaBaseDir, 'envs', CAD_ENV_NAME, 'bin', 'python');

/** 读取指定 python 的主次版本号（"3.11" 或 null） */
const getPythonVersion = async (pythonPath: string): Promise<string | null> => {
  try {
    const result = await exec(`"${pythonPath}" -c "${GET_PY_VERSION_SCRIPT}"`, {}, TAG);
    return result.message.trim() || null;
  } catch {
    return null;
  }
};

/**
 * 验证 OCC 是否可用且版本匹配
 * - OCC.Core 能 import
 * - OCC.VERSION === PYTHONOCC_VERSION
 */
const verifyOccRuntime = async (pythonPath: string): Promise<boolean> => {
  try {
    await spawn(pythonPath, [verify_occ, PYTHONOCC_VERSION], {}, TAG);
    return true;
  } catch {
    return false;
  }
};
// =====================================================================
// Conda 与环境操作
// =====================================================================

/** 检查 Conda 工具是否可用，返回其 base 目录 */
const checkConda = async (): Promise<EnvCheckResult | EnvCheckError> => {
  logger.info(TAG, '正在检测 Conda 工具');
  try {
    const versionResult = await exec('conda --version', {}, TAG);
    const baseResult = await exec('conda info --base', {}, TAG);
    const condaBaseDir = baseResult.message.trim().replace(/\r/g, '');

    logger.info(
      TAG,
      `检测到 Conda 工具; version: ${versionResult.message.trim()} dir: ${condaBaseDir}`
    );
    return { success: true, data: condaBaseDir };
  } catch {
    return {
      success: false,
      message: '未检测到 Conda 工具，请先安装 Miniconda / Anaconda 并配置环境变量。',
    };
  }
};

/** 删除 cad_env（不存在时静默忽略） */
const removeCadEnv = async (): Promise<void> => {
  try {
    logger.info(TAG, `正在删除旧的 [${CAD_ENV_NAME}] 环境...`);
    await spawn('conda', ['env', 'remove', '-n', CAD_ENV_NAME, '-y'], {}, TAG);
    logger.info(TAG, `[${CAD_ENV_NAME}] 环境已删除`);
  } catch {
    logger.info(TAG, `[${CAD_ENV_NAME}] 环境不存在或删除失败，跳过`);
  }
};

/** 创建 cad_env（锁定 Python 版本） */
const createCadEnv = async (): Promise<void> => {
  logger.info(TAG, `正在创建 [${CAD_ENV_NAME}] 环境 (python=${PYTHON_VERSION}) ...`);
  await spawn(
    'conda',
    ['create', '-n', CAD_ENV_NAME, '-c', CONDA_CHANNEL, `python=${PYTHON_VERSION}`, '-y'],
    {},
    TAG
  );
};

/** 安装锁定的 pythonocc-core */
const installPythonocc = async (): Promise<void> => {
  logger.info(TAG, `正在安装 pythonocc-core=${PYTHONOCC_VERSION} (channel=${CONDA_CHANNEL}) ...`);
  await spawn(
    'conda',
    [
      'install',
      '-n',
      CAD_ENV_NAME,
      '-c',
      CONDA_CHANNEL,
      `pythonocc-core=${PYTHONOCC_VERSION}`,
      '-y',
      '--clobber',
    ],
    {},
    TAG
  );
};

// =====================================================================
// 环境检测与构建
// =====================================================================

/**
 * 检查现有 cad_env 是否满足要求
 * 判定条件（全部满足即放行）：
 *   1. 环境目录存在（对应 python 可执行文件存在）
 *   2. Python 主次版本与 PYTHON_VERSION 一致
 *   3. OCC 能 import 且 OCC.VERSION 与 PYTHONOCC_VERSION 一致
 * 返回 success=true 时附带 python 可执行路径
 */
const checkExistingEnv = async (condaBaseDir: string): Promise<EnvCheckResult | EnvCheckError> => {
  const pythonPath = resolveEnvPython(condaBaseDir);

  // 1. 环境是否存在
  if (!fs.existsSync(pythonPath)) {
    return {
      success: false,
      message: `未找到 [${CAD_ENV_NAME}] 环境`,
    };
  }

  // 2. Python 版本是否匹配
  const actualPyVer = await getPythonVersion(pythonPath);
  if (actualPyVer !== PYTHON_VERSION) {
    return {
      success: false,
      message:
        `[${CAD_ENV_NAME}] Python 版本为 ${actualPyVer ?? '未知'}，` + `期望 ${PYTHON_VERSION}`,
    };
  }

  // 3. OCC 是否可导入且版本匹配
  if (!(await verifyOccRuntime(pythonPath))) {
    return {
      success: false,
      message: `[${CAD_ENV_NAME}] OCC 不可用或版本不是 ${PYTHONOCC_VERSION}`,
    };
  }

  logger.info(
    TAG,
    `检测到 [${CAD_ENV_NAME}] 环境：python=${actualPyVer}，OCC=${PYTHONOCC_VERSION}`
  );
  return { success: true, data: pythonPath };
};

/**
 * 从头构建 cad_env：删除 → 创建 → 安装 → 验证
 */
const buildCadEnvFromScratch = async (
  condaBaseDir: string
): Promise<EnvCheckResult | EnvCheckError> => {
  const pythonPath = resolveEnvPython(condaBaseDir);

  // 1. 删除旧环境
  await removeCadEnv();

  // 2. 创建环境
  try {
    await createCadEnv();
  } catch {
    return {
      success: false,
      message: `创建 [${CAD_ENV_NAME}] 环境失败，请检查 Conda 状态或网络。`,
    };
  }

  // 3. 安装 pythonocc-core
  try {
    await installPythonocc();
  } catch {
    return {
      success: false,
      message:
        `安装 pythonocc-core=${PYTHONOCC_VERSION} 失败，` +
        `请检查网络或 ${CONDA_CHANNEL} 源可用性。`,
    };
  }

  // 4. 可用性验证（OCC 能 import 且版本匹配）
  if (!(await verifyOccRuntime(pythonPath))) {
    return {
      success: false,
      message:
        `pythonocc-core=${PYTHONOCC_VERSION} 安装后验证失败：` +
        `OCC 无法 import 或版本不等于 ${PYTHONOCC_VERSION}。\n` +
        `请手动验证：\n` +
        `  ${pythonPath} -c "import OCC; print(OCC.VERSION)"`,
    };
  }

  logger.info(TAG, `[${CAD_ENV_NAME}] 环境构建完成并验证通过！`);
  return { success: true, data: pythonPath };
};

// =====================================================================
// 主入口
// =====================================================================

/**
 * 主入口：严格模式
 *   1. 必须检测到 Conda
 *   2. 检查 cad_env 是否满足 [Python 版本 + OCC 版本] 双重要求
 *   3. 不满足 → 推倒重建（不做增量修复、不降级到系统 Python）
 *
 */
export const checkLocalEnvironment = async (): Promise<EnvCheckResult | EnvCheckError> => {
  // 1. Conda 是硬性要求
  const condaResult = await checkConda();
  if (!condaResult.success) {
    return condaResult;
  }

  const condaBaseDir = condaResult.data;

  // 2. 检查现有环境
  const existing = await checkExistingEnv(condaBaseDir);
  if (existing.success) {
    return existing;
  }

  // 3. 不满足要求 → 推倒重建
  logger.warn(TAG, `${existing.message}，开始重建 [${CAD_ENV_NAME}] 环境...`);
  return await buildCadEnvFromScratch(condaBaseDir);
};
