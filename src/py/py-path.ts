import path from 'path';
import { fileURLToPath } from 'url';

// 获取当前文件的绝对目录
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export const cad_splitter = path.resolve(__dirname, './cad_splitter.py');
