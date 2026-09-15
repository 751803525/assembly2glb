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
    try {
      await fs.ensureDir(dir);
    } catch (e: any) {
      throw e;
    }
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
    try {
      if (fs.statSync(from).isFile()) {
        const dir = path.dirname(to);
        await fileUtils.ensureDir(dir);
        await fs.copyFile(from, to);
      } else {
        await fileUtils.ensureDir(to);
        await fs.copy(from, to, opt);
      }
      return to;
    } catch (e: any) {
      throw e;
    }
  },
  emptyDir: async function (p: string) {
    if (await fileUtils.exists(p)) {
      const stat = await fs.stat(p);
      if (stat.isFile()) {
        await fs.remove(p);
      } else {
        await fs.emptyDir(p);
      }
    } else {
      fileUtils.ensureDir(path.dirname(p));
    }
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
      result.filter((name) => {
        const ext = path.extname(name).toLocaleLowerCase();
        return ext == suffix.toLocaleLowerCase();
      });
    }
    return result.map((name) => path.join(dir, name));
  },
};
