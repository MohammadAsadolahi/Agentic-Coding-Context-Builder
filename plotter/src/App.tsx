import { useCallback, useEffect, useState, useMemo, useRef } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  BackgroundVariant,
  type NodeMouseHandler,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import FileNode from './components/FileNode';
import NodeDetailModal from './components/NodeDetailModal';
import { buildFlowGraph } from './components/layoutEngine';
import type { GraphData, OutputData } from './types';
import VirtualNode from './components/VirtualNode';

const nodeTypes = {
  fileNode: FileNode,
  virtualNode: VirtualNode,
};

const HISTORY_SLOTS = 10;

function formatRunLabel(meta: GraphData['metadata'], index: number, total: number) {
  const d = new Date(meta.timestamp);
  const time = d.toLocaleString();
  const prompt = meta.prompt
    ? meta.prompt.length > 60 ? meta.prompt.slice(0, 57) + '…' : meta.prompt
    : '(no prompt)';
  const dur = meta.durationMs != null
    ? `${(meta.durationMs / 1000).toFixed(1)}s`
    : '';
  return `#${total - index} — ${time}${dur ? ` — ${dur}` : ''} — ${prompt}`;
}

async function loadHistory(): Promise<GraphData[]> {
  // Fetch all 10 slot files in parallel, collect those that exist
  const fetches = Array.from({ length: HISTORY_SLOTS }, (_, i) =>
    fetch(`/graph_data_${i}.json`)
      .then((res) => (res.ok ? res.json() as Promise<GraphData> : null))
      .catch(() => null)
  );
  const results = await Promise.all(fetches);
  const runs = results.filter((r): r is GraphData => r !== null && !!r.metadata?.timestamp && Array.isArray(r.nodes));
  // Sort by timestamp so newest is last
  runs.sort((a, b) => new Date(a.metadata.timestamp).getTime() - new Date(b.metadata.timestamp).getTime());
  return runs;
}

