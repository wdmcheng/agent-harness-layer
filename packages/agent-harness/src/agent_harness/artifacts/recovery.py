"""Artifact claim journal、可信 event path 与硬退出恢复。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

PENDING_VERSION = 1
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ArtifactClaimRecoveryError(RuntimeError):
    """待完成 artifact claim 无法安全恢复；错误不得暴露 journal 内容。

    journal 中含有文件坐标和校验信息。任何结构、路径或完整性异常都统一映射为该错误，
    既阻止继续猜测恢复，也避免把本地诊断事实泄露到上层调用方。
    """


class ArtifactRecoveryMixin:
    """恢复 artifact/event 跨文件原子写入留下的耐久 journal。

    artifact 内容和 JSONL event 不能由单一文件操作原子提交，因此写入期间先记录最小
    恢复事实。启动时仅依据 journal、可信事件路径和 checksum 判断提交结果，绝不以
    目录扫描或猜测的外部文件作为恢复依据。
    """

    root: Path

    @contextmanager
    def _content_lock(self, checksum: str) -> Generator[None, None, None]:
        """由具体 store 提供 checksum 级跨进程锁，串行化同一内容的恢复与写入。"""

        raise NotImplementedError
        yield  # pragma: no cover - 仅用于保持 generator 类型

    @property
    def _pending_dir(self) -> Path:
        """返回仅存放本 store 恢复 journal 的受控目录，不能与用户 artifact 混用。"""

        return self.root / ".pending-artifact-claims"

    def _pending_path(self, checksum: str) -> Path:
        """由已校验的内容摘要定位唯一 journal 文件，不接受任意外部路径片段。"""

        return self._pending_dir / f"{checksum}.json"

    @property
    def _trusted_event_paths_path(self) -> Path:
        """返回记录可恢复事件文件白名单的私有注册表路径。"""

        return self._pending_dir / ".trusted-event-paths"

    def _recover_all_pending(self) -> None:
        """只扫描 store 自己的待完成目录；未知条目一律 fail closed。

        每个 checksum 在内容锁内独立恢复，避免两个进程同时截断事件文件或同时清理同一
        journal；注册表临时文件则通过全局注册表锁串行处理。
        """

        pending_dir = self._pending_dir
        if not pending_dir.exists():
            return
        if not pending_dir.is_dir():
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        entries = sorted(pending_dir.iterdir(), key=lambda item: item.name)
        for entry in entries:
            # 注册表必须先完整解析；存在但不可读时不能继续信任任何 journal 路径。
            if entry == self._trusted_event_paths_path:
                self._load_trusted_event_paths()
                continue
            if entry.name.startswith(".registry.") and entry.suffix == ".tmp":
                # 原子替换前残留的注册表临时文件从未成为真相，可在同一锁内安全删除。
                with self._registry_lock():
                    entry.unlink(missing_ok=True)
                    self._fsync_directory(pending_dir)
                continue
            checksum = self._checksum_from_pending_entry(entry)
            with self._content_lock(checksum):
                if entry.suffix == ".tmp":
                    # 内容 journal 的临时文件同样未提交，不能尝试解析其部分写入内容。
                    entry.unlink(missing_ok=True)
                    self._fsync_directory(pending_dir)
                    continue
                self._recover_checksum_pending_unlocked(checksum)

    def _checksum_from_pending_entry(self, entry: Path) -> str:
        """从受控目录条目提取合法 SHA-256 摘要，拒绝目录、陌生名称和路径穿越入口。"""

        if entry.is_dir():
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        if entry.suffix == ".json":
            checksum = entry.stem
        elif entry.suffix == ".tmp" and entry.name.startswith("."):
            checksum = entry.name[1:].split(".", 1)[0]
        else:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        if SHA256_PATTERN.fullmatch(checksum) is None:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        return checksum

    @contextmanager
    def _registry_lock(self) -> Generator[None, None, None]:
        """取得根目录级注册表锁，保护可信事件路径集合的读取、更新与清理。"""

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
        """把已解析的绝对事件文件路径耐久登记为允许恢复的唯一位置。"""

        with self._registry_lock():
            paths = self._load_trusted_event_paths()
            canonical = str(event_path)
            if canonical in paths:
                return
            paths.add(canonical)
            # 以替换式写入更新整个白名单，避免硬退出留下半行或部分 JSON。
            self._atomic_write_json(
                self._trusted_event_paths_path,
                {"version": 1, "event_paths": sorted(paths)},
                temporary_prefix=".registry.",
            )

    def _load_trusted_event_paths(self) -> set[str]:
        """读取并严格验证可信事件路径白名单，返回规范化绝对路径集合。"""

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
            # 注册表只接受已规范化的绝对路径，防止相对路径在恢复时指向工作目录之外。
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
        """在内容锁内写入最小 journal，记录 artifact 创建和事件 append 前的坐标。

        journal 只保存路径与 event id 的摘要，恢复时通过注册表反查可信路径，避免把
        原始本地路径或事件标识直接复制进可长期留存的恢复文件。
        """

        journal = {
            "version": PENDING_VERSION,
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
        """在已持有内容锁的条件下恢复或清理单个 checksum 的待完成写入。"""

        journal_path = self._pending_path(checksum)
        if not journal_path.exists():
            return
        journal = self._load_pending_journal(journal_path, expected_checksum=checksum)
        committed = self._event_is_committed(journal)
        artifact_path = self.root / f"{checksum}.json"
        if committed:
            # event 已证明提交时，artifact 必须存在且仍与内容地址一致，不能补造或覆盖。
            if not artifact_path.exists():
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
            if hashlib.sha256(artifact_path.read_bytes()).hexdigest() != checksum:
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        if not committed and journal["created"] and artifact_path.exists():
            # 仅删除由本次未提交 claim 创建且内容仍可校验的 artifact，避免误删已有内容。
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
        """读取并封闭式校验单个 journal，确保恢复只基于完整且可解释的事实。"""

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
            journal.get("version") != PENDING_VERSION
            or isinstance(journal.get("version"), bool)
            or not isinstance(event_path_sha256, str)
            or SHA256_PATTERN.fullmatch(event_path_sha256) is None
            or not isinstance(event_id_sha256, str)
            or SHA256_PATTERN.fullmatch(event_id_sha256) is None
            or not isinstance(event_size_before, int)
            or isinstance(event_size_before, bool)
            or event_size_before < 0
            or checksum != expected_checksum
            or not isinstance(checksum, str)
            or SHA256_PATTERN.fullmatch(checksum) is None
            or not isinstance(created, bool)
        ):
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        event_path = self._trusted_event_path_for_hash(event_path_sha256)
        # 路径可以尚未创建，但若存在则必须是普通文件，目录或设备文件不能参与截断。
        if event_path.exists() and not event_path.is_file():
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        return journal

    def _event_is_committed(self, journal: dict[str, Any]) -> bool:
        """根据 append 前大小和首条新增事件判断 journal 对应的 event 是否已提交。

        只认可与 journal 双摘要、payload checksum 和 artifact 引用全部匹配的首条事件；
        其余新增行只需保持 JSON 对象，以保留同一 append 后续可见的事件事实。
        """

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
            # 首条新增事件是 claim 的提交见证，必须同时绑定原 event、artifact 与 checksum。
            raw_event_id = event.get("event_id")
            if (
                not isinstance(raw_event_id, str)
                or hashlib.sha256(raw_event_id.encode()).hexdigest() != event_id_sha256
                or event.get("payload_checksum") != checksum
                or event.get("payload_ref") != f"artifact://{checksum}"
            ):
                raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
            for raw_line in raw_lines[1:]:
                # 同一 append 后的其他事件不参与 claim 身份，但也不能是损坏 JSON。
                if not isinstance(json.loads(raw_line), dict):
                    raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed") from exc
        self._fsync_file(event_path)
        self._fsync_directory(event_path.parent)
        return True

    def _truncate_event_file(self, event_path: Path, size: int) -> None:
        """将硬退出留下的单行前缀回滚到 journal 记录的 append 前长度并刷盘。"""

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
        """强制单个文件数据进入底层存储，供 journal 与 event 提交顺序使用。"""

        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _trusted_event_path_for_hash(self, event_path_sha256: str) -> Path:
        """从可信白名单中反查唯一匹配摘要的事件路径，拒绝零个或多个候选。"""

        matches = [
            raw_path
            for raw_path in self._load_trusted_event_paths()
            if hashlib.sha256(raw_path.encode()).hexdigest() == event_path_sha256
        ]
        if len(matches) != 1:
            raise ArtifactClaimRecoveryError("artifact pending claim recovery failed")
        return Path(matches[0])

    def _clear_pending_journal_unlocked(self, checksum: str) -> None:
        """在内容锁内删除已经恢复完毕的 journal，并同步目录元数据。"""

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
        """以同目录临时文件、文件 fsync 和原子替换写入恢复元数据。

        同目录创建保证 ``os.replace`` 不跨文件系统；无论替换是否成功，finally 都清理
        临时路径，防止下一次恢复把写入残留误认成正式 journal。
        """

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
            # replace 后临时路径已不存在；missing_ok 也覆盖异常路径中的清理。
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """同步目录项，确保创建、替换或删除的文件名在硬退出后可被重新发现。"""

        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "ArtifactClaimRecoveryError",
    "ArtifactRecoveryMixin",
    "SHA256_PATTERN",
]
