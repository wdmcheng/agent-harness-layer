"""Service smoke 的受信 wheel 复制、摘要校验与原子发布。"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from uuid import uuid4

APP_ROOT = Path(__file__).resolve().parents[1]


def _copy_fd(source_fd: int, target_fd: int) -> None:
    """不创建额外 stream wrapper，完整复制稳定 fd 并处理短写。"""

    os.lseek(source_fd, 0, os.SEEK_SET)
    while chunk := os.read(source_fd, 1024 * 1024):
        remaining = memoryview(chunk)
        while remaining:
            written = os.write(target_fd, remaining)
            if written <= 0:
                raise OSError("managed wheel copy made no progress")
            remaining = remaining[written:]


def _managed_file_digest(root_fd: int, name: str) -> bytes:
    """通过受管根句柄读取普通文件摘要，拒绝 symlink 或目录替换。"""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    file_fd = os.open(name, flags, dir_fd=root_fd)
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise RuntimeError("managed wheel entry must be a regular file")
        digest = hashlib.sha256()
        os.lseek(file_fd, 0, os.SEEK_SET)
        while chunk := os.read(file_fd, 1024 * 1024):
            digest.update(chunk)
        return digest.digest()
    finally:
        os.close(file_fd)


def _republish_managed_wheel(root_fd: int, name: str) -> None:
    """从先打开的稳定 fd 复制并原子替换已存在 wheel，关闭 stat/use 竞态。"""

    source_flags = os.O_RDONLY
    target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
        target_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        source_flags |= os.O_NONBLOCK
    try:
        source_fd = os.open(name, source_flags, dir_fd=root_fd)
    except OSError:
        raise RuntimeError("managed wheel entry must be a regular file") from None
    temporary_name = f".{name}.{uuid4().hex}.republish"
    target_fd: int | None = None
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise RuntimeError("managed wheel entry must be a regular file")
        target_fd = os.open(temporary_name, target_flags, 0o644, dir_fd=root_fd)
        os.fchmod(target_fd, 0o644)
        _copy_fd(source_fd, target_fd)
        os.fsync(target_fd)
        os.replace(
            temporary_name,
            name,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
    finally:
        if target_fd is not None:
            os.close(target_fd)
        os.close(source_fd)
        try:
            os.unlink(temporary_name, dir_fd=root_fd)
        except FileNotFoundError:
            pass


def prepare_core_wheel() -> None:
    """保证镜像只能从标准 wheel 入口安装 core，不能读取 workspace source。"""

    app_root = APP_ROOT.resolve(strict=True)
    managed_root = APP_ROOT / ".agent-harness"
    if managed_root.is_symlink():
        raise RuntimeError("managed wheel directory must not be a symbolic link")
    managed_root.mkdir(mode=0o700, exist_ok=True)
    if managed_root.resolve(strict=True) != app_root / ".agent-harness":
        raise RuntimeError("managed wheel directory must remain inside APP_ROOT")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    root_fd = os.open(managed_root, directory_flags)
    try:
        opened = os.fstat(root_fd)
        current = managed_root.stat(follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("managed wheel directory changed while it was being opened")
        os.fchmod(root_fd, 0o700)

        matches = [
            name
            for name in os.listdir(root_fd)
            if name.startswith("agent_harness-") and name.endswith(".whl")
        ]
        if matches:
            if len(matches) != 1:
                raise RuntimeError("managed wheel directory contains ambiguous wheel entries")
            _republish_managed_wheel(root_fd, matches[0])
            return

        source_value = os.environ.get("AGENT_HARNESS_SOURCE", "").strip()
        source = Path(source_value).expanduser().resolve() if source_value else None
        if (
            source is None
            or not source.is_file()
            or not source.name.startswith("agent_harness-")
            or source.suffix != ".whl"
        ):
            raise RuntimeError(
                "smoke-service requires .agent-harness/agent_harness-*.whl or "
                "AGENT_HARNESS_SOURCE=/path/to/agent_harness-0.1.0.whl"
            )

        source_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            source_flags |= os.O_NONBLOCK
        source_fd = os.open(source, source_flags)
        target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            target_flags |= os.O_NOFOLLOW
        temporary_name = f".{source.name}.{uuid4().hex}.tmp"
        target_fd: int | None = None
        try:
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                raise RuntimeError("AGENT_HARNESS_SOURCE must be a regular wheel file")
            target_fd = os.open(temporary_name, target_flags, 0o644, dir_fd=root_fd)
            os.fchmod(target_fd, 0o644)
            _copy_fd(source_fd, target_fd)
            os.fsync(target_fd)
            try:
                os.link(
                    temporary_name,
                    source.name,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                published = os.stat(source.name, dir_fd=root_fd, follow_symlinks=False)
                if not stat.S_ISREG(published.st_mode):
                    raise RuntimeError("managed wheel entry must be a regular file") from None
                if _managed_file_digest(root_fd, temporary_name) != _managed_file_digest(
                    root_fd,
                    source.name,
                ):
                    raise RuntimeError("published wheel does not match the trusted wheel") from None
        finally:
            os.close(source_fd)
            if target_fd is not None:
                os.close(target_fd)
            try:
                os.unlink(temporary_name, dir_fd=root_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(root_fd)


__all__ = ["prepare_core_wheel"]
