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
    "budget_charge_tokens",
    "charged_tokens",
    "estimated_tokens",
    "input_envelope_token_bound",
    "input_token_price_usd",
    "input_tokens",
    "max_output_tokens",
    "max_per_attempt_token_bound",
    "max_prompt_utf8_bytes",
    "max_tokens",
    "max_tokens_per_run",
    "original_tokens",
    "output_token_cap",
    "output_token_price_usd",
    "output_tokens",
    "per_attempt_token_bound",
    "reserved_token_bound",
    "retained_tokens",
    "token_budget",
    "token_count",
    "token_estimate",
    "token_usage",
    "tokens",
    "total_tokens",
    "trusted_input_token_bound",
    "trusted_token_bound",
    "used_tokens",
}
SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer|basic)?\s*['\"]?[^\s,'\";]+"),
    re.compile(r"(?i)\b(set-cookie|cookie)\s*:\s*[^\r\n;]+(?:;[^\r\n]*)?"),
    re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*['\"]?[^\s,'\";&]+"),
)


def redact_secrets(value: Any) -> Any:
    """递归脱敏结构化 payload 和自由文本中的常见凭证，保留非秘密 token 计数。

    返回新容器而非原地修改，调用方可安全复用输入对象；未知标量保持原样，
    以免审计和错误证据因过度转换丢失可诊断信息。
    """

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
