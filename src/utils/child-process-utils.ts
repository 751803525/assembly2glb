import { logger } from '@/cli/logger.js';
import { ChildProcess, spawn as spawnImpl, exec as execImpl, SpawnOptions } from 'child_process';
import { ExecException, ExecOptionsWithBufferEncoding } from 'node:child_process';
import { StringDecoder } from 'node:string_decoder';
import * as iconv from 'iconv-lite';
const spawn = (
  command: string,
  args?: readonly string[],
  options?: SpawnOptions,
  logTag?: string
) => {
  if (logTag) {
    const append = args ? ' ' + args.join(' ') : '';
    logger.info(logTag, `执行指令：${command.trim()} ${append}`);
  }
  return new Promise((resolve, reject) => {
    let child: ChildProcess = args
      ? spawnImpl(command.trim(), args, {
          ...options,
          env: {
            ...options?.env,
            PYTHONIOENCODING: 'utf-8',
          },
        })
      : spawnImpl(command.trim(), {
          ...options,
          env: {
            ...options?.env,
            PYTHONIOENCODING: 'utf-8',
          },
        });
    if (logTag) {
      const stdoutDec = new StringDecoder('utf-8');
      child.stdout?.on('data', (data: Buffer) => {
        stdoutDec
          .write(data)
          .split('\n')
          .forEach((l) => {
            if (l.trim()) {
              logger.info(logTag, l);
            }
          });
      });
      const stderrDec = new StringDecoder('utf-8');
      child.stderr?.on('data', (data: Buffer) => {
        stderrDec
          .write(data)
          .split('\n')
          .forEach((l) => {
            if (l.trim()) {
              logger.info(logTag, l);
            }
          });
      });
    }
    child.on('close', (code) => {
      if (code === 0) {
        resolve({ code });
      } else {
        reject(new Error(`${logTag || ''}进程异常退出，退出码: ${code}`));
      }
    });
    child.on('error', (err) => {
      if (logTag) {
        logger.error(logTag, `无法启动 ${command} 进程，请检查路径是否存在。`);
        logger.error(logTag, err);
      }
      reject(err);
    });
  });
};

const exec = (
  command: string,
  options?: Omit<ExecOptionsWithBufferEncoding, 'encoding'>,
  logTag?: string
) => {
  const safeDecode = (data: Buffer<ArrayBuffer>): string => {
    return iconv.decode(Buffer.from(data), process.platform == 'win32' ? 'gbk' : 'utf-8').trim();
  };
  if (logTag) {
    logger.info(logTag, `执行指令：${command}`);
  }
  return new Promise<{ code: number; message: string }>((resolve, reject) => {
    execImpl(
      command,
      {
        ...options,
        encoding: 'buffer',
      },
      (error: ExecException | null, stdout: Buffer<ArrayBuffer>, stderr: Buffer<ArrayBuffer>) => {
        const result = safeDecode(stdout);

        if (result && result.length && logTag) {
          logger.info(logTag, result);
        }
        if (stderr && stderr.length && logTag) {
          logger.warn(logTag, safeDecode(stderr));
        }

        if (error) {
          reject(error);
        } else {
          resolve({ code: 0, message: result });
        }
      }
    );
  });
};
export { spawn, exec };
