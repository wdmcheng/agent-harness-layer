"""Service smoke 目录身份首次绑定与 trace 类型竞态合同。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.contracts.service_deployment_test_support import load_smoke_service


def test_project_creation_never_adopts_replacement_before_first_identity_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mkdir 后首次 stat 已看到 replacement 时也不能把它认作本轮项目。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    original_stat = smoke.os.stat
    stat_attempted = False
    replacement_created = False
    result: tuple[Path, tuple[int, int], int] | None = None
    error: BaseException | None = None
    managed_root = tmp_path / ".agent-harness"
    orphan = managed_root / "original-project"
    marker = managed_root / project / "foreign-marker"

    def replace_during_initial_identity(
        path: object,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal replacement_created, stat_attempted
        if path == project and not stat_attempted:
            stat_attempted = True
            (managed_root / project).rename(orphan)
            (managed_root / project).mkdir(mode=0o777)
            (managed_root / project).chmod(0o777)
            marker.write_text("FOREIGN", encoding="utf-8")
            replacement_created = True
            return original_stat(path, *args, **kwargs)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "stat", replace_during_initial_identity)
    try:
        try:
            result = smoke._create_smoke_directory(project)
        except BaseException as caught:
            error = caught

        assert error is not None
        assert replacement_created is True
        assert marker.read_text(encoding="utf-8") == "FOREIGN"
        assert marker.parent.stat().st_mode & 0o777 == 0o777
        assert orphan.is_dir()
    finally:
        if result is not None:
            os.close(result[2])


def test_subdirectory_creation_never_adopts_replacement_before_first_identity_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """child 首次 stat 已看到 replacement 时也不能被开放或接受。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    smoke_dir, identity, smoke_fd = smoke._create_smoke_directory(project)
    original_stat = smoke.os.stat
    stat_attempted = False
    replacement_created = False
    error: BaseException | None = None
    orphan = smoke_dir / "original-workspace"
    marker = smoke_dir / "workspace" / "foreign-marker"

    def replace_during_initial_identity(
        path: object,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal replacement_created, stat_attempted
        if path == "workspace" and not stat_attempted:
            stat_attempted = True
            (smoke_dir / "workspace").rename(orphan)
            (smoke_dir / "workspace").mkdir(mode=0o777)
            (smoke_dir / "workspace").chmod(0o777)
            marker.write_text("FOREIGN", encoding="utf-8")
            replacement_created = True
            return original_stat(path, *args, **kwargs)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "stat", replace_during_initial_identity)
    try:
        try:
            smoke._create_smoke_subdirectory(
                smoke_dir,
                "workspace",
                expected_identity=identity,
            )
        except BaseException as caught:
            error = caught

        assert error is not None
        assert replacement_created is True
        assert marker.read_text(encoding="utf-8") == "FOREIGN"
        assert marker.parent.stat().st_mode & 0o777 == 0o777
        assert orphan.is_dir()
    finally:
        monkeypatch.setattr(smoke.os, "stat", original_stat)
        smoke._remove_smoke_directory(
            smoke_dir,
            expected_identity=identity,
            smoke_fd=smoke_fd,
        )
        os.close(smoke_fd)


