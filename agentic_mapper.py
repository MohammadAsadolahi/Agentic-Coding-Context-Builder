#!/usr/bin/env python3
"""
agentic_mapper.py — Single-agent repo map generator.

Generates a repo map, then runs one LLM agent that receives the structural
map plus the global architecture overview.  The agent can optionally call
get_feature_details ONCE to drill into a specific feature before returning
its file selection.  Returns a RepoMapResult with the agent's selections
set as chat_rel_fnames.

Dependencies:
    pip install aider-chat langchain langchain-openai python-dotenv
"""

from dataclasses import replace as _dc_replace
from pathlib import Path
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
from openai import OpenAIError
from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
import httpx
import json
import os
import re
import sys
from datetime import datetime


# ─── File Logger ──────────────────────────────────────────────────────────────

class AgenticLogger:
    """Logs every agentic action to a timestamped text file in the project root."""

    def __init__(self, root: str):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = Path(root) / f"agentic_log_{ts}.txt"
        try:
            self._fh = open(log_path, "w", encoding="utf-8")
            self._path = log_path
        except OSError:
            # Fallback when project dir is read-only (e.g. Docker bind mount)
            fallback = Path("/tmp") / f"agentic_log_{ts}.txt"
            self._fh = open(fallback, "w", encoding="utf-8")
            self._path = fallback
        self._write(f"=== Agentic Mapper Log — {datetime.now().isoformat()} ===")
        self._write(f"Log file: {self._path}\n")

    def _write(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        for line in text.splitlines():
            self._fh.write(f"[{ts}] {line}\n")
        self._fh.flush()

    def section(self, title: str) -> None:
        sep = "═" * 72
        self._write(f"\n{sep}")
        self._write(f"  {title}")
        self._write(sep)

    def kv(self, key: str, value: object) -> None:
        self._write(f"  {key}: {value}")

    def text(self, label: str, content: str, max_chars: int = 0) -> None:
        self._write(f"\n  ── {label} ({len(content)} chars) ──")
        body = content if (max_chars == 0 or len(content) <= max_chars) else (
            content[:max_chars] + f"\n... truncated ({len(content)} total chars)"
        )
        for line in body.splitlines():
            self._write(f"    │ {line}")

    def info(self, msg: str) -> None:
        self._write(f"  {msg}")

    def close(self) -> None:
        self._write("\n=== Log closed ===")
        self._fh.close()

    @property
    def path(self) -> Path:
        return self._path


_active_logger: AgenticLogger | None = None

from repo_map_generator import (
    RepoMapResult,
    generate_repo_map,
)

ENV_PATH = Path(__file__).with_name(".env")
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class FileSelectionResult(BaseModel):
    """Structured output: lists of files to boost and downgrade."""
    selected_files: list[str] = Field(default_factory=list)
    downgrade_files: list[str] = Field(default_factory=list)
    context_notes: str = Field(default="")


def _load_config() -> dict[str, str]:
    load_dotenv(ENV_PATH, override=False)
    return {
        "openrouter_api_key": os.getenv("OPENROUTER_API_KEY", "").strip(),
        "openrouter_base_url": os.getenv(
            "OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL
        ).strip(),
        "openrouter_model": os.getenv("OPENROUTER_MODEL", "").strip(),
        "triage_model": os.getenv("TRIAGE_MODEL", "").strip(),
        "repo_map_model": os.getenv("REPO_MAP_MODEL", "").strip(),
    }



# ─── Feature Exploration Agent ────────────────────────────────────────────────

_FLOWS_DIR = ".architectureX/flows"
_GENERAL_ARCH_FILE = ".architectureX/01_GENERAL_ARCHITECTURE.md"
_FLOW_FILES_DICT = ".architectureX/flows/flow_files_dictionary.json"

# ─── Triage prompt for lightweight feature selection ──────────────────────────
_TRIAGE_PROMPT = """\
You are a feature triage classifier. Given a user task and a feature catalog, \
return the 0-2 most relevant feature IDs as a JSON array.

RULES:
- Return [] if the task is too vague to map to any feature (e.g., "help", "fix it").
- Return [id] for tasks targeting ONE feature.
- Return [id1, id2] for tasks spanning TWO features. Never return more than 2.
- Match by SEMANTICS, not keywords. "SFU" → WebRTC. "DM" → Chat. "RBAC" → Auth+Team.
- For cross-cutting tasks ("add logging to all services"), pick the 1-2 features \
  most central to the task, not all features touched.

Output ONLY the JSON array of numeric IDs as strings, e.g. ["10"] or ["10", "14"] or [].
"""


def _extract_feature_catalog(architecture: str) -> str:
    """Extract just the Feature Catalog section from the architecture doc."""
    lines = architecture.splitlines()
    start = end = -1
    for i, line in enumerate(lines):
        if "Feature Catalog" in line and line.startswith("##"):
            start = i
        elif start >= 0 and line.startswith("## ") and i > start:
            end = i
            break
    if start < 0:
        return ""
    return "\n".join(lines[start:(end if end > 0 else len(lines))])


def _triage_features(
    user_prompt: str,
    feature_catalog: str,
    config: dict[str, str],
    flog: "AgenticLogger | None" = None,
) -> dict:
    """Lightweight LLM call to select 0-2 relevant feature IDs.

    Uses only the feature catalog (~600 tokens) as context, making this
    call ~10x cheaper and faster than the main agent call.
    Returns a dict with keys: ids, input_text, output_text, model.
    """
    empty = {"ids": [], "input_text": "", "output_text": "", "model": ""}
    if not feature_catalog.strip():
        return empty

    _triage_model_name = config.get("triage_model") or config["openrouter_model"]
    triage_model = ChatOpenAI(
        model=_triage_model_name,
        api_key=config["openrouter_api_key"],
        base_url=config["openrouter_base_url"],
        temperature=0,
        max_tokens=64,
    )

    human_content = (
        f"USER TASK:\n{user_prompt}\n\n"
        f"FEATURE CATALOG:\n{feature_catalog}"
    )
    triage_messages = [
        SystemMessage(content=_TRIAGE_PROMPT),
        HumanMessage(content=human_content),
    ]

    input_text = f"=== SYSTEM PROMPT ===\n{_TRIAGE_PROMPT}\n\n=== HUMAN MESSAGE ===\n{human_content}"

    try:
        resp = triage_model.invoke(triage_messages)
        raw = resp.content if isinstance(resp.content, str) else "[]"
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        ids = json.loads(cleaned)
        if isinstance(ids, list) and all(isinstance(x, (str, int)) for x in ids):
            result = [str(x).strip().zfill(2) for x in ids[:2]]
            if flog:
                flog.kv("Triage selected features", result)
            return {"ids": result, "input_text": input_text, "output_text": raw, "model": _triage_model_name}
    except Exception as exc:
        if flog:
            flog.info(f"Triage call failed: {exc} — continuing without flow injection.")
        print(f"  Triage call failed: {exc}", file=sys.stderr)
        return {"ids": [], "input_text": input_text, "output_text": f"ERROR: {exc}", "model": _triage_model_name}

    return {"ids": [], "input_text": input_text, "output_text": "", "model": _triage_model_name}


def _load_flow_contents(root: str, feature_ids: list[str]) -> str:
    """Read flow .md files for the given feature IDs and return as a combined string."""
    if not feature_ids:
        return ""

    flows_path = Path(root) / _FLOWS_DIR
    if not flows_path.is_dir():
        return ""

    sections = []
    for fid in feature_ids:
        fid = fid.strip().zfill(2)
        for f in sorted(flows_path.iterdir()):
            if f.is_file() and f.suffix.lower() == ".md" and f.name.startswith(fid):
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    sections.append(
                        f"═══ FEATURE {fid} FLOW DETAILS ═══\n{content}\n═══ END FEATURE {fid} ═══"
                    )
                except OSError:
                    pass
                break

    return "\n\n".join(sections)


def _get_general_architecture(root: str) -> str:
    """Read the general architecture document from {root}/.architectureX/."""
    arch_path = Path(root) / _GENERAL_ARCH_FILE
    if not arch_path.is_file():
        return ""
    try:
        return arch_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""



def _get_feature_details(root: str, feature_id: str) -> dict:
    """Read the flow file matching the given numeric ID.

    Looks in {root}/.architectureX/flows/ for any .md file whose name
    starts with the requested ID (e.g., feature_id="01" matches
    "01_auth_flow.md").  Returns the full content (truncated at ~20 file
    references for the agent) plus metadata.
    """
    fid = feature_id.strip().zfill(2)
    flows_path = Path(root) / _FLOWS_DIR

    if not flows_path.is_dir():
        return {
            "feature_id": fid,
            "name": f"Unknown Feature {fid}",
            "description": f"No .architectureX/flows/ directory found.",
            "related_files": [],
            "content": "",
        }

    # Find the first .md file whose name starts with the ID
    target: Path | None = None
    for f in sorted(flows_path.iterdir()):
        if f.is_file() and f.suffix.lower() == ".md" and f.name.startswith(fid):
            target = f
            break

    if target is None:
        return {
            "feature_id": fid,
            "name": f"Unknown Feature {fid}",
            "description": f"No flow file starting with '{fid}' found.",
            "related_files": [],
            "content": "",
        }

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {
            "feature_id": fid,
            "name": target.stem,
            "description": "(could not read file)",
            "related_files": [],
            "content": "",
        }

    # Extract a title from the first heading or first non-empty line
    name = target.stem
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            name = stripped
            break

    # Look up related files from the pre-built dictionary
    related_files: list[str] = []
    dict_path = Path(root) / _FLOW_FILES_DICT
    if dict_path.is_file():
        try:
            flow_dict = json.loads(
                dict_path.read_text(encoding="utf-8", errors="replace")
            )
            related_files = flow_dict.get(fid, [])
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "feature_id": fid,
        "name": name,
        "description": content,
        "related_files": related_files,
    }


