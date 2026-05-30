#!/usr/bin/env python3
"""
Aider_map.py — Generate an Aider repo map using Aider's own RepoMap class.

This is the **authentic** Aider repo map output — using aider-chat directly.
We use this as the reference to compare against our VS Code extension's output.

The output is bit-for-bit identical to what Aider injects into the LLM prompt
during a pair-programming session.

Dependencies:
    pip install aider-chat

Usage:
    PYTHONHASHSEED=0 python Aider_map.py [--model MODEL] [--prompt "text"] [root_dir]
"""

# ─── Deterministic hash seed ──────────────────────────────────────────────────
# Aider's RepoMap uses sets/dicts whose iteration order depends on Python's
# hash seed.  Files near the token budget boundary with near-identical
# PageRank scores swap in/out across runs when the seed differs.
# Setting PYTHONHASHSEED=0 before interpreter start eliminates this.
# When run as a script, we re-exec with the seed pinned.
# When imported (e.g. from service.py), the importer must set the env var
# before starting Python (see Dockerfile / docker-compose).
import os as _os
import sys as _sys
if __name__ == "__main__" and _sys.flags.hash_randomization:
    _os.environ["PYTHONHASHSEED"] = "0"
    _os.execv(_sys.executable, [_sys.executable] + _sys.argv)

from aider.io import InputOutput
from aider.models import Model
from aider.repo import GitRepo
from aider.repomap import RepoMap
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import math
import networkx as _nx
import os
import re
import subprocess
import sys
import time
import warnings

warnings.simplefilter("ignore", category=FutureWarning)

DEFAULT_MODEL_NAME = "gpt-4o-mini"

# ─── Result cache ─────────────────────────────────────────────────────────────
# Module-level cache: maps cache_key → RepoMapResult.
# Survives across calls within the same process (e.g. uvicorn service).
_result_cache: dict[str, "RepoMapResult"] = {}

# ─── Intermediate cache ──────────────────────────────────────────────────────
# Caches expensive intermediate objects (Model, RepoMap instance with warm
# tag cache, file list) across calls with the same (root, model_name).
# Invalidated when the repo fingerprint changes.  This allows the agentic
# pipeline's second pass (with boost/downgrade) to skip Model init, git file
# discovery, and tree-sitter tag parsing — only PageRank + rendering re-run.
_intermediate_cache: dict[tuple[str, str], dict] = {}

# ─── Dictionary-based personalization boost ───────────────────────────────────
# Maps rel_fname → personalization multiplier for files matched by the
# project dictionary (.architectureX/project_dictionary.json).
# Set before RepoMap.get_repo_map() and cleared immediately after, within
# generate_repo_map().  Thread-safe when workers are separate processes
# (uvicorn default) or requests are processed sequentially.
_DICT_BOOST_MULTIPLIER = 3
_dict_boost_targets: dict[str, float] = {}

# ─── Agent-selected personalization boost ─────────────────────────────────────
# Same mechanism as dict boost but for files chosen by the agentic mapper.
# Uses a higher multiplier (5×) so agent-selected files rank above
# dictionary-matched ones.  Set/cleared identically to _dict_boost_targets.
_AGENT_BOOST_MULTIPLIER = 10
_agent_boost_targets: dict[str, float] = {}

# ─── Agent-selected personalization downgrade ─────────────────────────────────
# Same mechanism as agent boost but for files the agent wants to suppress.
# Uses a 1/20 multiplier (0.05) so downgraded files receive 20× less
# PageRank teleportation traffic.  Set/cleared identically.
_AGENT_DOWNGRADE_MULTIPLIER = 0.05  # 1/20 → 20× reduction
_agent_downgrade_targets: dict[str, float] = {}


def _compute_repo_fingerprint(root: str) -> str | None:
    """Return a hash that changes whenever the repo content or history changes.

    Uses git HEAD SHA + working-tree status (staged + unstaged changes) +
    mtimes of dirty files as a fast proxy for content.  Returns None when
    the directory is not inside a git repository.
    """
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root, text=True, timeout=5, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return None

    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain", "-uno"],
            cwd=root, text=True, timeout=5, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        status = ""

    # Include mtimes for dirty files so edits within the same commit are detected
    dirty_mtimes: list[str] = []
    for line in status.splitlines():
        fname = line[3:].strip()
        fpath = os.path.join(root, fname)
        try:
            dirty_mtimes.append(f"{fname}:{os.path.getmtime(fpath):.6f}")
        except OSError:
            dirty_mtimes.append(f"{fname}:deleted")

    parts = [head, status] + sorted(dirty_mtimes)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _dict_fingerprint(root: str) -> str:
    """Return a fast fingerprint of the project dictionary file.

    Uses mtime + size as a proxy.  Returns "" when the file is absent,
    which is a valid (and stable) cache-key component.
    """
    dict_path = os.path.join(root, ".architectureX", "project_dictionary.json")
    try:
        st = os.stat(dict_path)
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return ""


def _make_cache_key(
    root: str,
    user_prompt: str | None,
    model_name: str,
    chat_rel_fnames: list[str] | None,
    repo_fingerprint: str,
    force_budget_multiplier: int | None = None,
    agent_boost_fnames: list[str] | None = None,
    agent_downgrade_fnames: list[str] | None = None,
) -> str:
    """Deterministic cache key from all inputs that affect the output."""
    key_data = json.dumps(
        {
            "root": root,
            "prompt": user_prompt,
            "model": model_name,
            "chat": sorted(chat_rel_fnames or []),
            "repo": repo_fingerprint,
            "dict": _dict_fingerprint(root),
            "budget_mul": force_budget_multiplier,
            "agent_boost": sorted(agent_boost_fnames or []),
            "agent_downgrade": sorted(agent_downgrade_fnames or []),
        },
        sort_keys=True,
    )
    return hashlib.sha256(key_data.encode()).hexdigest()


# ─── Strategy 2: Parallel tag pre-warming ─────────────────────────────────────
# Pre-warm Aider's on-disk tag cache (diskcache/SQLite) in parallel before the
# sequential pipeline runs.  get_tags() results are deterministic — same file
# content always produces the same tags — so pre-warming via ThreadPoolExecutor
# populates the cache without changing any outputs.  When the sequential
# get_ranked_tags() loop later calls get_tags() for each file, every call
# returns instantly from the warm cache.
#
# IMPORTANT: Parallel pre-warming is ONLY safe on a warm cache (reads are
# concurrent-safe).  On a cold cache, concurrent SQLite writes cause WAL
# contention and occasional tags_cache_error() calls that silently swap the
# cache backend to a plain dict, losing entries and introducing
# non-determinism.  Therefore we check the cache fill ratio before deciding
# whether to use threads.

