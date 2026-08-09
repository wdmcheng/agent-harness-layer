"""Service smoke 受管文件系统的竞态、原子发布与身份绑定合同。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from tests.contracts.service_deployment_test_support import (
    load_smoke_service,
    load_smoke_support,
)


def test_open_managed_root_rejects_wrong_fd_before_changing_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """root open 被替换为外部目录 fd 时，身份核验前不得修改其权限。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    managed_root = app_root / ".agent-harness"
    managed_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o777)
    outside.chmod(0o777)
    original_open = smoke.os.open
    redirected = False

    def redirect_root_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal redirected
        if path == managed_root and not redirected:
            redirected = True
            return original_open(outside, *args, **kwargs)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "open", redirect_root_open)

    with pytest.raises(RuntimeError, match="changed while it was being opened"):
        smoke.smoke_filesystem.open_managed_root(app_root)

    assert redirected is True
    assert outside.stat().st_mode & 0o777 == 0o777


def test_prepare_core_wheel_rejects_wrong_root_fd_before_changing_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wheel root open 被替换为外部 fd 时，身份核验前不得 chmod 外部目录。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    managed_root = app_root / ".agent-harness"
    managed_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o777)
    outside.chmod(0o777)
    globals_ = smoke.prepare_core_wheel.__globals__
    monkeypatch.setitem(globals_, "APP_ROOT", app_root)
    original_open = smoke.os.open
    redirected = False

    def redirect_root_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal redirected
        if path == managed_root and not redirected:
            redirected = True
            return original_open(outside, *args, **kwargs)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(smoke.os, "open", redirect_root_open)

    with pytest.raises(RuntimeError, match="changed while it was being opened"):
        smoke.prepare_core_wheel()

    assert redirected is True
    assert outside.stat().st_mode & 0o777 == 0o777


def test_prepare_core_wheel_refuses_single_managed_target_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单个匹配 symlink 必须命中 non-regular 防护，不能覆盖外部文件。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    managed_root = app_root / ".agent-harness"
    managed_root.mkdir(parents=True)
    source = tmp_path / "agent_harness-0.1.0-py3-none-any.whl"
    source.write_bytes(b"WHEEL-PAYLOAD")
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"KEEP")
    (managed_root / source.name).symlink_to(victim)
    monkeypatch.setenv("AGENT_HARNESS_SOURCE", str(source))
    monkeypatch.setitem(smoke.prepare_core_wheel.__globals__, "APP_ROOT", app_root)

    with pytest.raises(RuntimeError, match="regular file"):
        smoke.prepare_core_wheel()

    assert victim.read_bytes() == b"KEEP"


def test_prepare_core_wheel_refuses_ambiguous_managed_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多个匹配 wheel 必须作为独立的 ambiguity 分支拒绝。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    managed_root = app_root / ".agent-harness"
    managed_root.mkdir(parents=True)
    (managed_root / "agent_harness-one.whl").write_bytes(b"one")
    (managed_root / "agent_harness-two.whl").write_bytes(b"two")
    monkeypatch.setitem(smoke.prepare_core_wheel.__globals__, "APP_ROOT", app_root)

    with pytest.raises(RuntimeError, match="ambiguous"):
        smoke.prepare_core_wheel()


def test_prepare_core_wheel_publishes_only_complete_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发准备 wheel 时，最终文件名绝不能暴露复制中的半成品。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    app_root.mkdir()
    source = tmp_path / "agent_harness-0.1.0-py3-none-any.whl"
    payload = b"COMPLETE-WHEEL-PAYLOAD"
    source.write_bytes(payload)
    monkeypatch.setenv("AGENT_HARNESS_SOURCE", str(source))
    globals_ = smoke.prepare_core_wheel.__globals__
    monkeypatch.setitem(globals_, "APP_ROOT", app_root)
    original_copy = globals_["_copy_fd"]
    first_copy_started = threading.Event()
    release_first_copy = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def controlled_copy(source_fd: int, target_fd: int) -> None:
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            os.lseek(source_fd, 0, os.SEEK_SET)
            os.write(target_fd, os.read(source_fd, 1))
            first_copy_started.set()
            assert release_first_copy.wait(timeout=5)
            os.ftruncate(target_fd, 0)
            os.lseek(target_fd, 0, os.SEEK_SET)
        original_copy(source_fd, target_fd)

    monkeypatch.setitem(globals_, "_copy_fd", controlled_copy)
    errors: list[BaseException] = []

    def first_prepare() -> None:
        try:
            smoke.prepare_core_wheel()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=first_prepare)
    thread.start()
    assert first_copy_started.wait(timeout=5)
    try:
        smoke.prepare_core_wheel()
        published = app_root / ".agent-harness" / source.name
        assert published.read_bytes() == payload
    finally:
        release_first_copy.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []


def test_prepare_core_wheel_rejects_untrusted_publish_race_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最终名称被不同内容抢占时不能把攻击者文件当成本轮 wheel。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    app_root.mkdir()
    source = tmp_path / "agent_harness-0.1.0-py3-none-any.whl"
    source.write_bytes(b"TRUSTED-WHEEL")
    monkeypatch.setenv("AGENT_HARNESS_SOURCE", str(source))
    globals_ = smoke.prepare_core_wheel.__globals__
    monkeypatch.setitem(globals_, "APP_ROOT", app_root)
    original_link = globals_["os"].link

    def inject_winner(
        _source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        del src_dir_fd, follow_symlinks
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dst_dir_fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"ATTACKER")
        raise FileExistsError

    monkeypatch.setattr(globals_["os"], "link", inject_winner)
    try:
        with pytest.raises(RuntimeError, match="trusted wheel"):
            smoke.prepare_core_wheel()
    finally:
        monkeypatch.setattr(globals_["os"], "link", original_link)

    assert (app_root / ".agent-harness" / source.name).read_bytes() == b"ATTACKER"


def test_private_file_write_binds_created_directory_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同名真实目录替换后不得把秘密写进 replacement。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    smoke_dir = app_root / ".agent-harness" / "safe-project"
    smoke_dir.mkdir(parents=True)
    created = smoke_dir.stat()
    orphan = tmp_path / "orphan"
    smoke_dir.rename(orphan)
    smoke_dir.mkdir()
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)

    with pytest.raises(RuntimeError, match="identity changed"):
        smoke._write_private_file(
            smoke_dir / "storage-dsn.secret",
            "SECRET",
            mode=0o640,
            expected_identity=(created.st_dev, created.st_ino),
        )

    assert not (smoke_dir / "storage-dsn.secret").exists()
    assert not (orphan / "storage-dsn.secret").exists()


def test_cleanup_clears_created_inode_without_deleting_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目录被改名替换后仍清空原 inode 的秘密，且不删除 replacement。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    smoke_dir = app_root / ".agent-harness" / "safe-project"
    smoke_dir.mkdir(parents=True)
    (smoke_dir / "storage-dsn.secret").write_text("SECRET", encoding="utf-8")
    created = smoke_dir.stat()
    smoke_fd = os.open(smoke_dir, os.O_RDONLY | os.O_DIRECTORY)
    orphan = tmp_path / "orphan"
    smoke_dir.rename(orphan)
    smoke_dir.mkdir()
    replacement = smoke_dir / "replacement.txt"
    replacement.write_text("KEEP", encoding="utf-8")
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)

    try:
        with pytest.raises(RuntimeError, match="identity changed"):
            smoke._remove_smoke_directory(
                smoke_dir,
                expected_identity=(created.st_dev, created.st_ino),
                smoke_fd=smoke_fd,
            )
    finally:
        os.close(smoke_fd)

    assert replacement.read_text(encoding="utf-8") == "KEEP"
    assert not (orphan / "storage-dsn.secret").exists()


def test_cleanup_clears_stable_inode_when_project_entry_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """项目入口仅被改名时，报身份异常前也必须从稳定 fd 清除秘密。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    smoke_dir = app_root / ".agent-harness" / "safe-project"
    smoke_dir.mkdir(parents=True)
    secret = smoke_dir / "storage-dsn.secret"
    secret.write_text("SECRET", encoding="utf-8")
    created = smoke_dir.stat()
    smoke_fd = os.open(smoke_dir, os.O_RDONLY | os.O_DIRECTORY)
    orphan = tmp_path / "orphan"
    smoke_dir.rename(orphan)
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)

    try:
        with pytest.raises(RuntimeError, match="identity changed"):
            smoke._remove_smoke_directory(
                smoke_dir,
                expected_identity=(created.st_dev, created.st_ino),
                smoke_fd=smoke_fd,
            )
    finally:
        os.close(smoke_fd)

    assert orphan.stat().st_mode & 0o777 == 0o700
    assert not (orphan / "storage-dsn.secret").exists()


def test_cleanup_isolates_secret_before_recursive_clear_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """递归删除异常前必须先把项目移入 0700 holder。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    smoke_dir = app_root / ".agent-harness" / "safe-project"
    smoke_dir.mkdir(parents=True)
    (smoke_dir / "storage-dsn.secret").write_text("SECRET", encoding="utf-8")
    created = smoke_dir.stat()
    smoke_fd = os.open(smoke_dir, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)

    def fail_clear(_fd: int) -> None:
        raise OSError("injected clear failure")

    monkeypatch.setattr(smoke, "_clear_directory_fd", fail_clear)

    try:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            smoke._remove_smoke_directory(
                smoke_dir,
                expected_identity=(created.st_dev, created.st_ino),
                smoke_fd=smoke_fd,
            )
    finally:
        os.close(smoke_fd)

    assert not smoke_dir.exists()
    holders = list((app_root / ".agent-harness").glob(".cleanup-*"))
    assert len(holders) == 1
    assert holders[0].stat().st_mode & 0o777 == 0o700
    assert (holders[0] / "target" / "storage-dsn.secret").read_text() == "SECRET"