def test_project_publish_never_replaces_existing_empty_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """staging 发布必须排他，不能覆盖并发创建的空项目目录。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    original_publish = smoke.smoke_lifecycle.rename_directory_no_replace
    replacement_identity: tuple[int, int] | None = None

    def publish_after_replacement(
        parent_fd: int,
        source: str,
        target: str,
    ) -> None:
        nonlocal replacement_identity
        if target == project and replacement_identity is None:
            replacement = tmp_path / ".agent-harness" / project
            replacement.mkdir(mode=0o700)
            entry = replacement.stat()
            replacement_identity = (entry.st_dev, entry.st_ino)
        original_publish(parent_fd, source, target)

    monkeypatch.setattr(
        smoke.smoke_lifecycle,
        "rename_directory_no_replace",
        publish_after_replacement,
    )

    with pytest.raises(RuntimeError, match="already exists"):
        smoke._create_smoke_directory(project)

    replacement = tmp_path / ".agent-harness" / project
    current = replacement.stat()
    assert replacement_identity == (current.st_dev, current.st_ino)


def test_subdirectory_publish_never_replaces_existing_empty_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """child staging 发布必须排他，不能覆盖并发创建的空最终目录。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    smoke_dir, identity, smoke_fd = smoke._create_smoke_directory(project)
    original_publish = smoke.smoke_lifecycle.rename_directory_no_replace
    replacement_identity: tuple[int, int] | None = None

    def publish_after_replacement(
        parent_fd: int,
        source: str,
        target: str,
    ) -> None:
        nonlocal replacement_identity
        if target == "workspace" and replacement_identity is None:
            replacement = smoke_dir / "workspace"
            replacement.mkdir(mode=0o700)
            entry = replacement.stat()
            replacement_identity = (entry.st_dev, entry.st_ino)
        original_publish(parent_fd, source, target)

    monkeypatch.setattr(
        smoke.smoke_lifecycle,
        "rename_directory_no_replace",
        publish_after_replacement,
    )
    try:
        with pytest.raises(RuntimeError, match="already exists"):
            smoke._create_smoke_subdirectory(
                smoke_dir,
                "workspace",
                expected_identity=identity,
            )

        replacement = smoke_dir / "workspace"
        current = replacement.stat()
        assert replacement_identity == (current.st_dev, current.st_ino)
    finally:
        smoke._remove_smoke_directory(
            smoke_dir,
            expected_identity=identity,
            smoke_fd=smoke_fd,
        )
        os.close(smoke_fd)