_PARALLEL_TAG_MIN_FILES = 32  # below this, overhead > benefit
_CACHE_WARM_RATIO = 0.5       # cache must contain ≥ 50% of files to be "warm"


def _prewarm_tags_parallel(repo_map_instance, fnames, max_workers=None):
    """Pre-warm the tag cache for *fnames* using parallel threads.

    Skips parallel pre-warming when the cache is cold (< 50% of files cached)
    to avoid SQLite write contention that causes non-deterministic output.
    The sequential get_ranked_tags() loop will populate the cache instead.
    """
    if max_workers is None:
        max_workers = min(os.cpu_count() or 4, 8)

    if len(fnames) < _PARALLEL_TAG_MIN_FILES:
        return  # too few files to benefit from parallelism

    # Check cache fill ratio — only use threads when cache is warm
    try:
        cache_size = len(repo_map_instance.TAGS_CACHE)
    except Exception:
        cache_size = 0
    if cache_size < len(fnames) * _CACHE_WARM_RATIO:
        return  # cold cache — let the sequential loop handle it safely

    def _get_tags_one(fname):
        try:
            if not Path(fname).is_file():
                return
            rel_fname = repo_map_instance.get_rel_fname(fname)
            repo_map_instance.get_tags(fname, rel_fname)
        except Exception:
            pass  # will be retried by the sequential loop

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_get_tags_one, f) for f in fnames]
        for future in as_completed(futures):
            pass  # just wait for completion; errors handled inside worker


# ─── Strategy 3: GPU-accelerated PageRank ─────────────────────────────────────
# Monkey-patch networkx.pagerank so that *any* caller (including Aider's
# get_ranked_tags which does ``import networkx as nx; nx.pagerank(...)``) will
# transparently use GPU acceleration when available.
#
# Priority: cugraph (NVIDIA GPU) → original networkx (CPU fallback).
# When cugraph is not installed the wrapper is effectively a no-op — it calls
# the original function directly, producing bit-identical results.

_original_nx_pagerank = _nx.pagerank


def _gpu_accelerated_pagerank(
    G,
    alpha=0.85,
    personalization=None,
    max_iter=100,
    tol=1.0e-6,
    nstart=None,
    weight="weight",
    dangling=None,
):
    """Drop-in replacement for nx.pagerank that tries GPU acceleration."""
    # ── Apply dictionary-based personalization boosts ─────────────────────
    # When generate_repo_map() populates _dict_boost_targets, multiply the
    # personalization weight of those files so they attract more PageRank
    # teleportation traffic.  A 3× multiplier on the 15% teleportation
    # component is much gentler than chat files' 50× edge-weight boost and
    # does not distort the dominant 85% graph-walk signal.
    if personalization is not None and (_dict_boost_targets or _agent_boost_targets or _agent_downgrade_targets):
        personalization = dict(personalization)  # copy to avoid side-effects
        for fname, mult in _dict_boost_targets.items():
            if fname in personalization:
                personalization[fname] *= mult
        for fname, mult in _agent_boost_targets.items():
            if fname in personalization:
                personalization[fname] *= mult
        for fname, mult in _agent_downgrade_targets.items():
            if fname in personalization:
                personalization[fname] *= mult
        # Keep dangling distribution consistent with personalization
        if dangling is not None:
            dangling = personalization

    # ── Fast path: try cugraph (NVIDIA RAPIDS) ──
    try:
        import cugraph
        import cudf

        if len(G) == 0:
            return {}

        node_list = list(G.nodes())
        node_to_idx = {n: i for i, n in enumerate(node_list)}

        # Build edge list, aggregating multi-edges between same (u, v) pair
        edge_weights: dict[tuple[int, int], float] = {}
        for u, v, data in G.edges(data=True):
            key = (node_to_idx[u], node_to_idx[v])
            edge_weights[key] = edge_weights.get(key, 0.0) + data.get(weight, 1.0)

        if not edge_weights:
            return _original_nx_pagerank(
                G, alpha=alpha, personalization=personalization,
                max_iter=max_iter, tol=tol, nstart=nstart,
                weight=weight, dangling=dangling,
            )

        sources, targets, weights_list = [], [], []
        for (s, t), w in edge_weights.items():
            sources.append(s)
            targets.append(t)
            weights_list.append(w)

        df = cudf.DataFrame({"src": sources, "dst": targets, "weight": weights_list})
        cg = cugraph.Graph(directed=True)
        cg.from_cudf_edgelist(df, source="src", destination="dst",
                              edge_attr="weight")

        # Build personalization DataFrame if provided
        pers_df = None
        if personalization:
            pv, pp = [], []
            for node, val in personalization.items():
                if node in node_to_idx:
                    pv.append(node_to_idx[node])
                    pp.append(val)
            if pv:
                pers_df = cudf.DataFrame({"vertex": pv, "values": pp})

        result = cugraph.pagerank(
            cg, alpha=alpha, personalization=pers_df,
            max_iter=max_iter, tol=tol,
        )

        vertices = result["vertex"].to_pandas()
        pageranks = result["pagerank"].to_pandas()
        return {node_list[int(idx)]: float(pr) for idx, pr in zip(vertices, pageranks)}

    except ImportError:
        pass  # cugraph not installed — fall through
    except Exception:
        pass  # conversion or GPU error — fall through

    # ── Fallback: original networkx (CPU, bit-identical) ──
    return _original_nx_pagerank(
        G, alpha=alpha, personalization=personalization,
        max_iter=max_iter, tol=tol, nstart=nstart,
        weight=weight, dangling=dangling,
    )


# Apply monkey-patch at module load time
_nx.pagerank = _gpu_accelerated_pagerank


