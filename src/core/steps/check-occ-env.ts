import { logger } from '@/cli/logger.js';
import * as fs from 'fs';
import * as path from 'path';
import { exec, spawn } from '@/utils/child-process-utils.js';
// 假设你有类似的日志工具，这里做简单的 mock 或替换为你的 logger
const TAG = 'env-check';

export interface EnvCheckResult {
  /** 全流程/单步骤是否校验通过 */
  success: boolean;
  /** 可直接调用的目标 Python 可执行文件路径（成功时返回） */
  pythonPath?: string;
  /** 失败卡点的步骤: 1-Python, 2-Conda, 3-OCC */
  failedStep?: 1 | 2 | 3;
  /** 详细的成功/报错说明 */
  message: string;
}
/**
 * 步骤一：检查系统 Python 环境
 */
const checkPython = async (): Promise<EnvCheckResult> => {
  logger.info(TAG, '正在检查系统 Python 环境');
  try {
    const checkPythonCmd = process.platform === 'win32' ? 'python --version' : 'python3 --version';
    const result = await exec(checkPythonCmd, {}, TAG);
    logger.info(TAG, `系统 Python 环境存在; version: ${result.message}`);
    return { success: true, message: '系统 Python 环境正常' };
  } catch (err) {
    return {
      success: false,
      failedStep: 1,
      message: '未检测到系统 Python 环境！请先安装 Python (建议 3.9 ~ 3.11 版本)。',
    };
  }
};

/**
 * 步骤二：检查 Conda 环境
 */
const checkConda = async (): Promise<EnvCheckResult & { condaBaseDir?: string }> => {
  logger.info(TAG, '正在检查 Conda 工具');
  try {
    const condaVersion = await exec('conda --version', {}, TAG);
    const condaBaseDir = await exec('conda info --base', {}, TAG);
    logger.info(
      TAG,
      `检测到 Conda 工具; version: ${condaVersion.message} dir:${condaBaseDir.message}`
    );
    return {
      success: true,
      condaBaseDir: condaBaseDir.message,
      message: 'Conda 工具准备就绪',
    };
  } catch (err) {
    return {
      success: false,
      failedStep: 2,
      message: '未检测到 Conda 工具，请安装并配置环境变量。',
    };
  }
};

/**
 * 步骤三：检查 Python OCC 依赖（支持复用与自动补全）
 */
const checkOcc = async (condaBaseDir: string): Promise<EnvCheckResult> => {
  logger.info(TAG, '正在检查 Python OCC (pythonocc-core) 依赖');
  // 获取系统默认 Python 路径
  let defaultPython = '';
  try {
    const cmd = process.platform === 'win32' ? 'where python' : 'which python3 || which python';
    const result = await exec(cmd, {}, TAG);
    defaultPython = result.message.split('\n')[0]?.trim();
  } catch (e) {
    // 理论上不会走到这里，因为第一步已校验通过
  }

  // 1. 情况 A：尝试直接导入默认 Python 中的 OCC
  if (defaultPython) {
    try {
      await spawn(defaultPython, ['-c', 'import OCC.Core'], {}, TAG);
      logger.info(TAG, '直接在系统默认 Python 环境中识别到 OCC 依赖！');
      return { success: true, pythonPath: defaultPython, message: '默认环境 OCC 正常' };
    } catch (e) {
      logger.info(TAG, '系统默认 Python 中未发现 OCC，检查历史 [cad_env] 环境...');
    }
  }

  // 2. 情况 B：检查上次自动创建的 cad_env
  const cadEnvName = 'cad_env';
  const cadEnvPython =
    process.platform === 'win32'
      ? path.join(condaBaseDir, 'envs', cadEnvName, 'python.exe')
      : path.join(condaBaseDir, 'envs', cadEnvName, 'bin', 'python');

  if (fs.existsSync(cadEnvPython)) {
    try {
      await exec(`"${cadEnvPython}" -c "import OCC.Core"`, {}, TAG);
      logger.info(TAG, `成功复用已存在的 [${cadEnvName}] OCC 依赖！`);
      return { success: true, pythonPath: cadEnvPython, message: `复用环境 [${cadEnvName}] 成功` };
    } catch (e) {
      logger.warn(TAG, `检测到 [${cadEnvName}] 环境存在，但 OCC 损坏或缺失。`);
    }
  } else {
    logger.info(TAG, `未找到 [${cadEnvName}] 历史环境，准备自动创建...`);
    try {
      await exec(`conda create -n ${cadEnvName} python=3.10 -y`, {}, TAG);
    } catch (createErr) {
      return {
        success: false,
        failedStep: 3,
        message: `自动创建 Conda 隔离环境 [${cadEnvName}] 失败。`,
      };
    }
  }

  // 3. 情况 C：自动静默安装 OCC 依赖
  logger.warn(TAG, `开始在 [${cadEnvName}] 中通过 conda-forge 安装 pythonocc-core...`);
  try {
    await spawn(
      'conda',
      ['install', '-n', cadEnvName, '-c', 'conda-forge', 'pythonocc-core', '-y', '--clobber'],
      {},
      TAG
    );
    await spawn(cadEnvPython, ['-c', 'import OCC.Core'], {}, TAG);
    logger.info(TAG, 'pythonocc-core 安装成功并验证通过！');
    return { success: true, pythonPath: cadEnvPython, message: '首次自动安装 OCC 成功' };
  } catch (installErr) {
    return {
      success: false,
      failedStep: 3,
      message: '尝试安装 pythonocc-core 失败，请检查网络或 Conda 镜像源。',
    };
  }
};

/**
 * 主入口：组合三个独立方法进行环境检测
 */
export const checkLocalEnvironment = async (): Promise<EnvCheckResult> => {
  // 执行步骤 1
  let pythonResult = await checkPython();
  if (!pythonResult.success) {
    return pythonResult;
  }
  // 执行步骤 2
  const condaResult = await checkConda();
  if (!condaResult.success) {
    return condaResult;
  }

  // 执行步骤 3 (依赖步骤 2 的 condaBaseDir)
  const occResult = await checkOcc(condaResult.condaBaseDir!);
  return occResult;
};