# ─── File content injection ────────────────────────────────────────────────
# Pre-deliver full source code for the agent's top-selected files so the
# downstream coding agent (Copilot) can start editing immediately without
# issuing read_file tool calls.  Files are included in boost-priority order
# until the token budget is exhausted.

_FILE_CONTENT_BUDGET_TOKENS = 30_000   # ~120 KB of source code
_MAX_FILE_LINES = 500                  # truncate individual files beyond this
_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".tar", ".gz", ".bz2", ".7z",
    ".exe", ".dll", ".so", ".dylib",
    ".pdf", ".doc", ".docx",
    ".mp3", ".mp4", ".wav", ".avi",
    ".vsix", ".wasm", ".lock", ".pyc",
}


def _read_boosted_file_contents(
    root: str,
    selected_files: list[str],
    budget_tokens: int = _FILE_CONTENT_BUDGET_TOKENS,
) -> str:
    """Read full contents of top boosted files, fitting within *budget_tokens*.

    Files are included in the order they appear in *selected_files* (which
    reflects the agent's priority ranking).  Each file is capped at
    ``_MAX_FILE_LINES`` lines.  Returns a ready-to-inject text block with
    clear delimiters, or ``""`` if nothing fits.
    """
    if not selected_files:
        return ""

    sections: list[str] = []
    tokens_used = 0

    for rel_path in selected_files:
        # Skip binary / non-text files
        ext = os.path.splitext(rel_path)[1].lower()
        if ext in _BINARY_EXTENSIONS:
            continue

        abs_path = os.path.join(root, rel_path)
        if not os.path.isfile(abs_path):
            continue

        try:
            content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Truncate very long files
        lines = content.splitlines(keepends=True)
        if len(lines) > _MAX_FILE_LINES:
            content = "".join(lines[:_MAX_FILE_LINES])
            content += (
                f"\n... (truncated — {len(lines)} total lines, "
                f"showing first {_MAX_FILE_LINES})\n"
            )

        est_tokens = len(content) // 4
        if tokens_used + est_tokens > budget_tokens:
            if sections:
                break  # already have some files, stop here
            # First file and it's huge — truncate to fit
            max_chars = budget_tokens * 4
            content = content[:max_chars]
            content += "\n... (truncated to fit token budget)\n"
            est_tokens = len(content) // 4

        sections.append(
            f"── {rel_path} ──\n{content}\n── end of {rel_path} ──"
        )
        tokens_used += est_tokens

    if not sections:
        return ""

    return (
        f"═══ FULL FILE CONTENTS ({len(sections)} boosted files, "
        f"~{tokens_used} tokens) — read these; they are the primary edit targets ═══\n\n"
        + "\n\n".join(sections)
        + "\n\n═══ END FILE CONTENTS ═══\n\n"
    )