function App() {
  const [history, setHistory] = useState<GraphData[]>([]);
  const [selectedIndex, setSelectedIndex] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Request panel state
  const [prompt, setPrompt] = useState('');
  const [projectRoot, setProjectRoot] = useState('/host/path/to/your/repo');
  const [agentic, setAgentic] = useState(false);
  const [boostFactor, setBoostFactor] = useState(10);
  const [downgradeFactor, setDowngradeFactor] = useState(10);
  const [requesting, setRequesting] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const [outputOpen, setOutputOpen] = useState(false);
  const [outputData, setOutputData] = useState<OutputData | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refreshHistory = useCallback(async () => {
    const runs = await loadHistory();
    if (runs.length > 0) {
      setHistory(runs);
      setSelectedIndex(runs.length - 1);
    }
  }, []);

  useEffect(() => {
    loadHistory().then((runs) => {
      if (runs.length > 0) {
        setHistory(runs);
        setSelectedIndex(runs.length - 1);
        const newest = runs[runs.length - 1];
        if (newest.output) {
          setOutputData(newest.output);
          setOutputOpen(true);
        }
      } else {
        return fetch('/graph_data.json')
          .then((res) => {
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            return res.json() as Promise<GraphData>;
          })
          .then((data) => {
            setHistory([data]);
            setSelectedIndex(0);
            if (data.output) {
              setOutputData(data.output);
              setOutputOpen(true);
            }
          });
      }
    })
    .catch((err) => setError(err.message))
    .finally(() => setLoading(false));
  }, []);

  const handleSubmit = useCallback(async () => {
    if (!prompt.trim()) return;
    setRequesting(true);
    setRequestError(null);
    setOutputData(null);
    const t0 = performance.now();
    try {
      const res = await fetch('/api/map', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          root: projectRoot,
          prompt: prompt.trim(),
          agentic,
          boost_factor: boostFactor,
          downgrade_factor: 1 / downgradeFactor,
        }),
      });
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`HTTP ${res.status}: ${body}`);
      }
      const data = await res.json();
      const duration_ms = Math.round(performance.now() - t0);
      setOutputData({
        repo_map: data.repo_map || '',
        agentic_log: data.agentic_log || '',
        agentic_steps: data.agentic_steps || [],
        agent_boost_fnames: data.agent_boost_fnames || [],
        agent_downgrade_fnames: data.agent_downgrade_fnames || [],
        map_tokens: data.map_tokens || 0,
        effective_budget: data.effective_budget || 0,
        rel_files: data.rel_files || [],
        model_name: data.model_name || '',
        duration_ms,
      });
      setOutputOpen(true);
      // Wait a moment for the backend to write graph_data.json and vite to rotate
      await new Promise((r) => setTimeout(r, 2000));
      await refreshHistory();
    } catch (err: any) {
      setRequestError(err.message || 'Request failed');
    } finally {
      setRequesting(false);
    }
  }, [prompt, projectRoot, agentic, boostFactor, downgradeFactor, refreshHistory]);

  const graphData = history[selectedIndex] ?? null;

  const handleRunChange = useCallback((idx: number) => {
    setSelectedIndex(idx);
    const run = history[idx];
    if (run?.output) {
      setOutputData(run.output);
      setOutputOpen(true);
    } else {
      setOutputData(null);
      setOutputOpen(false);
    }
  }, [history]);

  const { initialNodes, initialEdges } = useMemo(() => {
    if (!graphData) return { initialNodes: [], initialEdges: [] };
    const { nodes, edges } = buildFlowGraph(graphData);
    return { initialNodes: nodes, initialEdges: edges };
  }, [graphData]);

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);

  const onNodeClick: NodeMouseHandler = useCallback((_event, node) => {
    setSelectedFile(node.id);
  }, []);

  useEffect(() => {
    setNodes(initialNodes);
    setEdges(initialEdges);
  }, [initialNodes, initialEdges, setNodes, setEdges]);

  if (loading) {
    return (
      <div className="loading-container">
        <div className="loading-spinner" />
        <span className="loading-text">Loading…</span>
      </div>
    );
  }

  if (error || !graphData) {
    return (
      <div className="error-container">
        <p>Run <code>repo_map_generator.py</code> first.</p>
      </div>
    );
  }

  return (
    <div style={{ width: '100%', height: '100%' }}>
      {/* ── Request Panel Toggle ── */}
      <button
        className="rp-toggle"
        onClick={() => setPanelOpen((v) => !v)}
        title="New Request"
      >
        {panelOpen ? '✕' : '⚡'}
      </button>

      {/* ── Request Panel ── */}
      {panelOpen && (
        <div className="rp-panel">
          <div className="rp-title">New Map Request</div>
          <label className="rp-label">
            Prompt
            <input
              className="rp-input"
              type="text"
              placeholder="e.g. what is sfu"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !requesting) handleSubmit(); }}
              disabled={requesting}
            />
          </label>
          <label className="rp-label">
            Project Root
            <input
              className="rp-input"
              type="text"
              value={projectRoot}
              onChange={(e) => setProjectRoot(e.target.value)}
              disabled={requesting}
            />
          </label>
          <label className="rp-checkbox">
            <input
              type="checkbox"
              checked={agentic}
              onChange={(e) => setAgentic(e.target.checked)}
              disabled={requesting}
            />
            <span>Agentic mode</span>
          </label>
          {agentic && (
            <>
              <label className="rp-label">
                Boost Factor: {boostFactor}×
                <input
                  type="range" min={1} max={50} step={1}
                  value={boostFactor}
                  onChange={(e) => setBoostFactor(Number(e.target.value))}
                  disabled={requesting}
                  className="rp-slider"
                />
              </label>
              <label className="rp-label">
                Downgrade Factor: 1/{downgradeFactor} ({downgradeFactor}× reduction)
                <input
                  type="range" min={1} max={50} step={1}
                  value={downgradeFactor}
                  onChange={(e) => setDowngradeFactor(Number(e.target.value))}
                  disabled={requesting}
                  className="rp-slider"
                />
              </label>
            </>
          )}
          <button className="rp-submit" onClick={handleSubmit} disabled={requesting || !prompt.trim()}>
            {requesting ? 'Running…' : 'Generate Map'}
          </button>
          {requestError && <div className="rp-error">{requestError}</div>}
        </div>
      )}

      {/* ── Output Panel ── */}
      {outputData && (
        <button
          className="rp-output-toggle"
          onClick={() => setOutputOpen((v) => !v)}
          title="Toggle output"
        >
          {outputOpen ? '▾ Output' : '▸ Output'}
        </button>
      )}
      {outputOpen && outputData && (
        <div className="rp-output">
          <div className="rp-output-header">
            <span className="rp-output-title">Results</span>
            <button className="rp-output-close" onClick={() => setOutputOpen(false)}>✕</button>
          </div>
          <div className="rp-output-stats">
            <span>Model: {outputData.model_name}</span>
            <span>Tokens: {outputData.map_tokens}</span>
            <span>Budget: {outputData.effective_budget}</span>
            <span>Files: {outputData.rel_files.length}</span>
            <span>Time: {(outputData.duration_ms / 1000).toFixed(1)}s</span>
          </div>
          {outputData.agent_boost_fnames.length > 0 && (
            <div className="rp-output-section">
              <div className="rp-output-section-title">↑ Boosted ({outputData.agent_boost_fnames.length})</div>
              {outputData.agent_boost_fnames.map((f) => <div key={f} className="rp-output-file boost">{f}</div>)}
            </div>
          )}
          {outputData.agent_downgrade_fnames.length > 0 && (
            <div className="rp-output-section">
              <div className="rp-output-section-title">↓ Downgraded ({outputData.agent_downgrade_fnames.length})</div>
              {outputData.agent_downgrade_fnames.map((f) => <div key={f} className="rp-output-file downgrade">{f}</div>)}
            </div>
          )}
          {outputData.agentic_steps.length > 0 && (
            <div className="rp-output-section">
              <div className="rp-output-section-title">Pipeline Steps ({outputData.agentic_steps.length})</div>
              {outputData.agentic_steps.map((step, i) => (
                <details key={i} className="rp-step">
                  <summary className="rp-step-summary">
                    <span className="rp-step-num">{i + 1}</span> {step.title}
                  </summary>
                  {step.meta && <div className="rp-step-meta">{step.meta}</div>}
                  <pre className="rp-output-log">{step.content}</pre>
                </details>
              ))}
            </div>
          )}
          {outputData.agentic_log && outputData.agentic_steps.length === 0 && (
            <details className="rp-output-section">
              <summary className="rp-output-section-title" style={{ cursor: 'pointer' }}>Agentic Log (click to expand)</summary>
              <pre className="rp-output-log">{outputData.agentic_log}</pre>
            </details>
          )}
          <details className="rp-output-section">
            <summary className="rp-output-section-title" style={{ cursor: 'pointer' }}>Repo Map (click to expand)</summary>
            <pre className="rp-output-log">{outputData.repo_map}</pre>
          </details>
        </div>
      )}

      {history.length > 1 && (
        <div className="run-selector">
          <label htmlFor="run-select">Run:</label>
          <select
            id="run-select"
            value={selectedIndex}
            onChange={(e) => handleRunChange(Number(e.target.value))}
          >
            {history.map((run, i) => (
              <option key={i} value={i}>
                {formatRunLabel(run.metadata, i, history.length)}
              </option>
            )).reverse()}
          </select>
        </div>
      )}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.05}
        maxZoom={3}
        defaultEdgeOptions={{ type: 'smoothstep' }}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={32} size={1} color="rgba(139,92,246,0.12)" />
        <Controls showInteractive={false} />
        <MiniMap
          nodeColor={(n) => {
            const hc = n.data?.heatColor as string;
            return hc || 'rgba(6,182,212,0.5)';
          }}
          maskColor="rgba(10,10,26,0.85)"
          pannable zoomable
        />
      </ReactFlow>
      {selectedFile && graphData && (
        <NodeDetailModal
          file={selectedFile}
          graphData={graphData}
          onClose={() => setSelectedFile(null)}
        />
      )}
    </div>
  );
}

export default App;
