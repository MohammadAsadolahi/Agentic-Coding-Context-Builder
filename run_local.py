#!/usr/bin/env python3
"""
run_local.py — Single-process runner: FastAPI backend + static frontend from plotter/dist/.
Skips npm/node entirely by serving the pre-built Vite output and proxying /api/* to /*.
"""

import os
import sys

# Ensure the project dir is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from service import app
from fastapi import Request
from fastapi.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plotter", "dist")


@app.post("/api/map")
async def api_map_proxy(request: Request):
    """Proxy /api/map -> /map (matches what the Vite dev server proxy does)."""
    from service import generate_map, MapRequest
    body = await request.json()
    req = MapRequest(**body)
    return generate_map(req)


@app.get("/api/health")
async def api_health_proxy():
    return {"status": "ok"}


# Serve static files from plotter/dist/ as catch-all (must be last)
app.mount("/", StaticFiles(directory=DIST_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print(f"Serving frontend from: {DIST_DIR}")
    print(f"Open http://localhost:5199 in your browser")
    uvicorn.run(app, host="0.0.0.0", port=5199, log_level="info")
