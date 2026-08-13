"""service-app worker、profile 与显式 env file 的聚焦合同。"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agents.examples.basic.schemas import Input as BasicInput
from app.main import create_app
from app.runtime import build_runtime_components
from app.workers import runtime_worker

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "templates" / "service-app" / "Makefile"


def test_make_worker_forwards_profile_profiles_dir_once_and_optional_env_file(
    tmp_path: Path,
) -> None:
    copied = tmp_path / "copied service app"
    shutil.copytree(ROOT / "templates" / "service-app", copied)
    profiles = copied / "configs" / "profiles"

    def dry_run(profile: str, env_file: Path | None = None) -> str:
        command = [
            "make",
            "-n",
            "worker",
            f"PROFILE={profile}",
            f"PROFILES_DIR={profiles}",
            "PYTHON=python3",
            "UV=uv",
        ]
        if env_file is not None:
            command.append(f"ENV_FILE={env_file}")
        return subprocess.run(
            command,
            cwd=copied,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    explicit_env = copied / "selected env" / "worker.env"
    local = dry_run("local", explicit_env)
    service = dry_run("service", explicit_env)
    default = dry_run("local")
    for output, profile in ((local, "local"), (service, "service")):
        assert f'--profile "{profile}"' in output
        assert f'--profiles-dir "{profiles}"' in output
        assert f'--env-file "{explicit_env}"' in output
        assert str(ROOT) not in output
    assert "--once" in local
    assert "--storage-dsn" in local and "--events-path" in local
    assert "--once" not in service
    assert "--storage-dsn" not in service and "--events-path" not in service
    assert "--env-file" not in default


def test_worker_cli_parses_the_shared_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runtime-worker",
            "--once",
            "--profile",
            "local",
            "--profiles-dir",
            "/tmp/profiles",
            "--env-file",
            "/tmp/selected.env",
        ],
    )
    args = runtime_worker.parse_args()
    assert args.once is True
    assert args.profile == "local"
    assert args.profiles_dir == Path("/tmp/profiles")
    assert args.env_file == Path("/tmp/selected.env")


@pytest.mark.asyncio
async def test_local_worker_once_uses_only_basic_business_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class RecordingOrchestrator:
        async def start_run(
            self,
            *,
            agent_id: str,
            input: dict[str, object],
            idempotency_key: str,
        ) -> SimpleNamespace:
            BasicInput.model_validate(input)
            observed.update(agent_id=agent_id, input=input, idempotency_key=idempotency_key)
            return SimpleNamespace(run_id="local-worker-run")

    class LocalComponents:
        queue = None
        orchestrator = RecordingOrchestrator()

        async def close(self) -> None:
            observed["closed"] = True

    def build_local_components(**kwargs: object) -> LocalComponents:
        observed["build_kwargs"] = kwargs
        return LocalComponents()

    def ignore_shared_budget_failpoint(_value: object) -> None:
        return None

    monkeypatch.setattr(runtime_worker, "build_runtime_components", build_local_components)
    monkeypatch.setattr(
        runtime_worker,
        "_install_shared_budget_failpoint",
        ignore_shared_budget_failpoint,
    )
    run_id = await runtime_worker.run_once(
        profile="local",
        profiles_dir=Path("/tmp/profiles"),
        env_file=Path("/tmp/selected.env"),
        idempotency_key="worker-key",
    )
    assert run_id == "local-worker-run"
    assert observed["agent_id"] == "examples.basic"
    assert observed["input"] == {"source": "worker"}
    assert observed["closed"] is True
    assert cast(dict[str, object], observed["build_kwargs"])["env_file"] == Path(
        "/tmp/selected.env"
    )


def test_app_and_runtime_share_keyword_only_env_file() -> None:
    for factory in (create_app, build_runtime_components):
        parameter = inspect.signature(factory).parameters["env_file"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_explicit_env_file_ignores_an_ambient_cwd_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AGENT_HARNESS_BUDGET__FINGERPRINT_KEY",
        "contract-only-entrypoint-fingerprint-key",
    )
    monkeypatch.delenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE", raising=False)

    script = """
import json
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from app import main as app_main
from app import runtime as app_runtime

copied = Path.cwd()
profiles = copied / "configs" / "profiles"
explicit_env = copied / "selected.env"
runtime_calls: list[Path | None] = []
real_runtime_load_settings = app_runtime.load_settings

def runtime_settings(**kwargs: Any) -> Any:
    runtime_calls.append(kwargs.get("env_file"))
    return real_runtime_load_settings(**kwargs)

class StopAfterRuntimeSettings(RuntimeError):
    pass

app_runtime.load_settings = runtime_settings
app = app_main.create_app(
    orchestrator=cast(Any, object()),
    event_sink=cast(Any, object()),
    profile="local",
    profiles_dir=profiles,
    env_file=explicit_env,
)
with TestClient(app) as client:
    response = client.get(
        "/api/v1/health",
        headers={"X-Request-Id": "env-file-contract"},
    )
app_runtime.require_migration_head = lambda _dsn: (_ for _ in ()).throw(
    StopAfterRuntimeSettings()
)
try:
    app_main.create_app(
        profile="local",
        profiles_dir=profiles,
        env_file=explicit_env,
    )
except StopAfterRuntimeSettings:
    pass
else:
    raise AssertionError("runtime composition did not reach the settings boundary")
print(
    json.dumps(
        {
            "app_file": str(Path(app_main.__file__).resolve()),
            "health": response.json(),
            "runtime_env_file": [
                None if value is None else Path(value).name for value in runtime_calls
            ],
        },
        sort_keys=True,
    )
)
"""

    def copied_result(name: str, *, conflicting_dotenv: bool) -> dict[str, object]:
        copied = tmp_path / name
        shutil.copytree(ROOT / "templates" / "service-app", copied)
        (copied / "selected.env").write_text("", encoding="utf-8")
        if conflicting_dotenv:
            (copied / ".env").write_text(
                "AGENT_HARNESS_STORAGE__KIND=postgresql\nAGENT_HARNESS_QUEUE__KIND=redis\n",
                encoding="utf-8",
            )
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=copied,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = cast(dict[str, object], json.loads(completed.stdout.splitlines()[-1]))
        assert payload["app_file"] == str((copied / "app" / "main.py").resolve())
        assert payload["runtime_env_file"] == ["selected.env"]
        return cast(dict[str, object], payload["health"])

    assert copied_result("clean copy", conflicting_dotenv=False) == copied_result(
        "conflict copy",
        conflicting_dotenv=True,
    )
