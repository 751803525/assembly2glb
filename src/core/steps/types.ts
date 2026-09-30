export interface PipelineConfig {
  inputPath: string;
  outputDir: string;
  simplify: number;
  mode: OutputMode;
  precision: number;
  dedup: boolean;
  keepTemp: boolean;
  compress: boolean;
}

export type OutputMode = 1 | 2 | 3 | 4 | 5 | 6 | 7;
export const MODE_SPLIT = 0b001;
export const MODE_MERGE = 0b010;
export const MODE_FLATTEN = 0b100;

export function hasMode(mode: OutputMode, flag: number): boolean {
  return (mode & flag) === flag;
}
export function isValidMode(v: unknown): v is OutputMode {
  return typeof v === 'number' && Number.isInteger(v) && v >= 1 && v <= 7;
}
