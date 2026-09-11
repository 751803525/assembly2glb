import { logger } from '@/cli/logger.js';
import * as fs from 'fs';
import * as path from 'path';
import { exec, spawn } from '@/utils/child-process-utils.js';

const TAG = 'env-check';
const cadEnvName = 'cad_env';

export type EnvCheckResult = { success: true; data: string };
export type EnvCheckError = {
  /** 全流程/单步骤是否校验通过 */
  success: false;
  /** 详细的成功/报错说明 */
  message: string;
};

/**
 * 检查 Conda 环境
 */
const checkConda = async (): Promise<EnvCheckResult | EnvCheckError> => {
  logger.info(TAG, '正在检测 Conda 工具');
  try {
    const condaVersion = await exec('conda --version', {}, TAG);
    const condaBaseDir = await exec('conda info --base', {}, TAG);
    const cleanedBaseDir = condaBaseDir.message.trim().replace(/\r/g, '');

    logger.info(
      TAG,
      `检测到 Conda 工具; version: ${condaVersion.message.trim()} dir:${cleanedBaseDir}`
    );

    return {
      success: true,
      data: cleanedBaseDir,
    };
  } catch (err) {
    return {
      success: false,
      message: '未检测到 Conda 工具，请安装并配置环境变量。',
    };
  }
};

/**
 * 检查系统 Python 环境
 */
const checkPython = async (): Promise<EnvCheckResult | EnvCheckError> => {
  logger.info(TAG, '正在检查系统 Python 环境');
  try {
    const isWin = process.platform === 'win32';
    const checkPythonCmd = isWin
      ? 'py -3 -c "import sys; print(sys.executable)" || python -c "import sys; print(sys.executable)"'
      : 'python3 -c "import sys; print(sys.executable)" || python -c "import sys; print(sys.executable)"';
    const result = await exec(checkPythonCmd, {}, TAG);

    // 清理 \r 并按换行截取第一行路径
    const pythonPath = result.message.replace(/\r/g, '').split('\n')[0]?.trim();

    if (pythonPath) {
      logger.info(TAG, `系统 Python 环境存在; path: ${pythonPath}`);
      return { success: true, data: pythonPath };
    }
  } catch (err) {}
  return {
    success: false,
    message: '未检测到系统 Python 环境！请先安装 Python (建议 3.9 ~ 3.11 版本)。',
  };
};

/**
 * 在指定 Conda 环境中安装并验证 OCC
 */
const importOcc = async (
  envName: string,
  pythonPath: string
): Promise<EnvCheckResult | EnvCheckError> => {
  try {
    logger.info(TAG, `正在向 [${envName}] 环境安装 pythonocc-core...`);
    await spawn(
      'conda',
      // -n 必须接环境名称 (envName)，而不是 python 路径
      ['install', '-n', envName, '-c', 'conda-forge', 'pythonocc-core', '-y', '--clobber'],
      {},
      TAG
    );
    await spawn(pythonPath, ['-c', 'import OCC.Core'], {}, TAG);
    logger.info(TAG, 'pythonocc-core 安装成功并验证通过！');
    return { success: true, data: pythonPath };
  } catch (installErr) {
    return {
      success: false,
      message: '尝试安装 pythonocc-core 失败，请检查网络或 Conda 镜像源。',
    };
  }
};

/**
 * 创建隔离环境并安装 OCC
 */
const createCadEnvAndImportOcc = async (
  condaBaseDir: string
): Promise<EnvCheckResult | EnvCheckError> => {
  const cadEnvPython =
    process.platform === 'win32'
      ? path.join(condaBaseDir, 'envs', cadEnvName, 'python.exe')
      : path.join(condaBaseDir, 'envs', cadEnvName, 'bin', 'python');
  try {
    logger.info(TAG, `正在创建隔离环境 [${cadEnvName}] ...`);
    await exec(`conda create -n ${cadEnvName} python=3.10 -y`, {}, TAG);
    return await importOcc(cadEnvName, cadEnvPython);
  } catch (createErr) {
    return {
      success: false,
      message: `自动创建 Conda 隔离环境 [${cadEnvName}] 失败。`,
    };
  }
};

/**
 * 检查 Conda 中是否包含 cad_env 及 OCC 依赖
 */
const checkOccByConda = async (condaBaseDir: string): Promise<EnvCheckResult | EnvCheckError> => {
  logger.info(TAG, '正在检测 Conda 中 cad_env 环境与 (pythonocc-core) 依赖');

  const cadEnvPython =
    process.platform === 'win32'
      ? path.join(condaBaseDir, 'envs', cadEnvName, 'python.exe')
      : path.join(condaBaseDir, 'envs', cadEnvName, 'bin', 'python');

  // 尝试复用已有的 cad_env
  if (fs.existsSync(cadEnvPython)) {
    try {
      await exec(`"${cadEnvPython}" -c "import OCC.Core"`, {}, TAG);
      logger.info(TAG, `检测到 [${cadEnvName}] 环境及 OCC 依赖！`);
      return { success: true, data: cadEnvPython };
    } catch (e) {
      logger.warn(TAG, `检测到 [${cadEnvName}] 环境存在，但 OCC 损坏或缺失，准备修复...`);
      return await importOcc(cadEnvName, cadEnvPython);
    }
  } else {
    return {
      success: false,
      message: `未找到 [${cadEnvName}] 环境`,
    };
  }
};

/**
 * 检查系统 Python 中是否包含 OCC 依赖
 */
const checkOccBySystem = async (systemPython: string): Promise<EnvCheckResult | EnvCheckError> => {
  logger.info(TAG, '正在检测系统默认 Python 中 (pythonocc-core) 依赖');

  try {
    await spawn(systemPython, ['-c', 'import OCC.Core'], {}, TAG);
    logger.info(TAG, '直接在系统默认 Python 环境中识别到 OCC 依赖！');
    return { success: true, data: systemPython };
  } catch (e) {
    return { success: false, message: '系统默认 Python 中未发现 OCC' };
  }
};

/**
 * 主入口：组合多级检测策略
 */
export const checkLocalEnvironment = async (): Promise<EnvCheckResult | EnvCheckError> => {
  // 1. 优先尝试 Conda 已有环境
  const condaResult = await checkConda();
  if (condaResult.success) {
    const occResult = await checkOccByConda(condaResult.data);
    if (occResult.success) {
      return occResult;
    }
  }

  // 2. 降级尝试系统 Python 是否天然具备 OCC
  const pythonResult = await checkPython();
  if (pythonResult.success) {
    const occResult = await checkOccBySystem(pythonResult.data);
    if (occResult.success) {
      return occResult;
    }
  }

  // 3. 最后使用 Conda 自动创建 cad_env 环境并安装 OCC
  if (condaResult.success) {
    return await createCadEnvAndImportOcc(condaResult.data);
  }

  return {
    success: false,
    message: '未检测到 Conda 工具或系统 Python 环境，请至少安装其中之一。',
  };
};
