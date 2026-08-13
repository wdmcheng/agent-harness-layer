"""服务 smoke 的 trace、失败清理与进程信号合同。"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from tests.contracts.service_deployment_test_support import (
    load_root_smoke,
    load_smoke_service,
)


def test_service_smoke_exports_postgresql_events_as_restricted_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service trace 必须来自 PostgreSQL inspect 事件，并以受限权限写入本轮隔离目录。"""

    smoke = load_smoke_service(monkeypatch)
    event = {
        "event_id": "event-1",
        "type": "run.completed",
        "seq": 3,
        "terminal": True,
        "visibility": "public",
        "request_id": "request-1",
        "trace_id": "trace-1",
        "payload": {"result": "ok"},
    }
    identity = tmp_path.stat()
    smoke_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    env = {
        "SERVICE_APP_SMOKE_DIR": str(tmp_path),
        "SERVICE_APP_SMOKE_FD": str(smoke_fd),
        "SERVICE_APP_SMOKE_DEVICE": str(identity.st_dev),
        "SERVICE_APP_SMOKE_INODE": str(identity.st_ino),
    }

    try:
        smoke.trace.write_service_trace(
            env,
            {"run_id": "run-1", "tenant_id": "tenant-1", "events": [event]},
        )
    finally:
        os.close(smoke_fd)

    trace = tmp_path / "trace.jsonl"
    records = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert records == [
        {
            "schema_version": "service-smoke-trace/v1",
            "source": "postgresql",
            "run_id": "run-1",
            "tenant_id": "tenant-1",
            "event": event,
        }
    ]
    assert trace.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize(
    ("platform", "host_uid", "expected_uid"),
    [("linux", 43101, "43101"), ("linux", 0, "10001"), ("darwin", 43101, "10001")],
)
def test_service_smoke_maps_runtime_uid_only_for_native_linux(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    host_uid: int,
    expected_uid: str,
) -> None:
    """普通 Linux 映射宿主 owner，root host 与 Docker Desktop 保留非 root 镜像用户。"""

    smoke = load_smoke_service(monkeypatch)
    monkeypatch.delenv("SERVICE_APP_RUNTIME_UID", raising=False)
    monkeypatch.setattr(smoke._runtime_uid.__globals__["sys"], "platform", platform)
    monkeypatch.setattr(smoke.os, "getuid", lambda: host_uid)

    assert smoke._runtime_uid() == expected_uid


@pytest.mark.parametrize(
    "override",
    ["", "0", "-1", "root", "１２３", "١٢٣", "2147483648"],
)
def test_service_smoke_rejects_invalid_or_root_runtime_uid_override(
    monkeypatch: pytest.MonkeyPatch,
    override: str,
) -> None:
    """显式 UID 覆盖必须保持非 root，不能绕过默认身份边界。"""

    smoke = load_smoke_service(monkeypatch)
    monkeypatch.setenv("SERVICE_APP_RUNTIME_UID", override)

    with pytest.raises(RuntimeError, match="positive non-root integer"):
        smoke._runtime_uid()


@pytest.mark.parametrize(
    "override",
    ["", "-1", "root", "１２３", "١٢٣", "2147483648"],
)
def test_service_smoke_rejects_invalid_runtime_gid_override(
    monkeypatch: pytest.MonkeyPatch,
    override: str,
) -> None:
    """显式 GID 覆盖只能是非负整数；root host 的 GID 0 仍需支持。"""

    smoke = load_smoke_service(monkeypatch)
    monkeypatch.setenv("SERVICE_APP_RUNTIME_GID", override)

    with pytest.raises(RuntimeError, match="non-negative integer"):
        smoke._runtime_gid()


def test_service_smoke_allows_root_host_group_for_restricted_shared_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """root runner 创建的 0640 文件由非 root UID 搭配宿主 GID 0 读取。"""

    smoke = load_smoke_service(monkeypatch)
    monkeypatch.setenv("SERVICE_APP_RUNTIME_GID", "0")

    assert smoke._runtime_gid() == "0"


