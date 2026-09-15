export interface PipelineConfig {
  inputPath: string;
  outputDir: string;
  simplify: number;
  dedup: boolean;
  keepTemp: boolean;
}
