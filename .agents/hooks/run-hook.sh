#!/usr/bin/env bash
set -euo pipefail

hook_name="${1:-}"
if [ -z "$hook_name" ]; then
  printf 'Usage: run-hook.sh <hook-name>\n' >&2
  exit 1
fi
agent_name="${2:-${AGENT_PACK_AGENT:-}}"

project_dir="${AGENT_PACK_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-${CODEX_PROJECT_DIR:-}}}"
if [ -z "$project_dir" ]; then
  project_dir="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
fi

runner="$project_dir/.agents/hooks/agent_pack_hook.py"
if [ ! -f "$runner" ]; then
  printf '错误：找不到 Agent Pack hook runner：%s\n' "$runner" >&2
  exit 1
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$runner" "$hook_name" "$agent_name"
elif command -v python >/dev/null 2>&1; then
  exec python "$runner" "$hook_name" "$agent_name"
elif command -v py >/dev/null 2>&1; then
  exec py -3 "$runner" "$hook_name" "$agent_name"
fi

printf '错误：需要 Python 3 来运行 Agent Pack hooks。\n' >&2
exit 1
