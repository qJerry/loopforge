#!/usr/bin/env bash
set -euo pipefail

LOOPFORGE_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ONBOARD_CONFIG_PATH="${LOOPFORGE_CONFIG:-$LOOPFORGE_REPO_ROOT/.loopforge/projects.json}"
ONBOARD_MODE=""
ONBOARD_ROOT=""
ONBOARD_TYPE=""
ONBOARD_PROJECT_ID=""
ONBOARD_NAME=""
ONBOARD_OWNER=""
ONBOARD_PLANNING_ADAPTER="builtin"
ONBOARD_BUILD_TARGET=""
ONBOARD_PLAN_HASH=""
ONBOARD_CHILDREN=()

usage() {
  cat <<'EOF'
用法：
  scripts/onboard-project.sh --root PATH --type PROFILE [选项] --dry-run
  scripts/onboard-project.sh --root PATH --type PROFILE [选项] --apply --plan-hash HASH
  scripts/onboard-project.sh --project-id ID [--config PATH] --doctor

选项：
  --config PATH             LoopForge projects.json 路径
  --project-id ID           稳定项目 ID
  --name NAME               项目显示名
  --owner NAME              项目负责人标识（必填）
  --planning-adapter NAME   builtin（默认）或外部 provider 名
  --build-target TARGET     Flutter 构建目标；无法唯一探测时必须提供
  --child KEY=PATH          项目组子仓，可重复
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --config) ONBOARD_CONFIG_PATH="$2"; shift 2 ;;
    --root) ONBOARD_ROOT="$2"; shift 2 ;;
    --type) ONBOARD_TYPE="$2"; shift 2 ;;
    --project-id) ONBOARD_PROJECT_ID="$2"; shift 2 ;;
    --name) ONBOARD_NAME="$2"; shift 2 ;;
    --owner) ONBOARD_OWNER="$2"; shift 2 ;;
    --planning-adapter) ONBOARD_PLANNING_ADAPTER="$2"; shift 2 ;;
    --build-target) ONBOARD_BUILD_TARGET="$2"; shift 2 ;;
    --plan-hash) ONBOARD_PLAN_HASH="$2"; shift 2 ;;
    --child) ONBOARD_CHILDREN+=("$2"); shift 2 ;;
    --dry-run|--apply|--doctor) ONBOARD_MODE="$1"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

export PYTHONPATH="$LOOPFORGE_REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

if [ "$ONBOARD_MODE" = "--doctor" ]; then
  if [ -z "$ONBOARD_PROJECT_ID" ]; then echo '--doctor 需要 --project-id。' >&2; exit 2; fi
  exec python3 -m loopforge.cli --config "$ONBOARD_CONFIG_PATH" --json project doctor "$ONBOARD_PROJECT_ID"
fi

if [ -z "$ONBOARD_MODE" ] || [ -z "$ONBOARD_ROOT" ] || [ -z "$ONBOARD_TYPE" ] || [ -z "$ONBOARD_OWNER" ]; then
  echo '接入项目需要 --root、--type、--owner 和 --dry-run/--apply。' >&2
  usage >&2
  exit 2
fi
if [ "$ONBOARD_MODE" = "--apply" ] && [ -z "$ONBOARD_PLAN_HASH" ]; then
  echo '--apply 必须使用最近一次 dry-run 返回的 --plan-hash。' >&2
  exit 2
fi

ONBOARD_ARGS=(--config "$ONBOARD_CONFIG_PATH" --json project onboard --root "$ONBOARD_ROOT" --type "$ONBOARD_TYPE")
if [ -n "$ONBOARD_PROJECT_ID" ]; then ONBOARD_ARGS+=(--project-id "$ONBOARD_PROJECT_ID"); fi
if [ -n "$ONBOARD_NAME" ]; then ONBOARD_ARGS+=(--name "$ONBOARD_NAME"); fi
ONBOARD_ARGS+=(--owner "$ONBOARD_OWNER")
if [ -n "$ONBOARD_PLANNING_ADAPTER" ]; then ONBOARD_ARGS+=(--planning-adapter "$ONBOARD_PLANNING_ADAPTER"); fi
if [ -n "$ONBOARD_BUILD_TARGET" ]; then ONBOARD_ARGS+=(--build-target "$ONBOARD_BUILD_TARGET"); fi
for child in ${ONBOARD_CHILDREN[@]+"${ONBOARD_CHILDREN[@]}"}; do ONBOARD_ARGS+=(--child "$child"); done
ONBOARD_ARGS+=("$ONBOARD_MODE")
if [ -n "$ONBOARD_PLAN_HASH" ]; then ONBOARD_ARGS+=(--plan-hash "$ONBOARD_PLAN_HASH"); fi

exec python3 -m loopforge.cli "${ONBOARD_ARGS[@]}"
