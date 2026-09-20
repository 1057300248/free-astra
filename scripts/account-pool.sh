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

assign_port() {
    local name="$1" port=""
    mkdir -p "$PID_DIR"
    if [ -f "$PID_DIR/$name.port" ]; then
        port="$(cat "$PID_DIR/$name.port")"
    fi
    if [ -n "$port" ] && ! port_taken "$port" "$name"; then
        echo "$port"
        return
    fi
    port="$BASE_PORT"
    while port_taken "$port" "$name"; do
        port=$((port + 1))
    done
    echo "$port" >"$PID_DIR/$name.port"
    echo "$port"
}

process_is_ours() {
    local pid="$1" port="$2" session="$3"
    if [ -r "/proc/$pid/environ" ]; then
        tr '\0' '\n' <"/proc/$pid/environ" | grep -qxF "PRISM_PORT=$port" || return 1
        tr '\0' '\n' <"/proc/$pid/environ" | grep -qxF "PRISM_SESSION=$session" || return 1
        return 0
    fi
    ps -p "$pid" -o command= 2>/dev/null | grep -q "freeastra.py"
}

start() {
    mkdir -p "$LOG_DIR" "$PID_DIR"
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
    local pidfile name pid port session
    shopt -s nullglob
    for pidfile in "$PID_DIR"/*.pid; do
        name="$(basename "$pidfile" .pid)"
        pid="$(cat "$pidfile")"
        port=""
        [ -f "$PID_DIR/$name.port" ] && port="$(cat "$PID_DIR/$name.port")"
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
