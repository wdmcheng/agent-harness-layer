#!/bin/bash
# Stop hook: 代码文件被修改但未 review 时阻止停止
# fail-closed：状态文件存在且内容不是 clean，一律拦截
# 不存在 = 无待审 = 放行；clean 或空 = 放行并删除；其余 = 拦截

STATE_FILE="$CLAUDE_PROJECT_DIR/.agents/.needs-review"

if [ ! -f "$STATE_FILE" ]; then
  exit 0
fi

STATE=$(cat "$STATE_FILE" 2>/dev/null | tr -d '[:space:]')

case "$STATE" in
  "clean"|"")
    rm -f "$STATE_FILE"
    exit 0
    ;;
  *)
    echo '{"decision": "block", "reason": "代码已修改但未通过 code review。请派发 code-reviewer 两阶段审查，通过后写入 clean。用 /goal 自驱时，把 code-reviewer 通过写进 /goal 完成条件。"}'
    exit 0
    ;;
esac
