import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';

const EXT_COLORS: Record<string, string> = {
  go: '#00ADD8', ts: '#3178C6', tsx: '#3178C6', js: '#F7DF1E',
  py: '#3776AB', rs: '#FF4500', java: '#ED8B00', rb: '#CC342D',
  yml: '#CB171E', yaml: '#CB171E', json: '#6D6D6D', md: '#083FA1',
  sql: '#E38C00', proto: '#4285F4', mod: '#00ADD8', sum: '#00ADD8',
  toml: '#9C4121', css: '#264de4', html: '#e34c26', sh: '#89e051',
};

function getAccentColor(fileName: string): string {
  const ext = fileName.split('.').pop()?.toLowerCase() || '';
  return EXT_COLORS[ext] || '#8b5cf6';
}

function FileNode({ data }: NodeProps) {
  const label = data.label as string;
  const fileName = label.split('/').pop() || label;
  const dirPath = label.includes('/') ? label.substring(0, label.lastIndexOf('/')) : '';
  const ext = fileName.split('.').pop()?.toLowerCase() || '';
  const accent = getAccentColor(fileName);
  const groupColor = (data.groupColor as string) || accent;
  const groupLabel = (data.groupLabel as string) || '';
  const heatColor = (data.heatColor as string) || '#3b82f6';
  const heat = (data.heat as number) ?? 0.5;
  // Heat bar width: 15% to 100%
  const heatBarWidth = 15 + heat * 85;
  // Whole-node visual scaling: hot = aggressive pop, cold = subtle but readable
  const nodeOpacity = 0.55 + heat * 0.45;           // 0.55 → 1.0  (cold still visible)
  const nodeSaturation = 0.5 + heat * 0.5;          // 0.5 → 1.0
  const nodeBrightness = 0.8 + heat * 0.2;          // 0.8 → 1.0
  // Hot nodes get a strong colored background fill
  const bgAlpha = Math.round(0x08 + heat * 0x38).toString(16).padStart(2, '0'); // 08 → 40
  const glowAlpha = Math.round(0x15 + heat * 0x6A).toString(16).padStart(2, '0'); // 15 → 7F
  const glowSpread = 10 + heat * 24;  // glow radius 10→34
  const borderWidth = 2 + Math.round(heat * 2); // 2→4px left border

  return (
    <div className="file-node" style={{
      borderLeftColor: heatColor,
      borderLeftWidth: `${borderWidth}px`,
      boxShadow: `0 2px 8px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.05), 0 0 ${glowSpread}px ${heatColor}${bgAlpha}`,
      background: `linear-gradient(135deg, ${heatColor}${bgAlpha}, ${heatColor}${glowAlpha})`,
      opacity: nodeOpacity,
      filter: `saturate(${nodeSaturation}) brightness(${nodeBrightness})`,
    }}>
      <Handle type="target" position={Position.Top} className="fn-handle" style={{ background: heatColor }} />
      <div className="fn-inner">
        <div className="fn-ext-badge" style={{ background: accent }}>{ext}</div>
        <div className="fn-text">
          {groupLabel && <span className="fn-group" style={{ color: groupColor }}>{groupLabel}</span>}
          <span className="fn-name" style={{ color: accent }}>{fileName}</span>
        </div>
      </div>
      {/* Heat bar indicator */}
      <div className="fn-heat-bar">
        <div className="fn-heat-fill" style={{ width: `${heatBarWidth}%`, background: heatColor }} />
      </div>
      <Handle type="source" position={Position.Bottom} className="fn-handle" style={{ background: heatColor }} />

      <style>{`
        .file-node {
          padding: 8px 12px;
          background: rgba(255,255,255,0.04);
          backdrop-filter: blur(16px);
          -webkit-backdrop-filter: blur(16px);
          border: 1px solid rgba(255,255,255,0.08);
          border-left: 3px solid;
          border-radius: 12px;
          font-family: 'Inter', -apple-system, sans-serif;
          transition: all 0.25s cubic-bezier(.4,0,.2,1);
          box-shadow: 0 2px 8px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.05);
        }
        .file-node:hover {
          background: rgba(255,255,255,0.08);
          border-color: rgba(255,255,255,0.15);
          box-shadow: 0 4px 24px rgba(0,0,0,0.4), 0 0 20px rgba(139,92,246,0.15), inset 0 1px 0 rgba(255,255,255,0.08);
          transform: translateY(-2px);
        }
        .fn-handle {
          width: 7px !important;
          height: 7px !important;
          background: rgba(139,92,246,0.8) !important;
          border: 2px solid rgba(10,10,26,0.8) !important;
          transition: all 0.2s;
        }
        .file-node:hover .fn-handle {
          background: #a78bfa !important;
          box-shadow: 0 0 6px rgba(139,92,246,0.5);
        }
        .fn-inner {
          display: flex;
          align-items: center;
          gap: 8px;
          min-width: 0;
        }
        .fn-ext-badge {
          flex-shrink: 0;
          font-size: 8px;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.05em;
          color: #0a0a1a;
          padding: 2px 5px;
          border-radius: 4px;
          line-height: 1.3;
        }
        .fn-text {
          min-width: 0;
          overflow: hidden;
        }
        .fn-group {
          font-size: 8px;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          display: block;
          line-height: 1.3;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
          opacity: 0.7;
        }
        .fn-name {
          font-size: 11.5px;
          font-weight: 600;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
          display: block;
          letter-spacing: -0.01em;
          filter: brightness(1.3);
        }
        .fn-heat-bar {
          margin-top: 4px;
          height: 3px;
          width: 100%;
          background: rgba(255,255,255,0.06);
          border-radius: 2px;
          overflow: hidden;
        }
        .fn-heat-fill {
          height: 100%;
          border-radius: 2px;
          transition: width 0.3s ease;
        }
      `}</style>
    </div>
  );
}

export default memo(FileNode);
