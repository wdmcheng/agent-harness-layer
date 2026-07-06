#!/usr/bin/env python3
from __future__ import annotations

import sys

from agent_pack_hook_review import mark_review_needed, stop_gate
from agent_pack_hook_runtime import (
    auto_push,
    check_evolution,
    detect_feedback_signal,
    pre_tool_shell,
    project_root,
    read_payload,
)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: agent_pack_hook.py <hook-name>", file=sys.stderr)
        return 1

    hook = argv[1]
    agent = argv[2] if len(argv) > 2 else ""
    root = project_root()
    data = read_payload()
    handlers = {
        "detect-feedback-signal": lambda: detect_feedback_signal(root, data, agent),
        "check-evolution": lambda: check_evolution(root, agent),
        "auto-push": lambda: auto_push(root, data, agent),
        "mark-review-needed": lambda: mark_review_needed(root, data, agent),
        "pre-tool-shell": lambda: pre_tool_shell(root, data, agent),
        "pre-commit-check": lambda: pre_tool_shell(root, data, agent),
        "stop-gate": lambda: stop_gate(root),
    }
    handler = handlers.get(hook)
    if handler is None:
        print(f"Unknown Agent Pack hook: {hook}", file=sys.stderr)
        return 1
    return handler()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
