# Agentic Coding Context Builder

A two-pass, LLM-guided repo map generator that builds **task-aware code context** for AI coding agents. Instead of dumping an entire repository into an LLM's context window, it uses structural analysis (tree-sitter AST + PageRank), git history enrichment (recency decay + co-change detection), and an agentic file-selection pass to surface only the code that matters for a given task.

## Why This Exists

AI coding assistants work best when they see the *right* code, not *all* the code. On large codebases, blindly stuffing files into a context window wastes tokens on irrelevant modules and buries the signal in noise.

This tool solves that by generating a **focused, ranked repo map** — a structural summary of your codebase where the most task-relevant files are expanded with full signatures, and everything else is compressed or omitted. The result is a context payload purpose-built for the task at hand: bug fixes see error-handling paths, feature work sees the right domain modules, and refactors see cross-cutting dependencies.

## Architecture

```
User prompt: "fix the authentication middleware"
                        |
                        v
              +---------------------+
              |   Pass 1 (Broad)    |
              |   8x token budget   |
              |   9-step pipeline   |
              +---------+-----------+
                        | broad repo map
                        v
              +---------------------+
              |   LLM Selection     |
              |   picks 3-12 files  |
              |   with reasoning    |
              +---------+-----------+
                        | selected files
                        v
              +---------------------+
              |   Pass 2 (Focused)  |
              |   boosted PageRank  |
              |   + source inject   |
              +---------+-----------+
                        |
                        v
              Task-aware context payload
```

**Pass 1** generates a broad exploratory map with an expanded token budget (8x). An LLM agent analyzes the structural map alongside optional architecture documentation and selects the most relevant files for the task. **Pass 2** re-generates a focused map with those selections boosted 50x in PageRank personalization, then injects the full source of top-ranked files.

## The 9-Step Pipeline

Each pass runs a deterministic pipeline (implemented in `repo_map_generator.py`):

| Step | Name | What It Does |
|------|------|--------------|
| 1 | **File Discovery** | Finds all tracked files via git — supports single repos, monorepos with nested `.git` dirs, and non-git fallback |
| 2 | **Prompt Conditioning** | Extracts identifiers, filenames, and stem matches from the user's task description |
| 3 | **Git Enrichment** | Applies recency boosts (exponential decay, 30-day half-life) and co-change boosts (files committed together >= 2 times) |
| 4 | **Tag Extraction** | Parses source files with tree-sitter to extract function/class definitions and cross-file references |
| 5 | **Graph Construction** | Builds a `MultiDiGraph` where edges represent cross-file symbol references |
| 6 | **PageRank** | Runs personalized PageRank with weighted vectors: chat files (50x), mentioned identifiers (10x), project dictionary (3x), with penalties for private/overloaded symbols |
| 7 | **Tree Rendering** | Renders ranked files with variable detail — top files show full signatures, lower files show names only |
| 8 | **Token Budgeting** | Fits output within model token limits (8x budget for broad maps, 1x for focused) |
| 9 | **Fallback Chain** | Three attempts with progressive relaxation: full context -> no chat files -> no hints |

## Quick Start

### Prerequisites

