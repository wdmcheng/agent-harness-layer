#!/bin/bash
# Hook: PreToolUse（命令工具）— 合并 Claude 版的 pre-commit-check 与 pnpm-dev 清端口
# Codex 的 matcher 只过滤 tool name，命令内容判断在脚本里做
# 命令字段按 .tool_input.command 与 .command 双重兼容读取，适配不同 Codex 版本的 input 格式

INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // .command // empty' 2>/dev/null)
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"

# git commit 前：编译检查，不过则阻止（exit 2）
if echo "$CMD" | grep -qE "git commit"; then
  TSCONFIG=$(find "$ROOT" -maxdepth 3 -name "tsconfig.json" -not -path "*/node_modules/*" -not -path "*/.next/*" 2>/dev/null | head -1)
  if [ -n "$TSCONFIG" ]; then
    cd "$(dirname "$TSCONFIG")"
    TSC_OUTPUT=$(npx tsc --noEmit 2>&1)
    if [ $? -ne 0 ]; then
      echo "编译检查未通过，commit 被阻止。请修复以下错误：" >&2
      echo "$TSC_OUTPUT" >&2
      exit 2
    fi
  fi
fi

# pnpm dev 前：清占用端口，避免端口被占导致启动失败
if echo "$CMD" | grep -qE "pnpm dev|npm run dev|yarn dev"; then
  for port in 3000 3001 4173 5173 8080; do kill -9 "$(lsof -ti:$port)" 2>/dev/null; done
fi

exit 0