def test_cleanup_removes_holder_before_closing_stable_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """holder 必须在稳定 fd 关闭前删除，不能留下 close/rmdir 竞态。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    smoke_dir = app_root / ".agent-harness" / "safe-project"
    smoke_dir.mkdir(parents=True)
    smoke_fd = os.open(smoke_dir, os.O_RDONLY | os.O_DIRECTORY)
    created = smoke_dir.stat()
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)
    original_open = smoke.os.open
    original_close = smoke.os.close
    holder_fd: int | None = None
    raced = False

    def record_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal holder_fd
        fd = original_open(path, *args, **kwargs)
        if isinstance(path, str) and path.startswith(".cleanup-"):
            holder_fd = fd
        return fd

    def race_on_close(fd: int) -> None:
        nonlocal raced
        if fd == holder_fd and not raced:
            managed_root = app_root / ".agent-harness"
            raced = bool(list(managed_root.glob(".cleanup-*")))
        original_close(fd)

    monkeypatch.setattr(smoke.os, "open", record_open)
    monkeypatch.setattr(smoke.os, "close", race_on_close)
    try:
        smoke._remove_smoke_directory(
            smoke_dir,
            expected_identity=(created.st_dev, created.st_ino),
            smoke_fd=smoke_fd,
        )
    finally:
        monkeypatch.setattr(smoke.os, "close", original_close)
        original_close(smoke_fd)

    assert raced is False
    assert not list((app_root / ".agent-harness").glob(".cleanup-*"))


def test_service_smoke_reports_incomplete_recursive_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """递归清理未删除秘密时必须显式失败，不能报告成功。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    smoke_dir = app_root / ".agent-harness" / "safe-project"
    smoke_dir.mkdir(parents=True)
    (smoke_dir / "storage-dsn.secret").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)

    def ignored_clear(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(smoke, "_clear_directory_fd", ignored_clear)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        smoke._remove_smoke_directory(smoke_dir)

    leftovers = list((app_root / ".agent-harness").glob(".cleanup-*"))
    assert len(leftovers) == 1
    assert (leftovers[0] / "target" / "storage-dsn.secret").read_text(encoding="utf-8") == "secret"


def test_publish_smoke_directory_keeps_project_private_when_chmod_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最终开放权限失败时目录仍保持私有，并可由主流程稳定清理。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    smoke_dir, identity, smoke_fd = smoke._create_smoke_directory(project)
    original_fchmod = smoke.os.fchmod

    def fail_project_chmod(fd: int, mode: int) -> None:
        if fd == smoke_fd and mode == 0o770:
            raise OSError("injected chmod failure")
        original_fchmod(fd, mode)

    monkeypatch.setattr(smoke.os, "fchmod", fail_project_chmod)

    try:
        with pytest.raises(OSError, match="injected chmod failure"):
            smoke._publish_smoke_directory(
                smoke_dir,
                expected_identity=identity,
                smoke_fd=smoke_fd,
            )
        assert smoke_dir.stat().st_mode & 0o777 == 0o700
    finally:
        monkeypatch.setattr(smoke.os, "fchmod", original_fchmod)
        smoke._remove_smoke_directory(
            smoke_dir,
            expected_identity=identity,
            smoke_fd=smoke_fd,
        )
        os.close(smoke_fd)


def test_publish_refuses_replaced_project_entry_before_permission_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发布前同名入口被替换时，不得 chmod 或删除 replacement。"""

    smoke = load_smoke_service(monkeypatch)
    project = "safe-project"
    orphan = tmp_path / "orphan-project"
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    smoke_dir, identity, smoke_fd = smoke._create_smoke_directory(project)
    smoke_dir.rename(orphan)
    smoke_dir.mkdir(mode=0o755)
    try:
        with pytest.raises(RuntimeError, match="identity changed before publication"):
            smoke._publish_smoke_directory(
                smoke_dir,
                expected_identity=identity,
                smoke_fd=smoke_fd,
            )
        assert smoke_dir.stat().st_mode & 0o777 == 0o755
        assert orphan.stat().st_mode & 0o777 == 0o700
    finally:
        smoke_dir.rmdir()
        orphan.rename(smoke_dir)
        smoke._remove_smoke_directory(
            smoke_dir,
            expected_identity=identity,
            smoke_fd=smoke_fd,
        )
        os.close(smoke_fd)


def test_private_file_failure_closes_fd_and_removes_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """私有文件 fchmod 失败必须关闭 fd 并删除半成品。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    smoke_dir = app_root / ".agent-harness" / "safe-project"
    smoke_dir.mkdir(parents=True)
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)
    original_open = smoke.os.open
    original_fchmod = smoke.os.fchmod
    private_fd: int | None = None

    def record_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal private_fd
        fd = original_open(path, *args, **kwargs)
        if path == "storage-dsn.secret":
            private_fd = fd
        return fd

    def fail_private_fchmod(fd: int, mode: int) -> None:
        if fd == private_fd:
            raise OSError("injected private chmod failure")
        original_fchmod(fd, mode)

    monkeypatch.setattr(smoke.os, "open", record_open)
    monkeypatch.setattr(smoke.os, "fchmod", fail_private_fchmod)

    with pytest.raises(OSError, match="private chmod failure"):
        smoke._write_private_file(smoke_dir / "storage-dsn.secret", "SECRET", mode=0o640)

    assert private_fd is not None
    with pytest.raises(OSError):
        os.fstat(private_fd)
    assert not (smoke_dir / "storage-dsn.secret").exists()