- Python 3.11+
- Git
- An [OpenRouter](https://openrouter.ai) API key (for agentic mode)

### Install

```bash
git clone https://github.com/MohammadAsadolahi/agentic-coding-context-builder.git
cd agentic-coding-context-builder

python3 -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env with your OpenRouter API key
```

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENROUTER_API_KEY` | API key for LLM-based file selection | *required for agentic mode* |
| `OPENROUTER_BASE_URL` | OpenRouter API endpoint | `https://openrouter.ai/api/v1` |
| `OPENROUTER_MODEL` | Model for agentic file selection | `anthropic/claude-haiku-4.5` |
| `REPO_MAP_MODEL` | Model name for token counting (no API calls) | `gpt-4o-mini` |

### Run

**CLI** (single run):
```bash
python app.py --root /path/to/repo --prompt "fix the authentication middleware"
```

**HTTP API** (persistent server):
```bash
uvicorn service:app --host 0.0.0.0 --port 8000

curl -X POST http://localhost:8000/map \
  -H "Content-Type: application/json" \
  -d '{"root": "/path/to/repo", "prompt": "fix the auth middleware", "agentic": true}'
```

**Local dev** (API + visualization frontend):
```bash
./cli.sh run    # Starts backend on :8000 and React frontend on :5199
```

## API Reference

### `POST /map`

Generate a context-aware repo map.

**Request:**
```json
{
  "root": "/path/to/repo",
  "prompt": "fix the authentication middleware",
  "agentic": true,
  "boost_factor": 10.0,
  "downgrade_factor": 0.1
}
```

| Field | Type | Description |
|-------|------|-------------|
| `root` | string | Absolute path to the repository root |
| `prompt` | string | Task description for context-aware ranking |
| `agentic` | bool | `true` for two-pass LLM-guided pipeline (default: `false`) |
| `boost_factor` | float | PageRank multiplier for agent-selected files (default: `10.0`) |
| `downgrade_factor` | float | PageRank multiplier for agent-suppressed files (default: `0.1`) |

**Response:**
```json
{
  "root": "/path/to/repo",
  "model_name": "gpt-4o-mini",
  "map_tokens": 4096,
  "effective_budget": 8192,
  "used_git": true,
  "rel_files": ["src/auth.py", "src/middleware.py", "..."],
  "repo_map": "src/\n  auth.py\n    def authenticate(request) ...",
  "agent_boost_fnames": ["src/auth.py", "src/middleware.py"],
  "agent_downgrade_fnames": ["docs/changelog.md"],
  "agentic_log": "...",
  "agentic_steps": [{"title": "...", "content": "..."}]
}
```

### `GET /health`

Returns `{"status": "ok"}`.

## Visualization Frontend

The `plotter/` directory contains a React + [React Flow](https://reactflow.dev/) application that visualizes the repo map as an interactive dependency graph:

- **Heat-mapped nodes** — files colored by PageRank importance (hot = high rank)
- **Language-specific styling** — Go, TypeScript, Python, Rust, etc. each get distinct colors
- **Run history** — rotating buffer of 10 previous runs, selectable via dropdown
- **Request panel** — generate new maps directly from the UI with boost/downgrade sliders
- **Output inspector** — view repo map output, agentic logs, and pipeline steps

```bash
cd plotter && npm install && npm run dev
```

## Key Features

### Git-Aware Context Enrichment

- **Recency boosts**: Files modified recently get exponentially decayed scores (`exp(-days/30)`), surfacing actively-developed code
- **Co-change detection**: Files frequently committed together (>= 2 times) are boosted, revealing implicit coupling between components

### Project Dictionary

Place a `.architectureX/project_dictionary.json` in your repo root to map domain features to files:

```json
{
  "user-authentication": ["src/auth.py", "src/middleware.py"],
  "payment-processing": ["src/billing.py", "src/stripe_client.py"]
}
```

Prompt n-grams are matched against dictionary keys, and matched files receive a 3x PageRank boost.

### GPU Acceleration

When [NVIDIA RAPIDS cuGraph](https://docs.rapids.ai/api/cugraph/stable/) is available, PageRank computation is transparently offloaded to GPU. Falls back to CPU (NetworkX) automatically.

### Caching and Reproducibility

- **Result caching**: Per-process cache keyed on repo fingerprint (git HEAD + working tree status + dirty file mtimes), prompt, model, and chat files
- **Deterministic output**: `PYTHONHASHSEED=0` eliminates set/dict iteration-order variance across runs

### Multi-Repo Support

Automatically detects monorepo structures with nested `.git` subdirectories and prefixes file paths to avoid collisions.

### Graceful Degradation

- Three-attempt fallback chain ensures a map is always produced
- Agentic mode falls back to Pass 1 results on LLM errors
- Non-git repositories use filesystem walk as fallback

## Docker

```bash
# Build and run
docker compose up --build

# Air-gapped networks (see docker-compose.offline.yml for proxy config)
docker compose -f docker-compose.yml -f docker-compose.offline.yml up --build
```

The container mounts the host filesystem read-only at `/host`, so repository paths in API requests should be prefixed accordingly (e.g., `/host/home/user/repos/myproject`).

## Stress Testing

The `audit_runner.py` and `audit_runner_40.py` scripts run batches of diverse prompts against a target repo to validate pipeline robustness:

```bash
# 20 prompts — narrow tasks, broad refactors, edge cases
python audit_runner.py /path/to/target/repo

# 40 prompts — adds typos, ambiguous input, security, perf, file references
python audit_runner_40.py /path/to/target/repo
```

Results are written to `audit_results.json` / `audit_results_40.json` with per-run timing, boost/downgrade counts, and error tracking.

## Remote Deployment

`deploy.sh` manages deployment to a remote VM via SSH + Docker:

```bash
# Configure your target
export DEPLOY_HOST=user@your-server
export DEPLOY_DIR=/opt/repo-map

# Full deploy: git sync, docker rebuild, restart
./deploy.sh fullstart

# Quick commands
./deploy.sh start | stop | restart | status
./deploy.sh test "fix the login bug" /path/to/project
./deploy.sh test --agentic "add rate limiting" /path/to/project
```

## Project Structure

```
app.py                     CLI entry point
service.py                 FastAPI HTTP API (POST /map, GET /health)
repo_map_generator.py      Core 9-step pipeline (~2500 LOC)
agentic_mapper.py          Two-pass orchestration with LLM selection
run_local.py               Single-process server (API + pre-built frontend)

cli.sh                     Local dev: start/stop backend + plotter
deploy.sh                  Remote VM deployment via SSH + Docker

audit_runner.py            20-prompt stress test harness
audit_runner_40.py         40-prompt extended stress test

Dockerfile                 Container image (python:3.11-slim)
docker-compose.yml         Standard deployment
docker-compose.offline.yml Air-gapped network overlay

plotter/                   React + React Flow visualization frontend
  src/
    App.tsx                Main application component
    components/
      FileNode.tsx         Heat-mapped file node renderer
      VirtualNode.tsx      Directory group node renderer
      NodeDetailModal.tsx  File detail modal
      layoutEngine.ts     Graph layout + heat coloring engine
    types.ts               TypeScript interfaces
  vite.config.ts           Vite config with graph history rotation
```

## Dependencies

| Package | Purpose |
|---------|---------|
| [aider-chat](https://github.com/paul-gauthier/aider) | RepoMap engine, GitRepo, tree-sitter tag extraction, token counting |
| [networkx](https://networkx.org/) | PageRank graph algorithm |
| [langchain](https://python.langchain.com/) + [langchain-openai](https://python.langchain.com/) | LLM prompting and OpenRouter integration |
| [fastapi](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) | HTTP API server |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | Environment configuration |

**Optional**: [cugraph](https://docs.rapids.ai/api/cugraph/stable/) for GPU-accelerated PageRank.

## License

[MIT](LICENSE)