FEATURE_EXPLORATION_PROMPT = """\
You are a repository analyst selecting files for a coding agent's context map. \
Files you boost get full structural detail (signatures); files you downgrade \
lose prominence. Your selection directly determines coding quality.

═══ INPUTS ═══
1. USER TASK — What the developer wants accomplished.
2. GENERAL ARCHITECTURE — High-level doc with layers, conventions, features \
(each with a numeric ID like 01, 02, 10).
3. FEATURE FLOW DETAILS (if present) — Pre-selected detailed flow docs for \
the 1-2 features most relevant to the task. Contains exact call chains, \
cross-feature coupling, FK cascades, hidden side effects, and file lists. \
TRUST THIS as ground truth for the target feature's file boundaries.
4. REPO MAP — Tree-sitter structural overview: file paths with symbols. \
Lines with │ = definitions; ⋮... = elided code. Ranked by PageRank.

You may select ANY file visible in REPO MAP, ARCHITECTURE, FLOW DETAILS, \
or returned by the get_feature_details tool.

═══ TOOL ═══
get_feature_details(feature_id: str) → feature description + up to 20 files.
You may call it ONCE or not at all. This is single-turn — no retries.

TOOL DECISION: Call ONLY when ONE feature has LOW confidence (you cannot \
determine its files from map alone) AND no other feature also has LOW \
confidence. If map already shows clear chains, or multiple features are \
uncertain, SKIP the tool.

═══ FILE SELECTION ═══

BOOST (3–12 files, precision over recall):
  Tier 1: files to directly edit
  Tier 2: interfaces/types imported by Tier 1
  Tier 3: consumers of Tier 1 that may need updates
  Tier 4: tests covering affected paths
  Tier 5: config/migrations/schemas
  For each file: "agent needs [file] because [specific reason]." Drop if \
  you can't complete this. Don't select on keyword overlap alone. Don't \
  include middleware/utilities unless task explicitly targets them.

DOWNGRADE (0–15 files, be thorough):
  Walk every non-boosted file in the REPO MAP. Downgrade files OUTSIDE \
  the task's feature domain — especially infra, config, DB setup, auth, \
  logging, utilities, test fixtures, CI boilerplate. Maximum 15 files.
  CRITICAL DOMAIN RULE: If a file is in the SAME package/directory/domain \
  as the task target (e.g., task mentions "agentical" → ALL files under \
  internal/agentical/ are in-domain), do NOT downgrade it — even if it \
  wasn't boosted. Peripheral files within the target domain (tests, \
  helpers, related actions) must keep their prominence.
  RULES: Only downgrade paths IN the REPO MAP. Never downgrade boosted \
  files. For broad/cross-cutting tasks, downgrade ZERO files.

═══ OUTPUT ═══
Return JSON only:
{
  "selected_files": ["path/file.go", ...],
  "downgrade_files": ["path/file.go", ...],
  "context_notes": "4-8 sentences of navigational guidance"
}

context_notes: Include ONLY what the map can't show — call/data-flow \
direction between selected files, entry points, cross-file contracts, \
runtime dependencies, non-obvious coupling. NEVER include implementation \
suggestions, symbol names already in the map, or general descriptions.

Do reasoning internally. Output ONLY the JSON.\
"""


