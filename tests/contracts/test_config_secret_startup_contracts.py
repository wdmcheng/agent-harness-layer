"""Secret file 配置失败的四入口 fail-closed 合同测试。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import NoReturn

import pytest
import typer

from agent_harness.config import SettingsLoadError, settings_error_lines
from app import main as app_main
from app.cli import main as service_cli
from app.workers import runtime_worker

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"
EXPECTED_DIAGNOSTIC = (
    "config.secret_file_conflict: field=storage.dsn "
    "direct env 与对应的 _FILE 不能同时设置 "
    "hint=只设置 direct env 或对应的 _FILE，移除另一个输入"
)
MISSING_DIAGNOSTIC = (
    "config.invalid: field=storage Field required "
    "hint=在 profile YAML 或 AGENT_HARNESS_* env 中设置 storage"
)
INVALID_DIAGNOSTIC = (
    "config.invalid_yaml: field=profile YAML 解析失败 "
    "hint=检查 profile YAML 的 UTF-8 编码和 YAML 语法"
)
INVALID_ENV_DIAGNOSTIC = (
    "config.invalid_env: field=.env .env 配置不可读或编码无效 "
    "hint=检查 .env 的读取权限和 UTF-8 编码"
)


def set_conflicting_storage_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    """注入唯一 direct/file 值，供所有入口复用同一失败前提。"""

    direct_fixture = "startup-direct-secret-fixture"
    file_fixture = "startup-file-secret-fixture"
    secret_path = tmp_path / "storage-dsn"
    secret_path.write_text(file_fixture, encoding="utf-8")
    monkeypatch.setenv("AGENT_HARNESS_STORAGE__DSN", direct_fixture)
    monkeypatch.setenv("AGENT_HARNESS_STORAGE__DSN_FILE", str(secret_path))
    return direct_fixture, file_fixture


def assert_structured_conflict(error: SettingsLoadError) -> None:
    detail = error.errors[0]
    assert detail.code == "config.secret_file_conflict"
    assert detail.field_path == "storage.dsn"
    assert detail.hint == "只设置 direct env 或对应的 _FILE，移除另一个输入"


def configure_startup_failure(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str, tuple[str, ...]]:
    """为三类失败返回相同入口使用的 profiles_dir、诊断和禁用值。"""

    if case == "conflict":
        forbidden = set_conflicting_storage_secret(tmp_path, monkeypatch)
        return PROFILES, EXPECTED_DIAGNOSTIC, forbidden
    monkeypatch.delenv("AGENT_HARNESS_STORAGE__DSN", raising=False)
    monkeypatch.delenv("AGENT_HARNESS_STORAGE__DSN_FILE", raising=False)
    service_root = tmp_path / "private"
    profiles_dir = service_root / "configs" / "profiles"
    profiles_dir.mkdir(parents=True)
    if case == "invalid":
        (profiles_dir / "service.yaml").write_text(
            '!!python/object/apply:os.system ["echo unsafe"]',
            encoding="utf-8",
        )
        return profiles_dir, INVALID_DIAGNOSTIC, (str(tmp_path), "echo unsafe")
    if case == "invalid_env":
        (profiles_dir / "service.yaml").write_text(
            (PROFILES / "service.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (service_root / ".env").write_bytes(b"\xff\xfe")
        return profiles_dir, INVALID_ENV_DIAGNOSTIC, (str(tmp_path),)
    (profiles_dir / "service.yaml").write_text(
        """
profile: service
queue:
  kind: redis
observability:
  kind: local-jsonl
policy:
  provider: yaml
model:
  provider: fake
  requires_api_key: false
