"""Service smoke 受管文件系统在注入失败下的清理闭合合同。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.contracts.service_deployment_test_support import (
    load_smoke_service,
    load_smoke_support,
)


def test_create_smoke_directory_stays_private_when_first_identity_stat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mkdir 后首次 identity stat 失败必须立即关闭，且入口保持 0700。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    original_stat = smoke.os.stat
    failed = False

    def fail_first_project_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal failed
        if isinstance(path, str) and path.startswith(".project-create-") and not failed:
            failed = True
            raise OSError("injected first project stat failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "stat", fail_first_project_stat)
    with pytest.raises(OSError, match="first project stat failure"):
        smoke._create_smoke_directory(project)

    assert failed is True
    managed_root = tmp_path / ".agent-harness"
    staging = list(managed_root.glob(".project-create-*"))
    assert len(staging) == 1
    assert staging[0].stat().st_mode & 0o777 == 0o700
    assert not (managed_root / project).exists()


def test_create_smoke_directory_stays_private_when_entry_stat_stays_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """路径身份持续不可读时不能猜测归属，残留入口必须保持 0700。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    original_stat = smoke.os.stat

    def fail_project_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if isinstance(path, str) and path.startswith(".project-create-"):
            raise OSError("injected persistent project stat failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "stat", fail_project_stat)

    with pytest.raises(OSError, match="persistent project stat failure"):
        smoke._create_smoke_directory(project)

    managed_root = tmp_path / ".agent-harness"
    staging = list(managed_root.glob(".project-create-*"))
    assert len(staging) == 1
    assert staging[0].stat().st_mode & 0o777 == 0o700
    assert not (managed_root / project).exists()


def test_create_smoke_directory_binds_fd_before_permission_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新目录不能在稳定 fd 建立前通过可替换路径修改权限。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    original_chmod = smoke.os.chmod

    def reject_project_path_chmod(path: object, *args: object, **kwargs: object) -> None:
        if path == project:
            raise OSError("path chmod before identity binding")
        original_chmod(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "chmod", reject_project_path_chmod)
    smoke_dir, identity, smoke_fd = smoke._create_smoke_directory(project)
    try:
        assert smoke_dir.stat().st_mode & 0o777 == 0o700
        smoke._publish_smoke_directory(
            smoke_dir,
            expected_identity=identity,
            smoke_fd=smoke_fd,
        )
        assert smoke_dir.stat().st_mode & 0o777 == 0o770
    finally:
        smoke._remove_smoke_directory(
            smoke_dir,
            expected_identity=identity,
            smoke_fd=smoke_fd,
        )
        os.close(smoke_fd)


def test_create_smoke_directory_rolls_back_when_first_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mkdir 后首次 project open 失败也必须删除本轮空目录。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    original_open = smoke.os.open
    failed = False

    def fail_first_project_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal failed
        if isinstance(path, str) and path.startswith(".project-create-") and not failed:
            failed = True
            raise OSError("injected first project open failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "open", fail_first_project_open)

    with pytest.raises(OSError, match="first project open failure"):
        smoke._create_smoke_directory(project)

    assert failed is True
    assert not (tmp_path / ".agent-harness" / project).exists()


def test_create_smoke_directory_rolls_back_when_created_fd_cannot_be_statted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """创建 fd 的 fstat 持续失败时仍按预记录 inode 删除空项目目录。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    original_open = smoke.os.open
    original_fstat = smoke.os.fstat
    project_fd: int | None = None

    def record_project_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal project_fd
        fd = original_open(path, *args, **kwargs)
        if isinstance(path, str) and path.startswith(".project-create-"):
            project_fd = fd
        return fd

    def fail_project_fstat(fd: int) -> os.stat_result:
        if fd == project_fd:
            raise OSError("injected project fstat failure")
        return original_fstat(fd)

    monkeypatch.setattr(smoke.os, "open", record_project_open)
    monkeypatch.setattr(smoke.os, "fstat", fail_project_fstat)

    with pytest.raises(OSError, match="project fstat failure"):
        smoke._create_smoke_directory(project)

    assert project_fd is not None
    assert not (tmp_path / ".agent-harness" / project).exists()


def test_create_smoke_subdirectory_stays_private_when_first_identity_stat_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """child 首次 identity stat 失败必须立即关闭，且入口保持 0700。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    smoke_dir, identity, smoke_fd = smoke._create_smoke_directory(project)
    original_stat = smoke.os.stat
    failed = False

    def fail_first_child_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal failed
        if isinstance(path, str) and path.startswith(".child-create-") and not failed:
            failed = True
            raise OSError("injected first child stat failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "stat", fail_first_child_stat)
    try:
        with pytest.raises(OSError, match="first child stat failure"):
            smoke._create_smoke_subdirectory(
                smoke_dir,
                "workspace",
                expected_identity=identity,
            )
        staging = list(smoke_dir.glob(".child-create-*"))
        assert len(staging) == 1
        assert staging[0].stat().st_mode & 0o777 == 0o700
        assert not (smoke_dir / "workspace").exists()
    finally:
        monkeypatch.setattr(smoke.os, "stat", original_stat)
        smoke._remove_smoke_directory(
            smoke_dir,
            expected_identity=identity,
            smoke_fd=smoke_fd,
        )
        os.close(smoke_fd)

    assert failed is True
    assert not smoke_dir.exists()


def test_create_smoke_subdirectory_stays_private_when_entry_stat_stays_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """child 身份持续不可读时不猜测归属，残留入口必须保持 0700。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    smoke_dir, identity, smoke_fd = smoke._create_smoke_directory(project)
    original_stat = smoke.os.stat

    def fail_child_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if isinstance(path, str) and path.startswith(".child-create-"):
            raise OSError("injected persistent child stat failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "stat", fail_child_stat)
    try:
        with pytest.raises(OSError, match="persistent child stat failure"):
            smoke._create_smoke_subdirectory(
                smoke_dir,
                "workspace",
                expected_identity=identity,
            )
        staging = list(smoke_dir.glob(".child-create-*"))
        assert len(staging) == 1
        assert staging[0].stat().st_mode & 0o777 == 0o700
        assert not (smoke_dir / "workspace").exists()
    finally:
        monkeypatch.setattr(smoke.os, "stat", original_stat)
        smoke._remove_smoke_directory(
            smoke_dir,
            expected_identity=identity,
            smoke_fd=smoke_fd,
        )
        os.close(smoke_fd)


def test_create_smoke_subdirectory_rolls_back_when_first_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mkdir 后首次 child open 失败也必须删除本轮空子目录。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    smoke_dir, identity, smoke_fd = smoke._create_smoke_directory(project)
    original_open = smoke.os.open
    failed = False

    def fail_first_child_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal failed
        if isinstance(path, str) and path.startswith(".child-create-") and not failed:
            failed = True
            raise OSError("injected first child open failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "open", fail_first_child_open)
    try:
        with pytest.raises(OSError, match="first child open failure"):
            smoke._create_smoke_subdirectory(
                smoke_dir,
                "workspace",
                expected_identity=identity,
            )
        assert not (smoke_dir / "workspace").exists()
    finally:
        smoke._remove_smoke_directory(
            smoke_dir,
            expected_identity=identity,
            smoke_fd=smoke_fd,
        )
        os.close(smoke_fd)


def test_create_smoke_subdirectory_does_not_open_before_identity_is_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """child 的 path identity 未绑定时不得继续 open，残留入口保持 0700。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    smoke_dir, identity, smoke_fd = smoke._create_smoke_directory(project)
    original_stat = smoke.os.stat
    original_open = smoke.os.open
    open_attempted = False

    def fail_child_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if isinstance(path, str) and path.startswith(".child-create-"):
            raise OSError("injected persistent child stat failure")
        return original_stat(path, *args, **kwargs)

    def fail_child_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal open_attempted
        if isinstance(path, str) and path.startswith(".child-create-"):
            open_attempted = True
            raise OSError("injected child open failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "stat", fail_child_stat)
    monkeypatch.setattr(smoke.os, "open", fail_child_open)
    try:
        with pytest.raises(OSError, match="persistent child stat failure"):
            smoke._create_smoke_subdirectory(
                smoke_dir,
                "workspace",
                expected_identity=identity,
            )
        monkeypatch.setattr(smoke.os, "stat", original_stat)
        monkeypatch.setattr(smoke.os, "open", original_open)
        assert open_attempted is False
        staging = list(smoke_dir.glob(".child-create-*"))
        assert len(staging) == 1
        assert staging[0].stat().st_mode & 0o777 == 0o700
        assert not (smoke_dir / "workspace").exists()
    finally:
        smoke._remove_smoke_directory(
            smoke_dir,
            expected_identity=identity,
            smoke_fd=smoke_fd,
        )
        os.close(smoke_fd)


def test_create_smoke_subdirectory_rolls_back_when_created_fd_cannot_be_statted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """child fd 的 fstat 持续失败时仍按预记录 inode 删除空子目录。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    smoke_dir, identity, smoke_fd = smoke._create_smoke_directory(project)
    original_open = smoke.os.open
    original_fstat = smoke.os.fstat
    child_fd: int | None = None

    def record_child_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal child_fd
        fd = original_open(path, *args, **kwargs)
        if isinstance(path, str) and path.startswith(".child-create-"):
            child_fd = fd
        return fd

    def fail_child_fstat(fd: int) -> os.stat_result:
        if fd == child_fd:
            raise OSError("injected child fstat failure")
        return original_fstat(fd)

    monkeypatch.setattr(smoke.os, "open", record_child_open)
    monkeypatch.setattr(smoke.os, "fstat", fail_child_fstat)
    try:
        with pytest.raises(OSError, match="child fstat failure"):
            smoke._create_smoke_subdirectory(
                smoke_dir,
                "workspace",
                expected_identity=identity,
            )
        assert child_fd is not None
        assert not (smoke_dir / "workspace").exists()
    finally:
        smoke._remove_smoke_directory(
            smoke_dir,
            expected_identity=identity,
            smoke_fd=smoke_fd,
        )
        os.close(smoke_fd)


def test_cleanup_holder_open_failure_is_fail_closed_and_rolls_back_holder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """holder 打开失败时项目先降为 0700，且不遗留空 holder。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    smoke_dir = app_root / ".agent-harness" / "safe-project"
    smoke_dir.mkdir(parents=True)
    secret = smoke_dir / "storage-dsn.secret"
    secret.write_text("SECRET", encoding="utf-8")
    created = smoke_dir.stat()
    smoke_fd = os.open(smoke_dir, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)
    original_open = smoke.os.open
    failed = False

    def fail_holder_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal failed
        if isinstance(path, str) and path.startswith(".cleanup-") and not failed:
            failed = True
            raise OSError("injected holder open failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "open", fail_holder_open)
    try:
        with pytest.raises(OSError, match="holder open failure"):
            smoke._remove_smoke_directory(
                smoke_dir,
                expected_identity=(created.st_dev, created.st_ino),
                smoke_fd=smoke_fd,
            )
    finally:
        os.close(smoke_fd)

    assert smoke_dir.stat().st_mode & 0o777 == 0o700
    assert secret.read_text(encoding="utf-8") == "SECRET"
    assert not list((app_root / ".agent-harness").glob(".cleanup-*"))


def test_private_file_cleanup_failure_still_closes_directory_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """半成品 unlink 自身失败也不能泄漏受管根和项目目录 fd。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    smoke_dir = app_root / ".agent-harness" / "safe-project"
    smoke_dir.mkdir(parents=True)
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)
    original_open = smoke.os.open
    original_fchmod = smoke.os.fchmod
    original_unlink = smoke.os.unlink
    opened_directory_fds: list[int] = []
    private_fd: int | None = None

    def record_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal private_fd
        fd = original_open(path, *args, **kwargs)
        if path == "storage-dsn.secret":
            private_fd = fd
        elif kwargs.get("dir_fd") is None:
            opened_directory_fds.append(fd)
        elif isinstance(path, str) and path == smoke_dir.name:
            opened_directory_fds.append(fd)
        return fd

    def fail_private_fchmod(fd: int, mode: int) -> None:
        if fd == private_fd:
            raise OSError("injected private chmod failure")
        original_fchmod(fd, mode)

    def fail_partial_unlink(path: object, *args: object, **kwargs: object) -> None:
        if path == "storage-dsn.secret":
            raise OSError("injected partial unlink failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "open", record_open)
    monkeypatch.setattr(smoke.os, "fchmod", fail_private_fchmod)
    monkeypatch.setattr(smoke.os, "unlink", fail_partial_unlink)

    with pytest.raises(OSError, match="partial unlink failure"):
        smoke._write_private_file(smoke_dir / "storage-dsn.secret", "SECRET", mode=0o640)

    assert opened_directory_fds
    for fd in opened_directory_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_child_cleanup_holder_open_failure_removes_empty_holder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子目录清理 holder 打开失败时也必须回滚自身创建的 holder。"""

    smoke = load_smoke_service(monkeypatch)
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    identity = child.stat()
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    remove_entry = smoke._lifecycle_create_smoke_subdirectory.__globals__[
        "remove_empty_directory_entry"
    ]
    original_open = smoke.os.open
    failed = False

    def fail_holder_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal failed
        if isinstance(path, str) and path.startswith(".entry-cleanup-") and not failed:
            failed = True
            raise OSError("injected child holder open failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "open", fail_holder_open)
    try:
        with pytest.raises(OSError, match="child holder open failure"):
            remove_entry(
                parent_fd,
                "child",
                (identity.st_dev, identity.st_ino),
            )
    finally:
        os.close(parent_fd)

    assert child.is_dir()
    assert not list(parent.glob(".entry-cleanup-*"))


def test_recursive_cleanup_changes_child_mode_only_through_verified_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """递归清理不能在 child fd 身份验证前按路径 chmod。"""

    smoke = load_smoke_service(monkeypatch)
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (child / "artifact.txt").write_text("payload", encoding="utf-8")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    original_chmod = smoke.os.chmod

    def reject_child_path_chmod(path: object, *args: object, **kwargs: object) -> None:
        if path == "child":
            raise OSError("path chmod before child identity binding")
        original_chmod(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "chmod", reject_child_path_chmod)
    try:
        smoke._clear_directory_fd(parent_fd)
    finally:
        os.close(parent_fd)

    assert list(parent.iterdir()) == []


def test_recursive_cleanup_keeps_directories_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """递归删除期间顶层和 child 都只能收紧为 0700，不能恢复组写权限。"""

    smoke = load_smoke_service(monkeypatch)
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (child / "artifact.txt").write_text("payload", encoding="utf-8")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    original_fchmod = smoke.os.fchmod
    modes: list[int] = []

    def record_fchmod(fd: int, mode: int) -> None:
        modes.append(mode)
        original_fchmod(fd, mode)

    monkeypatch.setattr(smoke.os, "fchmod", record_fchmod)
    try:
        smoke._clear_directory_fd(parent_fd)
    finally:
        os.close(parent_fd)

    assert modes
    assert set(modes) == {0o700}


def test_compose_override_uses_checked_content_instead_of_mutable_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """身份检查后文件被替换时，Compose 命令仍只从 stdin 消费冻结内容。"""

    support = load_smoke_support()
    smoke_dir = tmp_path / "safe-project"
    smoke_dir.mkdir()
    created = smoke_dir.stat()
    override = smoke_dir / "runtime-user.override.yml"
    override.write_text("safe", encoding="utf-8")
    original_assert = support.assert_smoke_directory_identity

    def check_then_replace(env: dict[str, str]) -> None:
        original_assert(env)
        override.write_text("services:\n  api:\n    privileged: true\n", encoding="utf-8")

    monkeypatch.setattr(support, "assert_smoke_directory_identity", check_then_replace)
    env = {
        "SERVICE_APP_COMPOSE_PROJECT": "safe-project",
        "SERVICE_APP_SMOKE_DIR": str(smoke_dir),
        "SERVICE_APP_SMOKE_DEVICE": str(created.st_dev),
        "SERVICE_APP_SMOKE_INODE": str(created.st_ino),
        "SERVICE_APP_RUNTIME_USER_OVERRIDE_FILE": str(override),
        "SERVICE_APP_RUNTIME_USER_OVERRIDE_CONTENT": "services: {}\n",
    }

    command = support._compose_command(env, "config")

    assert command.count("-") == 1
    assert str(override) not in command
