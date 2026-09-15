export interface PipelineConfig {
  inputPath: string;
  outputDir: string;
  simplify: number;
  dedup: boolean;
  keepTemp: boolean;
}

export interface AssemblyHierarchy {
  rootName: string;
  parts: PartNode[];
}

export interface PartNode {
  id: string;
  name: string;
  parentId: string | null;
  matrix: number[];
  meshFile?: string;
}

export interface PartInfo {
  id: string;
  name: string;
  originalGlb: string;
  optimizedGlb: string;
  faceCount: number;
  originalFaceCount: number;
}

export interface DedupMap {
  templates: TemplateInfo[];
  instances: InstanceInfo[];
}

export interface TemplateInfo {
  id: string;
  name: string;
  file: string;
  faceCount: number;
}

export interface InstanceInfo {
  templateId: string;
  partId: string;
  matrix: number[];
}