def test_service_smoke_runtime_override_mounts_isolated_writable_eval_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任意受控Linux UID都必须使用本轮隔离eval目录，不能写镜像层。"""

    smoke = load_smoke_service(monkeypatch)
    override = yaml.safe_load(smoke.runtime_user_override_content("43101", "43102"))

    for service_name in ("migration", "api", "worker"):
        service = override["services"][service_name]
        assert service["user"] == "43101:43102"
        assert service["volumes"] == [
            {
                "type": "bind",
                "source": "${SERVICE_APP_SMOKE_DIR}/eval-cases",
                "target": "/app/eval-cases",
            }
        ]


@pytest.mark.parametrize(
    ("variable", "value", "expected"),
    [
        ("SERVICE_APP_RUNTIME_UID", "00042", "42"),
        ("SERVICE_APP_RUNTIME_UID", "2147483647", "2147483647"),
        ("SERVICE_APP_RUNTIME_GID", "000", "0"),
        ("SERVICE_APP_RUNTIME_GID", "2147483647", "2147483647"),
    ],
)
def test_service_smoke_normalizes_supported_numeric_identity(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    expected: str,
) -> None:
    """Docker 支持范围内的 ASCII 身份应规范化为十进制字符串。"""

    smoke = load_smoke_service(monkeypatch)
    monkeypatch.delenv("SERVICE_APP_RUNTIME_UID", raising=False)
    monkeypatch.delenv("SERVICE_APP_RUNTIME_GID", raising=False)
    monkeypatch.setenv(variable, value)

    actual = smoke._runtime_uid() if variable.endswith("UID") else smoke._runtime_gid()

    assert actual == expected


@pytest.mark.parametrize(
    ("variable", "value"),
    [("SERVICE_APP_RUNTIME_UID", "0"), ("SERVICE_APP_RUNTIME_GID", "root")],
)
def test_service_smoke_validates_runtime_identity_before_preparing_wheel(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    """非法运行身份必须在复制 wheel 前失败，避免下次 smoke 复用陈旧产物。"""

    smoke = load_smoke_service(monkeypatch)
    prepared: list[bool] = []
    monkeypatch.delenv("SERVICE_APP_RUNTIME_UID", raising=False)
    monkeypatch.delenv("SERVICE_APP_RUNTIME_GID", raising=False)
    monkeypatch.setenv(variable, value)
    monkeypatch.setattr(smoke, "parse_args", lambda: SimpleNamespace(migrate_only=False))
    monkeypatch.setattr(smoke, "prepare_core_wheel", lambda: prepared.append(True))

    with pytest.raises(RuntimeError, match="SERVICE_APP_RUNTIME_(UID|GID)"):
        smoke.main()

    assert prepared == []


@pytest.mark.parametrize(
    "project",
    ["", "..", "../../victim", "/tmp/agent-harness-victim", "nested/name", "Uppercase"],
)
def test_service_smoke_rejects_unsafe_project_before_preparing_wheel(
    monkeypatch: pytest.MonkeyPatch,
    project: str,
) -> None:
    """Compose project 必须是安全 ASCII 单段名称，并在任何 wheel/目录副作用前校验。"""

    smoke = load_smoke_service(monkeypatch)
    prepared: list[bool] = []
    monkeypatch.delenv("SERVICE_APP_RUNTIME_UID", raising=False)
    monkeypatch.delenv("SERVICE_APP_RUNTIME_GID", raising=False)
    monkeypatch.setenv("SERVICE_APP_COMPOSE_PROJECT", project)
    monkeypatch.setattr(smoke, "parse_args", lambda: SimpleNamespace(migrate_only=False))

    def forbidden_prepare() -> None:
        prepared.append(True)
        raise AssertionError("wheel preparation must not run")

    monkeypatch.setattr(smoke, "prepare_core_wheel", forbidden_prepare)

    with pytest.raises(RuntimeError, match="SERVICE_APP_COMPOSE_PROJECT"):
        smoke.main()

    assert prepared == []


def test_service_smoke_refuses_cleanup_outside_managed_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最终递归清理必须拒绝受管根外目录以及解析后越界的符号链接。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    managed_root = app_root / ".agent-harness"
    outside = tmp_path / "victim"
    managed_root.mkdir(parents=True)
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    symlink = managed_root / "safe-name"
    symlink.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)

    for unsafe in (outside, symlink):
        with pytest.raises(RuntimeError, match="managed smoke directory"):
            smoke._remove_smoke_directory(unsafe)

    assert marker.read_text(encoding="utf-8") == "keep"


def test_service_smoke_rejects_managed_root_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受管根本身不能解析到模板外，否则创建和递归清理都会越界。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    outside = tmp_path / "outside"
    app_root.mkdir()
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    (app_root / ".agent-harness").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)

    with pytest.raises(RuntimeError, match="managed smoke root"):
        smoke._managed_smoke_directory("safe-project")
    with pytest.raises(RuntimeError, match="managed smoke root"):
        smoke._remove_smoke_directory(app_root / ".agent-harness" / "safe-project")

    assert marker.read_text(encoding="utf-8") == "keep"


def test_service_smoke_rejects_existing_project_directory_before_preparing_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本轮目录必须独占创建，不能复用可预置内部 symlink 的旧目录。"""

    smoke = load_smoke_service(monkeypatch)
    project = "agent-harness-existing"
    smoke_dir = tmp_path / ".agent-harness" / project
    smoke_dir.mkdir(parents=True)
    outside = tmp_path / "outside.secret"
    outside.write_text("keep", encoding="utf-8")
    (smoke_dir / "storage-dsn.secret").symlink_to(outside)
    prepared: list[bool] = []
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    monkeypatch.setenv("SERVICE_APP_COMPOSE_PROJECT", project)
    monkeypatch.setattr(smoke, "parse_args", lambda: SimpleNamespace(migrate_only=False))
    monkeypatch.setattr(smoke, "prepare_core_wheel", lambda: prepared.append(True))

    with pytest.raises(RuntimeError, match="already exists"):
        smoke.main()

    assert prepared == []
    assert outside.read_text(encoding="utf-8") == "keep"