def test_compose_command_rejects_replaced_smoke_directory(tmp_path: Path) -> None:
    """每次外部 Compose 副作用前都必须复核 smoke 目录 inode。"""

    support = load_smoke_support()
    smoke_dir = tmp_path / "safe-project"
    smoke_dir.mkdir()
    created = smoke_dir.stat()
    smoke_fd = os.open(smoke_dir, os.O_RDONLY | os.O_DIRECTORY)
    smoke_dir.rename(tmp_path / "orphan")
    smoke_dir.mkdir()
    env = {
        "SERVICE_APP_COMPOSE_PROJECT": "safe-project",
        "SERVICE_APP_SMOKE_DIR": str(smoke_dir),
        "SERVICE_APP_SMOKE_FD": str(smoke_fd),
        "SERVICE_APP_SMOKE_DEVICE": str(created.st_dev),
        "SERVICE_APP_SMOKE_INODE": str(created.st_ino),
    }

    with pytest.raises(RuntimeError, match="identity changed"):
        support._compose_command(env, "config")


def test_trace_writer_rejects_replaced_smoke_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """容器侧 trace writer 也必须在路径写入前复核创建 inode。"""

    smoke = load_smoke_service(monkeypatch)
    smoke_dir = tmp_path / "safe-project"
    smoke_dir.mkdir()
    created = smoke_dir.stat()
    smoke_fd = os.open(smoke_dir, os.O_RDONLY | os.O_DIRECTORY)
    smoke_dir.rename(tmp_path / "orphan")
    smoke_dir.mkdir()
    env = {
        "SERVICE_APP_SMOKE_DIR": str(smoke_dir),
        "SERVICE_APP_SMOKE_FD": str(smoke_fd),
        "SERVICE_APP_SMOKE_DEVICE": str(created.st_dev),
        "SERVICE_APP_SMOKE_INODE": str(created.st_ino),
    }

    try:
        with pytest.raises(RuntimeError, match="identity changed"):
            smoke.trace.write_service_trace(
                env,
                {"run_id": "run-1", "tenant_id": "tenant-1", "events": [{"type": "ok"}]},
            )
    finally:
        os.close(smoke_fd)

    assert not (smoke_dir / "trace.jsonl").exists()


def test_trace_writer_uses_stable_fd_after_path_check_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """路径检查后同名入口被替换时，trace 仍只能写进创建时 inode。"""

    smoke = load_smoke_service(monkeypatch)
    smoke_dir = tmp_path / "safe-project"
    smoke_dir.mkdir()
    created = smoke_dir.stat()
    smoke_fd = os.open(smoke_dir, os.O_RDONLY | os.O_DIRECTORY)
    orphan = tmp_path / "orphan"
    original_assert = smoke.trace.assert_smoke_directory_identity

    def check_then_replace(env: dict[str, str]) -> None:
        original_assert(env)
        smoke_dir.rename(orphan)
        smoke_dir.mkdir()

    monkeypatch.setattr(smoke.trace, "assert_smoke_directory_identity", check_then_replace)
    env = {
        "SERVICE_APP_SMOKE_DIR": str(smoke_dir),
        "SERVICE_APP_SMOKE_FD": str(smoke_fd),
        "SERVICE_APP_SMOKE_DEVICE": str(created.st_dev),
        "SERVICE_APP_SMOKE_INODE": str(created.st_ino),
    }
    try:
        smoke.trace.write_service_trace(
            env,
            {"run_id": "run-1", "tenant_id": "tenant-1", "events": [{"type": "ok"}]},
        )
    finally:
        os.close(smoke_fd)

    assert (orphan / "trace.jsonl").is_file()
    assert not (smoke_dir / "trace.jsonl").exists()
