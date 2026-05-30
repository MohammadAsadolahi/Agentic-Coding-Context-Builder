#!/usr/bin/env python3
"""
audit_runner_40.py — 40 diverse agentic loops for production-level stress testing.

Covers: typos, ambiguous, multi-feature, infra, security, perf,
debugging, investigation, vague vibe-coding, specific file references, etc.

Usage:
    python audit_runner_40.py /path/to/target/repo
    AUDIT_REPO=/path/to/repo python audit_runner_40.py
"""
import json
import os
import subprocess
import sys
import time
import re
from pathlib import Path

REPO_ROOT = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "AUDIT_REPO", str(Path.cwd())
)

PROMPTS = [
    # ── A. Typos / Sloppy vibe-coding (1-5) ──
    "fix teh mesaging bug in chats",
    "add notificaiton for new mesages",
    "the webscoket is not working plz fix",
    "i need to add a feild to the user tabel",
    "make the login thing work better",

    # ── B. Ultra-short / Ambiguous (6-10) ──
    "channels",
    "help",
    "refactor everything",
    "make it faster",
    "tests",

    # ── C. Multi-feature / Cross-cutting (11-15) ──
    "add message read receipts and typing indicators to group channels via socket.io",
    "implement role-based access control for teams with admin and member permissions across all endpoints",
    "add audit logging for all user actions including auth, chat, and project management",
    "integrate push notifications with the existing notification system and add email digest support",
    "add full-text search across messages, issues, and user profiles with pagination",

    # ── D. Security-focused (16-20) ──
    "fix the SQL injection vulnerability in the search endpoint",
    "add CSRF protection to all mutation endpoints",
    "implement JWT token refresh and revocation",
    "add input sanitization for all user-provided HTML content in messages",
    "review and fix authentication bypass in the WebSocket connection handler",

    # ── E. Performance / Scaling (21-25) ──
    "add database connection pooling and query optimization for the message repository",
    "implement Redis caching for frequently accessed channel member lists",
    "optimize the N+1 query problem in the issue listing endpoint",
    "add request batching for bulk message sending in channels",
    "profile and fix the slow sprint burndown calculation",

    # ── F. Infrastructure / DevOps (26-30) ──
    "add health check endpoints for all dependent services",
    "implement graceful shutdown with connection draining",
    "add database migration for adding indexes to the messages table",
    "configure CORS headers for the new frontend domain",
    "add Prometheus metrics for API latency and error rates",

    # ── G. Specific file/symbol references (31-35) ──
    "fix the SendMessage function in chat_service.go",
    "add a new method GetChannelMembers to the channel_repository.go",
    "the HandleWebSocketConnection in socket_handler.go crashes on nil pointer",
    "refactor the ActionRegistry in agentical/action.go to support async execution",
    "add validation to CreateProjectRequest in dto/project.go",

    # ── H. Investigation / Understanding (36-40) ──
    "how does the event bus work and what events are published",
    "trace the full request flow from login to getting a JWT token",
    "explain the relationship between channels, messages, and reactions",
    "what happens when a user sends a message in a group channel end to end",
    "map out all the database models and their foreign key relationships",
]

def run_one(prompt: str, idx: int) -> dict:
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  RUN {idx+1}/40: {prompt[:60]}...", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "app.py", "--root", REPO_ROOT, "--prompt", prompt],
            capture_output=True, text=True, timeout=180,
        )
        elapsed = time.time() - t0
        combined = proc.stdout + "\n" + proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        return {"idx": idx+1, "prompt": prompt, "errors": ["TIMEOUT"],
                "boosts": [], "downgrades": [], "boost_count": 0,
                "downgrade_count": 0, "tool_calls": [], "tool_call_count": 0,
                "pass1_time": None, "pass2_time": None, "total_time": 180,
                "context_notes_chars": 0, "exit_code": -1, "fast_path": False}

    boosts = re.findall(r'↑ (.+)$', combined, re.MULTILINE)
    downgrades = re.findall(r'↓ (.+)$', combined, re.MULTILINE)
    tool_calls = re.findall(r'Tool call: (.+)$', combined, re.MULTILINE)
    pass1_m = re.search(r'Pass 1 repo map generated in ([\d.]+)s', combined)
    pass2_m = re.search(r'Pass 2 repo map generated in ([\d.]+)s', combined)
    pass1_t = float(pass1_m.group(1)) if pass1_m else None
    pass2_t = float(pass2_m.group(1)) if pass2_m else None
    notes_m = re.search(r'Context notes: (\d+) chars', combined)
    notes_len = int(notes_m.group(1)) if notes_m else 0
    fast_path = "fast path" in combined
    agent_failed = bool(re.search(r'Agent failed', combined))
    no_files = "No files selected" in combined

    return {
        "idx": idx + 1,
        "prompt": prompt,
        "boosts": boosts,
        "boost_count": len(boosts),
        "downgrades": downgrades,
        "downgrade_count": len(downgrades),
        "tool_calls": tool_calls,
        "tool_call_count": len(tool_calls),
        "pass1_time": pass1_t,
        "pass2_time": pass2_t,
        "total_time": round(elapsed, 2),
        "context_notes_chars": notes_len,
        "fast_path": fast_path,
        "agent_failed": agent_failed,
        "no_files": no_files,
        "exit_code": exit_code,
    }

def main():
    results = []
    for i, prompt in enumerate(PROMPTS):
        r = run_one(prompt, i)
        results.append(r)
        fp = "FP" if r.get("fast_path") else "SP"
        af = " AGENT_FAIL" if r.get("agent_failed") else ""
        nf = " NO_FILES" if r.get("no_files") else ""
        print(f"  → [{fp}] B:{r['boost_count']:2d} D:{r['downgrade_count']:2d} "
              f"T:{r['total_time']:5.1f}s{af}{nf}", file=sys.stderr)
        time.sleep(2)

    out_path = Path(__file__).with_name("audit_results_40.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {out_path}", file=sys.stderr)
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
