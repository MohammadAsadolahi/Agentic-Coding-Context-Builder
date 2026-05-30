export interface SymbolInfo {
  name: string;
  kind: 'function' | 'class' | 'method' | 'symbol';
}

export interface FileNode {
  file: string;
  symbols: SymbolInfo[];
}

export interface Edge {
  source: string;
  target: string;
  symbols: string[];
  weight: number;
}

export interface OutputData {
  repo_map: string;
  agentic_log: string;
  agentic_steps: { title: string; content: string; meta?: string }[];
  agent_boost_fnames: string[];
  agent_downgrade_fnames: string[];
  map_tokens: number;
  effective_budget: number;
  rel_files: string[];
  model_name: string;
  duration_ms: number;
}

export interface GraphData {
  metadata: {
    root: string;
    model: string;
    prompt: string;
    timestamp: string;
    totalRepoFiles: number;
    listedFiles: number;
    editFormat: string;
    effectiveBudget: number;
    durationMs?: number;
  };
  nodes: FileNode[];
  edges: Edge[];
  boosts: {
    mentioned: string[];
    recency: string[];
    cochange: string[];
    dictionary: string[];
    chat: string[];
  };
  output?: OutputData;
}
