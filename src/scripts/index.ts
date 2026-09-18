import path from 'node:path';
import { fileURLToPath } from 'node:url';
// import { existsSync } from 'node:fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export const cad_splitter = path.resolve(__dirname, './cad-splitter.py');
export const merge_glb = path.resolve(__dirname, './merge-glb.py');
export const dedup = path.join(__dirname, 'dedup.py');
export const verify_occ = path.join(__dirname, 'verify-occ.py');
// export const probe_key_apis = path.resolve(__dirname, './probe-key-apis.py');
// 可选：开发时快速验证路径是否正确
// if (!existsSync(cad_splitter)) {
//   console.warn(`[warn] cad_splitter.py not found at: ${cad_splitter}`);
// }
