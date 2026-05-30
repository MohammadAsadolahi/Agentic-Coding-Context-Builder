import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'fs'
import path from 'path'

const HISTORY_SLOTS = 10;
const PUBLIC_DIR = path.resolve(__dirname, 'public');
const COUNTER_FILE = path.join(PUBLIC_DIR, '.graph_counter');
const SOURCE_FILE = path.join(PUBLIC_DIR, 'graph_data.json');

function readCounter(): number {
  try { return parseInt(fs.readFileSync(COUNTER_FILE, 'utf-8').trim(), 10) || 0; }
  catch { return 0; }
}

function rotateGraphData() {
  if (!fs.existsSync(SOURCE_FILE)) return;
  const data = fs.readFileSync(SOURCE_FILE, 'utf-8');
  // Check if this is a duplicate of the last saved slot
  const counter = readCounter();
  const lastSlot = ((counter - 1) % HISTORY_SLOTS + HISTORY_SLOTS) % HISTORY_SLOTS;
  const lastFile = path.join(PUBLIC_DIR, `graph_data_${lastSlot}.json`);
  if (fs.existsSync(lastFile)) {
    const lastData = fs.readFileSync(lastFile, 'utf-8');
    if (lastData === data) return; // same content, skip
  }
  const slot = counter % HISTORY_SLOTS;
  fs.writeFileSync(path.join(PUBLIC_DIR, `graph_data_${slot}.json`), data);
  fs.writeFileSync(COUNTER_FILE, String(counter + 1));
}

function graphHistoryPlugin() {
  return {
    name: 'graph-history',
    buildStart() {
      // Rotate on server start if there's fresh data
      rotateGraphData();
    },
    configureServer(server: any) {
      // Watch graph_data.json for changes and rotate
      const watcher = fs.watch(PUBLIC_DIR, (_, filename) => {
        if (filename === 'graph_data.json') {
          setTimeout(() => rotateGraphData(), 200); // small debounce for write-flush
        }
      });
      server.httpServer?.on('close', () => watcher.close());
    },
  };
}

export default defineConfig({
  plugins: [react(), graphHistoryPlugin()],
  server: {
    port: 5199,
    host: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
