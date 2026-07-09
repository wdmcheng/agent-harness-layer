"""观测 provider 前的统一脱敏入口。"""

from __future__ import annotations

from typing import Any

from agent_harness.security.redaction import redact_secrets


def redact_telemetry_payload(value: Any) -> Any:
    """脱敏 trace、eval-like、audit-like、error 和 provider payload。

    观测面会长期保留证据，也可能发送到外部 SaaS；统一入口可以避免每个
    provider adapter 各自实现一套不一致的 secret 过滤规则。
    """

    return redact_secrets(value)
