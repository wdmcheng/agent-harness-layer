"""local profile 使用的文件系统 artifact store。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from agent_harness.artifacts.recovery import SHA256_PATTERN, ArtifactRecoveryMixin
from agent_harness.artifacts.recovery import (
    ArtifactClaimRecoveryError as ArtifactClaimRecoveryError,
)
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.security.redaction import redact_secrets


class _ArtifactClaimState(threading.local):
    """每线程记录当前 store 已持有的 checksum 锁，避免 claim 内重复加锁。"""

    def __init__(self) -> None:
        """为线程首次访问创建独立的可重入 checksum 集合。"""

        self.checksums: set[str] = set()


class ArtifactRef(HarnessDTO):
    """事件、trace 和 eval 只暴露的 artifact 元数据，不暴露 payload。"""

    ref: str
    uri: str
    checksum_sha256: str
    size_bytes: int


class FileArtifactStore(ArtifactRecoveryMixin):
    """按内容 hash 写入 JSON artifact，供本地 trace/eval 复用。"""

    def __init__(self, root: Path) -> None:
        """设置 artifact 根目录并在任何写入前恢复已登记的崩溃中断 claim。"""

        self.root = root
        self._claim_state = _ArtifactClaimState()
        self._recover_all_pending()

    def write_json(self, payload: dict[str, Any]) -> ArtifactRef:
        """脱敏后写入 JSON payload，并返回可验证 checksum 的引用。"""

        data = self._json_bytes(payload)
        checksum = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{checksum}.json"
        if checksum in self._active_claims:
            return self._write_json_unlocked(path=path, checksum=checksum, data=data)
        with self._content_lock(checksum):
            self._recover_checksum_pending_unlocked(checksum)
            return self._write_json_unlocked(path=path, checksum=checksum, data=data)

    def reference_json(self, payload: dict[str, Any]) -> ArtifactRef:
        """只计算脱敏内容寻址引用，不创建目录或写入 artifact。"""

        data = self._json_bytes(payload)
        checksum = hashlib.sha256(data).hexdigest()
        return self._artifact_ref(checksum=checksum, size_bytes=len(data))

    def recover_pending(self) -> None:
        """显式受控操作入口；只恢复本 store 已登记的 pending claim。"""

        self._recover_all_pending()

    @contextmanager
    def claim_json(
        self,
        payload: dict[str, Any],
        *,
        event_path: Path,
        event_id: str,
        event_size_before: int,
    ) -> Generator[ArtifactRef, None, None]:
        """以持久 pending journal 跨硬退出保护 artifact/event 原子性。

        journal 在 artifact 可见前 durable；Local sink append+fsync 成功后，正常
        退出上下文才清除 journal。异常会立即走相同恢复路径，硬退出则由下一次
        store 启动或同 checksum 受控操作恢复。预存 artifact 永远不删除。
        """

        canonical_event_path = event_path.expanduser().resolve()
        if not event_id:
            raise ValueError("event_id is required for artifact claim")
        if event_size_before < 0:
            raise ValueError("event_size_before must be non-negative")
        data = self._json_bytes(payload)
        checksum = hashlib.sha256(data).hexdigest()
        path = self.root / f"{checksum}.json"
        with self._content_lock(checksum):
            self._recover_checksum_pending_unlocked(checksum)
            existed = path.exists()
            if existed and path.read_bytes() != data:
                raise RuntimeError("artifact checksum path contains different content")
            self._register_event_path(canonical_event_path)
            self._write_pending_journal_unlocked(
                checksum=checksum,
                event_path=canonical_event_path,
                event_id=event_id,
                event_size_before=event_size_before,
                created=not existed,
            )
            try:
                artifact = self.write_json(payload)
                if artifact.ref != f"artifact://{checksum}" or artifact.checksum_sha256 != checksum:
                    raise RuntimeError("artifact materialization returned inconsistent metadata")
                yield artifact
            except BaseException:
                try:
                    self._recover_checksum_pending_unlocked(checksum)
                except Exception as recovery_error:
                    raise ArtifactClaimRecoveryError(
                        "artifact pending claim recovery failed"
                    ) from recovery_error
                raise
            else:
                self._clear_pending_journal_unlocked(checksum)

    @property
    def _active_claims(self) -> set[str]:
        """暴露当前线程持有的锁集合，仅供内部避免同 checksum 的嵌套死锁。"""

        return self._claim_state.checksums

    @contextmanager
    def _content_lock(self, checksum: str) -> Generator[None, None, None]:
        """用稳定 lock inode 串行化跨实例、跨进程的同内容 materialize。"""

        if checksum in self._active_claims:
            yield
            return
        self.root.parent.mkdir(parents=True, exist_ok=True)
        root_token = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:16]
        lock_path = self.root.parent / f".{self.root.name}.{root_token}.{checksum}.artifact.lock"
        with lock_path.open("a+b") as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._active_claims.add(checksum)
            try:
                yield
            finally:
                self._active_claims.discard(checksum)
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _write_json_unlocked(self, *, path: Path, checksum: str, data: bytes) -> ArtifactRef:
        """在 checksum 锁内原子发布内容，避免崩溃暴露半写 artifact。"""

        if path.exists():
            if path.read_bytes() != data:
                raise RuntimeError("artifact checksum path contains different content")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.root,
                prefix=f".{checksum}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary_path, path)
                self._fsync_directory(self.root)
            finally:
                temporary_path.unlink(missing_ok=True)
        return self._artifact_ref(checksum=checksum, size_bytes=len(data))

    def _artifact_ref(self, *, checksum: str, size_bytes: int) -> ArtifactRef:
        """从内容寻址元数据构造引用；调用方不得据此假定文件已经 materialize。"""

        return ArtifactRef(
            ref=f"artifact://{checksum}",
            uri=str(self.root / f"{checksum}.json"),
            checksum_sha256=checksum,
            size_bytes=size_bytes,
        )

    @staticmethod
    def _json_bytes(payload: dict[str, Any]) -> bytes:
        """统一 preview、claim 与 materialize 的脱敏 canonical JSON。"""

        # artifact 是 trace/eval/audit 的长期证据，写盘前再做一次 redaction。
        # 即使上游漏处理，store 也不能把 secret 原样落地。
        return json.dumps(
            redact_secrets(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    def read_json(self, ref: str) -> dict[str, Any]:
        """按 artifact ref 读取 JSON object；非 object payload 视为损坏数据。"""

        checksum = ref.removeprefix("artifact://")
        if SHA256_PATTERN.fullmatch(checksum) is None:
            raise ValueError("artifact ref checksum is invalid")
        path = self.root / f"{checksum}.json"
        if checksum in self._active_claims:
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            with self._content_lock(checksum):
                self._recover_checksum_pending_unlocked(checksum)
                raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"artifact is not a JSON object: {ref}")
        return cast(dict[str, Any], raw)
