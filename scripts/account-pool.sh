#!/usr/bin/env bash
# Run one freeastra.py per Prism session so a gateway can treat each account as
# a separate upstream channel and load-balance across them.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${FREE_ASTRA_HOME:-$HOME/.free-astra}"
ACCOUNTS_DIR="${PRISM_ACCOUNTS_DIR:-$STATE/accounts}"
BASE_PORT="${PRISM_POOL_BASE_PORT:-${PRISM_PORT:-8319}}"
BIND="${PRISM_BIND:-127.0.0.1}"
PYTHON="${PYTHON:-python3}"
LOG_DIR="${PRISM_POOL_LOG_DIR:-$STATE/pool-logs}"
PID_DIR="${PRISM_POOL_PID_DIR:-$STATE/pool-pids}"

usage() {
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
}

sessions() {
    if [ -d "$ACCOUNTS_DIR" ]; then
        find "$ACCOUNTS_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | sort
    fi
}

start() {
    mkdir -p "$LOG_DIR" "$PID_DIR"
    local index=0 session name port pidfile
    for session in $(sessions); do
        name="$(basename "$session" .json)"
        port=$((BASE_PORT + index))
        pidfile="$PID_DIR/$name.pid"
        if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
            echo "$name already running on port $port"
            index=$((index + 1))
            continue
        fi
        (
            cd "$HERE"
            PRISM_API_ONLY="${PRISM_API_ONLY:-1}" \
            PRISM_SESSION="$session" \
            PRISM_PORT="$port" \
            PRISM_BIND="$BIND" \
            PRISM_API_KEY="${PRISM_API_KEY:-}" \
            PRISM_API_REFRESH="${PRISM_API_REFRESH:-0}" \
            nohup "$PYTHON" freeastra.py >>"$LOG_DIR/$name.log" 2>&1 &
            echo $! >"$pidfile"
        )
        echo "$name -> http://$BIND:$port"
        index=$((index + 1))
    done
    if [ "$index" -eq 0 ]; then
        echo "no session files in $ACCOUNTS_DIR" >&2
        exit 1
    fi
}

stop() {
    local pidfile pid
    shopt -s nullglob
    for pidfile in "$PID_DIR"/*.pid; do
        pid="$(cat "$pidfile")"
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            echo "stopped $(basename "$pidfile" .pid) (pid $pid)"
        fi
        rm -f "$pidfile"
    done
}

status() {
    local index=0 session name port
    for session in $(sessions); do
        name="$(basename "$session" .json)"
        port=$((BASE_PORT + index))
        if curl -fsS "http://$BIND:$port/healthz" >/dev/null 2>&1; then
            echo "$name :$port ok"
        else
            echo "$name :$port down"
        fi
        index=$((index + 1))
    done
}

[ $# -ge 1 ] || usage
case "$1" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    *) usage ;;
esac
