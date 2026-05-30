#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_PID_FILE="$PROJECT_DIR/.backend.pid"
PLOTTER_PID_FILE="$PROJECT_DIR/.plotter.pid"
BACKEND_PORT="${BACKEND_PORT:-8000}"
BACKEND_LOG="$PROJECT_DIR/.backend.log"
PLOTTER_LOG="$PROJECT_DIR/.plotter.log"

# ── Colors ──
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

log_info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ── Status check ──
check_pid_alive() {
    local pidfile="$1"
    if [[ -f "$pidfile" ]]; then
        local pid
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        fi
    fi
    return 1
}

show_status() {
    echo ""
    echo -e "${BOLD}═══════════════════════════════════════${NC}"
    echo -e "${BOLD}  Service Status${NC}"
    echo -e "${BOLD}═══════════════════════════════════════${NC}"

    # Backend
    if backend_pid=$(check_pid_alive "$BACKEND_PID_FILE"); then
        echo -e "  Backend  : ${GREEN}● running${NC}  (pid $backend_pid, port $BACKEND_PORT)"
    else
        echo -e "  Backend  : ${RED}○ stopped${NC}"
    fi

    # Plotter
    if plotter_pid=$(check_pid_alive "$PLOTTER_PID_FILE"); then
        echo -e "  Plotter  : ${GREEN}● running${NC}  (pid $plotter_pid, port 5199)"
    else
        echo -e "  Plotter  : ${RED}○ stopped${NC}"
    fi

    echo -e "${BOLD}═══════════════════════════════════════${NC}"
    echo ""
}

# ── Stop ──
stop_backend() {
    if backend_pid=$(check_pid_alive "$BACKEND_PID_FILE"); then
        log_info "Stopping backend (pid $backend_pid)..."
        kill "$backend_pid" 2>/dev/null && sleep 1
        kill -0 "$backend_pid" 2>/dev/null && kill -9 "$backend_pid" 2>/dev/null
        rm -f "$BACKEND_PID_FILE"
        log_ok "Backend stopped."
    else
        log_warn "Backend is not running."
        rm -f "$BACKEND_PID_FILE"
    fi
    # Also kill any lingering uvicorn on the port
    lsof -ti :"$BACKEND_PORT" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
}

stop_plotter() {
    if plotter_pid=$(check_pid_alive "$PLOTTER_PID_FILE"); then
        log_info "Stopping plotter (pid $plotter_pid)..."
        kill "$plotter_pid" 2>/dev/null && sleep 1
        kill -0 "$plotter_pid" 2>/dev/null && kill -9 "$plotter_pid" 2>/dev/null
        rm -f "$PLOTTER_PID_FILE"
        log_ok "Plotter stopped."
    else
        log_warn "Plotter is not running."
        rm -f "$PLOTTER_PID_FILE"
    fi
    # Also kill any lingering vite dev server on port 5199
    lsof -ti :5199 2>/dev/null | xargs -r kill -9 2>/dev/null || true
}

# ── Start ──
start_backend() {
    if check_pid_alive "$BACKEND_PID_FILE" >/dev/null; then
        log_warn "Backend is already running (pid $(cat "$BACKEND_PID_FILE")). Use 'rerun' to restart."
        return 1
    fi
    log_info "Starting backend on port $BACKEND_PORT..."
    cd "$PROJECT_DIR"
    python3 -m uvicorn service:app --host 0.0.0.0 --port "$BACKEND_PORT" > "$BACKEND_LOG" 2>&1 &
    echo $! > "$BACKEND_PID_FILE"
    sleep 1
    if check_pid_alive "$BACKEND_PID_FILE" >/dev/null; then
        log_ok "Backend started (pid $(cat "$BACKEND_PID_FILE")) -> http://localhost:$BACKEND_PORT"
    else
        log_err "Backend failed to start. Check logs: $BACKEND_LOG"
        tail -5 "$BACKEND_LOG" 2>/dev/null | sed 's/^/         /'
        return 1
    fi
}

