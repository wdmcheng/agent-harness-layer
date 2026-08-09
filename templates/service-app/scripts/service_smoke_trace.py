"""把 PostgreSQL inspect 事件导出为受限 service smoke 证据。"""

from __future__ import annotations

import json
import os
import stat
from typing import Any, cast
from uuid import uuid4

from service_smoke_support import assert_smoke_directory_identity


def _write_all(fd: int, payload: bytes) -> None:
    """完整写入一个稳定 fd，并显式处理短写。"""

    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("service smoke trace write made no progress")
        remaining = remaining[written:]


def _copy_fd(source_fd: int, target_fd: int) -> None:
    """在不构造额外 stream wrapper 的情况下复制稳定 fd。"""

    os.lseek(source_fd, 0, os.SEEK_SET)
    while chunk := os.read(source_fd, 1024 * 1024):
        _write_all(target_fd, chunk)


def export_service_trace(
    root_fd: int,
    smoke_fd: int,
    destination_name: str,
    project: str,
) -> None:
    """从稳定目录句柄读取 trace，并原子替换受管根级导出。"""

    temporary_name = f".{destination_name}.{project}.tmp"
    source_fd: int | None = None
    temporary_fd: int | None = None
    try:
        source_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            source_flags |= os.O_NONBLOCK
        source_fd = os.open("trace.jsonl", source_flags, dir_fd=smoke_fd)
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise RuntimeError("service smoke trace source must be a regular file")
        temporary_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            temporary_flags |= os.O_NOFOLLOW
        temporary_fd = os.open(temporary_name, temporary_flags, 0o640, dir_fd=root_fd)
        _copy_fd(source_fd, temporary_fd)
        os.fsync(temporary_fd)
        os.replace(
            temporary_name,
            destination_name,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=root_fd)
        except FileNotFoundError:
            pass


def write_service_trace(env: dict[str, str], completed: dict[str, Any]) -> None:
    """原子写入真实持久化事件，拒绝空事件或非对象事件。

    Service profile 的事件 sink 是 PostgreSQL，配置中的 observability path 不会产生
    local JSONL；CI trace 必须从已经过相关性核验的持久化事件导出，不能伪造空文件。
    """

    for required in (
        "SERVICE_APP_SMOKE_FD",
        "SERVICE_APP_SMOKE_DEVICE",
        "SERVICE_APP_SMOKE_INODE",
    ):
        if required not in env:
            raise RuntimeError("service smoke directory identity is incomplete")
    assert_smoke_directory_identity(env)
    smoke_fd = int(env["SERVICE_APP_SMOKE_FD"])
    opened = os.fstat(smoke_fd)
    if (opened.st_dev, opened.st_ino) != (
        int(env["SERVICE_APP_SMOKE_DEVICE"]),
        int(env["SERVICE_APP_SMOKE_INODE"]),
    ):
        raise RuntimeError("service smoke directory identity changed")
    raw_events = completed.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raise RuntimeError("service smoke PostgreSQL trace is empty")
    events = cast(list[object], raw_events)
    if any(not isinstance(event, dict) for event in events):
        raise RuntimeError("service smoke PostgreSQL trace contains an invalid event")

    trace_name = "trace.jsonl"
    temporary_name = f".{trace_name}.{uuid4().hex}.tmp"
    records = [
        {
            "schema_version": "service-smoke-trace/v1",
            "source": "postgresql",
            "run_id": completed["run_id"],
            "tenant_id": completed["tenant_id"],
            "event": cast(dict[str, Any], event),
        }
        for event in events
    ]
    try:
        content = "".join(
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
            for record in records
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        temporary_fd = os.open(temporary_name, flags, 0o640, dir_fd=smoke_fd)
        try:
            os.fchmod(temporary_fd, 0o640)
            _write_all(temporary_fd, content.encode("utf-8"))
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)
        os.replace(
            temporary_name,
            trace_name,
            src_dir_fd=smoke_fd,
            dst_dir_fd=smoke_fd,
        )
    finally:
        try:
            os.unlink(temporary_name, dir_fd=smoke_fd)
        except FileNotFoundError:
            pass


__all__ = ["export_service_trace", "write_service_trace"]
