"""Filesystem artifact store for local profile evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.security.redaction import redact_secrets


class ArtifactRef(HarnessDTO):
    ref: str
    uri: str
    checksum_sha256: str
    size_bytes: int


class FileArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write_json(self, payload: dict[str, Any]) -> ArtifactRef:
        # artifact 是 trace/eval/audit 的长期证据，写盘前再做一次 redaction。
        # 即使上游 EventBus 漏处理，store 也不能把 secret 原样落地。
        safe_payload = redact_secrets(payload)
        data = json.dumps(safe_payload).encode()
        checksum = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{checksum}.json"
        path.write_bytes(data)
        return ArtifactRef(
            ref=f"artifact://{checksum}",
            uri=str(path),
            checksum_sha256=checksum,
            size_bytes=len(data),
        )

    def read_json(self, ref: str) -> dict[str, Any]:
        checksum = ref.removeprefix("artifact://")
        path = self.root / f"{checksum}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"artifact is not a JSON object: {ref}")
        return cast(dict[str, Any], raw)
