#!/usr/bin/env bash
set -euo pipefail

hook_name="$(basename "$0" .sh)"
project_dir="${AGENT_PACK_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-${CODEX_PROJECT_DIR:-}}}"
if [ -z "$project_dir" ]; then
  project_dir="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
fi

exec "$project_dir/.agents/hooks/run-hook.sh" "$hook_name" "claude"
