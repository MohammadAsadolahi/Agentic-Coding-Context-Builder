# Algorithm internals

The technical reference for Agentic Coding Context Builder: how structural evidence becomes a task-specific, token-bounded context package.

[← Back to the README](../README.md)

> **Two layers of reasoning.** Aider's tree-sitter reference graph and weighted PageRank supply structural evidence. A task-aware LLM selects the files to emphasize for a second map. The agent never rewrites the dependency graph itself.

## Algorithm in detail

### 1. File discovery and prompt conditioning

`repo_map_generator.py` first asks Aider's `GitRepo` for tracked files. It supports immediate nested Git repositories under a common root (with prefixed paths) and falls back to a filtered filesystem walk when Git discovery is unavailable. The fallback excludes common build directories, dependencies, and binary extensions.

The task text is examined for file paths, unique basenames, identifiers, and filename-stem matches. These yield `mentioned_fnames` and `mentioned_idents`. A project dictionary can add feature-to-file matches:

```json
{
  "user-authentication": ["src/auth.py", "src/middleware.py"],
  "payment-processing": ["src/billing.py", "src/stripe_client.py"]
}
```

Place it at `.architectureX/project_dictionary.json`. Prompt word n-grams of one to three words are matched to hyphenated keys. Dictionary matches become task seeds and receive a 3× personalization modifier later.

### 2. Git history as an exploration signal

**Recency:** For each file observed in up to 50 recent commits, use the last observed modification time to compute

$$
r(f) = 2^{-\Delta t_f / 30}
$$

where `Δt_f` is elapsed time in days. The implemented decay has a **true 30-day half-life**. The top 20 recent files are identified, but only files already mentioned by the task receive recency boosts; this guards against unrelated recent edits.

**Co-change:** Look through up to 200 commits and count files committed together with seeded files. A file becomes eligible if it co-changed at least twice; up to 10 such files are promoted. This can reveal tests, migrations, configuration, and other coupling invisible to source-symbol references.

These heuristics **expand the task-relevant file seeds**. They are not themselves additional dependency edges or directly summed into PageRank scores.

### 3. Build the weighted file-reference graph

Aider extracts `def` (definition) and `ref` (reference) tags with tree-sitter and may fall back to Pygments for references where supported parsers only expose definitions. It creates a NetworkX `MultiDiGraph`: **files are vertices**, and an identifier referenced by file `u` and defined by file `v` creates a directed edge `u → v`.

The direction is important: *referencing/dependent file → defining/provider file*. Parallel edges can represent different identifiers between the same files. A small self-loop with weight 0.1 can preserve a definition with no detected references.

For identifier `s`, Aider computes an edge weight approximately as

$$
w(u,v;s)=\sqrt{c(u,s)}\cdot
m_{\mathrm{mentioned}}\cdot
m_{\mathrm{distinctive}}\cdot
m_{\mathrm{private}}\cdot
m_{\mathrm{overloaded}}\cdot
m_{\mathrm{chat}}
$$

where `c(u,s)` is the number of occurrences of `s` referenced by `u`, and the multipliers are:

| Condition | Weight |
| --- | ---: |
| Identifier explicitly mentioned in task | 10× |
| Distinctive camelCase/snake_case/kebab-case identifier at least 8 characters long | 10× |
| Identifier begins with underscore | 0.1× |
| Identifier defined in more than five files | 0.1× |
| Referencing file is explicitly in the chat | 50× |

The factors multiply; `sqrt(count)` dampens repetitive names. The **chat 50× factor applies to outgoing graph edges**, not automatically to every agent-selected file.

**Example:** If `handlers/login.py` references `authenticate` defined in `auth/service.py`, the graph includes `handlers/login.py → auth/service.py`. If that service references `issue_token` defined in `auth/tokens.py`, the graph includes `auth/service.py → auth/tokens.py`. The structural ranking can therefore surface important implementation paths even when those file names are not in the task.

**Limit:** This is a symbol-reference graph, not a proven runtime call graph. Dynamic dispatch, name collisions, reflection, generated code, and parser coverage can limit its fidelity.

### 4. Personalized PageRank

NetworkX PageRank scores files from weighted links and a task-biased teleportation distribution. In simplified form, with damping factor `α = 0.85`:

$$
PR_{t+1}(v) = (1-\alpha)p(v) +
\alpha\sum_{u\rightarrow v}PR_t(u)\frac{w(u,v)}{\sum_x w(u,x)}
$$

Here `PR(v)` is the importance of file `v`; `p(v)` is its *normalized personalization weight*; and `w(u,v)` aggregates weights of edges from `u` to `v`. Dangling nodes follow the personalization distribution when supplied.

