"""doctor 命令使用的 storage 与 service 依赖诊断。"""

from __future__ import annotations

import socket
from pathlib import Path
from urllib.parse import urlparse

from agent_harness.config.schemas import HarnessSettings
from agent_harness.storage.migrations.runner import get_current_revision
from agent_harness.storage.settings import storage_dsn_from_settings


def eval_directory_for_profiles(profiles_dir: Path | None) -> Path:
    if profiles_dir is not None:
        return profiles_dir.parent.parent / "eval-cases"
    cwd_candidate = Path.cwd() / "templates" / "service-app" / "eval-cases"
    if cwd_candidate.exists():
        return cwd_candidate
    return Path.cwd() / "eval-cases"


def migration_revision(settings: HarnessSettings, storage_dsn: str | None = None) -> str | None:
    dsn = storage_dsn or storage_dsn_from_settings(settings)
    return get_current_revision(dsn)


def _directory_writable(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    if not path.is_dir():
        return False, "not a directory"

    probe = path / ".agent-harness-doctor.tmp"
    try:
        # doctor 用一个短生命周期 probe 文件检查真实写权限。只看 os.access
        # 容易被 ACL、容器挂载或只读 volume 欺骗。
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"not writable ({exc})"
    return True, "ok"


def eval_directory_status(profiles_dir: Path | None) -> tuple[bool, str, Path]:
    directory = eval_directory_for_profiles(profiles_dir)
    ok, message = _directory_writable(directory)
    return ok, message, directory


def observability_status(settings: HarnessSettings) -> tuple[bool, str]:
    if settings.observability.kind != "local-jsonl":
        return True, f"{settings.observability.kind} configured"

    sink_path = Path(settings.observability.path or ".agent-harness/traces.jsonl")
    parent = sink_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    ok, message = _directory_writable(parent)
    if not ok:
        return False, f"local-jsonl path not writable: {message}"
    return True, f"local-jsonl writable ({sink_path})"


def redis_status(settings: HarnessSettings, timeout_seconds: float = 1.0) -> tuple[bool, str]:
    if settings.queue.kind != "redis":
        return True, "not required"
    if not settings.queue.dsn:
        return False, "missing dsn"

    parsed = urlparse(settings.queue.dsn)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds) as connection:
            connection.sendall(b"*1\r\n$4\r\nPING\r\n")
            response = connection.recv(16)
    except OSError as exc:
        return False, f"unreachable ({exc})"
    if response.startswith(b"+PONG"):
        return True, "ok"
    return False, "bad response"
