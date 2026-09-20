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
    echo "usage: $0 {start|stop|status|ports}" >&2
    exit 2
}

sessions() {
    if [ -d "$ACCOUNTS_DIR" ]; then
        find "$ACCOUNTS_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | sort
    fi
}

valid_port() {
    case "${1:-}" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

port_taken() {
    local port="$1" except="$2" file name
    shopt -s nullglob
    for file in "$PID_DIR"/*.port; do
        name="$(basename "$file" .port)"
        [ "$name" = "$except" ] && continue
        [ "$(cat "$file")" = "$port" ] && return 0
    done
    return 1
}

process_is_ours() {
    local pid="$1" port="${2:-}" session="$3" line
    if [ -r "/proc/$pid/environ" ]; then
        tr '\0' '\n' <"/proc/$pid/environ" | grep -qxF "PRISM_SESSION=$session" || return 1
        if [ -n "$port" ]; then
            tr '\0' '\n' <"/proc/$pid/environ" | grep -qxF "PRISM_PORT=$port" || return 1
        fi
        return 0
    fi
    line="$(ps eww -p "$pid" -o command= 2>/dev/null || true)"
    case "$line" in
        *freeastra.py*) ;;
        *) return 1 ;;
    esac
    case "$line" in
        *"PRISM_SESSION=$session"*) ;;
        *) return 1 ;;
    esac
    if [ -n "$port" ]; then
        case "$line" in
            *"PRISM_PORT=$port"*) ;;
            *) return 1 ;;
        esac
    fi
}

prune_orphans() {
    local file name pid pidfile
    [ -d "$ACCOUNTS_DIR" ] || return 0
    shopt -s nullglob
    for file in "$PID_DIR"/*.port "$PID_DIR"/*.pid; do
        name="$(basename "$file")"
        name="${name%.port}"
        name="${name%.pid}"
        [ -f "$ACCOUNTS_DIR/$name.json" ] && continue
        pidfile="$PID_DIR/$name.pid"
        if [ -f "$pidfile" ]; then
            pid="$(cat "$pidfile")"
            if kill -0 "$pid" 2>/dev/null \
                    && process_is_ours "$pid" "" "$ACCOUNTS_DIR/$name.json"; then
                continue
            fi
        fi
        rm -f "$PID_DIR/$name.port" "$PID_DIR/$name.pid"
    done
}

assign_port() {
    local name="$1" port="" candidate
    mkdir -p "$PID_DIR"
    if [ -f "$PID_DIR/$name.port" ]; then
        port="$(cat "$PID_DIR/$name.port")"
        if valid_port "$port" && ! port_taken "$port" "$name"; then
            echo "$port"
            return
        fi
    fi
    candidate="$BASE_PORT"
    while port_taken "$candidate" "$name"; do
        candidate=$((candidate + 1))
    done
    if ! valid_port "$candidate"; then
        echo "no usable port at or above PRISM_POOL_BASE_PORT=$BASE_PORT" >&2
        exit 1
    fi
    echo "$candidate" >"$PID_DIR/$name.port"
    echo "$candidate"
}

start() {
    mkdir -p "$LOG_DIR" "$PID_DIR"
    prune_orphans
    local session name port pidfile pid
    local count=0
    while IFS= read -r session; do
        name="$(basename "$session" .json)"
        port="$(assign_port "$name")"
        pidfile="$PID_DIR/$name.pid"
        if [ -f "$pidfile" ]; then
            pid="$(cat "$pidfile")"
            if kill -0 "$pid" 2>/dev/null && process_is_ours "$pid" "$port" "$session"; then
                echo "$name already running on port $port"
                count=$((count + 1))
                continue
            fi
            rm -f "$pidfile"
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
        count=$((count + 1))
    done < <(sessions)
    if [ "$count" -eq 0 ]; then
        echo "no session files in $ACCOUNTS_DIR" >&2
        exit 1
    fi
}

stop() {
    mkdir -p "$PID_DIR"
    prune_orphans
    local pidfile name pid port session
    shopt -s nullglob
    for pidfile in "$PID_DIR"/*.pid; do
        name="$(basename "$pidfile" .pid)"
        pid="$(cat "$pidfile")"
        port=""
        [ -f "$PID_DIR/$name.port" ] && port="$(cat "$PID_DIR/$name.port")"
        valid_port "$port" || port=""
        session="$ACCOUNTS_DIR/$name.json"
        if kill -0 "$pid" 2>/dev/null; then
            if process_is_ours "$pid" "$port" "$session"; then
                kill "$pid"
                echo "stopped $name (pid $pid)"
            else
                echo "skipped $name: pid $pid is not this adapter" >&2
            fi
        fi
        rm -f "$pidfile"
    done
}

status() {
    mkdir -p "$PID_DIR"
    prune_orphans
    local session name port
    while IFS= read -r session; do
        name="$(basename "$session" .json)"
        port="$(assign_port "$name")"
        if curl -fsS "http://$BIND:$port/healthz" >/dev/null 2>&1; then
            echo "$name :$port ok"
        else
            echo "$name :$port down"
        fi
    done < <(sessions)
}

ports() {
    mkdir -p "$PID_DIR"
    prune_orphans
    local session name port
    while IFS= read -r session; do
        name="$(basename "$session" .json)"
        port="$(assign_port "$name")"
        echo "$name $port"
    done < <(sessions)
}

[ $# -ge 1 ] || usage
case "$1" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    ports) ports ;;
    *) usage ;;
esac
