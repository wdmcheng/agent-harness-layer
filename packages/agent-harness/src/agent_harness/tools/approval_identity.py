"""审批与工具执行共享的纯canonical身份函数。"""

from __future__ import annotations

import hashlib
import json


def hash_tool_arguments(arguments: dict[str, object]) -> str:
    """返回checkpoint/grant绑定使用的canonical SHA-256。"""

    serialized = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).hexdigest()


__all__ = ["hash_tool_arguments"]