def test_private_file_write_refuses_existing_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """敏感文件必须独占且 no-follow 创建，不能通过内部 symlink 写穿。"""

    smoke = load_smoke_service(monkeypatch)
    app_root = tmp_path / "template"
    smoke_dir = app_root / ".agent-harness" / "safe-project"
    smoke_dir.mkdir(parents=True)
    outside = tmp_path / "outside.secret"
    outside.write_text("keep", encoding="utf-8")
    symlink = smoke_dir / "storage-dsn.secret"
    symlink.symlink_to(outside)
    monkeypatch.setattr(smoke, "APP_ROOT", app_root)

    with pytest.raises(RuntimeError, match="already exists"):
        smoke._write_private_file(symlink, "replacement", mode=0o640)

    assert outside.read_text(encoding="utf-8") == "keep"


def test_service_smoke_validates_directory_before_any_cleanup_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清理前目录被换成外部 symlink 时，不得先删除外部同名秘密文件。"""

    smoke = load_smoke_service(monkeypatch)
    project = "agent-harness-cleanup-swap"
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_names = (
        "storage-dsn.secret",
        "postgres-password.secret",
        "budget-fingerprint.secret",
    )
    for name in secret_names:
        (outside / name).write_text("keep", encoding="utf-8")

    def fail_smoke(*_args: object) -> dict[str, object]:
        raise RuntimeError("smoke failed")

    def swap_before_cleanup(env: dict[str, str], *, preserve_volume: bool) -> None:
        del preserve_volume
        smoke_dir = Path(env["SERVICE_APP_SMOKE_DIR"])
        shutil.rmtree(smoke_dir)
        smoke_dir.symlink_to(outside, target_is_directory=True)

    def no_compose_output(*_args: object, **_kwargs: object) -> str:
        return ""

    def cleanup_credential(*_args: object, **_kwargs: object) -> bool:
        return True

    def ignored_command(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    monkeypatch.setenv("SERVICE_APP_COMPOSE_PROJECT", project)
    monkeypatch.setattr(smoke, "parse_args", lambda: SimpleNamespace(migrate_only=False))
    monkeypatch.setattr(smoke, "prepare_core_wheel", lambda: None)
    monkeypatch.setattr(smoke, "free_port", lambda: 43126)
    monkeypatch.setattr(smoke, "run_service_smoke", fail_smoke)
    monkeypatch.setattr(smoke, "compose", no_compose_output)
    monkeypatch.setattr(smoke, "cleanup_credential_at_boundary", cleanup_credential)
    monkeypatch.setattr(smoke, "cleanup_project", swap_before_cleanup)
    monkeypatch.setattr(smoke, "run", ignored_command)

    with pytest.raises(RuntimeError, match="managed smoke directory"):
        smoke.main()

    assert {name: (outside / name).read_text(encoding="utf-8") for name in secret_names} == {
        name: "keep" for name in secret_names
    }


def test_service_smoke_allocates_port_before_directory_and_wheel_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端口分配失败必须发生在本轮目录创建和 wheel 准备之前。"""

    smoke = load_smoke_service(monkeypatch)
    project = "agent-harness-port-failure"
    prepared: list[bool] = []

    def prepare_wheel() -> None:
        prepared.append(True)
        wheel = tmp_path / ".agent-harness" / "agent_harness-test.whl"
        wheel.write_bytes(b"wheel")

    def fail_port() -> int:
        raise OSError("port allocation failed")

    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    monkeypatch.setenv("SERVICE_APP_COMPOSE_PROJECT", project)
    monkeypatch.setattr(smoke, "parse_args", lambda: SimpleNamespace(migrate_only=False))
    monkeypatch.setattr(smoke, "prepare_core_wheel", prepare_wheel)
    monkeypatch.setattr(smoke, "free_port", fail_port)

    with pytest.raises(OSError, match="port allocation failed"):
        smoke.main()

    assert prepared == []
    assert not (tmp_path / ".agent-harness" / project).exists()
    assert not list((tmp_path / ".agent-harness").glob("agent_harness-*.whl"))


