#!/usr/bin/env bash
# deploy.sh — Remote deployment helper for the repo map service.
#
# Configure via environment variables or a .deploy.env file:
#   DEPLOY_HOST     SSH target (e.g. user@192.168.1.100)
#   DEPLOY_DIR      Remote working directory (default: /opt/repo-map)
#   DEPLOY_PROJECT  Default project path for test commands (default: /root/my-project)
set -euo pipefail

# ── Load config ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[[ -f "$SCRIPT_DIR/.deploy.env" ]] && source "$SCRIPT_DIR/.deploy.env"

VM="${DEPLOY_HOST:?Set DEPLOY_HOST (e.g. user@your-server)}"
REMOTE_DIR="${DEPLOY_DIR:-/opt/repo-map}"
LOCAL_DIR="$SCRIPT_DIR"
DEFAULT_TEST_PROJECT="${DEPLOY_PROJECT:-/root/my-project}"

# ── Colors ──
GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[0;36m'
YELLOW='\033[0;33m'
NC='\033[0m'
BOLD='\033[1m'

log()  { echo -e "${CYAN}[>>]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
err()  { echo -e "${RED}[!!]${NC} $*"; }
warn() { echo -e "${YELLOW}[--]${NC} $*"; }

usage() {
    echo -e "${BOLD}Usage:${NC} $0 <command>"
    echo ""
    echo "  start                             Start services (no git, no rebuild)"
    echo "  stop                              Stop all services"
    echo "  restart                           Stop then start"
    echo "  status                            Show status of everything"
    echo "  fullstart                         Full deploy: git push/pull, rebuild, restart"
    echo "  test 'prompt' ['path']            Run repo map and show result"
    echo "  test --agentic 'prompt' ['path']  Run with agentic pipeline"
    echo ""
    echo "Environment:"
    echo "  DEPLOY_HOST     SSH target (required)"
    echo "  DEPLOY_DIR      Remote directory (default: /opt/repo-map)"
    echo "  DEPLOY_PROJECT  Default test project path"
    echo ""
    exit 1
}

# ═══════════════════════════════════════
#  Functions
# ═══════════════════════════════════════

do_git_sync() {
    log "Pushing local changes..."
    cd "$LOCAL_DIR"
    if git diff --quiet && git diff --cached --quiet; then
        log "Working tree clean, pushing any unpushed commits..."
    else
        git add -A
        git commit -m "deploy: $(date +%Y%m%d-%H%M%S)" --allow-empty-message
    fi
    git push origin main 2>&1 || true
    ok "Local git synced"

    log "Pulling latest code on VM..."
    ssh "$VM" "cd $REMOTE_DIR && git pull" 2>&1
    ok "VM code updated"
}

do_build() {
    log "Rebuilding backend image..."
    ssh "$VM" "cd $REMOTE_DIR && docker compose build 2>&1 | tail -5"
    ok "Backend image rebuilt"
}

do_stop() {
    log "Stopping existing services..."
    ssh "$VM" bash -s -- "$REMOTE_DIR" <<'STOP'
RDIR="$1"
cd "$RDIR"

if [ -f "$RDIR/plotter.pid" ]; then
    pid=$(cat "$RDIR/plotter.pid")
    kill "$pid" 2>/dev/null && echo "Plotter ($pid) stopped" || echo "Plotter not running"
    rm -f "$RDIR/plotter.pid"
fi
fuser -k 5199/tcp 2>/dev/null || true

docker compose down 2>&1 || true
echo "Services stopped"
STOP
    ok "Services stopped"
}

do_start() {
    log "Starting backend..."
    ssh "$VM" "cd $REMOTE_DIR && docker compose up -d 2>&1"
    ok "Backend started"

    log "Starting plotter..."
    ssh "$VM" bash -s -- "$REMOTE_DIR" <<'PLOTTER'
RDIR="$1"
cd "$RDIR/plotter"
mkdir -p public
[ -f public/graph_data.json ] || echo '{}' > public/graph_data.json
nohup npx vite --host 0.0.0.0 --port 5199 > "$RDIR/plotter.log" 2>&1 &
echo $! > "$RDIR/plotter.pid"
sleep 3
head -5 "$RDIR/plotter.log"
PLOTTER
    ok "Plotter started"
}

do_status() {
    echo ""
    echo -e "${BOLD}═══════════════════════════════════════${NC}"
    echo -e "${BOLD}  Service Status  ($VM)${NC}"
    echo -e "${BOLD}═══════════════════════════════════════${NC}"

    backend=$(ssh "$VM" "curl -sm5 http://localhost:8000/health 2>&1" || echo "FAIL")
    if [[ "$backend" == *'"ok"'* ]]; then
        echo -e "  Backend  : ${GREEN}UP${NC}  $backend"
    else
        echo -e "  Backend  : ${RED}DOWN${NC}  $backend"
    fi

    plotter=$(ssh "$VM" "curl -sm5 -o /dev/null -w '%{http_code}' http://localhost:5199/ 2>&1" || echo "000")
    if [[ "$plotter" == "200" ]]; then
        echo -e "  Plotter  : ${GREEN}UP${NC}  HTTP $plotter"
    else
        echo -e "  Plotter  : ${RED}DOWN${NC}  HTTP $plotter"
    fi

    container=$(ssh "$VM" "docker compose -f $REMOTE_DIR/docker-compose.yml ps --format '{{.Name}} {{.Status}}' 2>/dev/null" || echo "none")
    echo -e "  Container: $container"

    plotter_pid=$(ssh "$VM" "cat $REMOTE_DIR/plotter.pid 2>/dev/null && echo '' || echo 'no pidfile'")
    if [[ "$plotter_pid" != "no pidfile" ]]; then
        alive=$(ssh "$VM" "kill -0 $plotter_pid 2>/dev/null && echo 'alive' || echo 'dead'")
        echo -e "  Plotter PID: $plotter_pid ($alive)"
    else
        echo -e "  Plotter PID: ${YELLOW}no pidfile${NC}"
    fi

    echo -e "${BOLD}═══════════════════════════════════════${NC}"
    echo ""
}

do_healthcheck() {
    log "Running health checks..."
    sleep 2
    do_status
}

do_test() {
    local agentic="$1"
    local prompt="$2"
    local project_path="${3:-$DEFAULT_TEST_PROJECT}"
    local container_path="/host${project_path}"
    local mode="broad map"
    [[ "$agentic" == "true" ]] && mode="agentic"

    log "Sending repo map request ($mode)..."
    log "  Prompt         : $prompt"
    log "  Host path      : $project_path"
    log "  Container path : $container_path"
    log "  Agentic        : $agentic"

    result=$(ssh "$VM" "curl -s -X POST http://localhost:8000/map \
        -H 'Content-Type: application/json' \
        -d '{\"root\": \"$container_path\", \"prompt\": \"$prompt\", \"agentic\": $agentic}'" 2>&1)

    if echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['repo_map'][:200])" 2>/dev/null; then
        ok "Repo map received"
        echo ""
        echo -e "${BOLD}═══════════════════════════════════════${NC}"
        echo -e "${BOLD}  Repo Map Result${NC}"
        echo -e "${BOLD}═══════════════════════════════════════${NC}"
        echo "$result" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f\"  Model      : {d['model_name']}\")
print(f\"  Map tokens : {d['map_tokens']}\")
print(f\"  Files      : {len(d['rel_files'])}\")
print(f\"  Used git   : {d['used_git']}\")
print()
print(d['repo_map'])
" 2>/dev/null
        echo -e "${BOLD}═══════════════════════════════════════${NC}"
    else
        err "Request failed or returned invalid response:"
        echo "$result"
    fi
}

# ═══════════════════════════════════════
#  Main
# ═══════════════════════════════════════

[[ $# -lt 1 ]] && usage

case "$1" in
    start)
        do_start
        do_healthcheck
        ;;
    stop)
        do_stop
        ;;
    restart)
        do_stop
        do_start
        do_healthcheck
        ;;
    status)
        do_status
        ;;
    fullstart)
        do_git_sync
        do_stop
        do_build
        do_start
        do_healthcheck
        ;;
    test)
        agentic=false
        shift
        if [[ "${1:-}" == "--agentic" ]]; then
            agentic=true
            shift
        fi
        [[ $# -lt 1 ]] && { err "Usage: $0 test [--agentic] 'prompt' ['/path/to/project']"; exit 1; }
        do_test "$agentic" "$1" "${2:-}"
        ;;
    *)
        err "Unknown command: $1"
        usage
        ;;
esac
