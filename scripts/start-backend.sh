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

: "${LOOPFORGE_CONFIG:=.loopforge/projects.json}"
: "${LOOPFORGE_HOST:=127.0.0.1}"
: "${LOOPFORGE_PORT:=8765}"
: "${LOOPFORGE_TOKEN:=loopforge-local}"
: "${LOOPFORGE_BUILD_CONSOLE:=1}"
: "${LOOPFORGE_AUTO_SCHEDULE:=1}"
: "${LOOPFORGE_SCHEDULE_INTERVAL:=60}"

if [[ -z "${LOOPFORGE_CODEX_BIN:-}" ]]; then
  for codex_bin in \
    "/Applications/ChatGPT.app/Contents/Resources/codex" \
    "/Applications/Codex.app/Contents/Resources/codex"; do
    if [[ -x "$codex_bin" ]]; then
      export LOOPFORGE_CODEX_BIN="$codex_bin"
      break
    fi
  done
fi

if [[ "$LOOPFORGE_BUILD_CONSOLE" == "1" ]]; then
  npm run build:console
fi

kill_port "$LOOPFORGE_PORT"

echo "LoopForge 后端：http://${LOOPFORGE_HOST}:${LOOPFORGE_PORT}/?token=${LOOPFORGE_TOKEN}"
args=(
  --config "$LOOPFORGE_CONFIG"
  console
  --host "$LOOPFORGE_HOST"
  --port "$LOOPFORGE_PORT"
  --token "$LOOPFORGE_TOKEN"
  --schedule-interval "$LOOPFORGE_SCHEDULE_INTERVAL"
)

if [[ "$LOOPFORGE_AUTO_SCHEDULE" == "1" ]]; then
  args+=(--auto-schedule)
fi

exec python3 -m loopforge.cli "${args[@]}"
