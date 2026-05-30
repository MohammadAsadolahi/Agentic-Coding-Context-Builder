import dagre from 'dagre';
import type { GraphData } from '../types';
import type { Node, Edge } from '@xyflow/react';

const NODE_W = 210;
const NODE_H = 48;

/** Palette of distinct group colors */
const GROUP_PALETTE = [
  '#06b6d4', // cyan
  '#8b5cf6', // violet
  '#f59e0b', // amber
  '#10b981', // emerald
  '#f472b6', // pink
  '#3b82f6', // blue
  '#ef4444', // red
  '#a78bfa', // lavender
  '#14b8a6', // teal
  '#f97316', // orange
];

/**
 * Heat-map color stops from hot (rank 0 / most content) to cold.
 * Returns an HSL string.
 * heat=1 → red/hot, heat=0 → blue/cold
 */
function heatToColor(heat: number): string {
  // Clamp
  const h = Math.max(0, Math.min(1, heat));
  // Map heat 1→0° (red) through 30° (orange) 60° (yellow) to heat 0→240° (blue)
  const hue = (1 - h) * 240;
  const saturation = 85 + h * 15; // 85-100%
  const lightness = 45 + (1 - h) * 15; // hot=45% bright, cold=60% muted
  return `hsl(${Math.round(hue)}, ${Math.round(saturation)}%, ${Math.round(lightness)}%)`;
}

/** Group files by 2-level subdir: "internal/config/ai.go" → "internal/config" */
function groupBySubdir(files: string[]): Record<string, string[]> {
  const groups: Record<string, string[]> = {};
  for (const f of files) {
    const parts = f.split('/');
    const dir = parts.length > 2 ? parts.slice(0, 2).join('/')
              : parts.length > 1 ? parts[0]
              : '__root__';
    if (!groups[dir]) groups[dir] = [];
    groups[dir].push(f);
  }
  return groups;
}

export function buildFlowGraph(data: GraphData): { nodes: Node[]; edges: Edge[] } {
  const allFiles = new Set(data.nodes.map((n) => n.file));
  const groups = groupBySubdir(data.nodes.map((n) => n.file));

  // Assign each group a color
  const sortedDirs = Object.keys(groups).sort();
  const dirColor: Record<string, string> = {};
  sortedDirs.forEach((dir, i) => {
    dirColor[dir] = GROUP_PALETTE[i % GROUP_PALETTE.length];
  });

  // Map file → group dir
  const fileDir: Record<string, string> = {};
  for (const [dir, files] of Object.entries(groups)) {
    for (const f of files) fileDir[f] = dir;
  }

  // --- Priority-based vertical layout ---
  // Nodes are placed strictly by repo-map index: index 0 = top, last = bottom.
  // We lay them out in rows, with COLS nodes per row, centered horizontally.
  const COLS = Math.min(4, Math.max(2, Math.ceil(Math.sqrt(data.nodes.length * 0.6))));
  const X_GAP = NODE_W + 40;   // horizontal spacing between nodes
  const Y_GAP = NODE_H + 32;   // vertical spacing between rows

  const totalNodes = data.nodes.length;
  const maxSymbols = Math.max(1, ...data.nodes.map(n => n.symbols.length));
  const totalWidth = COLS * X_GAP;

  const flowNodes: Node[] = [];
  for (let i = 0; i < data.nodes.length; i++) {
    const n = data.nodes[i];
    const dir = fileDir[n.file] || '__root__';
    const groupColor = dirColor[dir] || '#8b5cf6';
    const groupLabel = dir === '__root__' ? '' : dir;

    // heat: 70% weight on position rank, 30% on symbol density
    const rankHeat = totalNodes > 1 ? 1 - i / (totalNodes - 1) : 1;
    const symbolHeat = n.symbols.length / maxSymbols;
    const heat = rankHeat * 0.7 + symbolHeat * 0.3;
    const heatColor = heatToColor(heat);

    const row = Math.floor(i / COLS);
    const col = i % COLS;
    // Center the row; for partial last row, also center
    const itemsInRow = Math.min(COLS, totalNodes - row * COLS);
    const rowOffset = (totalWidth - itemsInRow * X_GAP) / 2;
    const xPos = rowOffset + col * X_GAP;
    const yPos = row * Y_GAP;

    flowNodes.push({
      id: n.file, type: 'fileNode',
      position: { x: xPos, y: yPos },
      data: { label: n.file, groupColor, groupLabel, heatColor, heat },
    });
  }

  // Edge color scale: weight → color (cool cyan → hot pink)
  const edgeColor = (w: number) => {
    if (w >= 5) return '#f472b6';   // pink - very strong
    if (w >= 3) return '#a78bfa';   // violet - strong
    if (w >= 2) return '#67e8f9';   // cyan - moderate
    return 'rgba(139,92,246,0.25)'; // faint violet - weak
  };

  const flowEdges: Edge[] = [];
  for (const e of data.edges) {
    if (!allFiles.has(e.source) || !allFiles.has(e.target)) continue;
    // Only keep strong links (weight >= 3)
    if (e.weight < 3) continue;
    const sameGroup = fileDir[e.source] === fileDir[e.target];
    const color = edgeColor(e.weight);
    flowEdges.push({
      id: `e:${e.source}->${e.target}`,
      source: e.source, target: e.target,
      animated: e.weight >= 3,
      style: {
        stroke: sameGroup ? dirColor[fileDir[e.source]] || color : color,
        strokeWidth: Math.min(0.8 + e.weight * 0.3, 3),
        strokeOpacity: sameGroup ? 0.5 : 1,
        filter: e.weight >= 3 ? `drop-shadow(0 0 4px ${color})` : undefined,
      },
    });
  }

  return { nodes: flowNodes, edges: flowEdges };
}