""",
        encoding="utf-8",
    )
    return profiles_dir, MISSING_DIAGNOSTIC, (str(tmp_path),)


def test_fastapi_worker_and_migration_fail_before_external_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三个 composition seam 必须在监听、连接、migration 和 ready 前失败。"""

    from app import migrate
    from app import runtime as app_runtime

    direct_fixture, file_fixture = set_conflicting_storage_secret(tmp_path, monkeypatch)
    side_effects = {"runtime": 0, "migration": 0}

    def fail_runtime_build(**_kwargs: object) -> NoReturn:
        side_effects["runtime"] += 1
        raise AssertionError("FastAPI 配置失败后不得构造 runtime")

    def fail_migration(_dsn: str) -> NoReturn:
        side_effects["migration"] += 1
        raise AssertionError("配置失败后不得运行 migration")

    monkeypatch.setattr(app_main, "build_runtime_components", fail_runtime_build)
    monkeypatch.setattr(app_runtime, "require_migration_head", fail_migration)
    monkeypatch.setattr(migrate, "run_migrations", fail_migration)
    ready_file = tmp_path / "worker-ready"
    monkeypatch.setenv("SERVICE_APP_READY_FILE", str(ready_file))

    captured: list[SettingsLoadError] = []
    with pytest.raises(SettingsLoadError) as app_error:
        app_main.create_app(profile="service", profiles_dir=PROFILES)
    captured.append(app_error.value)
    with pytest.raises(SettingsLoadError) as worker_error:
        asyncio.run(runtime_worker.run_once(profile="service", profiles_dir=PROFILES))
    captured.append(worker_error.value)
    with pytest.raises(SettingsLoadError) as migration_error:
        migrate.run(profile="service", profiles_dir=PROFILES)
    captured.append(migration_error.value)

    assert len(captured) == 3
    for error in captured:
        assert_structured_conflict(error)
        serialized = str(error)
        assert direct_fixture not in serialized
        assert file_fixture not in serialized
        assert str(tmp_path) not in serialized
    assert side_effects == {"runtime": 0, "migration": 0}
    assert not ready_file.exists()


@pytest.mark.parametrize("case", ["conflict", "missing_field", "invalid", "invalid_env"])
def test_service_process_entrypoints_render_the_same_safe_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """API CLI、worker 与 migration 进程入口共享同一 operator-facing 诊断。"""

    from app import migrate
    from app import runtime as app_runtime

    profiles_dir, expected_diagnostic, forbidden_values = configure_startup_failure(
        case,
        tmp_path,
        monkeypatch,
    )
    side_effects = {"runtime": 0, "migration": 0}

    def fail_runtime_build(**_kwargs: object) -> NoReturn:
        side_effects["runtime"] += 1
        raise AssertionError("配置失败后不得构造 runtime")

    def fail_uvicorn(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("配置失败后不得监听端口")

    def fail_migration(_dsn: str) -> NoReturn:
        side_effects["migration"] += 1
        raise AssertionError("配置失败后不得运行 migration")

    monkeypatch.setattr(service_cli.uvicorn, "run", fail_uvicorn)
    monkeypatch.setattr(app_main, "build_runtime_components", fail_runtime_build)
    monkeypatch.setattr(app_runtime, "require_migration_head", fail_migration)
    monkeypatch.setattr(migrate, "run_migrations", fail_migration)
    with pytest.raises(typer.Exit):
        service_cli.serve(profile="service", profiles_dir=profiles_dir)
    cli_error = capsys.readouterr().err.strip()

    with pytest.raises(SettingsLoadError) as app_error:
        app_main.create_app(profile="service", profiles_dir=profiles_dir)
    app_diagnostic = "\n".join(settings_error_lines(app_error.value))

    monkeypatch.setattr(
        runtime_worker,
        "parse_args",
        lambda: argparse.Namespace(
            once=False,
            profile="service",
            profiles_dir=profiles_dir,
            storage_dsn=None,
            events_path=None,
            artifact_root=None,
            workspace_root=None,
            idempotency_key=None,
        ),
    )
    with pytest.raises(SystemExit):
        runtime_worker.main()
    worker_error = capsys.readouterr().err.strip()

    monkeypatch.setattr(
        migrate,
        "parse_args",
        lambda: argparse.Namespace(
            profile="service",
            profiles_dir=profiles_dir,
            storage_dsn=None,
        ),
    )
    with pytest.raises(SystemExit):
        migrate.main()
    migration_error = capsys.readouterr().err.strip()

    assert cli_error == app_diagnostic == worker_error == migration_error == expected_diagnostic
    assert side_effects == {"runtime": 0, "migration": 0}
    for forbidden in (*forbidden_values, "runtime-worker: ready"):
        assert forbidden not in "\n".join(
            (cli_error, app_diagnostic, worker_error, migration_error)
        )
