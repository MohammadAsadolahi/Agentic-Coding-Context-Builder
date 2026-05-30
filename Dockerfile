# ── Build args ──
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

# --- APT: optional proxy through apt-cacher-ng ---
ARG APT_PROXY=""
RUN if [ -n "$APT_PROXY" ]; then \
    echo "Acquire::http::Proxy \"$APT_PROXY\";"   > /etc/apt/apt.conf.d/01proxy && \
    echo "Acquire::https::Proxy \"$APT_PROXY\";"  >> /etc/apt/apt.conf.d/01proxy; \
    fi

# Install git (Python already present in python:3.11-slim; for Ubuntu base, add python3 python3-pip python3-venv)
ARG APT_PACKAGES="git"
RUN apt-get update && \
    apt-get install -y --no-install-recommends ${APT_PACKAGES} && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# For Ubuntu base, create a venv; for python:3.11-slim, use system python
RUN python3 -m venv /app/.venv 2>/dev/null || true
ENV PATH="/app/.venv/bin:$PATH"

# --- pip: optional Nexus PyPI proxy (override via build arg) ---
ARG PIP_INDEX_URL=https://pypi.org/simple/
ARG PIP_TRUSTED_HOST=""
COPY requirements.txt .
RUN pip install \
    --index-url "$PIP_INDEX_URL" \
    ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"} \
    -r requirements.txt

COPY repo_map_generator.py agentic_mapper.py service.py ./

# Allow git to work on host-mounted repos (mounted at /host)
RUN git config --global --add safe.directory '*'

# Deterministic hash seed: eliminates set/dict iteration-order variance in
# Aider's RepoMap, making repo map output reproducible across runs.
ENV PYTHONHASHSEED=0

EXPOSE 8000

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000"]
