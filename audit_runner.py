#!/usr/bin/env python3
"""
audit_runner.py — Run 20 agentic loops with diverse prompts and collect results.

Usage:
    python audit_runner.py /path/to/target/repo
    AUDIT_REPO=/path/to/repo python audit_runner.py
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

# 20 prompts covering diverse vibe-coding scenarios
PROMPTS = [
    # ── Category 1: Narrow/Focused tasks (1-5) ──
    "fix the bug where messages are not delivered in group channels",
    "add pagination to the chat message history endpoint",
    "add a new reaction emoji type to the reactions system",
    "fix WebRTC connection dropping when switching between channels",
    "update the user profile to include an avatar URL field",
    
    # ── Category 2: Broad/Cross-cutting tasks (6-10) ──
    "add error handling and logging across all service layer methods",
    "implement rate limiting for all API endpoints",
    "add OpenTelemetry tracing spans to every repository method",
    "refactor all handlers to use consistent error response format",
    "add soft delete support to all database models",
    
    # ── Category 3: Feature additions (11-15) ──
    "implement typing indicators for chat channels using socket.io",
    "add file upload support for chat message attachments",
    "create a new sprint burndown chart API endpoint",
    "implement notification preferences so users can mute channels",
    "add video recording functionality to the WebRTC meetings",
    
    # ── Category 4: Stress/Edge cases (16-20) ──
    "fix it",                                              # ultra-vague
    "the auth is broken after the last deploy, help me debug",  # debugging
    "how does the notification system work end to end",     # investigation
    "migrate the database schema to add team roles table",  # infra/migration
    "add unit tests for the chat service send message flow", # testing-specific
]

def run_one(prompt: str, idx: int) -> dict:
    """Run a single agentic loop and parse results from stderr."""
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  RUN {idx+1}/20: {prompt[:60]}...", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "app.py", "--root", REPO_ROOT, "--prompt", prompt],
        capture_output=True, text=True, timeout=180,
    )
    elapsed = time.time() - t0
    
    combined = proc.stdout + "\n" + proc.stderr
    
    # Parse boosted files
    boosts = re.findall(r'↑ (.+)$', combined, re.MULTILINE)
    # Parse downgraded files
    downgrades = re.findall(r'↓ (.+)$', combined, re.MULTILINE)
    # Parse tool calls
    tool_calls = re.findall(r'Tool call: (.+)$', combined, re.MULTILINE)
    # Parse pass times
    pass1_m = re.search(r'Pass 1 repo map generated in ([\d.]+)s', combined)
    pass2_m = re.search(r'Pass 2 repo map generated in ([\d.]+)s', combined)
    pass1_t = float(pass1_m.group(1)) if pass1_m else None
    pass2_t = float(pass2_m.group(1)) if pass2_m else None
    # Parse context notes
    notes_m = re.search(r'Context notes: (\d+) chars', combined)
    notes_len = int(notes_m.group(1)) if notes_m else 0
    # Check for errors
    errors = re.findall(r'(?:Agent failed|FAILED|Fallback also failed|error).*$', combined, re.MULTILINE | re.IGNORECASE)
    # Filter out noise (lines that just contain "error" as part of code output)
    real_errors = [e for e in errors if any(kw in e.lower() for kw in ['agent failed', 'failed', 'fallback', 'connection error'])]

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
        "errors": real_errors,
        "exit_code": proc.returncode,
    }

def main():
    results = []
    for i, prompt in enumerate(PROMPTS):
        try:
            r = run_one(prompt, i)
        except subprocess.TimeoutExpired:
            r = {"idx": i+1, "prompt": prompt, "errors": ["TIMEOUT"], "boosts": [], "downgrades": [], 
                 "boost_count": 0, "downgrade_count": 0, "tool_calls": [], "tool_call_count": 0,
                 "pass1_time": None, "pass2_time": None, "total_time": 180, "context_notes_chars": 0, "exit_code": -1}
        results.append(r)
        # Brief summary
        print(f"  → Boosts: {r['boost_count']}, Downgrades: {r['downgrade_count']}, "
              f"Tool calls: {r['tool_call_count']}, Time: {r['total_time']}s, "
              f"Errors: {len(r['errors'])}", file=sys.stderr)
        # Small delay to avoid rate limiting
        time.sleep(2)
    
    # Write results
    out_path = Path(__file__).with_name("audit_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {out_path}", file=sys.stderr)
    
    # Print summary table
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
