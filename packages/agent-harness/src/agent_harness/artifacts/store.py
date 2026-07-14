"""local profile 使用的文件系统 artifact store。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.security.redaction import redact_secrets

_PENDING_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ArtifactClaimRecoveryError(RuntimeError):
    """pending artifact claim 无法安全恢复；错误不得暴露 journal 内容。"""


class _ArtifactClaimState(threading.local):
    """每线程记录当前 store 已持有的 checksum 锁，避免 claim 内重复加锁。"""

    def __init__(self) -> None:
        self.checksums: set[str] = set()


class ArtifactRef(HarnessDTO):
    """事件、trace 和 eval 只暴露的 artifact 元数据，不暴露 payload。"""

    ref: str
    uri: str
    checksum_sha256: str
    size_bytes: int


class FileArtifactStore:
    """按内容 hash 写入 JSON artifact，供本地 trace/eval 复用。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._claim_state = _ArtifactClaimState()
        self._recover_all_pending()

    def write_json(self, payload: dict[str, Any]) -> ArtifactRef:
        """脱敏后写入 JSON payload，并返回可验证 checksum 的引用。"""

        # artifact 是 trace/eval/audit 的长期证据，写盘前再做一次 redaction。
        # 即使上游 EventBus 漏处理，store 也不能把 secret 原样落地。
        safe_payload = redact_secrets(payload)
        data = json.dumps(safe_payload).encode()
        checksum = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{checksum}.json"
        if checksum in self._active_claims:
            return self._write_json_unlocked(path=path, checksum=checksum, data=data)
        with self._content_lock(checksum):
            self._recover_checksum_pending_unlocked(checksum)
            return self._write_json_unlocked(path=path, checksum=checksum, data=data)

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
        safe_payload = redact_secrets(payload)
        data = json.dumps(safe_payload).encode()
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

    @property
    def _pending_dir(self) -> Path:
        return self.root / ".pending-artifact-claims"

    def _pending_path(self, checksum: str) -> Path:
        return self._pending_dir / f"{checksum}.json"

    @property
    def _trusted_event_paths_path(self) -> Path:
        return self._pending_dir / ".trusted-event-paths"

    def _recover_all_pending(self) -> None:
        """只扫描 store 自己的 pending 目录；未知条目一律 fail closed。"""

        pending_dir = self._pending_dir
        if not pending_dir.exists():
            return
        if not pending_dir.is_dir():
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        entries = sorted(pending_dir.iterdir(), key=lambda item: item.name)
        for entry in entries:
            if entry == self._trusted_event_paths_path:
                self._load_trusted_event_paths()
                continue
            if entry.name.startswith(".registry.") and entry.suffix == ".tmp":
                with self._registry_lock():
                    entry.unlink(missing_ok=True)
                    self._fsync_directory(pending_dir)
                continue
            checksum = self._checksum_from_pending_entry(entry)
            with self._content_lock(checksum):
                if entry.suffix == ".tmp":
                    entry.unlink(missing_ok=True)
                    self._fsync_directory(pending_dir)
                    continue
                self._recover_checksum_pending_unlocked(checksum)

    def _checksum_from_pending_entry(self, entry: Path) -> str:
        if entry.is_dir():
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        if entry.suffix == ".json":
            checksum = entry.stem
        elif entry.suffix == ".tmp" and entry.name.startswith("."):
            checksum = entry.name[1:].split(".", 1)[0]
        else:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        if _SHA256_PATTERN.fullmatch(checksum) is None:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        return checksum

    @contextmanager
    def _registry_lock(self) -> Generator[None, None, None]:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        root_token = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:16]
        lock_path = self.root.parent / f".{self.root.name}.{root_token}.artifact-registry.lock"
        with lock_path.open("a+b") as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _register_event_path(self, event_path: Path) -> None:
        with self._registry_lock():
            paths = self._load_trusted_event_paths()
            canonical = str(event_path)
            if canonical in paths:
                return
            paths.add(canonical)
            self._atomic_write_json(
                self._trusted_event_paths_path,
                {"version": 1, "event_paths": sorted(paths)},
                temporary_prefix=".registry.",
            )

    def _load_trusted_event_paths(self) -> set[str]:
        path = self._trusted_event_paths_path
        if not path.exists():
            return set()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed") from exc
        if not isinstance(raw, dict):
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        registry = cast(dict[str, Any], raw)
        if set(registry) != {"version", "event_paths"}:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        raw_paths = registry.get("event_paths")
        if registry.get("version") != 1 or not isinstance(raw_paths, list):
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        trusted: set[str] = set()
        for raw_path in cast(list[Any], raw_paths):
            if not isinstance(raw_path, str) or not raw_path:
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
            path_value = Path(raw_path)
            if not path_value.is_absolute() or str(path_value.resolve()) != raw_path:
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
            if raw_path in trusted:
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
            trusted.add(raw_path)
        return trusted

    def _write_pending_journal_unlocked(
        self,
        *,
        checksum: str,
        event_path: Path,
        event_id: str,
        event_size_before: int,
        created: bool,
    ) -> None:
        journal = {
            "version": _PENDING_VERSION,
            "event_path_sha256": hashlib.sha256(str(event_path).encode()).hexdigest(),
            "event_id_sha256": hashlib.sha256(event_id.encode()).hexdigest(),
            "event_size_before": event_size_before,
            "checksum": checksum,
            "created": created,
        }
        self._atomic_write_json(
            self._pending_path(checksum),
            journal,
            temporary_prefix=f".{checksum}.",
        )

    def _recover_checksum_pending_unlocked(self, checksum: str) -> None:
        journal_path = self._pending_path(checksum)
        if not journal_path.exists():
            return
        journal = self._load_pending_journal(journal_path, expected_checksum=checksum)
        committed = self._event_is_committed(journal)
        artifact_path = self.root / f"{checksum}.json"
        if committed:
            if not artifact_path.exists():
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
            if hashlib.sha256(artifact_path.read_bytes()).hexdigest() != checksum:
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        if not committed and journal["created"] and artifact_path.exists():
            artifact_data = artifact_path.read_bytes()
            if hashlib.sha256(artifact_data).hexdigest() != checksum:
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
            artifact_path.unlink()
            self._fsync_directory(self.root)
        self._clear_pending_journal_unlocked(checksum)

    def _load_pending_journal(
        self,
        path: Path,
        *,
        expected_checksum: str,
    ) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed") from exc
        if not isinstance(raw, dict):
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        journal = cast(dict[str, Any], raw)
        if set(journal) != {
            "version",
            "event_path_sha256",
            "event_id_sha256",
            "event_size_before",
            "checksum",
            "created",
        }:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        event_path_sha256 = journal.get("event_path_sha256")
        event_id_sha256 = journal.get("event_id_sha256")
        event_size_before = journal.get("event_size_before")
        checksum = journal.get("checksum")
        created = journal.get("created")
        if (
            journal.get("version") != _PENDING_VERSION
            or isinstance(journal.get("version"), bool)
            or not isinstance(event_path_sha256, str)
            or _SHA256_PATTERN.fullmatch(event_path_sha256) is None
            or not isinstance(event_id_sha256, str)
            or _SHA256_PATTERN.fullmatch(event_id_sha256) is None
            or not isinstance(event_size_before, int)
            or isinstance(event_size_before, bool)
            or event_size_before < 0
            or checksum != expected_checksum
            or not isinstance(checksum, str)
            or _SHA256_PATTERN.fullmatch(checksum) is None
            or not isinstance(created, bool)
        ):
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        event_path = self._trusted_event_path_for_hash(event_path_sha256)
        if event_path.exists() and not event_path.is_file():
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        return journal

    def _event_is_committed(self, journal: dict[str, Any]) -> bool:
        event_path = self._trusted_event_path_for_hash(cast(str, journal["event_path_sha256"]))
        event_size_before = cast(int, journal["event_size_before"])
        if not event_path.exists():
            if event_size_before != 0:
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
            return False
        event_id_sha256 = cast(str, journal["event_id_sha256"])
        checksum = cast(str, journal["checksum"])
        try:
            data = event_path.read_bytes()
        except OSError as exc:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed") from exc
        if len(data) < event_size_before:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        suffix = data[event_size_before:]
        if not suffix:
            return False
        # Local sink 每个 claim 只 append 一行。没有换行结尾表示硬退出留下的
        # 单行前缀；在 checksum 锁内恢复原长度，再按未提交处理。
        if not suffix.endswith(b"\n"):
            if b"\n" in suffix:
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
            self._truncate_event_file(event_path, event_size_before)
            return False
        try:
            raw_lines = [line for line in suffix.decode().splitlines() if line]
            if not raw_lines:
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
            first_raw = json.loads(raw_lines[0])
            if not isinstance(first_raw, dict):
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
            event = cast(dict[str, Any], first_raw)
            raw_event_id = event.get("event_id")
            if (
                not isinstance(raw_event_id, str)
                or hashlib.sha256(raw_event_id.encode()).hexdigest() != event_id_sha256
                or event.get("payload_checksum") != checksum
                or event.get("payload_ref") != f"artifact://{checksum}"
            ):
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
            for raw_line in raw_lines[1:]:
                if not isinstance(json.loads(raw_line), dict):
                    raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed") from exc
        self._fsync_file(event_path)
        self._fsync_directory(event_path.parent)
        return True

    def _truncate_event_file(self, event_path: Path, size: int) -> None:
        try:
            with event_path.open("r+b") as file:
                file.truncate(size)
                file.flush()
                os.fsync(file.fileno())
            self._fsync_directory(event_path.parent)
        except OSError as exc:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed") from exc

    @staticmethod
    def _fsync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _trusted_event_path_for_hash(self, event_path_sha256: str) -> Path:
        matches = [
            raw_path
            for raw_path in self._load_trusted_event_paths()
            if hashlib.sha256(raw_path.encode()).hexdigest() == event_path_sha256
        ]
        if len(matches) != 1:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        return Path(matches[0])

    def _clear_pending_journal_unlocked(self, checksum: str) -> None:
        journal_path = self._pending_path(checksum)
        if not journal_path.exists():
            return
        journal_path.unlink()
        self._fsync_directory(self._pending_dir)

    def _atomic_write_json(
        self,
        path: Path,
        payload: dict[str, Any],
        *,
        temporary_prefix: str,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=temporary_prefix,
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
            self._fsync_directory(path.parent)
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

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
        return ArtifactRef(
            ref=f"artifact://{checksum}",
            uri=str(path),
            checksum_sha256=checksum,
            size_bytes=len(data),
        )

    def read_json(self, ref: str) -> dict[str, Any]:
        """按 artifact ref 读取 JSON object；非 object payload 视为损坏数据。"""

        checksum = ref.removeprefix("artifact://")
        if _SHA256_PATTERN.fullmatch(checksum) is None:
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
