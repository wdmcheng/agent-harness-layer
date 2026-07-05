#!/bin/bash
# Hook: SessionStart
# 提示：有待确认的进化建议，或有待消化的信号。session 启动主 Agent 扫信号、同步消化、逐条问用户
# 只提示一行，绝不阻塞用户

EVO="$(git rev-parse --show-toplevel)/.codex/evolution"
PROPOSALS="$EVO/proposals.md"
SIGNALS="$EVO/signals.jsonl"

MSG=""

# 只数 ## 待审阅 区的建议，不误数 ## 已消化日志 等其他区的 - 行
if [ -f "$PROPOSALS" ]; then
  N=$(awk '/^## 待审阅/{f=1;next} /^## /{f=0} f&&/^- /{c++} END{print c+0}' "$PROPOSALS" 2>/dev/null)
  if [ "${N:-0}" -gt 0 ] 2>/dev/null; then
    MSG="📋 有 ${N} 条进化建议待确认，session 启动我会逐条摆给你问同不同意。"
  fi
fi

if [ -f "$SIGNALS" ] && [ -s "$SIGNALS" ]; then
  MSG="${MSG} 🔄 有新进化信号，session 启动我会扫一遍、消化成建议并逐条问你。"
fi

[ -n "$MSG" ] && echo "$MSG"
exit 0
