"""Service smoke 项目目录的创建、身份绑定、隔离清理与根级回收。"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from service_smoke_filesystem import (
    clear_directory_fd,
    directory_open_flags,
    managed_smoke_directory,
    open_managed_root,
    open_smoke_directory,
    remove_empty_directory_entry,
    rename_directory_no_replace,
)


def _stat_entry(parent_fd: int, name: str) -> os.stat_result:
    """读取直接子项身份；允许一次瞬时失败，但绝不跳过最终身份核验。"""

    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def create_smoke_directory(
    app_root: Path,
    project: str,
) -> tuple[Path, tuple[int, int], int]:
    """先以不可预测 staging 名绑定 inode，再发布项目名并开放共享权限。"""

    smoke_dir = managed_smoke_directory(app_root, project)
    _, root_fd = open_managed_root(app_root)
    staging_name = f".project-create-{uuid4().hex}"
    entry_name = staging_name
    created = False
    smoke_fd: int | None = None
    created_identity: tuple[int, int] | None = None
    try:
        os.mkdir(staging_name, mode=0o700, dir_fd=root_fd)
        created = True
        created_entry = os.stat(staging_name, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(created_entry.st_mode):
            raise RuntimeError("managed smoke directory is unavailable or unsafe")
        created_identity = (created_entry.st_dev, created_entry.st_ino)
        smoke_fd = os.open(staging_name, directory_open_flags(), dir_fd=root_fd)
        opened = os.fstat(smoke_fd)
        opened_identity = (opened.st_dev, opened.st_ino)
        if opened_identity != created_identity:
            raise RuntimeError("managed smoke directory is unavailable or unsafe")
        created_identity = opened_identity
        current = _stat_entry(root_fd, staging_name)
        if not stat.S_ISDIR(current.st_mode) or created_identity != (
            current.st_dev,
            current.st_ino,
        ):
            raise RuntimeError("managed smoke directory is unavailable or unsafe")
        try:
            rename_directory_no_replace(root_fd, staging_name, project)
        except FileExistsError:
            raise RuntimeError("service smoke project directory already exists") from None
        entry_name = project
        current = _stat_entry(root_fd, project)
        if created_identity != (current.st_dev, current.st_ino):
            raise RuntimeError("managed smoke directory is unavailable or unsafe")
    except BaseException:
        cleanup_error: BaseException | None = None
        if smoke_fd is not None and created_identity is None:
            try:
                opened = os.fstat(smoke_fd)
                created_identity = (opened.st_dev, opened.st_ino)
            except OSError:
                pass
        if created and created_identity is not None:
            try:
                remove_empty_directory_entry(root_fd, entry_name, created_identity)
            except BaseException as error:
                cleanup_error = error
        if smoke_fd is not None:
            os.close(smoke_fd)
        if cleanup_error is not None:
            raise RuntimeError("partial smoke directory rollback failed") from cleanup_error
        raise
    finally:
        os.close(root_fd)
    assert smoke_fd is not None and created_identity is not None
    return smoke_dir, created_identity, smoke_fd


def create_smoke_subdirectory(
    app_root: Path,
    smoke_dir: Path,
    name: str,
    *,
    expected_identity: tuple[int, int],
) -> None:
    """以随机 staging 目录绑定 inode，再原子发布容器共享子目录名。"""

    root_fd, smoke_fd = open_smoke_directory(
        app_root,
        smoke_dir,
        expected_identity=expected_identity,
    )
    staging_name = f".child-create-{uuid4().hex}"
    entry_name = staging_name
    created_identity: tuple[int, int] | None = None
    child_fd: int | None = None
    try:
        os.mkdir(staging_name, mode=0o700, dir_fd=smoke_fd)
        created_entry = os.stat(staging_name, dir_fd=smoke_fd, follow_symlinks=False)
        if not stat.S_ISDIR(created_entry.st_mode):
            raise RuntimeError("smoke subdirectory identity changed during creation")
        created_identity = (created_entry.st_dev, created_entry.st_ino)
        child_fd = os.open(staging_name, directory_open_flags(), dir_fd=smoke_fd)
        opened = os.fstat(child_fd)
        opened_identity = (opened.st_dev, opened.st_ino)
        if opened_identity != created_identity:
            raise RuntimeError("smoke subdirectory identity changed during creation")
        created_identity = opened_identity
        current = _stat_entry(smoke_fd, staging_name)
        if created_identity != (current.st_dev, current.st_ino):
            raise RuntimeError("smoke subdirectory identity changed during creation")
        try:
            rename_directory_no_replace(smoke_fd, staging_name, name)
        except FileExistsError:
            raise RuntimeError(f"smoke subdirectory already exists: {name}") from None
        entry_name = name
        current = _stat_entry(smoke_fd, name)
        if created_identity != (current.st_dev, current.st_ino):
            raise RuntimeError("smoke subdirectory identity changed during creation")
        os.fchmod(child_fd, 0o770)
    except BaseException:
        cleanup_error: BaseException | None = None
        if child_fd is not None and created_identity is None:
            try:
                opened = os.fstat(child_fd)
                created_identity = (opened.st_dev, opened.st_ino)
            except OSError:
                pass
        if created_identity is not None:
            try:
                remove_empty_directory_entry(smoke_fd, entry_name, created_identity)
            except BaseException as error:
                cleanup_error = error
        if cleanup_error is not None:
            raise RuntimeError("partial smoke subdirectory rollback failed") from cleanup_error
        raise
    finally:
        if child_fd is not None:
            os.close(child_fd)
        os.close(smoke_fd)
        os.close(root_fd)


def publish_smoke_directory(
    app_root: Path,
    smoke_dir: Path,
    *,
    expected_identity: tuple[int, int],
    smoke_fd: int,
) -> None:
    """全部私有初始化完成后，按创建 inode 将项目目录开放给容器组。"""

    managed_root, root_fd = open_managed_root(app_root)
    try:
        if smoke_dir.parent != managed_root:
            raise RuntimeError("path is outside managed smoke directory")
        opened = os.fstat(smoke_fd)
        if (opened.st_dev, opened.st_ino) != expected_identity:
            raise RuntimeError("managed smoke directory identity changed before publication")
        current = os.stat(smoke_dir.name, dir_fd=root_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (
                current.st_dev,
                current.st_ino,
            )
            != expected_identity
        ):
            raise RuntimeError("managed smoke directory identity changed before publication")
        os.fchmod(smoke_fd, 0o770)
    finally:
        os.close(root_fd)


def remove_smoke_directory(
    app_root: Path,
    smoke_dir: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    smoke_fd: int | None = None,
    clear_directory: Callable[[int], None] = clear_directory_fd,
) -> None:
    """先隔离创建时 inode，再清空内容并按 holder 身份删除目录入口。"""

    managed_root, root_fd = open_managed_root(app_root)
    if smoke_dir.parent != managed_root:
        os.close(root_fd)
        raise RuntimeError("refusing to remove path outside managed smoke directory")
    owns_smoke_fd = smoke_fd is None
    holder_name = f".cleanup-{uuid4().hex}"
    holder_fd: int | None = None
    holder_identity: tuple[int, int] | None = None
    isolated = False
    derived_identity = expected_identity is None
    try:
        if derived_identity:
            try:
                current = os.stat(
                    smoke_dir.name,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except OSError:
                raise RuntimeError("managed smoke directory is unavailable or unsafe") from None
            if not stat.S_ISDIR(current.st_mode):
                raise RuntimeError("managed smoke directory is unavailable or unsafe")
            expected_identity = (current.st_dev, current.st_ino)
        if smoke_fd is None:
            try:
                smoke_fd = os.open(smoke_dir.name, directory_open_flags(), dir_fd=root_fd)
            except OSError:
                raise RuntimeError("managed smoke directory is unavailable or unsafe") from None
        opened = os.fstat(smoke_fd)
        opened_identity = (opened.st_dev, opened.st_ino)
        if opened_identity != expected_identity:
            raise RuntimeError("managed smoke directory identity changed before cleanup")
        if derived_identity:
            try:
                current = os.stat(
                    smoke_dir.name,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except OSError:
                raise RuntimeError(
                    "managed smoke directory identity changed before cleanup"
                ) from None
            if (current.st_dev, current.st_ino) != expected_identity:
                raise RuntimeError("managed smoke directory identity changed before cleanup")
        os.fchmod(smoke_fd, 0o700)

        os.mkdir(holder_name, mode=0o700, dir_fd=root_fd)
        holder_entry = os.stat(holder_name, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(holder_entry.st_mode):
            raise RuntimeError("managed smoke cleanup holder identity changed")
        holder_identity = (holder_entry.st_dev, holder_entry.st_ino)
        holder_fd = os.open(holder_name, directory_open_flags(), dir_fd=root_fd)
        opened_holder = os.fstat(holder_fd)
        opened_holder_identity = (opened_holder.st_dev, opened_holder.st_ino)
        if opened_holder_identity != holder_identity:
            raise RuntimeError("managed smoke cleanup holder identity changed")
        holder_identity = opened_holder_identity
        current_holder = os.stat(holder_name, dir_fd=root_fd, follow_symlinks=False)
        if (current_holder.st_dev, current_holder.st_ino) != holder_identity:
            raise RuntimeError("managed smoke cleanup holder identity changed")
        os.fchmod(holder_fd, 0o700)
        try:
            os.rename(
                smoke_dir.name,
                "target",
                src_dir_fd=root_fd,
                dst_dir_fd=holder_fd,
            )
            isolated = True
        except FileNotFoundError:
            try:
                clear_directory(smoke_fd)
            except Exception as error:
                raise RuntimeError("managed smoke directory cleanup failed") from error
            raise RuntimeError("managed smoke directory identity changed before cleanup") from None
        current = os.stat("target", dir_fd=holder_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != expected_identity:
            try:
                clear_directory(smoke_fd)
            except Exception as error:
                raise RuntimeError("managed smoke directory cleanup failed") from error
            try:
                os.rename(
                    "target",
                    smoke_dir.name,
                    src_dir_fd=holder_fd,
                    dst_dir_fd=root_fd,
                )
                isolated = False
            except OSError:
                pass
            raise RuntimeError("managed smoke directory identity changed during cleanup")
        try:
            clear_directory(smoke_fd)
        except Exception as error:
            raise RuntimeError("managed smoke directory cleanup failed") from error
        try:
            os.rmdir("target", dir_fd=holder_fd)
        except OSError as error:
            raise RuntimeError("managed smoke directory cleanup failed") from error
        isolated = False
    finally:
        try:
            if holder_identity is not None and not isolated:
                try:
                    current_holder = os.stat(
                        holder_name,
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    raise RuntimeError("managed smoke cleanup holder identity changed") from None
                if (current_holder.st_dev, current_holder.st_ino) != holder_identity:
                    raise RuntimeError("managed smoke cleanup holder identity changed")
                try:
                    os.rmdir(holder_name, dir_fd=root_fd)
                except OSError as error:
                    raise RuntimeError("managed smoke cleanup holder removal failed") from error
        finally:
            try:
                if holder_fd is not None:
                    os.close(holder_fd)
            finally:
                if owns_smoke_fd and smoke_fd is not None:
                    os.close(smoke_fd)
                os.close(root_fd)


def unlink_managed_root_file(app_root: Path, path: Path) -> None:
    """通过受管根句柄删除一个根级普通文件或 symlink 本身。"""

    managed_root, root_fd = open_managed_root(app_root)
    try:
        if path.parent != managed_root:
            raise RuntimeError("root file is outside managed smoke root")
        try:
            os.unlink(path.name, dir_fd=root_fd)
        except FileNotFoundError:
            pass
    finally:
        os.close(root_fd)


__all__ = [
    "create_smoke_directory",
    "create_smoke_subdirectory",
    "publish_smoke_directory",
    "remove_smoke_directory",
    "unlink_managed_root_file",
]
