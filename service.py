#!/usr/bin/env python3
"""
service.py — HTTP API for the agentic repo map generator.

Exposes a single POST endpoint that accepts a repo root path and user prompt,
runs the agentic pipeline, and returns the RepoMapResult as JSON.

Usage:
    uvicorn service:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, HTTPException
import glob
import os
import time
import json

from agentic_mapper import AgenticConfigurationError, run_agentic_map
from schemas import MapRequest, MapResponse
from repo_map_generator import generate_repo_map

app = FastAPI(title="Agentic Repo Map Service")


@app.post("/map", response_model=MapResponse)
def generate_map(req: MapRequest):
    print(
        f"\n>>> Received /map request: root={req.root!r}, prompt={req.prompt!r}", flush=True)
    root = req.root
    if not os.path.isdir(root):
        raise HTTPException(status_code=400, detail=f"Not a directory: {root}")

    t0 = time.monotonic()
    agentic_log = ""
    if req.agentic:
        try:
            result = run_agentic_map(
                root, req.prompt,
                boost_factor=req.boost_factor,
                downgrade_factor=req.downgrade_factor,
            )
        except AgenticConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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

    resp = result.model_dump()
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

    return MapResponse.model_validate(resp)


@app.get("/health")
def health():
    return {"status": "ok"}