start_plotter() {
    if check_pid_alive "$PLOTTER_PID_FILE" >/dev/null; then
        log_warn "Plotter is already running (pid $(cat "$PLOTTER_PID_FILE")). Use 'rerun' to restart."
        return 1
    fi
    log_info "Starting plotter (vite dev server on port 5199)..."
    cd "$PROJECT_DIR/plotter"
    npm run dev > "$PLOTTER_LOG" 2>&1 &
    echo $! > "$PLOTTER_PID_FILE"
    sleep 2
    if check_pid_alive "$PLOTTER_PID_FILE" >/dev/null; then
        log_ok "Plotter started (pid $(cat "$PLOTTER_PID_FILE")) -> http://localhost:5199"
    else
        log_err "Plotter failed to start. Check logs: $PLOTTER_LOG"
        tail -5 "$PLOTTER_LOG" 2>/dev/null | sed 's/^/         /'
        return 1
    fi
}

# ── Test ──
cmd_test() {
    local root="${1:-/path/to/your/project}"
    local prompt="${2:-explain to me how the sfu works}"
    log_info "Sending test request to http://localhost:$BACKEND_PORT/map ..."
    log_info "Root: $root"
    log_info "Prompt: $prompt"
    echo ""
    curl -s -X POST "http://localhost:$BACKEND_PORT/map" \
      -H "Content-Type: application/json" \
      -d "{\"root\": \"$root\", \"prompt\": \"$prompt\", \"agentic\": true}" \
      | python3 -m json.tool | tee "$PROJECT_DIR/tmp.txt"
    echo ""
    log_ok "Response saved to tmp.txt"
    show_status
}

# ── Commands ──
cmd_run() {
    log_info "Starting services..."
    start_backend
    start_plotter
    show_status
}

cmd_stop() {
    log_info "Stopping all services..."
    stop_backend
    stop_plotter
    show_status
}

cmd_rerun() {
    log_info "Restarting all services..."
    stop_backend
    stop_plotter
    sleep 1
    start_backend
    start_plotter
    show_status
}

cmd_status() {
    show_status
}

cmd_logs() {
    local target="${1:-all}"
    case "$target" in
        backend)
            echo -e "${BOLD}── Backend logs ──${NC}"
            tail -30 "$BACKEND_LOG" 2>/dev/null || log_warn "No backend logs found."
            ;;
        plotter)
            echo -e "${BOLD}── Plotter logs ──${NC}"
            tail -30 "$PLOTTER_LOG" 2>/dev/null || log_warn "No plotter logs found."
            ;;
        *)
            echo -e "${BOLD}── Backend logs (last 15 lines) ──${NC}"
            tail -15 "$BACKEND_LOG" 2>/dev/null || log_warn "No backend logs found."
            echo ""
            echo -e "${BOLD}── Plotter logs (last 15 lines) ──${NC}"
            tail -15 "$PLOTTER_LOG" 2>/dev/null || log_warn "No plotter logs found."
            ;;
    esac
}

usage() {
    echo ""
    echo -e "${BOLD}Usage:${NC} $0 <command> [args]"
    echo ""
    echo -e "${BOLD}Commands:${NC}"
    echo "  run                  Start the backend API and plotter dev server"
    echo "  rerun                Stop running services, then start fresh"
    echo "  stop                 Stop all running services"
    echo "  status               Show current service status"
    echo "  logs [backend|plotter]  Show recent logs (default: both)"
    echo "  test [root] [prompt] Send a test request to the backend"
    echo ""
    exit 1
}

case "${1:-}" in
    run)     cmd_run ;;
    rerun)   cmd_rerun ;;
    stop)    cmd_stop ;;
    status)  cmd_status ;;
    logs)    cmd_logs "${2:-all}" ;;
    test)    cmd_test "${2:-}" "${3:-}" ;;
    *)       usage ;;
esac
