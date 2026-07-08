"""工具输出进入上下文前的截断、脱敏和风险标注。"""

from __future__ import annotations

import json
from typing import Any

from agent_harness.artifacts import FileArtifactStore
from agent_harness.security.redaction import redact_secrets

PROMPT_INJECTION_PATTERNS = (
    "ignore previous instructions",
    "reveal the system prompt",
    "system prompt",
    "developer message",
)


def guarded_tool_payload(
    *,
    tool_name: str,
    invocation_id: str,
    payload: dict[str, Any],
    artifact_store: FileArtifactStore,
    inline_bytes: int,
) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
    """返回可内联 payload、可选 artifact 引用和截断摘要。"""

    safe_payload = redact_secrets(payload)
    encoded = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True).encode()
    detected = _detected_patterns(safe_payload)
    truncation: dict[str, Any] = {
        "original_bytes": len(encoded),
        "inline_bytes": min(len(encoded), inline_bytes),
        "truncated": False,
        "prompt_injection_signals": detected,
    }
    if len(encoded) <= inline_bytes:
        return safe_payload, None, truncation

    artifact = artifact_store.write_json(
        {
            "tool_name": tool_name,
            "invocation_id": invocation_id,
            "payload": safe_payload,
        }
    )
    truncation["truncated"] = True
    return {"artifact_ref": artifact.ref}, artifact.ref, truncation


def write_stream_artifact(
    *,
    artifact_store: FileArtifactStore,
    tool_name: str,
    invocation_id: str,
    stream: str,
    stream_name: str,
    inline_bytes: int,
) -> tuple[str, str | None, dict[str, Any]]:
    """按 stream 维度截断 Shell stdout/stderr，并在需要时写 artifact。"""

    safe_stream = redact_secrets(stream)
    encoded = safe_stream.encode()
    if len(encoded) <= inline_bytes:
        return safe_stream, None, {"truncated": False, "original_bytes": len(encoded)}

    artifact = artifact_store.write_json(
        {
            "tool_name": tool_name,
            "invocation_id": invocation_id,
            "stream": stream_name,
            "content": safe_stream,
        }
    )
    truncated = encoded[:inline_bytes].decode(errors="ignore")
    return (
        truncated,
        artifact.ref,
        {
            "truncated": True,
            "original_bytes": len(encoded),
            "inline_bytes": len(truncated.encode()),
        },
    )


def _detected_patterns(value: Any) -> list[str]:
    text = json.dumps(value, ensure_ascii=False).lower()
    return [pattern for pattern in PROMPT_INJECTION_PATTERNS if pattern in text]
