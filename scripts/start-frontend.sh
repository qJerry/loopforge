#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

kill_port() {
  local port="$1"
  local pids
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    return
  fi

  echo "端口 ${port} 已被占用，停止进程：${pids//$'\n'/ }"
  kill $pids 2>/dev/null || true
  sleep 1

  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    echo "端口 ${port} 仍被占用，强制停止进程：${pids//$'\n'/ }"
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi
}

: "${LOOPFORGE_HOST:=127.0.0.1}"
: "${LOOPFORGE_PORT:=8765}"
: "${LOOPFORGE_FRONTEND_PORT:=5173}"
: "${LOOPFORGE_TOKEN:=loopforge-local}"

export VITE_LOOPFORGE_BACKEND="http://${LOOPFORGE_HOST}:${LOOPFORGE_PORT}"
export VITE_LOOPFORGE_TOKEN="$LOOPFORGE_TOKEN"

kill_port "$LOOPFORGE_FRONTEND_PORT"

echo "LoopForge 前端：http://127.0.0.1:${LOOPFORGE_FRONTEND_PORT}/?token=${LOOPFORGE_TOKEN}"
echo "API 代理：${VITE_LOOPFORGE_BACKEND}"
exec npm run dev:console -- --port "$LOOPFORGE_FRONTEND_PORT"