@dataclass
class RepoMapResult:
    root: str
    model_name: str
    edit_format: str
    map_tokens: int
    max_input_tokens: int
    effective_budget: int
    used_git: bool
    rel_files: list[str]
    mentioned_idents: list[str]
    mentioned_fnames: list[str]
    repo_map: str
    chat_rel_fnames: list[str] = field(default_factory=list)
    recency_boost_fnames: list[str] = field(default_factory=list)
    cochange_boost_fnames: list[str] = field(default_factory=list)
    dict_boost_fnames: list[str] = field(default_factory=list)
    agent_boost_fnames: list[str] = field(default_factory=list)
    agent_downgrade_fnames: list[str] = field(default_factory=list)
    agentic_steps: list[dict] = field(default_factory=list)


def _load_project_dictionary(root: str) -> dict | None:
    """Load .architectureX/project_dictionary.json if it exists.

    Returns the parsed dict or None when the file is absent / invalid.
    """
    dict_path = os.path.join(root, ".architectureX", "project_dictionary.json")
    if not os.path.isfile(dict_path):
        return None
    try:
        with open(dict_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def get_project_dict_boosts(
    root: str,
    user_prompt: str,
    rel_files: list[str],
) -> set[str]:
    """Return rel-paths of files boosted by the project dictionary.

    Loads ``{root}/.architectureX/project_dictionary.json`` whose keys are
    hyphenated feature names (e.g. ``"user-login"``) mapping to lists of
    relative file paths.  User prompts are tokenised and consecutive-word
    n-grams (up to 3) are joined with ``-`` so a prompt containing
    ``"user login"`` matches the key ``"user-login"``.

    Only files present in *rel_files* are returned (ghost entries are
    silently dropped).
    """
    dictionary = _load_project_dictionary(root)
    if not dictionary:
        return set()

    # ── Build candidate keys from the user prompt ─────────────────────────
    words = [w for w in re.split(r"\W+", user_prompt.lower()) if w]
    if not words:
        return set()

    candidates: set[str] = set()
    for n in range(1, 4):  # 1-gram, 2-gram, 3-gram
        for i in range(len(words) - n + 1):
            candidates.add("-".join(words[i : i + n]))

    # ── Match candidates against dictionary keys ─────────────────────────
    rel_files_set = set(rel_files)
    boosted: set[str] = set()
    for key, files in dictionary.items():
        if not isinstance(files, list):
            continue
        if key.lower() in candidates:
            for f in files:
                if isinstance(f, str) and f in rel_files_set:
                    boosted.add(f)

    return boosted


def find_sub_repos(root):
    """Find immediate subdirectories that contain a .git directory."""
    sub_repos = []
    try:
        for entry in os.scandir(root):
            if entry.is_dir() and not entry.name.startswith('.'):
                git_dir = os.path.join(entry.path, '.git')
                if os.path.isdir(git_dir) or os.path.isfile(git_dir):
                    sub_repos.append(entry.name)
    except OSError:
        pass
    return sorted(sub_repos)


def find_repo_files_git(root, io):
    """Discover files using Aider's own GitRepo class (identical to Aider).

    Falls back to git ls-files if GitRepo fails, then to filesystem walk.
    For multi-repo roots (no .git at root, but sub-dirs have .git),
    collects git-tracked files from each sub-repo and prefixes paths.
    Returns (list_of_relative_paths, used_git: bool).
    """
    # Try Aider's GitRepo first (this is what Aider actually uses)
    try:
        repo = GitRepo(io, fnames=[], git_dname=root)
        files = repo.get_tracked_files()
        if files:
            return sorted(set(files)), True
    except (FileNotFoundError, OSError, ValueError, RuntimeError):
        pass

    # Detect multi-repo: root has no .git but sub-dirs do
    sub_repos = find_sub_repos(root)
    if sub_repos:
        print(f"Detected multi-repo project with {len(sub_repos)} sub-repos: {sub_repos}",
              file=sys.stderr)
        all_files = []
        for sub in sub_repos:
            sub_path = os.path.join(root, sub)
            try:
                repo = GitRepo(io, fnames=[], git_dname=sub_path)
                files = repo.get_tracked_files()
                if files:
                    # Prefix each file with the sub-repo directory name
                    prefixed = [os.path.join(sub, f) for f in files]
                    all_files.extend(prefixed)
                    print(f"  {sub}: {len(files)} tracked files",
                          file=sys.stderr)
            except (FileNotFoundError, OSError, ValueError, RuntimeError) as e:
                print(f"  {sub}: skipped ({e})", file=sys.stderr)

        # Also include any non-git files at the root level (e.g. docs/)
        for entry in os.scandir(root):
            if entry.is_file():
                all_files.append(entry.name)
            elif entry.is_dir() and entry.name not in sub_repos and not entry.name.startswith('.'):
                # Walk non-git subdirs (like docs/) with the filesystem fallback
                for dirpath, dirnames, filenames in os.walk(entry.path):
                    dirnames[:] = [
                        d for d in dirnames if not d.startswith('.')]
                    for fname in filenames:
                        full = os.path.join(dirpath, fname)
                        rel = os.path.relpath(full, root)
                        all_files.append(rel)

        if all_files:
            return sorted(set(all_files)), True

    # Fall back: not a git repo — warn user this won't match Aider
    print("WARNING: Not a git repo. Aider uses git-tracked files.", file=sys.stderr)
    print("         Output will NOT be identical to Aider.", file=sys.stderr)
    return _find_repo_files_walk(root), False


def _find_repo_files_walk(root):
    """Fallback filesystem walk (used only when git is not available)."""
    SKIP_DIRS = {
        ".git", "node_modules", ".vscode", "out", "dist", "build",
        ".next", "__pycache__", ".tox", "venv", ".venv", ".aider",
        ".ctxpilot", "coverage", ".nyc_output", ".pytest_cache",
        "target", "bin", "obj",
    }
    BINARY_EXTENSIONS = {
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
        ".woff", ".woff2", ".ttf", ".eot",
        ".zip", ".tar", ".gz", ".bz2", ".7z",
        ".exe", ".dll", ".so", ".dylib",
        ".pdf", ".doc", ".docx",
        ".mp3", ".mp4", ".wav", ".avi",
        ".vsix", ".wasm", ".lock",
    }
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in BINARY_EXTENSIONS:
                continue
            full = os.path.join(dirpath, fname)
            try:
                rel = os.path.relpath(full, root)
                files.append(rel)
            except ValueError:
                continue
    return sorted(files)


def get_git_recency_boosts(
    root: str,
    rel_files: list[str],
    max_commits: int = 50,
    top_n: int = 20,
    half_life_days: float = 30.0,
) -> set[str]:
    """Return up to *top_n* recently-modified files to boost in the repo map.

    Files are scored by time-based exponential decay using the real commit
    timestamp (Unix epoch via ``%ct``):

        score = exp(-elapsed_days / half_life_days)

    A file touched today scores ~1.0; one last modified *half_life_days* ago
    scores exp(-1) ≈ 0.37; one modified 3× *half_life_days* ago scores ~0.05.
    This is independent of commit frequency, so a slow repo with one
    commit/month and a fast repo with 20 commits/day are treated consistently.

    The function is always safe to call — it returns an empty set if git is
    not installed, *root* is not inside a git repository, the repo has no
    commits yet, or any other error occurs.  It will never raise.
    """
    try:
        proc = subprocess.run(
            [
                "git", "log",
                "--diff-filter=MAR",
                "--name-only",
                "--pretty=format:__COMMIT__ %ct",
                f"-n{max_commits}",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            return set()
    except (OSError, subprocess.TimeoutExpired):
        return set()

    now = time.time()
    # Map normalised path → best (highest) decay score seen for that file.
    # "Best" means the most recent commit the file appeared in.
    weights: dict[str, float] = {}
    current_decay: float = 0.0  # set each time we encounter a __COMMIT__ line

    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("__COMMIT__"):
            parts = line.split()
            try:
                commit_ts = int(parts[1])
            except (IndexError, ValueError):
                commit_ts = int(now)
            elapsed_days = max(0.0, (now - commit_ts) / 86400.0)
            current_decay = math.exp(-elapsed_days / max(half_life_days, 1.0))
            continue
        if not line:
            continue
        norm = line.replace("\\", "/")
        if norm not in weights or current_decay > weights[norm]:
            weights[norm] = current_decay

    rel_files_norm = {f.replace("\\", "/"): f for f in rel_files}
    scored = {
        rel_files_norm[norm]: score
        for norm, score in weights.items()
        if norm in rel_files_norm
    }
    if not scored:
        return set()

    top = sorted(scored, key=scored.__getitem__, reverse=True)[:top_n]
    return set(top)


def get_cochange_boosts(
    root: str,
    seed_fnames: set[str],
    rel_files: list[str],
    max_commits: int = 200,
    min_cochange: int = 2,
    top_n: int = 10,
) -> set[str]:
    """Return files that frequently co-change with *seed_fnames* in git history.

    Scans recent commits; when a commit touches at least one seed file, every
    *other* file in that commit accumulates a co-change count.  Files with a
    count >= *min_cochange* are eligible; the top *top_n* by count are returned.

    This surfaces test files, config files, and migration files that have no
    import edges but are always committed together with the relevant code.

    Returns an empty set when *seed_fnames* is empty, git is unavailable, the
    directory is not a git repo, or any other error occurs.  Never raises.
    """
    if not seed_fnames:
        return set()

    try:
        proc = subprocess.run(
            [
                "git", "log",
                "--diff-filter=MAR",
                "--name-only",
                "--pretty=format:__COMMIT__",
                f"-n{max_commits}",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            return set()
    except (OSError, subprocess.TimeoutExpired):
        return set()

    seed_norm: set[str] = {f.replace("\\", "/") for f in seed_fnames}
    rel_files_norm: dict[str, str] = {
        f.replace("\\", "/"): f for f in rel_files}
    cochange_count: Counter = Counter()
    current_files: list[str] = []

    def _flush_commit(files: list[str]) -> None:
        norm_set = {f.replace("\\", "/") for f in files}
        if norm_set & seed_norm:
            for norm in norm_set - seed_norm:
                if norm in rel_files_norm:
                    cochange_count[norm] += 1

    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if line == "__COMMIT__":
            _flush_commit(current_files)
            current_files = []
        elif line:
            current_files.append(line)
    _flush_commit(current_files)  # flush the final commit's files

    boosted = {
        rel_files_norm[norm]: count
        for norm, count in cochange_count.items()
        if count >= min_cochange
    }
    if not boosted:
        return set()

    top = sorted(boosted, key=boosted.__getitem__, reverse=True)[:top_n]
    return set(top)


# ─── Identical replicas of Aider's base_coder.py prompt-conditioning methods ───

def get_ident_mentions(text):
    """Identical to Coder.get_ident_mentions() in aider/coders/base_coder.py.

    Split the string on any character that is not alphanumeric.
    \\W+ matches one or more non-word characters (equivalent to [^a-zA-Z0-9_]+).
    """
    words = set(re.split(r"\W+", text))
    return words


def get_file_mentions(content, rel_files, chat_rel_fnames=None, read_only_rel_fnames=None):
    """Identical to Coder.get_file_mentions() in aider/coders/base_coder.py.

    Since we have no chat files by default, addable_rel_fnames = all rel_files,
    and existing_basenames = basenames of chat + read-only files.
    """
    if chat_rel_fnames is None:
        chat_rel_fnames = set()
    if read_only_rel_fnames is None:
        read_only_rel_fnames = set()

    words = set(word for word in content.split())

    # drop sentence punctuation from the end
    words = set(word.rstrip(",.!;:?") for word in words)

    # strip away all kinds of quotes
    quotes = "\"'`*_"
    words = set(word.strip(quotes) for word in words)

    # addable = all files minus chat files minus read-only files
    addable_rel_fnames = set(rel_files) - \
        chat_rel_fnames - read_only_rel_fnames

    # Get basenames of files already in chat or read-only
    existing_basenames = (
        {os.path.basename(f) for f in chat_rel_fnames}
        | {os.path.basename(f) for f in read_only_rel_fnames}
    )

    mentioned_rel_fnames = set()
    fname_to_rel_fnames = {}
    for rel_fname in addable_rel_fnames:
        normalized_rel_fname = rel_fname.replace("\\", "/")
        normalized_words = set(word.replace("\\", "/") for word in words)
        if normalized_rel_fname in normalized_words:
            mentioned_rel_fnames.add(rel_fname)

        fname = os.path.basename(rel_fname)

        # Don't add basenames that could be plain words like "run" or "make"
        if "/" in fname or "\\" in fname or "." in fname or "_" in fname or "-" in fname:
            if fname not in fname_to_rel_fnames:
                fname_to_rel_fnames[fname] = []
            fname_to_rel_fnames[fname].append(rel_fname)

    for fname, rel_fnames in fname_to_rel_fnames.items():
        # If the basename is already in chat, don't add based on a basename mention
        if fname in existing_basenames:
            continue
        # If the basename mention is unique among addable files and present in the text
        if len(rel_fnames) == 1 and fname in words:
            mentioned_rel_fnames.add(rel_fnames[0])

    return mentioned_rel_fnames


def get_ident_filename_matches(idents, rel_files):
    """Identical to Coder.get_ident_filename_matches() in aider/coders/base_coder.py."""
    all_fnames = defaultdict(set)
    for fname in rel_files:
        # Skip empty paths or just '.'
        if not fname or fname == ".":
            continue

        try:
            # Handle dotfiles properly
            path = Path(fname)
            base = path.stem.lower()  # Use stem instead of with_suffix("").name
            if len(base) >= 5:
                all_fnames[base].add(fname)
        except ValueError:
            # Skip paths that can't be processed
            continue

    matches = set()
    for ident in idents:
        if len(ident) < 5:
            continue
        matches.update(all_fnames[ident.lower()])

    return matches


class SimpleModel:
    """Minimal model interface for token counting (Aider requires this).

    WARNING: This produces different token counts than real models.
    Use only as a last resort when the real model is unavailable.
    """

    def __init__(self):
        self.name = "simple-estimator"

    def token_count(self, text):
        # ~4 chars per token (rough estimate matching GPT-4 tokenizer)
        return len(text) // 4


# ─── Prompt prefixes per edit format (from aider/coders/*_prompts.py) ───
# These are what Aider prepends to the repo map before injecting into the LLM.
REPO_CONTENT_PREFIXES = {
    "diff": (
        "Here are summaries of some files present in my git repository.\n"
        "Do not propose changes to these files, treat them as *read-only*.\n"
        "If you need to edit any of these files, ask me to *add them to the chat* first.\n"
    ),
    "whole": (
        "Here are summaries of some files present in my git repository.\n"
        "Do not propose changes to these files, treat them as *read-only*.\n"
        "If you need to edit any of these files, ask me to *add them to the chat* first.\n"
    ),
    "architect": (
        "I am working with you on code in a git repository.\n"
        "Here are summaries of some files present in my git repo.\n"
        "If you need to see the full contents of any files to answer my questions, "
        "ask me to *add them to the chat*.\n"
    ),
    "ask": (
        "I am working with you on code in a git repository.\n"
        "Here are summaries of some files present in my git repo.\n"
        "If you need to see the full contents of any files to answer my questions, "
        "ask me to *add them to the chat*.\n"
    ),
}


def generate_repo_map(
    root,
    user_prompt=None,
    model_name=DEFAULT_MODEL_NAME,
    chat_rel_fnames=None,
    force_budget_multiplier=None,
    agent_boost_fnames=None,
    agent_downgrade_fnames=None,
    agent_boost_factor=None,
    agent_downgrade_factor=None,
):
    """Generate a repo map and return structured metadata for application use.

    *chat_rel_fnames* — relative paths (within *root*) of files to treat as
    "in chat".  These receive a 50× PageRank seed weight so the map expands
    outward from them.  When provided, Aider's token budget also drops from
    map_tokens × map_mul_no_files back to map_tokens, because the chat files
    themselves anchor the context.  Pass the output of a previous LLM file
    selection here to run a focused second-pass map.

    *force_budget_multiplier* — if set, overrides the automatic budget
    multiplier (which normally drops to 1 when chat files are provided).
    Use this to keep the full budget in a boosted second-pass.

    *agent_boost_fnames* — relative paths of files selected by the agentic
    mapper.  These receive a 5× PageRank personalization boost (similar to
    dictionary boost but stronger) and are added to mentioned_fnames so they
    rank highly in the output.  Unlike chat_rel_fnames, these files are NOT
    excluded from the map output.

    *agent_downgrade_fnames* — relative paths of files the agentic mapper
    wants to suppress.  These receive a 20× PageRank personalization
    reduction (multiplier 0.05) so they lose prominence in the output.
    """
    root = os.path.abspath(root)

    # ── Cache lookup ──────────────────────────────────────────────────────────
    repo_fp = _compute_repo_fingerprint(root)
    _cache_key: str | None = None
    if repo_fp is not None:
        _cache_key = _make_cache_key(root, user_prompt, model_name,
                                     chat_rel_fnames, repo_fp,
                                     force_budget_multiplier,
                                     agent_boost_fnames,
                                     agent_downgrade_fnames)
        cached = _result_cache.get(_cache_key)
        if cached is not None:
            return cached
    # ── Intermediate cache: reuse Model, RepoMap instance, file list ─────
    # The agentic pipeline calls generate_repo_map twice with different
    # boost/budget parameters but the same (root, model).  The expensive
    # parts (Model init, git file discovery, tree-sitter tag parsing) are
    # identical.  Reuse them so the second pass only re-runs PageRank.
    _inter_key = (root, model_name)
    _inter = _intermediate_cache.get(_inter_key) if repo_fp is not None else None
    if _inter is not None and _inter["repo_fingerprint"] == repo_fp:
        edit_format = _inter["edit_format"]
        map_tokens = _inter["map_tokens"]
        max_inp_tokens = _inter["max_inp_tokens"]
        rel_files = _inter["rel_files"]
        used_git = _inter["used_git"]
        repo_map = _inter["repo_map_instance"]
        map_mul_no_files = _inter["map_mul_no_files"]
    else:
        main_model = Model(model_name)
        edit_format = main_model.edit_format
        map_tokens = main_model.get_repo_map_tokens()
        max_inp_tokens = main_model.info.get("max_input_tokens") or 0
        repo_content_prefix = REPO_CONTENT_PREFIXES.get(
            edit_format, REPO_CONTENT_PREFIXES["diff"]
        )

        io = InputOutput(
            yes=True,
            pretty=False,
        )

        rel_files, used_git = find_repo_files_git(root, io)

        map_mul_no_files = 2
        repo_map = RepoMap(
            map_tokens,
            root,
            main_model,
            io,
            repo_content_prefix,
            False,
            max_inp_tokens,
            map_mul_no_files=map_mul_no_files,
            refresh="auto",
        )

        if repo_fp is not None:
            _intermediate_cache[_inter_key] = {
                "repo_fingerprint": repo_fp,
                "edit_format": edit_format,
                "map_tokens": map_tokens,
                "max_inp_tokens": max_inp_tokens,
                "rel_files": rel_files,
                "used_git": used_git,
                "repo_map_instance": repo_map,
                "map_mul_no_files": map_mul_no_files,
            }

    # Convert caller-supplied relative chat paths → absolute, validated against
    # the discovered file list so we never pass phantom paths to RepoMap.
    rel_files_set = set(rel_files)
    valid_chat_rel = [
        f for f in (chat_rel_fnames or [])
        if f in rel_files_set
    ]
    chat_fnames = [os.path.join(root, rel_path) for rel_path in valid_chat_rel]

    # Validate agent-boost paths the same way.
    valid_agent_boost = [
        f for f in (agent_boost_fnames or [])
        if f in rel_files_set
    ]

    # Validate agent-downgrade paths the same way.
    valid_agent_downgrade = [
        f for f in (agent_downgrade_fnames or [])
        if f in rel_files_set
    ]

    # When chat files are provided Aider's internal budget is map_tokens (not
    # multiplied), because the chat files anchor the map.  Reflect that here so
    # the reported effective_budget is accurate.
    chat_files_provided = bool(chat_fnames)
    if force_budget_multiplier is not None:
        budget_multiplier = force_budget_multiplier
    else:
        budget_multiplier = 1 if chat_files_provided else map_mul_no_files

    # other_fnames = all repo files (Aider keeps chat + other separate)
    other_fnames = [os.path.join(root, rel_path) for rel_path in rel_files]
    padding = 4096
    effective_budget = 0
    if map_tokens and max_inp_tokens:
        effective_budget = min(
            int(map_tokens * budget_multiplier),
            max_inp_tokens - padding,
        )

    mentioned_fnames = set()
    mentioned_idents = set()
    if user_prompt:
        cur_msg_text = user_prompt + "\n"
        mentioned_fnames = get_file_mentions(cur_msg_text, rel_files)
        mentioned_idents = get_ident_mentions(cur_msg_text)
        mentioned_fnames.update(
            get_ident_filename_matches(mentioned_idents, rel_files)
        )

    # ── Project dictionary boost ──────────────────────────────────────────────
    # Look up .architectureX/project_dictionary.json for feature→file
    # mappings.  Matched files are added to mentioned_fnames (so they seed
    # cochange analysis downstream) and receive a 3× personalization boost
    # inside PageRank via the _dict_boost_targets module-level state.
    dict_boosts: set[str] = set()
    if user_prompt:
        dict_boosts = get_project_dict_boosts(root, user_prompt, rel_files)
        if dict_boosts:
            mentioned_fnames |= dict_boosts
            print(
                f"  Dictionary boost: {len(dict_boosts)} files from "
                f".architectureX/project_dictionary.json",
                file=sys.stderr,
            )

    # ── Agent-selected boost ──────────────────────────────────────────────────
    # Files selected by the agentic mapper receive a 5× personalization boost
    # via _agent_boost_targets in the monkey-patched PageRank.  They are NOT
    # added to mentioned_fnames to avoid double-boosting (mentioned status
    # already gives ~10× base weight).
    agent_boosts: set[str] = set(valid_agent_boost)
    if agent_boosts:
        print(
            f"  Agent boost: {len(agent_boosts)} files (5× personalization)",
            file=sys.stderr,
        )

    # ── Agent-selected downgrade ──────────────────────────────────────────────
    # Files the agentic mapper wants to suppress receive a 20× reduction
    # (multiplier 0.05) in PageRank personalization so they lose prominence.
    agent_downgrades: set[str] = set(valid_agent_downgrade)
    if agent_downgrades:
        print(
            f"  Agent downgrade: {len(agent_downgrades)} files (20× reduction)",
            file=sys.stderr,
        )

    # ── Git-based context enrichment ──────────────────────────────────────────
    # Only active when file tracking is via git.  Both helpers always return
    # empty sets on any failure, so these lines are unconditionally safe.
    recency_boosts: set[str] = set()
    cochange_boosts: set[str] = set()
    if used_git:
        recency_boosts = get_git_recency_boosts(root, rel_files)
        # Only boost recently-edited files that are already mentioned by the
        # user prompt.  Files with no prompt relevance are not boosted.
        recency_boosts = recency_boosts & mentioned_fnames
        # Seed co-change lookup with prompt-mentioned files AND recently active
        # files so the coupling graph extends from both entry points.
        cochange_boosts = get_cochange_boosts(
            root,
            mentioned_fnames | recency_boosts,
            rel_files,
        )
        mentioned_fnames = mentioned_fnames | recency_boosts | cochange_boosts

    all_abs_files = set(other_fnames)

    # ── Strategy 2: pre-warm tag cache in parallel ────────────────────────────
    _prewarm_tags_parallel(repo_map, sorted(set(chat_fnames) | all_abs_files))

    # ── Activate dictionary + agent boosts/downgrades in PageRank ────────
    global _dict_boost_targets, _agent_boost_targets, _agent_downgrade_targets
    try:
        if dict_boosts:
            _dict_boost_targets = {f: _DICT_BOOST_MULTIPLIER for f in dict_boosts}
        else:
            _dict_boost_targets = {}

        _effective_boost = agent_boost_factor if agent_boost_factor is not None else _AGENT_BOOST_MULTIPLIER
        _effective_downgrade = agent_downgrade_factor if agent_downgrade_factor is not None else _AGENT_DOWNGRADE_MULTIPLIER
        if agent_boosts:
            _agent_boost_targets = {f: _effective_boost for f in agent_boosts}
        else:
            _agent_boost_targets = {}

        if agent_downgrades:
            _agent_downgrade_targets = {f: _effective_downgrade for f in agent_downgrades}
        else:
            _agent_downgrade_targets = {}

        result = repo_map.get_repo_map(
            chat_fnames,
            other_fnames,
            mentioned_fnames=mentioned_fnames,
            mentioned_idents=mentioned_idents,
        )

        if not result:
            result = repo_map.get_repo_map(
                set(),
                all_abs_files,
                mentioned_fnames=mentioned_fnames,
                mentioned_idents=mentioned_idents,
            )

        if not result:
            result = repo_map.get_repo_map(
                set(),
                all_abs_files,
            )
    finally:
        _dict_boost_targets = {}
        _agent_boost_targets = {}
        _agent_downgrade_targets = {}

    final = RepoMapResult(
        root=root,
        model_name=model_name,
        edit_format=edit_format,
        map_tokens=map_tokens,
        max_input_tokens=max_inp_tokens,
        effective_budget=effective_budget,
        used_git=used_git,
        rel_files=rel_files,
        mentioned_idents=sorted(mentioned_idents),
        mentioned_fnames=sorted(mentioned_fnames),
        repo_map=result or "",
        chat_rel_fnames=valid_chat_rel,
        recency_boost_fnames=sorted(recency_boosts),
        cochange_boost_fnames=sorted(cochange_boosts),
        dict_boost_fnames=sorted(dict_boosts),
        agent_boost_fnames=sorted(agent_boosts),
        agent_downgrade_fnames=sorted(agent_downgrades),
    )

    # ── Export visualisation data for plotter (non-fatal side-effect) ──────
    _export_plotter_data(final, repo_map, user_prompt)

    # ── Cache store ───────────────────────────────────────────────────────────
    if _cache_key is not None:
        _result_cache[_cache_key] = final

    return final


def extract_listed_files(repo_map_text, rel_files):
    """Return the repo files that are explicitly represented in the rendered repo map."""
    normalized_files = {rel_path.replace(
        "\\", "/"): rel_path for rel_path in rel_files}
    listed_files = []
    seen = set()

    for raw_line in repo_map_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        candidate = stripped.split(":", 1)[0].rstrip("/")
        normalized_candidate = candidate.replace("\\", "/")
        matched_file = normalized_files.get(normalized_candidate)
        if matched_file and matched_file not in seen:
            listed_files.append(matched_file)
            seen.add(matched_file)

    return listed_files


# ─── Plotter visualisation data export ────────────────────────────────────────

def _parse_repo_map_tree(repo_map_text: str) -> list[dict]:
    """Parse the rendered repo-map text into a list of file nodes with symbols.

    Each entry: {"file": "path/to/file.go", "symbols": [{"name": "Foo", "kind": "function"}, ...]}
    """
    nodes: list[dict] = []
    current_file: str | None = None
    current_symbols: list[dict] = []

    # The tree-sitter tag output looks like:
    #   path/to/file.go
    #   ├── func Foo
    #   │   ├── ...
    # Or the simpler Aider format:
    #   path/to/file.go:
    #     ClassName
    #       method_name
    for raw_line in repo_map_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        # Remove tree drawing characters: │ ├ └ ─ ╰ ╭
        clean = re.sub(r'[│├└─╰╭┬┤┴┼╮╯┌┐┘┗┛┏┓]', '', stripped).strip()
        if not clean:
            continue

        # Detect file lines: end with ':' or have a file extension and no leading whitespace
        # File lines in Aider output have no leading tree chars in the original
        is_file_line = False
        if ':' in stripped and not stripped.startswith(('│', '├', '└', '╰', ' ')):
            candidate = stripped.split(':', 1)[0].strip()
            if '.' in candidate or '/' in candidate:
                is_file_line = True
        elif not raw_line.startswith((' ', '\t', '│', '├', '└', '╰')) and ('.' in stripped or '/' in stripped):
            candidate = stripped.rstrip(':')
            if any(candidate.endswith(ext) for ext in ('.go', '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.rs', '.c', '.cpp', '.h', '.rb', '.php', '.cs', '.swift', '.kt', '.scala', '.vue', '.svelte', '.sql', '.proto', '.graphql', '.yaml', '.yml', '.json', '.toml', '.mod', '.sum')):
                is_file_line = True

        if is_file_line:
            if current_file is not None:
                nodes.append({"file": current_file, "symbols": current_symbols})
            current_file = stripped.split(':', 1)[0].strip()
            current_symbols = []
        elif current_file is not None:
            # This is a symbol line
            # Clean up tree-drawing chars and extract symbol info
            sym_text = clean
            kind = "symbol"
            # Detect kind from common patterns
            if re.match(r'(func|def|function)\b', sym_text, re.IGNORECASE):
                kind = "function"
                sym_text = re.sub(r'^(func|def|function)\s+', '', sym_text, flags=re.IGNORECASE)
            elif re.match(r'(class|struct|type|interface|enum)\b', sym_text, re.IGNORECASE):
                kind = "class"
                sym_text = re.sub(r'^(class|struct|type|interface|enum)\s+', '', sym_text, flags=re.IGNORECASE)
            elif re.match(r'(method|fn)\b', sym_text, re.IGNORECASE):
                kind = "method"
                sym_text = re.sub(r'^(method|fn)\s+', '', sym_text, flags=re.IGNORECASE)

            sym_name = sym_text.strip().split('(')[0].split('{')[0].strip()
            if sym_name:
                current_symbols.append({"name": sym_name, "kind": kind})

    if current_file is not None:
        nodes.append({"file": current_file, "symbols": current_symbols})

    return nodes


def _build_edges_from_tags(repo_map_instance, listed_files: list[str], root: str) -> list[dict]:
    """Build dependency edges between listed files using tag cross-references.

    For each file, get its tags:
    - 'def' tags: symbols defined in the file
    - 'ref' tags: symbols referenced from the file

    An edge source→target is created when file source has a 'ref' tag for
    a symbol that is 'def'-ined in file target.
    """
    listed_set = set(listed_files)

    # Collect definitions: symbol_name → set of files that define it
    defs: dict[str, set[str]] = defaultdict(set)
    # Collect references: (file, symbol_name) pairs
    refs: list[tuple[str, str]] = []

    for rel_fname in listed_files:
        abs_fname = os.path.join(root, rel_fname)
        if not os.path.isfile(abs_fname):
            continue
        try:
            tags = list(repo_map_instance.get_tags(abs_fname, rel_fname))
        except Exception:
            continue

        for tag in tags:
            if hasattr(tag, 'kind') and hasattr(tag, 'name') and hasattr(tag, 'rel_fname'):
                if tag.kind == 'def':
                    defs[tag.name].add(tag.rel_fname)
                elif tag.kind == 'ref':
                    refs.append((tag.rel_fname, tag.name))

    # Build edges from references to definitions
    edge_map: dict[tuple[str, str], list[str]] = {}  # (src, tgt) → [symbols]
    for ref_file, sym_name in refs:
        if sym_name not in defs:
            continue
        for def_file in defs[sym_name]:
            if def_file != ref_file and def_file in listed_set and ref_file in listed_set:
                key = (ref_file, def_file)
                if key not in edge_map:
                    edge_map[key] = []
                if sym_name not in edge_map[key]:
                    edge_map[key].append(sym_name)

    edges = []
    for (src, tgt), symbols in edge_map.items():
        edges.append({
            "source": src,
            "target": tgt,
            "symbols": symbols[:10],  # cap for readability
            "weight": len(symbols),
        })

    return edges


def _export_plotter_data(
    result: RepoMapResult,
    repo_map_instance,
    user_prompt: str | None,
) -> None:
    """Export visualization data to plotter/public/graph_data.json.

    Called as a side-effect at the end of generate_repo_map().
    Failures are silently ignored — this must never affect the main pipeline.
    """
    try:
        plotter_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plotter", "public")
        os.makedirs(plotter_dir, exist_ok=True)

        # Parse the rendered repo map into file→symbols structure
        nodes_raw = _parse_repo_map_tree(result.repo_map) if result.repo_map else []

        listed_files = [n["file"] for n in nodes_raw]

        # Build dependency edges
        edges = _build_edges_from_tags(repo_map_instance, listed_files, result.root)

        # Categorize files by boost type
        boost_categories: dict[str, list[str]] = {
            "mentioned": list(result.mentioned_fnames),
            "recency": list(result.recency_boost_fnames),
            "cochange": list(result.cochange_boost_fnames),
            "dictionary": list(result.dict_boost_fnames),
            "chat": list(result.chat_rel_fnames),
            "agent": list(result.agent_boost_fnames),
        }

        # Build the export payload
        payload = {
            "metadata": {
                "root": result.root,
                "model": result.model_name,
                "prompt": user_prompt or "",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "totalRepoFiles": len(result.rel_files),
                "listedFiles": len(listed_files),
                "editFormat": result.edit_format,
                "effectiveBudget": result.effective_budget,
            },
            "nodes": nodes_raw,
            "edges": edges,
            "boosts": boost_categories,
        }

        out_path = os.path.join(plotter_dir, "graph_data.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        print(f"  [plotter] Exported graph data → {out_path} "
              f"({len(nodes_raw)} nodes, {len(edges)} edges)", file=sys.stderr)
    except Exception as exc:
        print(f"  [plotter] Export failed (non-fatal): {exc}", file=sys.stderr)


def main():
    # Usage: python repo_map_generator.py [--prompt "text"] [--model MODEL] [root_dir]
    args = sys.argv[1:]
    user_prompt = None
    model_name = DEFAULT_MODEL_NAME

    if "--prompt" in args:
        idx = args.index("--prompt")
        if idx + 1 < len(args):
            user_prompt = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        else:
            print("Error: --prompt requires a value", file=sys.stderr)
            sys.exit(1)

    if "--model" in args:
        idx = args.index("--model")
        if idx + 1 < len(args):
            model_name = args[idx + 1]
            args = args[:idx] + args[idx + 2:]
        else:
            print("Error: --model requires a value", file=sys.stderr)
            sys.exit(1)

    root = os.path.abspath(args[0] if args else ".")
    result = generate_repo_map(
        root, user_prompt=user_prompt, model_name=model_name)

    print(f"{'=' * 70}")
    print("Aider Repo Map Generator (using aider-chat directly)")
    print(f"{'=' * 70}")
    print(f"Root: {result.root}")
    print(f"Model: {result.model_name}")
    if user_prompt:
        print(f"User prompt: {user_prompt!r}")
    else:
        print("User prompt: (none — unhinted map)")
    print(f"Edit format: {result.edit_format}")
    print(f"map_tokens (from model): {result.map_tokens}")
    print(f"max_input_tokens: {result.max_input_tokens}")
    print(
        f"Found {len(result.rel_files)} source files (via {'git' if result.used_git else 'filesystem walk'})"
    )
    for rel_path in result.rel_files[:15]:
        print(f"  {rel_path}")
    if len(result.rel_files) > 15:
        print(f"  ... and {len(result.rel_files) - 15} more")

    print("\nChat files: 0")
    print(f"Other files: {len(result.rel_files)}")
    print(f"Max tokens (expanded for no-chat): {result.effective_budget}")

    if user_prompt:
        print("\n--- Prompt conditioning ---")
        print(
            f"  mentioned_idents ({len(result.mentioned_idents)}): {result.mentioned_idents[:20]}{'...' if len(result.mentioned_idents) > 20 else ''}"
        )
        print(
            f"  mentioned_fnames ({len(result.mentioned_fnames)}): {result.mentioned_fnames}"
        )

    if result.used_git and (result.recency_boost_fnames or result.cochange_boost_fnames):
        print("\n--- Git-based context enrichment ---")
        if result.recency_boost_fnames:
            print(
                f"  recency_boosts  ({len(result.recency_boost_fnames)}): "
                f"{result.recency_boost_fnames[:10]}"
                f"{'...' if len(result.recency_boost_fnames) > 10 else ''}"
            )
        if result.cochange_boost_fnames:
            print(
                f"  cochange_boosts ({len(result.cochange_boost_fnames)}): "
                f"{result.cochange_boost_fnames[:10]}"
                f"{'...' if len(result.cochange_boost_fnames) > 10 else ''}"
            )

    print("\nGenerating repo map...")
    print(f"\n{'=' * 70}")
    if result.repo_map:
        num_tokens = Model(result.model_name).token_count(result.repo_map)
        print(
            f"REPO MAP OUTPUT ({len(result.repo_map)} chars, ~{num_tokens} real tokens)")
        print(f"{'=' * 70}")
        try:
            print(result.repo_map)
        except UnicodeEncodeError:
            import io as io_mod

            out = io_mod.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace"
            )
            out.write(result.repo_map)
            out.flush()
    else:
        print("REPO MAP OUTPUT: (empty/null)")
        print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
