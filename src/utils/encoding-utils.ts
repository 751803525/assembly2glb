import fs from 'fs-extra';
import chardet from 'chardet';
import iconv from 'iconv-lite';
import { pipeline } from 'node:stream/promises';

import { logger } from '../cli/logger.js';
import { fileUtils } from './file.js';

/**
 * 从文件头部提取连续的非 ASCII 字节片段并推测其编码
 * @param filePath 文件路径
 * @param readLimitBytes 头部读取最大字节数，默认读取前 4KB
 * @returns 提取结果及推测的编码
 */
async function detectNonAsciiEncoding(
  filePath: string,
  readLimitBytes: number = 1024 * 1024,
  defaultEncoding: string
): Promise<string> {
  // 1. 检查文件是否存在
  if (!(await fs.pathExists(filePath))) {
    throw new Error(`文件不存在: ${filePath}`);
  }

  // 2. 打开文件并仅读取头部字节 Buffer
  const fileHandle = await fs.open(filePath, 'r');
  const buffer = Buffer.alloc(readLimitBytes);
  const { bytesRead } = await fs.read(fileHandle, buffer, 0, readLimitBytes, 0);
  await fs.close(fileHandle);

  const headerBuffer = buffer.subarray(0, bytesRead);

  // 3. 提取连续的非 ASCII 字节片段 (字节值 > 127)
  const nonAsciiChunks: Buffer[] = [];
  let currentChunk: number[] = [];

  for (let i = 0; i < headerBuffer.length; i++) {
    const byte = headerBuffer[i];

    // ASCII 范围为 0x00 - 0x7F (0 - 127)
    if (byte > 127) {
      currentChunk.push(byte);
    } else {
      if (currentChunk.length > 0) {
        nonAsciiChunks.push(Buffer.from(currentChunk));
        currentChunk = [];
      }
    }
  }

  // 补齐末尾收尾的非 ASCII 字节
  if (currentChunk.length > 0) {
    nonAsciiChunks.push(Buffer.from(currentChunk));
  }

  // 4. 将所有提取到的非 ASCII 字节片段拼接在一起
  const combinedNonAsciiBuffer = Buffer.concat(nonAsciiChunks);

  if (combinedNonAsciiBuffer.length === 0) {
    return 'ASCII';
  }

  // 5. 使用 chardet 分析提取出的 Buffer
  const detectedEncoding = chardet.detect(combinedNonAsciiBuffer);

  return detectedEncoding || defaultEncoding;
}

export async function convertFileEncodingStream(
  fromPath: string,
  toPath: string,
  fromEncoding: string,
  toEncoding: string = 'utf-8'
): Promise<void> {
  // 2. 检查 iconv-lite 是否支持指定的编码
  if (!iconv.encodingExists(fromEncoding)) {
    throw new Error(`不支持的源编码类型: ${fromEncoding}`);
  }
  if (!iconv.encodingExists(toEncoding)) {
    throw new Error(`不支持的目标编码类型: ${toEncoding}`);
  }
  await fileUtils.emptyDir(toPath);
  // 4. 创建可读流与可写流
  const readStream = fs.createReadStream(fromPath);
  const writeStream = fs.createWriteStream(toPath);
  const decoder = iconv.decodeStream(fromEncoding);
  const encoder = iconv.encodeStream(toEncoding);
  // 结构: 文件读入 -> 源编码解码 -> 目标编码重编 -> 文件写入
  await pipeline(readStream, decoder, encoder, writeStream);
}

/**
 * 转换文件编码（流式处理，自动检测源编码）
 */
export async function fileEncoding(
  inputPath: string,
  outputPath: string,
  targetEncoding: string = 'utf8',
  options: { limitBytes?: number; defaultEncoding?: string } = {}
): Promise<string> {
  const { limitBytes = 1024 * 1024, defaultEncoding = 'gbk' } = options;
  if (!fs.existsSync(inputPath)) throw new Error(`输入文件不存在: ${inputPath}`);
  const encoding = await detectNonAsciiEncoding(inputPath, limitBytes, defaultEncoding);
  await convertFileEncodingStream(inputPath, outputPath, encoding, targetEncoding);
  return outputPath;
}
