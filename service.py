#!/usr/bin/env python3
"""
service.py — HTTP API for the agentic repo map generator.

Exposes a single POST endpoint that accepts a repo root path and user prompt,
runs the agentic pipeline, and returns the RepoMapResult as JSON.

Usage:
    uvicorn service:app --host 0.0.0.0 --port 8000
"""

from dataclasses import asdict
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import glob
import os
import time
import json

from agentic_mapper import run_agentic_map
from repo_map_generator import generate_repo_map

app = FastAPI(title="Agentic Repo Map Service")


class MapRequest(BaseModel):
    root: str = Field(description="Absolute path to the repository root.")
    prompt: str = Field(description="User prompt describing the task.")
    agentic: bool = Field(default=False, description="If true, run the agentic pipeline. If false, return only the broad map.")
    boost_factor: float = Field(default=10.0, description="Boost multiplier for agent-selected files (default 10×).")
    downgrade_factor: float = Field(default=0.1, description="Downgrade multiplier for agent-suppressed files (default 1/10 = 10× reduction).")


class MapResponse(BaseModel):
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
    chat_rel_fnames: list[str]
    recency_boost_fnames: list[str]
    cochange_boost_fnames: list[str]
    dict_boost_fnames: list[str]
    agent_boost_fnames: list[str] = []
    agent_downgrade_fnames: list[str] = []
    agentic_log: str = ""
    agentic_steps: list[dict] = []


@app.post("/map", response_model=MapResponse)
def generate_map(req: MapRequest):
    print(
        f"\n>>> Received /map request: root={req.root!r}, prompt={req.prompt!r}", flush=True)
    root = os.path.abspath(req.root)
    if not os.path.isdir(root):
        raise HTTPException(status_code=400, detail=f"Not a directory: {root}")

    t0 = time.monotonic()
    agentic_log = ""
    if req.agentic:
        result = run_agentic_map(
            root, req.prompt,
            boost_factor=req.boost_factor,
            downgrade_factor=req.downgrade_factor,
        )
        # Read the latest agentic log file
        log_files = sorted(glob.glob(os.path.join(root, "agentic_log_*.txt")))
        if log_files:
            try:
                with open(log_files[-1], "r", encoding="utf-8") as f:
                    agentic_log = f.read()
            except OSError:
                pass
    else:
        result = generate_repo_map(root, user_prompt=req.prompt)
    duration_ms = round((time.monotonic() - t0) * 1000)

    resp = asdict(result)
    resp["agentic_log"] = agentic_log

    # Patch plotter graph_data.json with duration + output data for history
    try:
        gd_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plotter", "public", "graph_data.json")
        if os.path.exists(gd_path):
            with open(gd_path, "r") as f:
                gd = json.load(f)
            gd["metadata"]["durationMs"] = duration_ms
            gd["output"] = {
                "repo_map": resp.get("repo_map", ""),
                "agentic_log": agentic_log,
                "agentic_steps": resp.get("agentic_steps", []),
                "agent_boost_fnames": resp.get("agent_boost_fnames", []),
                "agent_downgrade_fnames": resp.get("agent_downgrade_fnames", []),
                "map_tokens": resp.get("map_tokens", 0),
                "effective_budget": resp.get("effective_budget", 0),
                "rel_files": resp.get("rel_files", []),
                "model_name": resp.get("model_name", ""),
                "duration_ms": duration_ms,
            }
            with open(gd_path, "w") as f:
                json.dump(gd, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

    return MapResponse(**resp)


@app.get("/health")
def health():
    return {"status": "ok"}
