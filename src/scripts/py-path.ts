import path from 'path';
import { fileURLToPath } from 'url';

// 获取当前文件的绝对目录
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export const occt_split_to_glb = path.resolve(__dirname, './occt_split_to_glb.py');
