import { useMemo } from 'react';
import type { GraphData } from '../types';

interface Props {
  file: string;
  graphData: GraphData;
  onClose: () => void;
}

function NodeDetailModal({ file, graphData, onClose }: Props) {
  const node = useMemo(
    () => graphData.nodes.find((n) => n.file === file),
    [graphData, file]
  );

  if (!node) return null;

  // Reconstruct the repo-map output lines for this file
  const outputLines = useMemo(() => {
    const lines: string[] = [`${file}:`];
    for (const s of node.symbols) {
      if (s.name === '⋮') {
        lines.push('  ⋮');
      } else {
        const indent = s.kind === 'class' || s.kind === 'function' ? '  ' : '    ';
        lines.push(`${indent}${s.name}`);
      }
    }
    return lines;
  }, [node, file]);

  return (
    <div className="ndm-overlay" onClick={onClose}>
      <div className="ndm-modal" onClick={(e) => e.stopPropagation()}>
        <button className="ndm-close" onClick={onClose}>✕</button>
        <h3 className="ndm-heading">Repo Map Output</h3>
        <pre className="ndm-output">{outputLines.join('\n')}</pre>
      </div>
    </div>
  );
}

export default NodeDetailModal;
