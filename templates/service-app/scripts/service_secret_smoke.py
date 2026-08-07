"""Service smoke 的 secret failure matrix 与公开 evidence 脱敏验证。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from service_smoke_support import compose, compose_result, failure_diagnostic, stream_length

Request = Callable[[str, str, str], tuple[int, dict[str, Any]]]


def _postgres_password(env: dict[str, str]) -> str:
    """只在 smoke 进程内读取临时密码，用于泄漏断言。"""

    path = Path(env["SERVICE_APP_POSTGRES_PASSWORD_FILE"])
    return path.read_text(encoding="utf-8")


def _expect_secret_startup_failure(
    env: dict[str, str],
    case: str,
    *run_args: str,
    expected_code: str = "config.secret_file_invalid",
    expected_hint: str = "使用受信 root 内绝对、可读、非空且不超过 64 KiB 的普通 UTF-8 文件",
    forbidden: tuple[str, ...] = (),
) -> None:
    """证明无效 secret 阻止 migration readiness，公开诊断只保留安全 case。"""

    env["SERVICE_APP_SMOKE_BOUNDARY"] = f"secret-contract-{case}"
    result = compose_result(env, "run", "--rm", *run_args, "migration")
    if result.returncode == 0:
        raise RuntimeError(f"secret failure case unexpectedly started migration: {case}")
    raw_diagnostic = "\n".join((result.stdout, result.stderr))
    expected_parts = {
        "code": expected_code,
        "field": "field=storage.dsn",
        "hint": f"hint={expected_hint}",
    }
    for part_name, expected in expected_parts.items():
        if expected not in raw_diagnostic:
            category = next(
                (
                    label
                    for label, marker in (
                        ("permission-denied", "Permission denied"),
                        ("operation-not-permitted", "Operation not permitted"),
                        ("invalid-mount", "invalid mount config"),
                        ("structured-other", "config."),
                    )
                    if marker in raw_diagnostic
                ),
                "other",
            )
            env["SERVICE_APP_SMOKE_BOUNDARY"] = (
                f"secret-contract-{case}-missing-{part_name}-{category}"
            )
            raise RuntimeError(f"secret failure case did not reach typed loader diagnostic: {case}")
    diagnostic = failure_diagnostic(
        f"secret-{case}",
        env,
        raw_detail=raw_diagnostic,
        exit_code=result.returncode,
    )
    protected = (
        *forbidden,
        _postgres_password(env),
        env.get("SERVICE_APP_STORAGE_DSN_FILE", ""),
        env.get("SERVICE_APP_POSTGRES_PASSWORD_FILE", ""),
    )
    for value in protected:
        if value and (value in diagnostic or value in raw_diagnostic):
            raise RuntimeError(f"secret failure diagnostic leaked protected value: {case}")


def _assert_api_worker_readiness_blocked(env: dict[str, str], original_secret: bytes) -> None:
    """以真实 Compose dependency/readiness 证明无效配置不会启动 API 或 worker。"""

    secret_path = Path(env["SERVICE_APP_STORAGE_DSN_FILE"])
    secret_path.write_bytes(b"")
    try:
        env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-contract-api-worker-readiness"
        result = compose_result(env, "up", "-d", "--wait", "api", "worker")
        if result.returncode == 0:
            raise RuntimeError("invalid secret unexpectedly made api/worker ready")
        running = compose_result(
            env,
            "ps",
            "--status",
            "running",
            "--services",
            "api",
            "worker",
        )
        if running.stdout.strip():
            raise RuntimeError("invalid secret left api/worker running")
    finally:
        compose_result(env, "rm", "-sf", "migration", "api", "worker")
        secret_path.write_bytes(original_secret)
        secret_path.chmod(0o640)


def verify_secret_failure_cases(env: dict[str, str]) -> dict[str, bool]:
    """在真实 Compose migration 边界验证所有受控 secret 拒绝场景。"""

    smoke_dir = Path(env["SERVICE_APP_SMOKE_DIR"])
    secret_path = Path(env["SERVICE_APP_STORAGE_DSN_FILE"])
    original_secret = secret_path.read_bytes()
    public_tables_before = compose(
        env,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "agent_harness",
        "-d",
        "agent_harness",
        "-At",
        "-c",
        "select count(*) from pg_tables where schemaname = 'public';",
    )
    queue_before = stream_length(env, "agent-harness:service:runs:stream")

    _expect_secret_startup_failure(
        env,
        "missing",
        "-e",
        "AGENT_HARNESS_STORAGE__DSN_FILE=/run/secrets/missing-storage-dsn",
    )

    secret_path.write_bytes(b"")
    try:
        _expect_secret_startup_failure(env, "empty")
    finally:
        secret_path.write_bytes(original_secret)
        secret_path.chmod(0o640)

    unreadable_dir = smoke_dir / "unreadable-secret-fixture"
    unreadable_dir.mkdir()
    unreadable_path = unreadable_dir / "storage-dsn.secret"
    unreadable_path.write_bytes(original_secret)
    unreadable_path.chmod(0)
    try:
        _expect_secret_startup_failure(
            env,
            "unreadable",
            "-e",
            "AGENT_HARNESS_STORAGE__DSN_FILE=/run/secrets/unreadable-fixture/storage-dsn.secret",
            "-v",
            f"{unreadable_dir}:/run/secrets/unreadable-fixture:ro",
            forbidden=(str(unreadable_dir), str(unreadable_path)),
        )
    finally:
        unreadable_path.unlink(missing_ok=True)
        unreadable_dir.rmdir()

    symlink_dir = smoke_dir / "symlink-secret-fixture"
    symlink_dir.mkdir()
    symlink_target = symlink_dir / "storage-dsn-target"
    symlink_target.write_bytes(original_secret)
    symlink = symlink_dir / "storage-dsn-link"
    symlink.symlink_to(symlink_target.name)
    outside = smoke_dir / "outside-storage-dsn.secret"
    outside.write_bytes(original_secret)
    try:
        _expect_secret_startup_failure(
            env,
            "symlink",
            "-e",
            "AGENT_HARNESS_STORAGE__DSN_FILE=/run/secrets/symlink-fixture/storage-dsn-link",
            "-v",
            f"{symlink_dir}:/run/secrets/symlink-fixture:ro",
            forbidden=(str(symlink_dir), str(symlink_target), original_secret.decode()),
        )
        _expect_secret_startup_failure(
            env,
            "outside",
            "-e",
            f"AGENT_HARNESS_STORAGE__DSN_FILE=/smoke/{outside.name}",
            forbidden=(str(outside),),
        )
        direct_fixture = "postgresql+asyncpg://agent:direct-conflict-secret@postgres/app"
        _expect_secret_startup_failure(
            env,
            "conflict",
            "-e",
            f"AGENT_HARNESS_STORAGE__DSN={direct_fixture}",
            expected_code="config.secret_file_conflict",
            expected_hint="只设置 direct env 或对应的 _FILE，移除另一个输入",
            forbidden=(direct_fixture,),
        )
    finally:
        symlink.unlink(missing_ok=True)
        symlink_target.unlink(missing_ok=True)
        symlink_dir.rmdir()
        outside.unlink(missing_ok=True)
    _assert_api_worker_readiness_blocked(env, original_secret)
    public_tables_after = compose(
        env,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "agent_harness",
        "-d",
        "agent_harness",
        "-At",
        "-c",
        "select count(*) from pg_tables where schemaname = 'public';",
    )
    queue_after = stream_length(env, "agent-harness:service:runs:stream")
    if (public_tables_before, queue_before) != (public_tables_after, queue_after):
        raise RuntimeError("secret failure cases created migration or queue side effects")
    return {
        "missing": True,
        "unreadable": True,
        "empty": True,
        "symlink": True,
        "outside": True,
        "conflict": True,
        "api_worker_readiness_blocked": True,
        "side_effects": False,
    }


def assert_configuration_secret_absent(
    env: dict[str, str],
    *,
    base_url: str,
    evidence: dict[str, object],
    request: Request,
) -> None:
    """扫描公开/持久化观测面，排除本轮唯一 storage secret。"""

    secret_path = Path(env["SERVICE_APP_STORAGE_DSN_FILE"])
    postgres_password_path = Path(env["SERVICE_APP_POSTGRES_PASSWORD_FILE"])
    postgres_password = _postgres_password(env)
    protected_values = {
        "storage-dsn": secret_path.read_text(encoding="utf-8"),
        "postgres-password": postgres_password,
    }
    protected_paths = {
        "host-storage-secret-path": str(secret_path),
        "host-postgres-secret-path": str(postgres_password_path),
    }
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-evidence-health"
    health_status, health = request(base_url, "GET", "/api/v1/health")
    if health_status != 200:
        raise RuntimeError("health endpoint was unavailable during secret evidence scan")
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-evidence-doctor"
    doctor = compose(
        env,
        "run",
        "--rm",
        "migration",
        "agent-harness",
        "doctor",
        "--profile",
        "service",
        "--profiles-dir",
        "/app/configs/profiles",
    )
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-evidence-logs"
    logs = compose(env, "logs", "--no-color", "api", "worker")
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-evidence-compose-config"
    compose_config = compose(env, "config")
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-evidence-postgres"
    database_dump = compose(
        env,
        "exec",
        "-T",
        "postgres",
        "pg_dump",
        "-U",
        "agent_harness",
        "-d",
        "agent_harness",
        "--data-only",
    )
    surfaces = [
        ("smoke-evidence", json.dumps(evidence, ensure_ascii=False, sort_keys=True)),
        ("health", json.dumps(health, ensure_ascii=False, sort_keys=True)),
        ("doctor", doctor),
        ("service-logs", logs),
        ("compose-config", compose_config),
        ("postgres-data", database_dump),
    ]
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-evidence-artifacts"
    for path in Path(env["SERVICE_APP_SMOKE_DIR"]).rglob("*"):
        if path.is_file() and path not in {secret_path, postgres_password_path}:
            try:
                surfaces.append(
                    (
                        f"smoke-artifact:{path.relative_to(Path(env['SERVICE_APP_SMOKE_DIR']))}",
                        path.read_text(encoding="utf-8"),
                    )
                )
            except UnicodeDecodeError:
                payload = path.read_bytes()
                for protected_name, protected in {
                    **protected_values,
                    **protected_paths,
                }.items():
                    if protected.encode() in payload:
                        raise RuntimeError(
                            "binary smoke artifact leaked protected configuration "
                            f"value={protected_name}"
                        ) from None
    for surface_name, surface in surfaces:
        env["SERVICE_APP_SMOKE_BOUNDARY"] = f"secret-evidence-scan-{surface_name.split(':')[0]}"
        protected_for_surface = dict(protected_values)
        # Compose 的规范化配置必须显示 operator 提供的 secret source path；路径是部署
        # 元数据而不是 secret 值。应用输出、持久化 evidence 和日志仍禁止泄漏宿主路径。
        if surface_name != "compose-config":
            protected_for_surface.update(protected_paths)
        for protected_name, protected in protected_for_surface.items():
            if protected in surface:
                raise RuntimeError(
                    "public or persisted evidence leaked protected configuration "
                    f"value={protected_name} surface={surface_name}"
                )
