import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';

interface VirtualNodeData {
  label: string;
  fileCount: number;
  [key: string]: unknown;
}

function VirtualNode({ data }: NodeProps) {
  const d = data as VirtualNodeData;
  return (
    <div className="vn-node">
      <Handle type="target" position={Position.Top} className="vn-handle" />

      <div className="vn-glow" />
      <div className="vn-label">{d.label}</div>
      <div className="vn-count">{d.fileCount}</div>

      <Handle type="source" position={Position.Bottom} className="vn-handle" />

      <style>{`
        .vn-node {
          position: relative;
          min-width: 80px;
          padding: 8px 16px;
          background: linear-gradient(135deg, rgba(139,92,246,0.12), rgba(6,182,212,0.08));
          border: 1px solid rgba(139,92,246,0.25);
          border-radius: 24px;
          text-align: center;
          transition: all 0.3s cubic-bezier(.4,0,.2,1);
          cursor: default;
        }
        .vn-glow {
          position: absolute;
          inset: -4px;
          border-radius: 28px;
          background: radial-gradient(ellipse at center, rgba(139,92,246,0.15), transparent 70%);
          pointer-events: none;
          opacity: 0;
          transition: opacity 0.3s;
        }
        .vn-node:hover .vn-glow { opacity: 1; }
        .vn-node:hover {
          background: linear-gradient(135deg, rgba(139,92,246,0.2), rgba(6,182,212,0.14));
          border-color: rgba(139,92,246,0.4);
          box-shadow: 0 0 20px rgba(139,92,246,0.2);
          transform: scale(1.05);
        }
        .vn-handle {
          width: 5px !important;
          height: 5px !important;
          background: rgba(139,92,246,0.5) !important;
          border: 1px solid rgba(139,92,246,0.3) !important;
        }
        .vn-label {
          font-size: 9px;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          background: linear-gradient(135deg, #c084fc, #67e8f9);
          -webkit-background-clip: text;
          -webkit-text-fill-color: transparent;
          background-clip: text;
          font-family: 'Inter', sans-serif;
        }
        .vn-count {
          font-size: 8px;
          color: rgba(167,139,250,0.6);
          margin-top: 2px;
          font-family: 'JetBrains Mono', monospace;
          letter-spacing: 0.02em;
        }
        .vn-count::after { content: ' files'; }
      `}</style>
    </div>
  );
}

export default memo(VirtualNode);
