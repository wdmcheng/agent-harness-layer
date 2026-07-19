"""服务容器打包、冒烟链路与密钥加载边界的合同测试。"""

from __future__ import annotations

from tests.contracts.test_service_deployment_compose_contracts import (
    ROOT as ROOT,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    TEMPLATE as TEMPLATE,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    Any as Any,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    Path as Path,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    SimpleNamespace as SimpleNamespace,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    _compose as _compose,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    _root_smoke as _root_smoke,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    _smoke_service as _smoke_service,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    cast as cast,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    os as os,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    pytest as pytest,
)
from tests.contracts.test_service_deployment_compose_contracts import (
    yaml as yaml,
)


def test_compose_declares_migration_api_worker_and_shared_runtime_configuration() -> None:
    """Compose 必须为迁移、API 和 worker 提供一致的密钥与运行时边界。"""

    payload = _compose()
    services = payload["services"]
    assert {"postgres", "redis", "migration", "api", "worker"} <= services.keys()

    shared_keys = {
        "AGENT_HARNESS_STORAGE__DSN_FILE",
        "AGENT_HARNESS_QUEUE__DSN",
        "AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE",
        "SERVICE_APP_EXECUTOR_ID",
        "SERVICE_APP_RECLAIM_IDLE_SECONDS",
    }
    # 三个进程共享同一密钥来源，但只能通过文件挂载读取，不能回退到明文环境变量。
    for name in ("migration", "api", "worker"):
        assert shared_keys <= services[name]["environment"].keys()
        assert services[name]["profiles"] == ["service"]
        assert services[name]["environment"]["AGENT_HARNESS_STORAGE__DSN_FILE"] == (
            "/run/secrets/agent_harness_storage_dsn"
        )
        assert services[name]["secrets"] == [
            "agent_harness_storage_dsn",
            "agent_harness_budget_fingerprint_key",
        ]
        assert "AGENT_HARNESS_STORAGE__DSN" not in services[name]["environment"]

    assert "agent-harness-service" in services["api"]["command"]
    assert "app.workers.runtime_worker" in services["worker"]["command"]
    assert "app.migrate" in services["migration"]["command"]
    assert payload["secrets"]["agent_harness_storage_dsn"]["file"] == (
        "${SERVICE_APP_STORAGE_DSN_FILE:?SERVICE_APP_STORAGE_DSN_FILE is required}"
    )
    assert services["postgres"]["environment"]["POSTGRES_PASSWORD_FILE"] == (
        "/run/secrets/agent_harness_postgres_password"
    )
    assert "POSTGRES_PASSWORD" not in services["postgres"]["environment"]
    assert services["postgres"]["secrets"] == ["agent_harness_postgres_password"]
    assert payload["secrets"]["agent_harness_postgres_password"]["file"] == (
        "${SERVICE_APP_POSTGRES_PASSWORD_FILE:?SERVICE_APP_POSTGRES_PASSWORD_FILE is required}"
    )
    assert services["api"]["depends_on"]["migration"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["worker"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert "healthcheck" in services["api"]
    assert "healthcheck" in services["worker"]


def test_container_build_requires_core_wheel_and_does_not_copy_workspace_source() -> None:
    """镜像构建只能安装已产出的核心 wheel，避免把宿主工作区带入运行镜像。"""

    dockerfile = (TEMPLATE / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (TEMPLATE / ".dockerignore").read_text(encoding="utf-8")

    assert "COPY .agent-harness/agent_harness-*.whl" in dockerfile
    assert "packages/agent-harness" not in dockerfile
    assert "PYTHONPATH" not in dockerfile
    assert "--no-deps ." in dockerfile
    assert "!.agent-harness/agent_harness-*.whl" in dockerignore
    assert ".env" in dockerignore


def test_service_smoke_uses_http_auth_crash_reclaim_checkpoint_and_scoped_cleanup() -> None:
    """服务冒烟脚本需覆盖关键恢复链路，并将清理范围约束在当前项目。"""

    template_smoke = (TEMPLATE / "scripts" / "smoke_service.py").read_text(encoding="utf-8")
    secret_smoke = (TEMPLATE / "scripts" / "service_secret_smoke.py").read_text(encoding="utf-8")
    approval_smoke = (TEMPLATE / "scripts" / "service_approval_smoke.py").read_text(
        encoding="utf-8"
    )
    root_smoke = (ROOT / "scripts" / "smoke_service.py").read_text(encoding="utf-8")

    # 这些标记分别来自运行冒烟、密钥冒烟和审批冒烟，组合后覆盖跨进程恢复边界。
    for marker in (
        "missing_status",
        "invalid_status",
        "XPENDING",
        "SERVICE_APP_SMOKE_CRASH_AFTER_OWNER",
        "hard_crash_exit",
        "checkpoint_id",
        "enqueue_recovery",
        "continuations",
        "stale_receipt_rejected",
        "worker-a-receipt.json",
        "worker-b-receipt.json",
        "reclaim-release",
        "SERVICE_APP_SMOKE_FAIL_AFTER_BOOTSTRAP",
        "SERVICE_APP_KEEP_DATA",
        '"postgresql"',
        "cleanup_project",
        "SERVICE_APP_STORAGE_DSN_FILE",
        "SERVICE_APP_POSTGRES_PASSWORD_FILE",
        "storage-dsn.secret",
        "verify_secret_failure_cases",
        '"symlink": True',
        '"conflict": True',
        '"postgres_password_file": True',
        '"compose_config_redacted": True',
    ):
        assert marker in template_smoke or marker in secret_smoke or marker in approval_smoke
    for marker in (
        '"unreadable": True',
        '"api_worker_readiness_blocked": True',
        '"side_effects": False',
        "AGENT_HARNESS_STORAGE__DSN_FILE=/run/secrets/symlink-fixture/storage-dsn-link",
        ":/run/secrets/symlink-fixture:ro",
        "assert_configuration_secret_absent",
        'compose(env, "config")',
        '"compose-config"',
        "path not in {secret_path, postgres_password_path}",
        '"doctor"',
        '"pg_dump"',
    ):
        assert marker in secret_smoke
    assert "TemporaryDirectory" in root_smoke
    assert "copytree" in root_smoke
    assert "wheel.name" in root_smoke
    assert "secret-cleanup=ok" in root_smoke
    assert "docker system prune" not in template_smoke
    assert "secret_path.unlink" in template_smoke
    support = (TEMPLATE / "scripts" / "service_smoke_support.py").read_text(encoding="utf-8")
    assert '"terminal_event"' in support
    assert '"docker", "network", "inspect", network' in support
    assert '"docker", "volume", "inspect", volume' in support


@pytest.mark.parametrize("failure", [RuntimeError("smoke failed"), KeyboardInterrupt()])
def test_service_smoke_cleans_secret_after_failure_or_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    """运行失败或中断时，临时密钥目录和 Compose 项目都必须被定向清理。"""

    smoke = _smoke_service(monkeypatch)
    project = "agent-harness-cleanup-contract"
    cleanup_calls: list[tuple[str, bool]] = []

    def fail_smoke(*_args: object) -> dict[str, object]:
        """模拟业务冒烟在凭据已创建后失败的路径。"""

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
    monkeypatch.setattr(smoke, "_run_smoke", fail_smoke)
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


def test_service_smoke_deletes_secret_when_project_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """项目清理失败不能阻断临时密钥删除，且调用环境不得泄露目录变量。"""

    smoke = _smoke_service(monkeypatch)
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
    monkeypatch.setattr(smoke, "_run_smoke", fail_smoke)
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

    root_smoke = _root_smoke()
    sent_signals: list[tuple[int, int]] = []

    class InterruptedProcess:
        """首次等待时模拟用户中断、第二次等待时模拟子进程正常退出的进程替身。"""

        pid = 43125

        def __init__(self) -> None:
            """初始化等待次数，以便验证中断后的有限等待窗口。"""

            self.wait_calls = 0

        def wait(self, timeout: float | None = None) -> int:
            """按预设顺序暴露中断和收尾完成，验证父进程的信号处理分支。"""

            self.wait_calls += 1
            if self.wait_calls == 1:
                assert timeout is None
                raise KeyboardInterrupt
            assert timeout == 30
            return 0

    process = InterruptedProcess()

    def fake_popen(
        _command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        start_new_session: bool,
    ) -> InterruptedProcess:
        """校验根脚本以新进程组启动复制后的冒烟命令。"""

        assert cwd == tmp_path
        assert env["AGENT_HARNESS_SOURCE"] == str(tmp_path / "core.whl")
        assert start_new_session is True
        return process

    def record_signal(process_group: int, sent_signal: int) -> None:
        """记录信号目标，避免测试真正向系统进程发送中断。"""

        sent_signals.append((process_group, sent_signal))

    monkeypatch.setattr(root_smoke.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(root_smoke.os, "killpg", record_signal)

    # 调用方仍应感知原始中断，脚本只负责在抛出前通知并等待子进程组。
    with pytest.raises(KeyboardInterrupt):
        root_smoke._run_copied_smoke(
            ["make", "smoke-service"],
            tmp_path,
            tmp_path / "core.whl",
        )

    assert sent_signals == [(process.pid, root_smoke.signal.SIGINT)]


def test_service_profile_and_admin_do_not_bypass_typed_secret_loader() -> None:
    """服务配置和管理脚本必须复用类型化密钥加载器，不能直接读取敏感环境变量。"""

    profile = cast(
        dict[str, Any],
        yaml.safe_load(
            (TEMPLATE / "configs" / "profiles" / "service.yaml").read_text(encoding="utf-8")
        ),
    )
    admin = (TEMPLATE / "scripts" / "service_admin.py").read_text(encoding="utf-8")
    env_example = (TEMPLATE / ".env.example").read_text(encoding="utf-8")

    assert profile["storage"]["dsn"] is None
    assert "load_settings" in admin
    assert "storage_dsn_from_settings" in admin
    assert 'os.environ.get("AGENT_HARNESS_STORAGE__DSN"' not in admin
    assert "SERVICE_APP_STORAGE_DSN_FILE=.agent-harness/secrets/storage-dsn" in env_example
    assert (
        "SERVICE_APP_POSTGRES_PASSWORD_FILE=.agent-harness/secrets/postgres-password" in env_example
    )
    assert "rm -f .agent-harness/secrets/storage-dsn" in env_example
