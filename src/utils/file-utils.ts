import fs from 'fs-extra';
import path from 'node:path';
import bfj from 'bfj';
import { logger } from '@/utils/logger.js';

export const fileUtils = {
  exists: async function (filePath: string): Promise<boolean> {
    try {
      return await fs.pathExists(filePath);
    } catch {
      return false;
    }
  },
  ensureDir: async function (dir: string) {
    await fs.ensureDir(dir);
  },
  remove: async function (dir: string): Promise<void> {
    if (await fileUtils.exists(dir)) {
      logger.info('清理目录', `开始清理临时目录: ${dir}`);
      await fs.remove(dir);
      logger.info('清理目录', `清理临时目录完成: ${dir}`);
    } else {
      logger.info('清理目录', `${dir} 目录不存在跳过清理！`);
    }
  },
  copy: async function (
    from: string,
    to: string,
    opt?: {
      filter?: (from: string, to: string) => boolean;
      overwrite?: boolean;
      errorOnExist?: boolean;
    }
  ): Promise<string> {
    if (fs.statSync(from).isFile()) {
      const dir = path.dirname(to);
      await fileUtils.ensureDir(dir);
      await fs.copyFile(from, to);
    } else {
      await fileUtils.ensureDir(to);
      await fs.copy(from, to, opt);
    }
    return to;
  },
  /**
   * 准备一个空目录：保证 dirPath 存在且为空。
   * 用于"接下来要往这个目录里写一批文件"的场景，
   * 特别是 tempDir 里反复运行、需要丢掉上次残留的情况。
   */
  prepareEmptyDir: async function (dirPath: string): Promise<void> {
    if (await fs.pathExists(dirPath)) {
      const stat = await fs.stat(dirPath);
      if (stat.isDirectory()) {
        await fs.emptyDir(dirPath);
        return;
      }
      // 是文件：删掉，再建同名目录
      await fs.remove(dirPath);
    }
    await fs.ensureDir(dirPath);
  },

  /**
   * 准备写一个文件：保证 filePath 的父目录存在，并清掉 filePath 上
   *
   * 注意：不清空父目录，因此同级其他文件不受影响。
   * 用于"接下来要写 filePath"这类单文件场景。
   */
  prepareForFile: async function (filePath: string): Promise<void> {
    await fs.ensureDir(path.dirname(filePath));
    await fs.remove(filePath);
  },
  writeFile: async function (filePath: string, data: any, space: number = 2): Promise<void> {
    // 1. 确保目录存在
    await fs.ensureDir(path.dirname(filePath));

    // 2. 创建写入流
    const writeStream = fs.createWriteStream(filePath);

    // 3. 使用 bfj 异步管道式序列化并写入
    return new Promise((resolve, reject) => {
      bfj
        .streamify(data, { space })
        .pipe(writeStream)
        .on('finish', () => resolve())
        .on('error', (err: any) => reject(err));
    });
  },

  readdir: async function (dir: string, suffix?: string): Promise<string[]> {
    if (!this.exists(dir)) {
      return [] as string[];
    }
    if (!(await fs.stat(dir)).isDirectory()) {
      return [] as string[];
    }
    let result = await fs.readdir(dir, { encoding: 'utf-8' });
    if (suffix) {
      const normalized = suffix.startsWith('.')
        ? suffix.toLocaleLowerCase()
        : '.' + suffix.toLocaleLowerCase();

      result = result.filter((name) => {
        const ext = path.extname(name).toLocaleLowerCase();
        return ext === normalized;
      });
    }
    return result.map((name) => path.join(dir, name));
  },
};
