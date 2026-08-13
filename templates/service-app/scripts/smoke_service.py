"""以隔离 Compose project 验证 HTTP、Redis、DBOS、PostgreSQL 与审批恢复。"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

import service_smoke_filesystem as smoke_filesystem
import service_smoke_lifecycle as smoke_lifecycle
import service_smoke_runtime as smoke_runtime
from service_secret_smoke import (
    verify_secret_failure_cases,
)
from service_smoke_operations import (
    parse_args,
)
from service_smoke_scenarios import run_service_smoke
from service_smoke_support import (
    cleanup_credential_at_boundary,
    cleanup_project,
    compose,
    failure_diagnostic,
    free_port,
    preserve_postgres_volume,
    run,
)
from service_smoke_trace import export_service_trace
from service_smoke_wheel import prepare_core_wheel

APP_ROOT = Path(__file__).resolve().parents[1]
STREAM = "agent-harness:service:runs:stream"
GROUP = "agent-harness-workers"

_clear_directory_fd = smoke_filesystem.clear_directory_fd
managed_smoke_directory = smoke_filesystem.managed_smoke_directory
open_smoke_directory = smoke_filesystem.open_smoke_directory
write_private_file = smoke_filesystem.write_private_file
_lifecycle_create_smoke_directory = smoke_lifecycle.create_smoke_directory
_lifecycle_create_smoke_subdirectory = smoke_lifecycle.create_smoke_subdirectory
_lifecycle_publish_smoke_directory = smoke_lifecycle.publish_smoke_directory
_lifecycle_remove_smoke_directory = smoke_lifecycle.remove_smoke_directory
_lifecycle_unlink_managed_root_file = smoke_lifecycle.unlink_managed_root_file
_compose_project = smoke_runtime.compose_project
_runtime_gid = smoke_runtime.runtime_gid
_runtime_uid = smoke_runtime.runtime_uid
runtime_user_override_content = smoke_runtime.runtime_user_override_content
_server_versions = smoke_runtime.server_versions


def _managed_smoke_directory(project: str) -> Path:
    """按当前模板根验证并返回待独占创建的本轮目录。"""

    return managed_smoke_directory(APP_ROOT, project)


def _create_smoke_directory(project: str) -> tuple[Path, tuple[int, int], int]:
    """独占创建本轮目录，返回其路径、稳定身份和持续持有的目录句柄。"""

    return _lifecycle_create_smoke_directory(APP_ROOT, project)


def _open_smoke_directory(
    smoke_dir: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """按当前模板根打开并绑定本轮目录身份。"""

    return open_smoke_directory(
        APP_ROOT,
        smoke_dir,
        expected_identity=expected_identity,
    )


def _create_smoke_subdirectory(
    smoke_dir: Path,
    name: str,
    *,
    expected_identity: tuple[int, int],
) -> None:
    """相对本轮目录句柄独占创建容器共享子目录。"""

    _lifecycle_create_smoke_subdirectory(
        APP_ROOT,
        smoke_dir,
        name,
        expected_identity=expected_identity,
    )


def _remove_smoke_directory(
    smoke_dir: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    smoke_fd: int | None = None,
) -> None:
    """先隔离创建时 inode，再清空内容并按 holder 身份删除目录入口。"""

    _lifecycle_remove_smoke_directory(
        APP_ROOT,
        smoke_dir,
        expected_identity=expected_identity,
        smoke_fd=smoke_fd,
        clear_directory=_clear_directory_fd,
    )


def _publish_smoke_directory(
    smoke_dir: Path,
    *,
    expected_identity: tuple[int, int],
    smoke_fd: int,
) -> None:
    """在私有初始化完成后经稳定 fd 开放本轮目录。"""

    _lifecycle_publish_smoke_directory(
        APP_ROOT,
        smoke_dir,
        expected_identity=expected_identity,
        smoke_fd=smoke_fd,
    )


def _write_private_file(
    path: Path,
    content: str,
    *,
    mode: int,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    """按当前模板根独占写入本轮私有文件。"""

    write_private_file(
        APP_ROOT,
        path,
        content,
        mode=mode,
        expected_identity=expected_identity,
    )


def _unlink_managed_root_file(path: Path) -> None:
    """通过受管根句柄删除一个根级普通文件或 symlink 本身。"""

    _lifecycle_unlink_managed_root_file(APP_ROOT, path)


def main() -> int:
    """创建一次性隔离环境，运行 smoke，并在所有退出路径清除临时凭据与资源。

    即使中途失败，也先尝试删除 bootstrap credential，再依据显式保留条件决定
    是否保留 PostgreSQL volume；所有 token、DSN 与预算指纹文件均以受限权限
    写入，finally 中必须删除，防止本地调试残留泄露到下一次运行。
    """
    args = parse_args()
    # 在构建或复制 wheel 之前拒绝非法身份，避免失败路径留下可被下次复用的产物。
    runtime_uid = _runtime_uid()
    runtime_gid = _runtime_gid()
    project = _compose_project()
    # 先完成端口等可能失败但无文件副作用的初始化，再创建本轮目录与 wheel。
    smoke_dir = _managed_smoke_directory(project)
    database_password = secrets.token_urlsafe(24)
    secret_path = smoke_dir / "storage-dsn.secret"
    postgres_password_path = smoke_dir / "postgres-password.secret"
    budget_fingerprint_path = smoke_dir / "budget-fingerprint.secret"
    runtime_user_override_path = smoke_dir / "runtime-user.override.yml"
    runtime_user_override = runtime_user_override_content(runtime_uid, runtime_gid)
    token = secrets.token_urlsafe(32)
    tenant_id = f"smoke-{uuid4()}"
    # 原生 Linux 保留 bind mount 数值所有权；Docker Desktop 不提供同一语义，
    # 因而继续使用镜像内已声明的 harness UID，只映射共享文件所需的宿主 GID。
    env = {
        **os.environ,
        "SERVICE_APP_COMPOSE_PROJECT": project,
        "SERVICE_APP_API_PORT": str(free_port()),
        "SERVICE_APP_RUNTIME_UID": runtime_uid,
        "SERVICE_APP_RUNTIME_GID": runtime_gid,
        "SERVICE_APP_RUNTIME_USER_OVERRIDE_FILE": str(runtime_user_override_path),
        "SERVICE_APP_RUNTIME_USER_OVERRIDE_CONTENT": runtime_user_override,
        "SERVICE_APP_SMOKE_DIR": str(smoke_dir),
        "SERVICE_APP_STORAGE_DSN_FILE": str(secret_path),
        "SERVICE_APP_POSTGRES_PASSWORD_FILE": str(postgres_password_path),
        "SERVICE_APP_BUDGET_FINGERPRINT_KEY_FILE": str(budget_fingerprint_path),
        "SERVICE_APP_BOOTSTRAP_TENANT": tenant_id,
        "SERVICE_APP_RECLAIM_IDLE_SECONDS": os.environ.get("SERVICE_APP_RECLAIM_IDLE_SECONDS", "1"),
        "SERVICE_APP_IMAGE": f"agent-harness-service-app:{project}",
        "SERVICE_APP_SMOKE_BOUNDARY": "startup",
    }
    keep_data = os.environ.get("SERVICE_APP_KEEP_DATA") == "1"
    credential_cleanup_confirmed = args.migrate_only
    credential_cleanup_needed = False
    worker_a = f"{project}-worker-a"
    approval_write_worker = f"{project}-approval-write-fail"
    approval_ack_worker = f"{project}-approval-ack-fail"
    trace_export = APP_ROOT / ".agent-harness/service-smoke-trace.jsonl"
    smoke_dir, smoke_identity, smoke_fd = _create_smoke_directory(project)
    env["SERVICE_APP_SMOKE_DEVICE"] = str(smoke_identity[0])
    env["SERVICE_APP_SMOKE_INODE"] = str(smoke_identity[1])
    env["SERVICE_APP_SMOKE_FD"] = str(smoke_fd)
    try:
        prepare_core_wheel()
    except BaseException:
        try:
            _remove_smoke_directory(
                smoke_dir,
                expected_identity=smoke_identity,
                smoke_fd=smoke_fd,
            )
        finally:
            os.close(smoke_fd)
        raise
    try:
        os.environ["SERVICE_APP_SMOKE_DIR"] = str(smoke_dir)
        _unlink_managed_root_file(trace_export)
        _create_smoke_subdirectory(
            smoke_dir,
            "workspace",
            expected_identity=smoke_identity,
        )
        _create_smoke_subdirectory(
            smoke_dir,
            "artifacts",
            expected_identity=smoke_identity,
        )
        # service eval review queue 是运行期可写状态。Linux smoke 使用宿主UID时
        # 不能写镜像层的/app/eval-cases，因此与workspace/artifacts一样挂进
        # 本轮隔离目录；ReviewDatasetAdapter会按需创建drafts/approved子目录。
        _create_smoke_subdirectory(
            smoke_dir,
            "eval-cases",
            expected_identity=smoke_identity,
        )
        _write_private_file(
            runtime_user_override_path,
            runtime_user_override,
            mode=0o600,
            expected_identity=smoke_identity,
        )
        _write_private_file(
            secret_path,
            f"postgresql+asyncpg://agent_harness:{database_password}@postgres:5432/agent_harness",
            mode=0o640,
            expected_identity=smoke_identity,
        )
        _write_private_file(
            postgres_password_path,
            database_password,
            mode=0o640,
            expected_identity=smoke_identity,
        )
        _write_private_file(
            budget_fingerprint_path,
            secrets.token_urlsafe(48),
            mode=0o640,
            expected_identity=smoke_identity,
        )
        _publish_smoke_directory(
            smoke_dir,
            expected_identity=smoke_identity,
            smoke_fd=smoke_fd,
        )
        if args.migrate_only:
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "image-build"
            compose(env, "build", "migration")
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "postgres-readiness"
            compose(env, "up", "-d", "--wait", "postgres")
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-failure-contracts"
            secret_failures = verify_secret_failure_cases(env)
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "migration"
            compose(env, "run", "--rm", "migration")
            evidence: dict[str, object] = {
                "migration": "0018_model_tool_loop_state",
                "secret_file": {
                    "consumers": ["migration"],
                    "postgres_password_file": True,
                    "compose_config_redacted": True,
                    "redacted": True,
                    "failure_cases": secret_failures,
                },
            }
        else:
            credential_cleanup_needed = True
            evidence = run_service_smoke(env, token, tenant_id)
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "credential-cleanup"
            if not cleanup_credential_at_boundary(env, token):
                raise RuntimeError("service smoke credential cleanup did not delete one record")
            credential_cleanup_confirmed = True
            credential_cleanup_needed = False
            evidence["credential_cleanup"] = {"deleted": 1}
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "server-version-evidence"
            evidence["server_versions"] = _server_versions(env, compose_runner=compose)
            env["SERVICE_APP_SMOKE_BOUNDARY"] = "trace-export"
            trace_source = smoke_dir / "trace.jsonl"
            if not trace_source.is_file() or trace_source.stat().st_size == 0:
                raise RuntimeError("service smoke runtime trace is missing")
            trace_root_fd, trace_smoke_fd = _open_smoke_directory(
                smoke_dir,
                expected_identity=smoke_identity,
            )
            try:
                export_service_trace(
                    trace_root_fd,
                    trace_smoke_fd,
                    trace_export.name,
                    project,
                )
            finally:
                os.close(trace_smoke_fd)
                os.close(trace_root_fd)
        print("smoke-service: " + json.dumps(evidence, ensure_ascii=False, sort_keys=True))
        print("smoke-service: ok")
        return 0
    except Exception:
        compose(env, "logs", "--no-color", "--tail", "120", "api", "worker", check=False)
        diagnostic = failure_diagnostic(env["SERVICE_APP_SMOKE_BOUNDARY"], env)
        raise RuntimeError(diagnostic) from None
    finally:
        try:
            if credential_cleanup_needed and not credential_cleanup_confirmed:
                credential_cleanup_confirmed = cleanup_credential_at_boundary(
                    env,
                    token,
                    check=False,
                )
        finally:
            preserve_volume = preserve_postgres_volume(
                keep_data,
                credential_cleanup_confirmed=credential_cleanup_confirmed,
            )
            try:
                run(
                    [
                        "docker",
                        "rm",
                        "-f",
                        worker_a,
                        approval_write_worker,
                        approval_ack_worker,
                    ],
                    env=env,
                    check=False,
                )
            finally:
                try:
                    cleanup_project(env, preserve_volume=preserve_volume)
                finally:
                    try:
                        _remove_smoke_directory(
                            smoke_dir,
                            expected_identity=smoke_identity,
                            smoke_fd=smoke_fd,
                        )
                    finally:
                        os.close(smoke_fd)
                        os.environ.pop("SERVICE_APP_SMOKE_DIR", None)
        if preserve_volume:
            volume = f"{project}_agent_harness_postgres_data"
            inspected = run(["docker", "volume", "inspect", volume], env=env, check=False)
            if inspected.returncode == 0:
                print(f"smoke-service: kept volume={volume}")
                print(f"smoke-service: cleanup=docker volume rm {volume}")


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:
        print(str(exc))
        exit_code = 1
    raise SystemExit(exit_code)
