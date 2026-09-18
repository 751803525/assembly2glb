import { logger } from '@/utils/logger.js';
import { ChildProcess, spawn as spawnImpl, exec as execImpl, SpawnOptions } from 'child_process';
import { ExecException, ExecOptionsWithBufferEncoding } from 'node:child_process';
import { StringDecoder } from 'node:string_decoder';
import * as iconv from 'iconv-lite';
const spawn = (
  command: string,
  args?: (string | undefined)[],
  options?: SpawnOptions,
  logTag?: string
) => {
  const fliterUndefined = (s?: (string | undefined)[]): string[] | undefined => {
    return s?.filter((item) => {
      return item != null || item != undefined;
    });
  };
  const realArgs = fliterUndefined(args);
  if (logTag) {
    const append = realArgs ? ' ' + realArgs.join(' ') : '';
    logger.info(logTag, `执行：${command.trim()} ${append}`);
  }

  return new Promise((resolve, reject) => {
    const child: ChildProcess = realArgs
      ? spawnImpl(command.trim(), realArgs, {
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
        reject({
          code,
          message: `${logTag || ''} 进程异常退出，退出码: ${code}`,
        });
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
    logger.info(logTag, `执行：${command}`);
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
          logger.error(logTag, safeDecode(stderr));
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