def test_project_remains_private_while_child_identity_is_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部 child 初始化前项目根必须保持 0700，禁止同组替换 staging。"""

    smoke = load_smoke_service(monkeypatch)
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    smoke_dir, identity, smoke_fd = smoke._create_smoke_directory("safe-project")
    observed_modes: list[int] = []
    original_stat = smoke.os.stat

    def record_parent_mode(
        path: object,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        if isinstance(path, str) and path.startswith(".child-create-"):
            observed_modes.append(smoke_dir.stat().st_mode & 0o777)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "stat", record_parent_mode)
    try:
        smoke._create_smoke_subdirectory(
            smoke_dir,
            "workspace",
            expected_identity=identity,
        )
        assert observed_modes
        assert set(observed_modes) == {0o700}
    finally:
        monkeypatch.setattr(smoke.os, "stat", original_stat)
        smoke._remove_smoke_directory(
            smoke_dir,
            expected_identity=identity,
            smoke_fd=smoke_fd,
        )
        os.close(smoke_fd)


def test_cleanup_without_expected_identity_rejects_wrong_fd_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容性清理未显式传 identity 时也不得 chmod 或清空错误目录 fd。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    smoke_dir = app_root / ".agent-harness" / "safe-project"
    smoke_dir.mkdir(parents=True)
    secret = smoke_dir / "storage-dsn.secret"
    secret.write_text("SECRET", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o777)
    outside.chmod(0o777)
    marker = outside / "foreign-marker"
    marker.write_text("FOREIGN", encoding="utf-8")
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)
    original_open = smoke.os.open
    redirected = False

    def redirect_project_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal redirected
        if path == smoke_dir.name and not redirected:
            redirected = True
            return original_open(outside, *args, **kwargs)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "open", redirect_project_open)

    with pytest.raises(RuntimeError, match="identity changed"):
        smoke._remove_smoke_directory(smoke_dir)

    assert redirected is True
    assert outside.stat().st_mode & 0o777 == 0o777
    assert marker.read_text(encoding="utf-8") == "FOREIGN"
    assert secret.read_text(encoding="utf-8") == "SECRET"


def test_child_cleanup_holder_never_adopts_replacement_after_first_stat_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """child cleanup holder 首次身份不可读时不得收养 replacement。"""

    smoke = load_smoke_service(monkeypatch)
    parent = tmp_path / "parent"
    child = parent / "workspace"
    child.mkdir(parents=True)
    child_entry = child.stat()
    child_identity = (child_entry.st_dev, child_entry.st_ino)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    original_stat = smoke.os.stat
    injected = False
    replacement: Path | None = None
    marker: Path | None = None

    def replace_holder_on_first_stat(
        path: object,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal injected, marker, replacement
        if isinstance(path, str) and path.startswith(".entry-cleanup-") and not injected:
            injected = True
            holder = parent / path
            holder.rename(parent / f"{path}-orphan")
            holder.mkdir(mode=0o777)
            holder.chmod(0o777)
            replacement = holder
            marker = holder / "foreign-marker"
            marker.write_text("FOREIGN", encoding="utf-8")
            raise OSError("injected child holder identity failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "stat", replace_holder_on_first_stat)
    try:
        with pytest.raises(OSError, match="child holder identity failure"):
            smoke.smoke_filesystem.remove_empty_directory_entry(
                parent_fd,
                child.name,
                child_identity,
            )
    finally:
        os.close(parent_fd)

    assert injected is True
    assert replacement is not None and replacement.stat().st_mode & 0o777 == 0o777
    assert marker is not None and marker.read_text(encoding="utf-8") == "FOREIGN"
    assert child.is_dir()


def test_project_cleanup_holder_never_adopts_replacement_after_first_stat_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """project cleanup holder 首次身份不可读时不得移动项目或修改 replacement。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    smoke_dir, identity, smoke_fd = smoke._create_smoke_directory(project)
    secret = smoke_dir / "storage-dsn.secret"
    secret.write_text("SECRET", encoding="utf-8")
    managed_root = tmp_path / ".agent-harness"
    original_stat = smoke.os.stat
    injected = False
    replacement: Path | None = None
    marker: Path | None = None

    def replace_holder_on_first_stat(
        path: object,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal injected, marker, replacement
        if isinstance(path, str) and path.startswith(".cleanup-") and not injected:
            injected = True
            holder = managed_root / path
            holder.rename(managed_root / f"{path}-orphan")
            holder.mkdir(mode=0o777)
            holder.chmod(0o777)
            replacement = holder
            marker = holder / "foreign-marker"
            marker.write_text("FOREIGN", encoding="utf-8")
            raise OSError("injected project holder identity failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "stat", replace_holder_on_first_stat)
    try:
        with pytest.raises(OSError, match="project holder identity failure"):
            smoke._remove_smoke_directory(
                smoke_dir,
                expected_identity=identity,
                smoke_fd=smoke_fd,
            )
    finally:
        os.close(smoke_fd)

    assert injected is True
    assert replacement is not None and replacement.stat().st_mode & 0o777 == 0o777
    assert marker is not None and marker.read_text(encoding="utf-8") == "FOREIGN"
    assert secret.read_text(encoding="utf-8") == "SECRET"


def test_trace_export_rejects_fifo_without_blocking_or_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享 trace 被换成 FIFO 时必须 nonblocking 拒绝且不发布证据。"""

    smoke = load_smoke_service(monkeypatch)
    managed_root = tmp_path / ".agent-harness"
    smoke_dir = managed_root / "safe-project"
    smoke_dir.mkdir(parents=True)
    os.mkfifo(smoke_dir / "trace.jsonl", mode=0o640)
    root_fd = os.open(managed_root, os.O_RDONLY | os.O_DIRECTORY)
    smoke_fd = os.open(smoke_dir, os.O_RDONLY | os.O_DIRECTORY)
    original_open = smoke.trace.os.open
    supplied_flags: int | None = None

    def observe_trace_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal supplied_flags
        if path == "trace.jsonl":
            supplied_flags = flags
            flags |= os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(smoke.trace.os, "open", observe_trace_open)
    try:
        with pytest.raises(RuntimeError, match="regular file"):
            smoke.trace.export_service_trace(
                root_fd,
                smoke_fd,
                "service-smoke-trace.jsonl",
                "safe-project",
            )
    finally:
        os.close(smoke_fd)
        os.close(root_fd)

    assert supplied_flags is not None
    assert supplied_flags & os.O_NONBLOCK
    assert not (managed_root / "service-smoke-trace.jsonl").exists()
    assert not list(managed_root.glob(".service-smoke-trace.jsonl.*.tmp"))


def test_existing_managed_wheel_fifo_is_opened_nonblocking_and_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既有受管 wheel 被换成 FIFO 时不能阻塞或进入复制。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    managed_root = app_root / ".agent-harness"
    managed_root.mkdir(parents=True)
    wheel_name = "agent_harness-0.1.0-py3-none-any.whl"
    os.mkfifo(managed_root / wheel_name, mode=0o640)
    globals_ = smoke.prepare_core_wheel.__globals__
    monkeypatch.setitem(globals_, "APP_ROOT", app_root)
    original_open = smoke.os.open
    supplied_flags: int | None = None

    def observe_wheel_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal supplied_flags
        if path == wheel_name:
            supplied_flags = flags
            flags |= os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "open", observe_wheel_open)

    with pytest.raises(RuntimeError, match="regular file"):
        smoke.prepare_core_wheel()

    assert supplied_flags is not None
    assert supplied_flags & os.O_NONBLOCK


def test_managed_wheel_digest_opens_fifo_nonblocking_and_rejects_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """摘要读取也必须 nonblocking 打开并拒绝 FIFO。"""

    smoke = load_smoke_service(monkeypatch)
    managed_root = tmp_path / ".agent-harness"
    managed_root.mkdir()
    wheel_name = "agent_harness-0.1.0-py3-none-any.whl"
    os.mkfifo(managed_root / wheel_name, mode=0o640)
    root_fd = os.open(managed_root, os.O_RDONLY | os.O_DIRECTORY)
    globals_ = smoke.prepare_core_wheel.__globals__
    original_open = smoke.os.open
    supplied_flags: int | None = None

    def observe_digest_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal supplied_flags
        if path == wheel_name:
            supplied_flags = flags
            flags |= os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "open", observe_digest_open)
    try:
        with pytest.raises(RuntimeError, match="regular file"):
            globals_["_managed_file_digest"](root_fd, wheel_name)
    finally:
        os.close(root_fd)

    assert supplied_flags is not None
    assert supplied_flags & os.O_NONBLOCK


def test_external_wheel_source_fifo_race_is_nonblocking_and_not_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部 source 在 is_file 后变成 FIFO 时必须立即拒绝。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    app_root.mkdir()
    source = tmp_path / "agent_harness-0.1.0-py3-none-any.whl"
    source.write_bytes(b"TRUSTED")
    source_orphan = tmp_path / "source-orphan.whl"
    monkeypatch.setenv("AGENT_HARNESS_SOURCE", str(source))
    globals_ = smoke.prepare_core_wheel.__globals__
    monkeypatch.setitem(globals_, "APP_ROOT", app_root)
    original_open = smoke.os.open
    supplied_flags: int | None = None
    replaced = False

    def replace_source_before_open(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal replaced, supplied_flags
        if path == source and not replaced:
            replaced = True
            supplied_flags = flags
            source.rename(source_orphan)
            os.mkfifo(source, mode=0o640)
            flags |= os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "open", replace_source_before_open)

    with pytest.raises(RuntimeError, match="regular wheel file"):
        smoke.prepare_core_wheel()

    assert replaced is True
    assert supplied_flags is not None
    assert supplied_flags & os.O_NONBLOCK
    assert not (app_root / ".agent-harness" / source.name).exists()
