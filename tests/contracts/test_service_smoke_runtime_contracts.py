"""服务 smoke 的 trace、失败清理与进程信号合同。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
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
    env = {"SERVICE_APP_SMOKE_DIR": str(tmp_path)}

    smoke.trace.write_service_trace(
        env,
        {"run_id": "run-1", "tenant_id": "tenant-1", "events": [event]},
    )

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
    runtime_groups: list[str] = []
    runtime_modes: list[tuple[int, int]] = []

    def fail_smoke(env: dict[str, str], *_args: object) -> dict[str, object]:
        """模拟业务冒烟在凭据已创建后失败的路径。"""

        runtime_groups.append(env["SERVICE_APP_RUNTIME_GID"])
        smoke_dir = Path(env["SERVICE_APP_SMOKE_DIR"])
        secret_path = Path(env["SERVICE_APP_STORAGE_DSN_FILE"])
        runtime_modes.append(
            (
                smoke_dir.stat().st_mode & 0o777,
                secret_path.stat().st_mode & 0o777,
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
    assert runtime_groups == [str(os.getgid())]
    assert runtime_modes == [(0o770, 0o640)]


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
