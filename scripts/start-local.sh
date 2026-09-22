#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

: "${LOOPFORGE_BUILD_CONSOLE:=0}"
export LOOPFORGE_BUILD_CONSOLE

backend_pid=""

cleanup() {
  if [[ -n "$backend_pid" ]] && kill -0 "$backend_pid" 2>/dev/null; then
    kill "$backend_pid" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

scripts/start-backend.sh &
backend_pid="$!"

sleep 1
if ! kill -0 "$backend_pid" 2>/dev/null; then
  echo "LoopForge 后端启动失败。"
  exit 1
fi

scripts/start-frontend.sh
