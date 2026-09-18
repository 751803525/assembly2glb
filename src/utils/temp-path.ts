import os from 'os';
import path from 'path';

const tempRoot = os.tmpdir();
export const tempDir = path.join(tempRoot, 'assembly2glb');
