#!/bin/bash
# Hook: PreToolUse (Bash) if git commit*
# commit 前自动编译检查，不通过则阻止 commit
# 通用：自动查找包含 tsconfig.json 的项目代码目录
# 注意：settings.json 的 if 字段在当前 harness 不生效，会导致本 hook 对所有 Bash
# 无条件执行 tsc，typecheck 一红就拦住全部 Bash（含子 Agent 自检）。故在脚本内自行
# 判定命令，只对 git commit 执行编译门禁，恢复脚本注释与 settings 声明的本意。

INPUT=$(cat 2>/dev/null)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)

# dev 启动前：清占用端口，避免端口被占导致启动失败。
# 同 git commit 门禁，settings.json 的 if 字段在当前 harness 不生效，故在脚本内自判命令，
# 只对 pnpm dev / npm run dev / yarn dev 清端口，不波及其余 Bash。
case "$CMD" in
  *"pnpm dev"*|*"npm run dev"*|*"yarn dev"*)
    for port in 3000 3001 4173 5173 8080; do kill -9 "$(lsof -ti:$port)" 2>/dev/null; done
    ;;
esac

case "$CMD" in
  *"git commit"*) ;;
  *) exit 0 ;;
esac

TSCONFIG=$(find "$CLAUDE_PROJECT_DIR" -maxdepth 3 -name "tsconfig.json" -not -path "*/node_modules/*" -not -path "*/.next/*" 2>/dev/null | head -1)

if [ -z "$TSCONFIG" ]; then
  exit 0
fi

PROJECT_CODE=$(dirname "$TSCONFIG")
cd "$PROJECT_CODE"

TSC_OUTPUT=$(npx tsc --noEmit 2>&1)
TSC_EXIT=$?

if [ $TSC_EXIT -ne 0 ]; then
  echo "编译检查未通过，commit 被阻止。请修复以下错误：" >&2
  echo "$TSC_OUTPUT" >&2
  exit 2
fi

exit 0
