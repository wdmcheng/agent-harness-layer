#!/bin/bash
# Hook: PostToolUse (Bash) if git commit*
# commit 后自动 push。保护分支不自动推，push 失败必须报出来，不吞错误

BRANCH=$(git -C "$CLAUDE_PROJECT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)

case "$BRANCH" in
  main|master)
    echo "⚠️ 当前在 $BRANCH 分支，已跳过自动 push。保护分支需手动 push 或走 PR。" >&2
    exit 0
    ;;
  "")
    exit 0
    ;;
esac

PUSH_OUT=$(git -C "$CLAUDE_PROJECT_DIR" push 2>&1)
if [ $? -ne 0 ]; then
  echo "❌ 自动 push 失败，请手动检查：" >&2
  echo "$PUSH_OUT" >&2
fi

exit 0
