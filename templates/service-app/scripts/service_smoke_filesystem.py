"""Service smoke 受管目录的 no-follow 打开、身份验证与递归清理原语。"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from pathlib import Path
from uuid import uuid4


def rename_directory_no_replace(parent_fd: int, source: str, target: str) -> None:
    """以平台原生排他原语发布目录，绝不用 check-then-rename 模拟。"""

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "linux":
        rename = getattr(libc, "renameat2", None)
        flag = 1  # RENAME_NOREPLACE
    elif sys.platform == "darwin":
        rename = getattr(libc, "renameatx_np", None)
        flag = 4  # RENAME_EXCL
    else:
        raise RuntimeError("exclusive directory publication is unsupported")
    if rename is None:
        raise RuntimeError("exclusive directory publication is unavailable")
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(parent_fd, source_bytes, parent_fd, target_bytes, flag) == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), target)
    raise OSError(error_number, os.strerror(error_number), target)


def directory_open_flags() -> int:
    """返回拒绝最终 symlink 的目录打开标志。"""

    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def managed_smoke_root(app_root: Path, *, create: bool = True) -> Path:
    """验证受管根是 app root 下的真实目录，绝不接受符号链接。"""

    resolved_app_root = app_root.resolve(strict=True)
    managed_root = app_root / ".agent-harness"
    if managed_root.is_symlink():
        raise RuntimeError("managed smoke root must not be a symbolic link")
    if create:
        managed_root.mkdir(mode=0o700, exist_ok=True)
    if not managed_root.is_dir() or managed_root.is_symlink():
        raise RuntimeError("managed smoke root must be a real directory")
    if managed_root.resolve(strict=True) != resolved_app_root / ".agent-harness":
        raise RuntimeError("managed smoke root must remain inside APP_ROOT")
    return managed_root


def open_managed_root(app_root: Path) -> tuple[Path, int]:
    """打开稳定的受管根句柄，并验证它仍与路径指向同一目录。"""

    managed_root = managed_smoke_root(app_root, create=False)
    root_fd = os.open(managed_root, directory_open_flags())
    try:
        opened = os.fstat(root_fd)
        current = managed_root.stat(follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError("managed smoke root changed while it was being opened")
        os.fchmod(root_fd, 0o700)
    except BaseException:
        os.close(root_fd)
        raise
    return managed_root, root_fd


def managed_smoke_directory(app_root: Path, project: str) -> Path:
    """返回待独占创建的本轮目录，拒绝受管根 symlink 与既有项目目录。"""

    managed_root = managed_smoke_root(app_root)
    smoke_dir = managed_root / project
    if smoke_dir.parent != managed_root:
        raise RuntimeError("service smoke directory must remain inside managed smoke directory")
    if smoke_dir.exists() or smoke_dir.is_symlink():
        raise RuntimeError("service smoke project directory already exists")
    return smoke_dir


def open_smoke_directory(
    app_root: Path,
    smoke_dir: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """从受管根打开本轮目录，并按需绑定创建时的设备号与 inode。"""

    managed_root, root_fd = open_managed_root(app_root)
    if smoke_dir.parent != managed_root:
        os.close(root_fd)
        raise RuntimeError("path is outside managed smoke directory")
    try:
        smoke_fd = os.open(smoke_dir.name, directory_open_flags(), dir_fd=root_fd)
    except BaseException:
        os.close(root_fd)
        raise RuntimeError("managed smoke directory is unavailable or unsafe") from None
    try:
        opened = os.fstat(smoke_fd)
        if expected_identity is not None and (opened.st_dev, opened.st_ino) != expected_identity:
            raise RuntimeError("managed smoke directory identity changed")
    except BaseException:
        os.close(smoke_fd)
        os.close(root_fd)
        raise
    return root_fd, smoke_fd


def write_private_file(
    app_root: Path,
    path: Path,
    content: str,
    *,
    mode: int,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    """相对 no-follow 目录句柄独占创建文件，失败时清空并删除本轮入口。"""

    root_fd, directory_fd = open_smoke_directory(
        app_root,
        path.parent,
        expected_identity=expected_identity,
    )
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    file_fd: int | None = None
    file_identity: tuple[int, int] | None = None
    completed = False
    try:
        try:
            file_fd = os.open(path.name, file_flags, mode, dir_fd=directory_fd)
        except FileExistsError:
            raise RuntimeError(f"private smoke file already exists: {path.name}") from None
        opened = os.fstat(file_fd)
        file_identity = (opened.st_dev, opened.st_ino)
        try:
            os.fchmod(file_fd, mode)
            duplicate_fd = os.dup(file_fd)
            try:
                stream = os.fdopen(duplicate_fd, "w", encoding="utf-8")
            except BaseException:
                os.close(duplicate_fd)
                raise
            with stream:
                stream.write(content)
            completed = True
        except BaseException:
            os.ftruncate(file_fd, 0)
            raise
    finally:
        try:
            if file_fd is not None:
                if file_identity is None:
                    try:
                        opened = os.fstat(file_fd)
                        file_identity = (opened.st_dev, opened.st_ino)
                    except OSError:
                        pass
                os.close(file_fd)
            if file_identity is not None:
                try:
                    current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if (
                    not completed
                    and current is not None
                    and (current.st_dev, current.st_ino) == file_identity
                ):
                    os.unlink(path.name, dir_fd=directory_fd)
        finally:
            try:
                os.close(directory_fd)
            finally:
                os.close(root_fd)


def remove_empty_directory_entry(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> None:
    """在 0700 私有 holder 中按 inode 删除一个已清空的直接子目录。"""

    holder_name = f".entry-cleanup-{uuid4().hex}"
    os.mkdir(holder_name, mode=0o700, dir_fd=parent_fd)
    holder_fd: int | None = None
    holder_identity: tuple[int, int] | None = None
    isolated = False
    try:
        holder_entry = os.stat(holder_name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(holder_entry.st_mode):
            raise RuntimeError("managed smoke child cleanup holder identity changed")
        holder_identity = (holder_entry.st_dev, holder_entry.st_ino)
        holder_fd = os.open(holder_name, directory_open_flags(), dir_fd=parent_fd)
        holder = os.fstat(holder_fd)
        opened_identity = (holder.st_dev, holder.st_ino)
        if opened_identity != holder_identity:
            raise RuntimeError("managed smoke child cleanup holder identity changed")
        holder_identity = opened_identity
        current_holder = os.stat(holder_name, dir_fd=parent_fd, follow_symlinks=False)
        if (current_holder.st_dev, current_holder.st_ino) != holder_identity:
            raise RuntimeError("managed smoke child cleanup holder identity changed")
        os.fchmod(holder_fd, 0o700)
        os.rename(name, "target", src_dir_fd=parent_fd, dst_dir_fd=holder_fd)
        isolated = True
        current = os.stat("target", dir_fd=holder_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != expected_identity:
            try:
                os.rename("target", name, src_dir_fd=holder_fd, dst_dir_fd=parent_fd)
                isolated = False
            except OSError:
                pass
            raise RuntimeError("managed smoke child directory identity changed")
        os.rmdir("target", dir_fd=holder_fd)
        isolated = False
    finally:
        try:
            if holder_identity is not None and not isolated:
                current_holder = os.stat(holder_name, dir_fd=parent_fd, follow_symlinks=False)
                if (current_holder.st_dev, current_holder.st_ino) != holder_identity:
                    raise RuntimeError("managed smoke child cleanup holder identity changed")
                os.rmdir(holder_name, dir_fd=parent_fd)
        finally:
            if holder_fd is not None:
                os.close(holder_fd)


def clear_directory_fd(directory_fd: int) -> None:
    """通过稳定目录句柄递归清空内容，绝不跟随子项 symlink。"""

    os.fchmod(directory_fd, 0o700)
    for name in os.listdir(directory_fd):
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISDIR(entry.st_mode):
            os.unlink(name, dir_fd=directory_fd)
            continue
        child_fd = os.open(name, directory_open_flags(), dir_fd=directory_fd)
        try:
            opened = os.fstat(child_fd)
            if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
                raise RuntimeError("managed smoke child directory identity changed")
            os.fchmod(child_fd, 0o700)
            clear_directory_fd(child_fd)
        finally:
            os.close(child_fd)
        remove_empty_directory_entry(
            directory_fd,
            name,
            (entry.st_dev, entry.st_ino),
        )
    if os.listdir(directory_fd):
        raise RuntimeError("managed smoke directory cleanup did not remove target contents")


__all__ = [
    "clear_directory_fd",
    "directory_open_flags",
    "managed_smoke_directory",
    "managed_smoke_root",
    "open_smoke_directory",
    "open_managed_root",
    "remove_empty_directory_entry",
    "write_private_file",
]