Aider seeds personalization for files already in chat, explicitly mentioned filenames, and path components matched against mentioned identifiers. The builder changes **the personalization vector before ranking**, not the final PageRank scores:

| Signal | Default multiplier | Effect |
| --- | ---: | --- |
| Project dictionary match | 3× | Promotes matched domain files |
| Agent-selected file | 10× | Seeds a selected file, then raises its personalization |
| Agent-downgraded file | 0.1× via API; 0.05× generator fallback | Reduces *existing* personalization |

**Why seeding matters:** If a selected file has no entry in Aider's personalization vector, multiplying zero changes nothing. In Pass 2, the builder now explicitly adds agent-selected files to `mentioned_fnames` *before* applying their configurable boost factor. This makes the selected-file boost effective even when the original task did not mention the filename.

Downgrading affects only files with existing personalization; it does **not** remove a vertex, delete edges, multiply its final rank by 0.1, or guarantee exclusion. The actual rank shift depends on graph topology and normalization.

An optional NVIDIA RAPIDS cuGraph implementation is attempted for PageRank when available, with NetworkX as CPU fallback. Floating-point rankings can differ slightly between implementations.

### 5. Translate PageRank into a token-bounded map

After PageRank, Aider distributes each source file's rank across its outgoing edges proportionally to edge weight, crediting **(defining file, identifier)** pairs:

$$
\mathrm{credit}(v,s)\;{+}{=}\;PR(u)\,\frac{w(u,v;s)}{\sum_x w(u,x)}
$$

These definition tags are sorted by credit and rendered with source structure and contextual lines. Important definitions appear earlier; lesser-priority details are compressed or omitted. Files with no usable tags can still appear. Aider searches for how many ranked tags fit the available token budget.

**Actual budgets in this implementation:** `RepoMap` is configured with `map_mul_no_files=2`. When there are no chat files, the map target is up to **2×** the model's map-token setting, capped by its input limit minus 4,096 reserved tokens. Both passes currently use the same 2× no-chat setting; there is **no implemented 8× first-pass budget**. The structural map's budget is separate from the additional source-injection allowance.

When map generation is empty, fallback attempts run without chat files and then without task hints.

## Agentic graph exploration and Pass 2

The agent **does not modify the graph's reference edges**. It reads the structural map and advises which files should receive more or less teleportation weight.

1. **Architecture triage (optional):** If a Feature Catalog exists in `.architectureX/01_GENERAL_ARCHITECTURE.md`, a small LLM classifies the task into zero to two feature IDs. The corresponding `.architectureX/flows/*.md` files are supplied to the main agent. This runs alongside Pass 1.
2. **Exploration:** The main LLM receives the task, architecture, flow docs, and repo map. When one feature remains unclear, it can call `get_feature_details` once to retrieve its flow and optional `.architectureX/flows/flow_files_dictionary.json` file list.
3. **Selection:** The prompt guides the agent toward roughly **3–12 files to boost**, **0–15 to downgrade**, and short notes about hidden coupling and call/data-flow direction. Broad cross-cutting tasks should have no downgrades; these ranges are guidance rather than hard enforcement.
4. **Validation:** `FileSelectionResult` (Pydantic v2) parses the LLM JSON, normalizes repository-relative paths, rejects absolute/traversal paths, and deduplicates. The pipeline then removes filenames absent from file discovery and makes boosts take precedence over downgrades.
5. **Reranking:** Pass 2 reseeds selected files, applies configured personalization factors, and rerenders the map. The result cache keys on the repository fingerprint, prompt, model, selections, and factor values; the Aider map cache is bypassed when customized ranking requires recomputation.
6. **Context assembly:** Navigational notes are prepended; selected readable files are appended in priority order, capped at **500 lines per file** and about **30,000 extra tokens** (estimated using four characters/token). Symlinks resolving outside the repository are skipped.

If the first LLM response is valid JSON, it is used directly. Otherwise, a structured-output LLM call is attempted, followed by a raw-JSON fallback. An LLM failure returns the broad map. Missing required agentic configuration produces an explicit error (HTTP **503** in service mode).

## Implementation pointers

| Concern | Implementation |
| --- | --- |
| File discovery, Git signals, result caching, and PageRank customization | [`repo_map_generator.py`](../repo_map_generator.py) |
| LLM triage, file selection, fallback, and source injection | [`agentic_mapper.py`](../agentic_mapper.py) |
| Validated selection and response contracts | [`schemas.py`](../schemas.py) |
| Visualization graph export | [`repo_map_generator.py`](../repo_map_generator.py) and [`plotter/`](../plotter/) |

The visual graph in the React frontend is a **filtered reconstruction** of rendered-map tags. It is not a direct picture of the complete weighted graph fed into PageRank.
