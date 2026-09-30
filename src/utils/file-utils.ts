import fs from 'fs-extra';
import path from 'node:path';
import bfj from 'bfj';

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
      console.info('清理目录', `开始清理临时目录: ${dir}`);
      await fs.remove(dir);
      console.info('清理目录', `清理临时目录完成: ${dir}`);
    } else {
      console.info('清理目录', `${dir} 目录不存在跳过清理！`);
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
    const stat = await fs.stat(from);
    if (stat.isFile()) {
      const filter = opt?.filter?.apply(null, [from, to]);
      if (!filter) {
        const dir = path.dirname(to);
        await fileUtils.ensureDir(dir);
        await fs.copyFile(from, to);
      }
    } else {
      const result = await fs.readdir(from);
      for (const item of result) {
        await this.copy(path.join(from, item), path.join(to, item), opt);
      }
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
  writeFile: async function (filePath: string, data: unknown, space: number = 2): Promise<void> {
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
        .on('error', (err: never) => reject(err));
    });
  },

  readdir: async function (dir: string, ...suffixes: (string | string[])[]): Promise<string[]> {
    if (!this.exists(dir)) {
      return [] as string[];
    }
    if (!(await fs.stat(dir)).isDirectory()) {
      return [] as string[];
    }

    const result: string[] = [];
    const children = await fs.readdir(dir, { encoding: 'utf-8' });
    for (const item of children) {
      const child = path.join(dir, item);
      const stat = await fs.stat(child);

      if (stat.isDirectory()) {
        const r = await this.readdir(child);
        result.push(...r);
      } else {
        result.push(child);
      }
    }

    // 拍平 + 归一化
    const flat: Set<string> = new Set();
    for (const s of suffixes) {
      if (Array.isArray(s)) {
        for (const x of s) {
          if (x.startsWith('.')) {
            flat.add(x.toLowerCase());
          } else {
            flat.add(`.${x.toLowerCase()}`);
          }
        }
      } else if (s) {
        if (s.startsWith('.')) {
          flat.add(s.toLowerCase());
        } else {
          flat.add(`.${s.toLowerCase()}`);
        }
      }
    }
    if (flat.size > 0) {
      return result.filter((item) => {
        return flat.has(path.extname(item).toLocaleLowerCase());
      });
    }
    return result;
  },

  readdirByFilter: async function (
    dir: string,
    ...suffixes: (string | string[])[]
  ): Promise<string[]> {
    if (!this.exists(dir)) {
      return [] as string[];
    }
    if (!(await fs.stat(dir)).isDirectory()) {
      return [] as string[];
    }

    const result: string[] = [];
    const children = await fs.readdir(dir, { encoding: 'utf-8' });
    for (const item of children) {
      const child = path.join(dir, item);
      const stat = await fs.stat(child);
      if (stat.isDirectory()) {
        const r = await this.readdir(child);
        result.push(...r);
      } else {
        result.push(child);
      }
    }

    // 拍平 + 归一化
    const flat: Set<string> = new Set();
    for (const s of suffixes) {
      if (Array.isArray(s)) {
        for (const x of s) {
          if (x.startsWith('.')) {
            flat.add(x.toLowerCase());
          } else {
            flat.add(`.${x.toLowerCase()}`);
          }
        }
      } else if (s) {
        if (s.startsWith('.')) {
          flat.add(s.toLowerCase());
        } else {
          flat.add(`.${s.toLowerCase()}`);
        }
      }
    }
    if (flat.size > 0) {
      return result.filter((item) => {
        return !flat.has(path.extname(item).toLocaleLowerCase());
      });
    }
    return result;
  },
};
