"""本地 evidence payload 的确定性脱敏规则。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)
NON_SECRET_TOKEN_KEYS = {
    "input_tokens",
    "output_tokens",
    "token_count",
    "tokens",
    "total_tokens",
}
SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer|basic)?\s*['\"]?[^\s,'\";]+"),
    re.compile(r"(?i)\b(set-cookie|cookie)\s*:\s*[^\r\n;]+(?:;[^\r\n]*)?"),
    re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*['\"]?[^\s,'\";&]+"),
)


def redact_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        mapping = cast(Mapping[object, object], value)
        for key, item in mapping.items():
            key_text = str(key).lower()
            if key_text in NON_SECRET_TOKEN_KEYS:
                redacted[str(key)] = redact_secrets(item)
            elif any(marker in key_text for marker in SECRET_KEY_MARKERS):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        items = cast(list[object], value)
        return [redact_secrets(item) for item in items]
    if isinstance(value, str):
        redacted_text = value
        # 有些 provider/tool output 会把 secret 塞在普通字符串里，而不是结构化
        # metadata key 下。pattern redaction 兜住这类 evidence 泄漏。
        for pattern in SECRET_VALUE_PATTERNS:
            redacted_text = pattern.sub("[REDACTED]", redacted_text)
        return redacted_text
    return value
