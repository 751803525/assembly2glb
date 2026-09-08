import path from 'path';
import { logger } from '../cli/logger.js';
import { fileUtils } from './file.js';
import { promisify } from 'util';
import { exec } from 'child_process';
import fs from 'fs/promises';

const execAsync = promisify(exec);

/**
 * 寻找可用于执行 Python 脚本的 FreeCAD 解释器路径
 * 优先返回 FreeCAD 附带的 bin/python.exe，次选 freecadcmd.exe
 */
export async function findFreeCADCmd(): Promise<{ exe: string; isPython: boolean }> {
  // 1. 优先使用自定义环境变量
  if (process.env.FREECAD_CMD) {
    const customPath = process.env.FREECAD_CMD;
    if (await fileUtils.exists(customPath)) {
      logger.info(`使用环境变量指定的 FreeCAD: ${customPath}`);
      const isPython = path.basename(customPath).toLowerCase().startsWith('python');
      return { exe: customPath, isPython };
    }
    logger.warn(`环境变量 FREECAD_CMD 指向的路径不存在: ${customPath}`);
  }

  // 2. 检查系统 PATH 中的 freecadcmd 或 python
  try {
    const { stdout } = await execAsync('where freecadcmd', { timeout: 3000 });
    const paths = stdout
      .trim()
      .split('\n')
      .map((p) => p.trim())
      .filter(Boolean);
    if (paths.length > 0) {
      const cmdPath = paths[0];
      const binDir = path.dirname(cmdPath);
      const pyPath = path.join(binDir, 'python.exe');

      // 优先使用同目录下的 python.exe
      if (await fileUtils.exists(pyPath)) {
        logger.info(`从系统 PATH 的 FreeCAD 目录找到 python.exe: ${pyPath}`);
        return { exe: pyPath, isPython: true };
      }

      logger.info(`从系统 PATH 找到 freecadcmd: ${cmdPath}`);
      return { exe: cmdPath, isPython: false };
    }
  } catch {
    // 忽略 where 命令失败
  }

  // 3. 检查 Windows 注册表
  try {
    const { stdout } = await execAsync('reg query "HKLM\\SOFTWARE\\FreeCAD" /v InstallPath', {
      timeout: 3000,
    });
    const match = stdout.match(/InstallPath\s+REG_SZ\s+(.*)/);
    if (match && match[1]) {
      const installPath = match[1].trim();
      const binDir = path.join(installPath, 'bin');
      const pyPath = path.join(binDir, 'python.exe');
      const cmdPath = path.join(binDir, 'freecadcmd.exe');

      if (await fileUtils.exists(pyPath)) {
        logger.info(`从注册表找到 FreeCAD Python 解释器: ${pyPath}`);
        return { exe: pyPath, isPython: true };
      }
      if (await fileUtils.exists(cmdPath)) {
        logger.info(`从注册表找到 FreeCAD: ${cmdPath}`);
        return { exe: cmdPath, isPython: false };
      }
    }
  } catch {
    // 忽略注册表查询失败
  }

  // 4. 常见盘符与目录扫描（支持 C、D、E 盘等）
  const commonRoots = [
    'C:\\Program Files',
    'C:\\Program Files (x86)',
    'D:\\Program Files',
    'D:\\',
    'E:\\',
  ];
  for (const root of commonRoots) {
    try {
      const entries = await fs.readdir(root, { withFileTypes: true });
      for (const entry of entries) {
        if (entry.isDirectory() && entry.name.toLowerCase().startsWith('freecad')) {
          const binDir = path.join(root, entry.name, 'bin');
          const pyPath = path.join(binDir, 'python.exe');
          const cmdPath = path.join(binDir, 'freecadcmd.exe');

          // 优先寻找 bin/python.exe，彻底避开 FreeCAD 1.0 的 -c/[no supported file format] 报错
          if (await fileUtils.exists(pyPath)) {
            logger.info(`从常见目录找到 FreeCAD Python: ${pyPath}`);
            return { exe: pyPath, isPython: true };
          }

          if (await fileUtils.exists(cmdPath)) {
            logger.info(`从常见目录找到 FreeCAD Command: ${cmdPath}`);
            return { exe: cmdPath, isPython: false };
          }
        }
      }
    } catch {
      // 忽略无权限或不存在的盘符目录
    }
  }

  throw new Error(
    '未找到 FreeCAD 或其 Python 解释器。\n' +
      '请确保 FreeCAD 已安装，并执行以下任一操作：\n' +
      '  1. 设置环境变量 FREECAD_CMD 指向 bin/python.exe 或 freecadcmd.exe\n' +
      '  2. 将 FreeCAD 的 bin 目录添加到系统 PATH 中'
  );
}
