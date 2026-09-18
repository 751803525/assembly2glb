export interface PipelineConfig {
  inputPath: string;
  outputDir: string;
  simplify: number;
  mode: 'split' | 'merge' | 'all';
  precision: number;
  dedup: boolean;
  keepTemp: boolean;
}
