export interface PipelineConfig {
  inputPath: string;
  outputDir: string;
  simplify: number;
  precision: number;
  dedup: boolean;
  keepTemp: boolean;
}