def test_service_smoke_prepare_failure_preserves_preexisting_managed_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本轮失败不得删除预置或其他并发 smoke 共享的受管 wheel。"""

    smoke = load_smoke_service(monkeypatch)
    project = "agent-harness-wheel-owner"
    managed_root = tmp_path / ".agent-harness"
    managed_root.mkdir()
    wheel = managed_root / "agent_harness-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"PREEXISTING")

    def fail_prepare() -> None:
        raise RuntimeError("injected wheel prepare failure")

    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    monkeypatch.setenv("SERVICE_APP_COMPOSE_PROJECT", project)
    monkeypatch.setattr(smoke, "parse_args", lambda: SimpleNamespace(migrate_only=False))
    monkeypatch.setattr(smoke, "free_port", lambda: 43127)
    monkeypatch.setattr(smoke, "prepare_core_wheel", fail_prepare)

    with pytest.raises(RuntimeError, match="wheel prepare failure"):
        smoke.main()

    assert wheel.read_bytes() == b"PREEXISTING"


@pytest.mark.parametrize("failure", [RuntimeError("smoke failed"), KeyboardInterrupt()])
def test_service_smoke_cleans_secret_after_failure_or_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    """运行失败或中断时，临时密钥目录和 Compose 项目都必须被定向清理。"""

    smoke = load_smoke_service(monkeypatch)
    project = "agent-harness-cleanup-contract"
    cleanup_calls: list[tuple[str, bool]] = []
    runtime_users: list[tuple[str, str]] = []
    runtime_modes: list[tuple[int, int, int, int]] = []
    runtime_overrides: list[str] = []

    def fail_smoke(env: dict[str, str], *_args: object) -> dict[str, object]:
        """模拟业务冒烟在凭据已创建后失败的路径。"""

        runtime_users.append((env["SERVICE_APP_RUNTIME_UID"], env["SERVICE_APP_RUNTIME_GID"]))
        smoke_dir = Path(env["SERVICE_APP_SMOKE_DIR"])
        secret_path = Path(env["SERVICE_APP_STORAGE_DSN_FILE"])
        override_path = Path(env["SERVICE_APP_RUNTIME_USER_OVERRIDE_FILE"])
        eval_directory = smoke_dir / "eval-cases"
        runtime_overrides.append(override_path.read_text(encoding="utf-8"))
        runtime_modes.append(
            (
                smoke_dir.stat().st_mode & 0o777,
                secret_path.stat().st_mode & 0o777,
                override_path.stat().st_mode & 0o777,
                eval_directory.stat().st_mode & 0o777,
            )
        )
        raise failure

    def no_compose_output(*_args: object, **_kwargs: object) -> str:
        """避免测试依赖 Docker 命令输出；此路径只验证 finally 清理。"""

        return ""

    def cleanup_credential(*_args: object, **_kwargs: object) -> bool:
        """表示密钥边界清理已经成功执行。"""

        return True

    def record_cleanup(env: dict[str, str], *, preserve_volume: bool) -> None:
        """记录项目级清理参数，证明没有请求保留运行卷。"""

        cleanup_calls.append((env["SERVICE_APP_COMPOSE_PROJECT"], preserve_volume))

    def ignored_command(*_args: object, **_kwargs: object) -> SimpleNamespace:
        """为未参与断言的外部命令提供失败结果，防止实际执行。"""

        return SimpleNamespace(returncode=1)

    monkeypatch.setenv("SERVICE_APP_COMPOSE_PROJECT", project)
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    monkeypatch.setattr(smoke, "parse_args", lambda: SimpleNamespace(migrate_only=False))
    monkeypatch.setattr(smoke, "prepare_core_wheel", lambda: None)
    monkeypatch.setattr(smoke, "free_port", lambda: 43123)
    monkeypatch.setattr(smoke, "run_service_smoke", fail_smoke)
    monkeypatch.setattr(smoke, "compose", no_compose_output)
    monkeypatch.setattr(smoke, "cleanup_credential_at_boundary", cleanup_credential)
    monkeypatch.setattr(smoke, "cleanup_project", record_cleanup)
    monkeypatch.setattr(smoke, "run", ignored_command)

    # 无论异常类型如何，清理责任都在 main 的 finally，而非仅在成功路径触发。
    with pytest.raises(type(failure)):
        smoke.main()

    smoke_dir = tmp_path / ".agent-harness" / project
    assert not smoke_dir.exists()
    assert cleanup_calls == [(project, False)]
    host_uid = os.getuid()
    expected_uid = "10001" if sys.platform == "darwin" or host_uid == 0 else str(host_uid)
    assert runtime_users == [(expected_uid, str(os.getgid()))]
    assert runtime_modes == [(0o770, 0o640, 0o600, 0o770)]
    assert runtime_overrides == [
        "services:\n"
        f'  migration:\n    user: "{expected_uid}:{os.getgid()}"\n'
        "    volumes:\n"
        "      - type: bind\n"
        "        source: ${SERVICE_APP_SMOKE_DIR}/eval-cases\n"
        "        target: /app/eval-cases\n"
        f'  api:\n    user: "{expected_uid}:{os.getgid()}"\n'
        "    volumes:\n"
        "      - type: bind\n"
        "        source: ${SERVICE_APP_SMOKE_DIR}/eval-cases\n"
        "        target: /app/eval-cases\n"
        f'  worker:\n    user: "{expected_uid}:{os.getgid()}"\n'
        "    volumes:\n"
        "      - type: bind\n"
        "        source: ${SERVICE_APP_SMOKE_DIR}/eval-cases\n"
        "        target: /app/eval-cases\n"
    ]


def test_service_smoke_deletes_secret_when_project_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """项目清理失败不能阻断临时密钥删除，且调用环境不得泄露目录变量。"""

    smoke = load_smoke_service(monkeypatch)
    project = "agent-harness-cleanup-failure"

    def fail_smoke(*_args: object) -> dict[str, object]:
        """模拟触发 finally 块的业务失败。"""

        raise RuntimeError("smoke failed")

    def fail_cleanup(_env: dict[str, str], *, preserve_volume: bool) -> None:
        """模拟 Compose 资源清理自身报错，保留参数以匹配真实调用面。"""

        del preserve_volume
        raise RuntimeError("project cleanup failed")

    def no_compose_output(*_args: object, **_kwargs: object) -> str:
        """隔离外部 Compose 查询，避免该合同测试依赖本机 Docker。"""

        return ""

    def cleanup_credential(*_args: object, **_kwargs: object) -> bool:
        """表示密钥删除操作成功，关注其与项目清理失败的先后关系。"""

        return True

    def ignored_command(*_args: object, **_kwargs: object) -> SimpleNamespace:
        """阻止测试执行未被关注的外部子进程。"""

        return SimpleNamespace(returncode=1)

    monkeypatch.setenv("SERVICE_APP_COMPOSE_PROJECT", project)
    monkeypatch.setattr(smoke, "APP_ROOT", tmp_path)
    monkeypatch.setattr(smoke, "parse_args", lambda: SimpleNamespace(migrate_only=False))
    monkeypatch.setattr(smoke, "prepare_core_wheel", lambda: None)
    monkeypatch.setattr(smoke, "free_port", lambda: 43124)
    monkeypatch.setattr(smoke, "run_service_smoke", fail_smoke)
    monkeypatch.setattr(smoke, "compose", no_compose_output)
    monkeypatch.setattr(smoke, "cleanup_credential_at_boundary", cleanup_credential)
    monkeypatch.setattr(smoke, "cleanup_project", fail_cleanup)
    monkeypatch.setattr(smoke, "run", ignored_command)

    with pytest.raises(RuntimeError, match="project cleanup failed"):
        smoke.main()

    assert not (tmp_path / ".agent-harness" / project).exists()
    assert "SERVICE_APP_SMOKE_DIR" not in os.environ


def test_root_smoke_forwards_interrupt_to_child_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """根级冒烟收到中断后必须向独立子进程组转发信号并等待收尾。"""

    root_smoke = load_root_smoke()
    sent_signals: list[tuple[int, int]] = []

    class InterruptedProcess:
        """首次等待模拟中断，第二次等待模拟子进程正常退出。"""

        pid = 43125

        def __init__(self) -> None:
            self.wait_calls = 0

        def communicate(self) -> tuple[str, None]:
            raise KeyboardInterrupt

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            assert timeout == 30
            return 0

    process = InterruptedProcess()

    def fake_popen(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        start_new_session: bool,
        stdout: int,
        stderr: int,
        text: bool,
    ) -> InterruptedProcess:
        """校验根脚本以新进程组启动并捕获可归档的冒烟输出。"""

        assert cwd == tmp_path
        assert env["AGENT_HARNESS_SOURCE"] == str(tmp_path / "core.whl")
        assert start_new_session is True
        assert stdout == root_smoke.subprocess.PIPE
        assert stderr == root_smoke.subprocess.STDOUT
        assert text is True
        return process

    def record_signal(process_group: int, sent_signal: int) -> None:
        sent_signals.append((process_group, sent_signal))

    monkeypatch.setattr(root_smoke.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(root_smoke.os, "killpg", record_signal)

    with pytest.raises(KeyboardInterrupt):
        root_smoke._run_copied_smoke(
            ["make", "smoke-service"],
            tmp_path,
            tmp_path / "core.whl",
        )

    assert sent_signals == [(process.pid, root_smoke.signal.SIGINT)]