def run_agentic_map(
    root: str,
    user_prompt: str,
    boost_factor: float | None = None,
    downgrade_factor: float | None = None,
) -> RepoMapResult:
    """Generate a repo map and run a single agent to select relevant files.

    The agent receives the repo map + general architecture and can
    optionally call get_feature_details ONCE before returning its file
    selection.  Returns a RepoMapResult with the agent's selections set
    as chat_rel_fnames.
    """
    global _active_logger
    config = _load_config()

    flog = AgenticLogger(root)
    _active_logger = flog
    print(f"Agentic log file: {flog.path}", file=sys.stderr)

    flog.section("INPUT")
    flog.kv("Root", root)
    flog.kv("User prompt", user_prompt)
    flog.kv("OpenRouter model", config.get("openrouter_model", "(not set)"))
    flog.kv("Repo map model", config.get("repo_map_model", "(not set)"))

    # ── Structured step log for UI ────────────────────────────────────────
    steps: list[dict] = []

    missing = []
    if not config["openrouter_api_key"]:
        missing.append("OPENROUTER_API_KEY")
    if not config["openrouter_model"]:
        missing.append("OPENROUTER_MODEL")
    if not config["repo_map_model"]:
        missing.append("REPO_MAP_MODEL")
    if missing:
        flog.info(f"ERROR: Missing .env keys: {', '.join(missing)}")
        flog.close()
        _active_logger = None
        print(
            f"ERROR: Missing .env keys: {', '.join(missing)}", file=sys.stderr
        )
        sys.exit(1)

    # ── Read architecture early (needed for triage, runs in parallel) ────
    general_architecture = _get_general_architecture(root)
    feature_catalog = _extract_feature_catalog(general_architecture)

    # ── Generate repo map + triage in parallel ────────────────────────────
    # Triage only needs the feature catalog (~600 tokens) and user prompt,
    # NOT the repo map.  Running them concurrently eliminates triage latency.
    from concurrent.futures import ThreadPoolExecutor, Future

    print("Generating repo map...", file=sys.stderr)
    flog.section("REPO MAP GENERATION")
    import time as _time
    _t0 = _time.perf_counter()

    triage_future: Future[list[str]] | None = None

    with ThreadPoolExecutor(max_workers=2) as pool:
        # Submit repo map generation
        repo_map_future = pool.submit(
            generate_repo_map,
            root,
            user_prompt=user_prompt,
            model_name=config["repo_map_model"],
        )
        # Submit triage concurrently (if we have a catalog)
        if feature_catalog:
            triage_future = pool.submit(
                _triage_features, user_prompt, feature_catalog, config, flog
            )

        result = repo_map_future.result()
        triage_result: dict = (
            triage_future.result() if triage_future is not None
            else {"ids": [], "input_text": "", "output_text": "", "model": ""}
        )
        triage_feature_ids: list[str] = triage_result["ids"]

    _t1 = _time.perf_counter()
    print(f"  Pass 1 repo map + triage completed in {_t1 - _t0:.2f}s", file=sys.stderr)
    flog.kv("Pass 1 + triage duration", f"{_t1 - _t0:.2f}s")

    flog.kv("Effective budget", f"{result.effective_budget} tokens")
    flog.text("Repo map", result.repo_map)

    # STEP 1: Pass 1 repo map result
    steps.append({
        "title": "Pass 1 — Initial Repo Map",
        "content": result.repo_map,
        "meta": f"Budget: {result.effective_budget} tokens | Duration: {_t1 - _t0:.2f}s | Files: {len(result.rel_files)}",
    })

    # STEP 2: Triage LLM input
    if triage_result["input_text"]:
        steps.append({
            "title": "Triage LLM — Input",
            "content": triage_result["input_text"],
            "meta": f"Model: {triage_result['model']}",
        })
    # STEP 3: Triage LLM output
    if triage_result["output_text"] or triage_feature_ids:
        steps.append({
            "title": "Triage LLM — Output",
            "content": triage_result["output_text"] or "(no output)",
            "meta": f"Selected features: {triage_feature_ids or '(none)'}",
        })

    if not result.repo_map.strip():
        print("  Empty repo map — returning as-is.", file=sys.stderr)
        flog.info("Empty repo map — returning as-is.")
        flog.close()
        _active_logger = None
        return result

    # ── Run agent ─────────────────────────────────────────────────────────
    print("Running agent...", file=sys.stderr)
    flog.section("AGENT")

    if general_architecture:
        flog.text("General architecture", general_architecture)

    # ── Load flow content from triage results ─────────────────────────────
    flow_content = ""

    if triage_feature_ids:
        flog.section("FEATURE TRIAGE")
        flog.kv("Triage features", triage_feature_ids)

        # Validate IDs against actual flow files on disk
        flows_path = Path(root) / _FLOWS_DIR
        if flows_path.is_dir():
            valid_ids = set()
            for f in flows_path.iterdir():
                if f.is_file() and f.suffix.lower() == ".md":
                    valid_ids.add(f.name[:2])
            triage_feature_ids = [fid for fid in triage_feature_ids if fid in valid_ids]
            flog.kv("Validated triage features", triage_feature_ids)

        if triage_feature_ids:
            flow_content = _load_flow_contents(root, triage_feature_ids)
            if flow_content:
                flog.text("Injected flow content", flow_content, max_chars=2000)
                print(f"  Injected {len(triage_feature_ids)} flow(s) "
                      f"({len(flow_content)} chars)", file=sys.stderr)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_feature_details",
                "description": (
                    "Get detailed information about a repository feature. "
                    "Returns the feature's description, data flow, and up to "
                    "20 related files. You may call this ONCE or not at all."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "feature_id": {
                            "type": "string",
                            "description": (
                                "The numeric ID of the feature (e.g., '01', "
                                "'02', '10'). Must match an ID from the "
                                "GENERAL ARCHITECTURE document."
                            ),
                        }
                    },
                    "required": ["feature_id"],
                },
            },
        }
    ]

    model = ChatOpenAI(
        model=config["openrouter_model"],
        api_key=config["openrouter_api_key"],
        base_url=config["openrouter_base_url"],
        temperature=0,
        max_tokens=8192,
    )
    model_with_tools = model.bind_tools(tools)

    general_arch_section = (
        f"GENERAL ARCHITECTURE:\n{general_architecture}\n\n"
        if general_architecture else ""
    )

    flow_section = (
        f"FEATURE FLOW DETAILS (pre-selected by triage):\n{flow_content}\n\n"
        if flow_content else ""
    )

    messages = [
        SystemMessage(content=FEATURE_EXPLORATION_PROMPT),
        HumanMessage(
            content=(
                f"USER TASK:\n{user_prompt}\n\n"
                f"{general_arch_section}"
                f"{flow_section}"
                f"REPO MAP:\n{result.repo_map}\n\n"
                f"Output ONLY the JSON with selected_files, downgrade_files, context_notes."
            )
        ),
    ]

    flog.text("System prompt", FEATURE_EXPLORATION_PROMPT)
    flog.text("Human message", messages[1].content)

    # STEP 4: Exact input to first LLM call
    steps.append({
        "title": "LLM Call 1 — Input",
        "content": f"=== SYSTEM PROMPT ===\n{FEATURE_EXPLORATION_PROMPT}\n\n=== HUMAN MESSAGE ===\n{messages[1].content}",
    })

    # ── LLM call (with optional single tool call) ─────────────────────────
    try:
        response = model_with_tools.invoke(messages)
    except (OpenAIError, httpx.HTTPError, ValueError) as exc:
        print(f"  Agent failed: {exc}\n  Returning repo map as-is.", file=sys.stderr)
        flog.info(f"Agent FAILED: {exc}")
        flog.close()
        _active_logger = None
        return result

    messages.append(response)
    explored_features: list[str] = []

    response_text = response.content if isinstance(response.content, str) else ""
    print(f"  LLM responded ({len(response_text)} chars, "
          f"{len(response.tool_calls)} tool calls)", file=sys.stderr)
    if flog:
        flog.kv("Response length", f"{len(response_text)} chars")
        flog.kv("Tool calls", len(response.tool_calls))
        if response_text:
            flog.text("LLM response", response_text)

    # STEP 5: Output of first LLM call
    step3_content = response_text or "(no text content)"
    if response.tool_calls:
        tc = response.tool_calls[0]
        step3_content += f"\n\n[Tool call: {tc['name']}({json.dumps(tc['args'])})]"
    steps.append({
        "title": "LLM Call 1 — Output",
        "content": step3_content,
        "meta": f"Length: {len(response_text)} chars | Tool calls: {len(response.tool_calls)}",
    })

    if response.tool_calls:
        tc = response.tool_calls[0]
        print(f"  Tool call: {tc['name']}({json.dumps(tc['args'])})", file=sys.stderr)
        if tc["name"] == "get_feature_details":
            fid = tc["args"].get("feature_id", "")
            explored_features.append(fid)
            details = _get_feature_details(root, fid)
            flog.kv("Feature explored", f"{fid} — {details.get('name', '?')}")
            flog.text("Feature details", json.dumps(details, indent=2))
            messages.append(
                ToolMessage(content=json.dumps(details), tool_call_id=tc["id"])
            )

    # ── Extract structured file selection ─────────────────────────────────
    # FAST PATH: If the initial LLM response already contains valid JSON
    # with selected_files, extract directly and skip the second LLM call.
    # This avoids the re-generation hallucination issue where the structured
    # extraction call (seeing only a file listing, not the full repo map)
    # produces different selections than the initial response.
    selection: FileSelectionResult | None = None

    if response_text and not response.tool_calls:
        try:
            cleaned = re.sub(r"^```(?:json)?\s*\n?", "", response_text.strip())
            cleaned = re.sub(r"\n?```\s*$", "", cleaned)
            parsed = json.loads(cleaned)
            if "selected_files" in parsed:
                selection = FileSelectionResult(
                    selected_files=parsed.get("selected_files", []),
                    downgrade_files=parsed.get("downgrade_files", []),
                    context_notes=parsed.get("context_notes", ""),
                )
                print("  Extracted selection from initial response (fast path).",
                      file=sys.stderr)
                if flog:
                    flog.info("Fast-path extraction from initial LLM response succeeded.")
                # STEP 6 & 7: Fast path — no second LLM call needed
                steps.append({
                    "title": "LLM Call 2 — Input (skipped: fast path)",
                    "content": "Initial LLM response already contained valid JSON. No second call needed.",
                })
                steps.append({
                    "title": "LLM Call 2 — Output (skipped: fast path)",
                    "content": f"Extracted directly from first response:\n{json.dumps({'selected_files': selection.selected_files, 'downgrade_files': selection.downgrade_files, 'context_notes': selection.context_notes}, indent=2)}",
                })
        except (json.JSONDecodeError, KeyError):
            pass  # Fall through to structured extraction

    # SLOW PATH: Build a trimmed message list for the structured output call.
    # Used when: (a) initial response didn't contain parseable JSON, or
    # (b) a tool call was made and we need the final structured selection.
    if selection is None:
        # Include a compact listing of ALL file paths in the repo map so the model
        # can systematically evaluate each file for downgrade decisions.
        repo_map_files = []
        for line in result.repo_map.splitlines():
            stripped = line.strip()
            if (stripped and not stripped.startswith("│") and
                    not stripped.startswith("⋮") and stripped.endswith(":") and
                    "/" in stripped):
                repo_map_files.append(stripped.rstrip(":"))

        file_listing = "\n".join(f"  {f}" for f in repo_map_files)

        selection_messages = [messages[0]]  # SystemMessage
        selection_messages.append(
            HumanMessage(content=(
                f"USER TASK:\n{user_prompt}\n\n"
                f"FILES IN REPO MAP ({len(repo_map_files)} files):\n{file_listing}"
            ))
        )
        # Carry over assistant response + optional tool message
        for msg in messages[2:]:
            selection_messages.append(msg)
        selection_messages.append(
            HumanMessage(content=(
                "Provide your final file selection now. "
                "Output ONLY the JSON with selected_files, downgrade_files, "
                "and context_notes. "
                "Do NOT repeat your reasoning — you already did it above."
            ))
        )

        final_model = ChatOpenAI(
            model=config["openrouter_model"],
            api_key=config["openrouter_api_key"],
            base_url=config["openrouter_base_url"],
            temperature=0,
            max_tokens=16384,
        ).with_structured_output(FileSelectionResult)

        # STEP 6: Exact input to second LLM call
        step4_parts = []
        for i, msg in enumerate(selection_messages):
            role = type(msg).__name__
            content = msg.content if hasattr(msg, 'content') else str(msg)
            step4_parts.append(f"=== Message {i+1} ({role}) ===\n{content}")
        steps.append({
            "title": "LLM Call 2 — Input (structured extraction)",
            "content": "\n\n".join(step4_parts),
        })

        try:
            selection = final_model.invoke(selection_messages)
            # STEP 7: Output of second LLM call
            steps.append({
                "title": "LLM Call 2 — Output",
                "content": json.dumps({
                    "selected_files": selection.selected_files,
                    "downgrade_files": selection.downgrade_files,
                    "context_notes": selection.context_notes,
                }, indent=2),
            })
        except (OpenAIError, httpx.HTTPError, ValueError) as exc:
            # Fallback: try to extract JSON from a raw model response
            # (handles models that wrap output in ```json ... ``` fences)
            print(f"  Structured extraction failed: {exc}", file=sys.stderr)
            print("  Trying raw JSON fallback...", file=sys.stderr)
            flog.info(f"Structured extraction FAILED: {exc}")
            flog.info("Trying raw JSON fallback...")
            try:
                raw_model = ChatOpenAI(
                    model=config["openrouter_model"],
                    api_key=config["openrouter_api_key"],
                    base_url=config["openrouter_base_url"],
                    temperature=0,
                    max_tokens=16384,
                )
                raw_response = raw_model.invoke(selection_messages)
                raw_text = raw_response.content if isinstance(raw_response.content, str) else ""
                # Strip markdown code fences if present
                cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw_text.strip())
                cleaned = re.sub(r"\n?```\s*$", "", cleaned)
                parsed = json.loads(cleaned)
                selection = FileSelectionResult(
                    selected_files=parsed.get("selected_files", []),
                    downgrade_files=parsed.get("downgrade_files", []),
                    context_notes=parsed.get("context_notes", ""),
                )
                print(f"  Fallback succeeded.", file=sys.stderr)
                flog.info("Raw JSON fallback succeeded.")
                # STEP 7: Output of second LLM call (fallback)
                steps.append({
                    "title": "LLM Call 2 — Output (raw fallback)",
                    "content": json.dumps({
                        "selected_files": selection.selected_files,
                        "downgrade_files": selection.downgrade_files,
                        "context_notes": selection.context_notes,
                    }, indent=2),
                })
            except Exception as fallback_exc:
                print(f"  Fallback also failed: {fallback_exc}\n  Returning repo map as-is.",
                      file=sys.stderr)
                flog.info(f"Fallback FAILED: {fallback_exc}")
                flog.close()
                _active_logger = None
                return result

    selected = selection.selected_files
    downgraded = selection.downgrade_files

    # Remove any overlap: boost wins over downgrade
    downgrade_set = set(downgraded) - set(selected)
    downgraded = [f for f in downgraded if f in downgrade_set]

    print(f"  Selected {len(selected)} files to boost", file=sys.stderr)
    for i, f in enumerate(selected, 1):
        print(f"    {i:2d}. ↑ {f}", file=sys.stderr)
    print(f"  Selected {len(downgraded)} files to downgrade", file=sys.stderr)
    for i, f in enumerate(downgraded, 1):
        print(f"    {i:2d}. ↓ {f}", file=sys.stderr)

    flog.section("AGENT SELECTION")
    flog.kv("Boosted files", len(selected))
    for i, f in enumerate(selected, 1):
        flog.info(f"  {i:2d}. ↑ {f}")
    flog.kv("Downgraded files", len(downgraded))
    for i, f in enumerate(downgraded, 1):
        flog.info(f"  {i:2d}. ↓ {f}")
    flog.kv("Explored features", explored_features or "(none)")

    if not selected and not downgraded:
        print("  No files selected — returning initial repo map.", file=sys.stderr)
        flog.info("No files selected — returning initial repo map.")
        flog.close()
        _active_logger = None
        return result

    # ── Pass 2: Regenerate repo map with agent's files boosted/downgraded ─
    print("Regenerating repo map with agent-selected files boosted/downgraded...",
          file=sys.stderr)
    flog.section("PASS 2 — BOOSTED/DOWNGRADED REPO MAP")
    flog.kv("Boosted files", selected)
    flog.kv("Downgraded files", downgraded)

    _t2 = _time.perf_counter()
    boosted_result = generate_repo_map(
        root,
        user_prompt=user_prompt,
        model_name=config["repo_map_model"],
        agent_boost_fnames=selected,
        agent_downgrade_fnames=downgraded,
        force_budget_multiplier=2,
        agent_boost_factor=boost_factor,
        agent_downgrade_factor=downgrade_factor,
    )
    _t3 = _time.perf_counter()
    print(f"  Pass 2 repo map generated in {_t3 - _t2:.2f}s", file=sys.stderr)
    flog.kv("Pass 2 duration", f"{_t3 - _t2:.2f}s")

    flog.kv("Effective budget", f"{boosted_result.effective_budget} tokens")
    flog.text("Boosted repo map", boosted_result.repo_map)

    # STEP 8: Pass 2 repo map with boost/downgrade
    boost_info = "\n".join(f"  ↑ {f}" for f in selected)
    downgrade_info = "\n".join(f"  ↓ {f}" for f in downgraded)
    steps.append({
        "title": "Pass 2 — Boosted/Downgraded Repo Map",
        "content": f"Boosted files:\n{boost_info}\n\nDowngraded files:\n{downgrade_info}\n\n--- Repo Map ---\n{boosted_result.repo_map}",
        "meta": f"Budget: {boosted_result.effective_budget} tokens | Duration: {_t3 - _t2:.2f}s | Files: {len(boosted_result.rel_files)} | Boost: {boost_factor}× | Downgrade: {downgrade_factor}×",
    })

    # ── Prepend agent context notes to the repo map ───────────────────────
    context_notes = selection.context_notes.strip() if selection.context_notes else ""
    if context_notes:
        notes_block = (
            "═══ AGENT CONTEXT — read before the map ═══\n"
            f"{context_notes}\n"
            "═══ END CONTEXT ═══\n\n"
        )
        boosted_result = _dc_replace(
            boosted_result, repo_map=notes_block + boosted_result.repo_map
        )
        flog.section("AGENT CONTEXT NOTES")
        flog.text("Context notes", context_notes)
        print(f"  Context notes: {len(context_notes)} chars prepended to repo map",
              file=sys.stderr)
    else:
        flog.info("No context notes produced — repo map returned as-is.")

    # ── Inject full file contents for top boosted files ─────────────────
    # Pre-deliver the actual source code so the downstream coding agent
    # (Copilot) can start editing without issuing read_file tool calls.
    file_content_block = _read_boosted_file_contents(root, selected)
    if file_content_block:
        boosted_result = _dc_replace(
            boosted_result,
            repo_map=boosted_result.repo_map + "\n" + file_content_block,
        )
        # Count how many files were injected (from the header line)
        injected_count = file_content_block.count("── end of ")
        flog.section("FILE CONTENT INJECTION")
        flog.kv("Files injected", injected_count)
        flog.kv("Content block size", f"{len(file_content_block)} chars")
        flog.kv("Estimated tokens", f"~{len(file_content_block) // 4}")
        print(
            f"  Injected full contents of {injected_count} boosted files "
            f"(~{len(file_content_block) // 4} tokens)",
            file=sys.stderr,
        )
    else:
        flog.info("No file contents injected (no readable boosted files).")

    flog.close()
    _active_logger = None
    print(f"Agentic log saved to: {flog.path}", file=sys.stderr)

    # Attach structured steps to the result
    boosted_result = _dc_replace(boosted_result, agentic_steps=steps)

    return boosted_result
