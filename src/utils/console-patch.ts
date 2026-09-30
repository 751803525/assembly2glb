// console-patch.ts
import { createWriteStream, mkdirSync, writeFileSync, type WriteStream } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { format } from 'node:util';

export type LogLevel = 'LOG' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';

export interface LoggerConfig {
  /** 是否把日志同时写入文件 */
  enabled: boolean;
  /** 文件路径，enabled 为 true 时必填；相对路径按 process.cwd() 解析 */
  filePath?: string;
  /** 是否自动创建缺失的父目录，默认 true */
  mkdir?: boolean;
  /**
   * 追加模式；
   * - false（默认）：启动时清空文件一次，本次运行内仍持续追加；
   * - true：保留上次内容，本次继续追加。
   * 脚本场景推荐默认值。
   */
  append?: boolean;
}

// ---- 备份原生方法 ----
// native：未 bind 的原始引用，用于 restore
// bound：bind 过的引用，用于在覆盖后安全调用原生实现
const native = {
  log: console.log,
  info: console.info,
  warn: console.warn,
  error: console.error,
  debug: console.debug,
};

const bound = {
  log: native.log.bind(console),
  info: native.info.bind(console),
  warn: native.warn.bind(console),
  error: native.error.bind(console),
  debug: native.debug.bind(console),
};

// ---- 内部状态 ----
let fileStream: WriteStream | null = null;
let isPatched = false;

// ---- 配置入口：开关 + 路径 ----
export function configureLogger(config: LoggerConfig): void {
  // 先关闭旧流
  if (fileStream) {
    fileStream.end();
    fileStream = null;
  }

  if (!config.enabled) return;

  if (!config.filePath) {
    // enabled 为 true 却没给路径，明确提示，而不是静默失败
    process.stderr.write('[logger] enabled 为 true 但未提供 filePath，文件输出已跳过\n');
    return;
  }

  // 1) 转绝对路径：相对路径基于 process.cwd()，避免语义歧义
  const absPath = resolve(config.filePath);

  // 2) 创建父目录（默认递归创建）
  const autoMkdir = config.mkdir ?? true;
  if (autoMkdir) {
    try {
      mkdirSync(dirname(absPath), { recursive: true });
    } catch (err) {
      process.stderr.write(
        `[logger] 创建日志目录失败: ${(err as Error).message}，文件输出已跳过\n`
      );
      return;
    }
  }

  // 3) 清空只在这里做一次（除非显式要求 append）
  if (!(config.append ?? false)) {
    try {
      writeFileSync(absPath, ''); // 文件不存在则创建，存在则清空
    } catch (err) {
      process.stderr.write(
        `[logger] 清空日志文件失败: ${(err as Error).message}，文件输出已跳过\n`
      );
      return;
    }
  }

  // 4) 之后用追加模式：本次运行内所有 write 持续追加
  try {
    fileStream = createWriteStream(absPath, { flags: 'a' });
    fileStream.on('error', (err) => {
      // 文件写失败只提示，不影响主流程；出错后停止文件输出，避免刷屏
      process.stderr.write(`[logger] 文件写入失败，后续文件输出已停止: ${err.message}\n`);
      fileStream?.end();
      fileStream = null;
    });
  } catch (err) {
    // createWriteStream 大多数错误是异步的，这里兜同步异常（如参数非法）
    process.stderr.write(`[logger] 创建日志流失败: ${(err as Error).message}\n`);
    fileStream = null;
  }
}

// ---- 时间格式化 ----
function timestamp(): string {
  const d = new Date();
  const pad = (n: number, len = 2) => String(n).padStart(len, '0');
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.` +
    `${pad(d.getMilliseconds(), 3)}`
  );
}

// ---- 核心写入 ----
function write(level: LogLevel, output: (...args: unknown[]) => void, args: unknown[]): void {
  const prefix = `[${timestamp()}] [${level}]`;

  // 1) 控制台：走备份的原生方法，保留颜色和对象格式化
  output(prefix, ...args);

  // 2) 文件：受开关控制，stream 为 null 时跳过
  if (fileStream) {
    // util.format 与 console 的格式化规则一致，但不带 ANSI 颜色码
    fileStream.write(`${prefix} ${format(...args)}\n`);
  }
}

// ---- 覆盖 console ----
export function patchConsole(): void {
  if (isPatched) return;
  isPatched = true;

  console.log = (...args: unknown[]) => write('LOG', bound.log, args);
  console.info = (...args: unknown[]) => write('INFO', bound.info, args);
  console.warn = (...args: unknown[]) => write('WARN', bound.warn, args);
  console.error = (...args: unknown[]) => write('ERROR', bound.error, args);
  console.debug = (...args: unknown[]) => write('DEBUG', bound.debug, args);
}

// ---- 恢复原生 console（测试或临时关闭时用）----
export function restoreConsole(): void {
  if (!isPatched) return;
  isPatched = false;

  console.log = native.log;
  console.info = native.info;
  console.warn = native.warn;
  console.error = native.error;
  console.debug = native.debug;
}

// ---- 优雅退出：确保缓冲区落盘 ----
export function flushLogger(): Promise<void> {
  return new Promise((resolve) => {
    if (!fileStream) return resolve();
    const stream = fileStream;
    fileStream = null; // 提前置空，避免 flush 后又有写入进入已 end 的流
    stream.end(() => resolve());
  });
}
