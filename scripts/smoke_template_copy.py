"""把 service-app 复制到仓库外，验证 wheel-only bootstrap 与应用表面。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "service-app"


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """运行一步复制 smoke，并在失败时保留完整 stdout/stderr。"""

    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _free_port() -> int:
    """向操作系统申请短暂 loopback 端口，供独立复制模板进程监听。"""

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_health(port: int, *, timeout_seconds: float = 30.0) -> dict[str, Any]:
    """轮询复制项目的公开 health，失败时保留最后一个网络错误。"""

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    url = f"http://127.0.0.1:{port}/api/v1/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:  # noqa: S310 - 固定 loopback
                return json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"copied template health did not become ready: {last_error}")


def _read_url(port: int, path: str) -> tuple[int, str, str]:
    """读取 loopback 管理面，并返回状态、内容类型和正文。"""

    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=3.0) as response:  # noqa: S310 - 固定 loopback
        return (
            response.status,
            response.headers.get("content-type", ""),
            response.read().decode("utf-8"),
        )


def _compose_down(*, copied: Path, env: dict[str, str]) -> None:
    """只清理本次复制 smoke 创建的 compose project 和匿名数据卷。"""

    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(copied / "docker-compose.yml"),
            "-p",
            env["SERVICE_APP_COMPOSE_PROJECT"],
            "--profile",
            "service",
            "down",
            "--volumes",
            "--remove-orphans",
        ],
        cwd=copied,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def parse_args() -> argparse.Namespace:
    """解析是否额外启动复制项目的 PostgreSQL/Redis 服务冒烟。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--service",
        action="store_true",
        help="also run copied PostgreSQL/Redis/repository/worker smoke",
    )
    return parser.parse_args()


def main() -> int:
    """使用本地构建 wheel，证明复制产物不依赖 monorepo 相对路径。"""

    args = parse_args()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    env["UV_NO_PROGRESS"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    with tempfile.TemporaryDirectory(prefix="agent-harness-template-copy.") as temp_dir:
        temp_root = Path(temp_dir)
        dist = temp_root / "dist"
        copied = temp_root / "service-app"
        state = temp_root / "state"
        dist.mkdir()
        _run(
            [
                "uv",
                "build",
                "--wheel",
                "--out-dir",
                str(dist),
                str(ROOT / "packages" / "agent-harness"),
            ],
            cwd=ROOT,
            env=env,
        )
        wheel = next(dist.glob("agent_harness-0.1.0-*.whl"))
        shutil.copytree(
            TEMPLATE,
            copied,
            ignore=shutil.ignore_patterns(".venv", ".agent-harness", "__pycache__", "*.pyc"),
        )
        bootstrap = _run(
            ["make", "bootstrap", f"AGENT_HARNESS_SOURCE={wheel}"],
            cwd=copied,
            env=env,
        )
        if "cp .env.example .env" not in bootstrap.stdout:
            raise RuntimeError(f"bootstrap did not emit the missing .env hint:\n{bootstrap.stdout}")

        state.mkdir(exist_ok=True)
        storage_dsn = f"sqlite+aiosqlite:///{state / 'agent_harness.db'}"
        _run(
            [
                "uv",
                "run",
                "python",
                "app/migrate.py",
                "--profile",
                "local",
                "--profiles-dir",
                str(copied / "configs" / "profiles"),
                "--storage-dsn",
                storage_dsn,
            ],
            cwd=copied,
            env=env,
        )
        port = _free_port()
        log_path = temp_root / "dev.log"
        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                ["make", "dev", f"PORT={port}", f"STATE_DIR={state}"],
                cwd=copied,
                env=env,
                text=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            try:
                health = _wait_for_health(port)
                openapi_status, openapi_type, openapi_text = _read_url(port, "/openapi.json")
                swagger_status, swagger_type, _ = _read_url(port, "/docs")
                redoc_status, redoc_type, _ = _read_url(port, "/redoc")
            except Exception as exc:
                process.terminate()
                process.wait(timeout=5)
                raise RuntimeError(
                    f"{exc}\nserver log:\n{log_path.read_text(encoding='utf-8')}"
                ) from exc
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

        if health.get("status") != "ok" or health.get("profile") != "local":
            raise RuntimeError(f"unexpected health payload: {health}")
        openapi = json.loads(openapi_text)
        if openapi_status != 200 or "application/json" not in openapi_type:
            raise RuntimeError("copied OpenAPI endpoint is not available")
        if "/api/v1/health" not in openapi.get("paths", {}):
            raise RuntimeError("copied OpenAPI schema is missing /api/v1/health")
        if swagger_status != 200 or "text/html" not in swagger_type:
            raise RuntimeError("copied Swagger endpoint is not available")
        if redoc_status != 200 or "text/html" not in redoc_type:
            raise RuntimeError("copied Redoc endpoint is not available")
        if "workspace = true" in (copied / "pyproject.toml").read_text(encoding="utf-8"):
            raise RuntimeError("copied pyproject retained a workspace-only source")
        if str(ROOT) in (copied / "pyproject.toml").read_text(encoding="utf-8"):
            raise RuntimeError("copied pyproject retained a monorepo source path")

        scaffold_result = _run(
            ["uv", "run", "agent-harness", "scaffold", "agent", "generated.smoke"],
            cwd=copied,
            env=env,
        )
        generated = copied / "agents" / "generated" / "smoke"
        if "created: generated/smoke" not in scaffold_result.stdout or not generated.is_dir():
            raise RuntimeError(
                f"copied scaffold did not discover <copied-root>/agents:\n{scaffold_result.stdout}"
            )
        if (copied / "templates" / "service-app" / "agents").exists():
            raise RuntimeError("copied scaffold created a nested source-workspace path")
        scaffold_dsn = f"sqlite+aiosqlite:///{state / 'scaffold.db'}"
        profiles_dir = copied / "configs" / "profiles"
        agents_dir = copied / "agents"
        _run(
            [
                "uv",
                "run",
                "python",
                "app/migrate.py",
                "--profile",
                "local",
                "--profiles-dir",
                str(profiles_dir),
                "--storage-dsn",
                scaffold_dsn,
            ],
            cwd=copied,
            env=env,
        )
        list_result = _run(
            [
                "uv",
                "run",
                "agent-harness",
                "agents",
                "list",
                "--profile",
                "local",
                "--profiles-dir",
                str(profiles_dir),
                "--agents-dir",
                str(agents_dir),
                "--storage-dsn",
                scaffold_dsn,
            ],
            cwd=copied,
            env=env,
        )
        if "generated.smoke" not in list_result.stdout:
            raise RuntimeError(
                f"copied scaffold agent is absent from CLI list:\n{list_result.stdout}"
            )
        run_result = _run(
            [
                "uv",
                "run",
                "agent-harness",
                "run",
                "generated.smoke",
                "--profile",
                "local",
                "--profiles-dir",
                str(profiles_dir),
                "--agents-dir",
                str(agents_dir),
                "--storage-dsn",
                scaffold_dsn,
                "--events-path",
                str(state / "scaffold-events.jsonl"),
                "--prompt",
                "复制模板 scaffold runtime 验证",
            ],
            cwd=copied,
            env=env,
        )
        if "status: completed" not in run_result.stdout:
            raise RuntimeError(f"copied scaffold agent did not complete:\n{run_result.stdout}")

        dataset_dir = generated / "evals"
        scores_path = state / "scaffold-scores.jsonl"
        draft_result = _run(
            [
                "uv",
                "run",
                "agent-harness",
                "eval",
                "draft",
                "generated.smoke",
                "--dataset-dir",
                str(dataset_dir),
                "--profile",
                "local",
                "--profiles-dir",
                str(profiles_dir),
                "--storage-dsn",
                scaffold_dsn,
                "--scores-path",
                str(scores_path),
                "--trigger",
                "manual",
                "--prompt",
                "复制模板 eval 验证",
                "--output",
                "scaffold-ready",
                "--expected",
                "scaffold-ready",
            ],
            cwd=copied,
            env=env,
        )
        case_id = next(
            (
                line.removeprefix("case_id: ")
                for line in draft_result.stdout.splitlines()
                if line.startswith("case_id: ")
            ),
            None,
        )
        if case_id is None:
            raise RuntimeError(f"copied eval draft did not return case_id:\n{draft_result.stdout}")
        _run(
            [
                "uv",
                "run",
                "agent-harness",
                "eval",
                "approve",
                case_id,
                "--dataset-dir",
                str(dataset_dir),
                "--profile",
                "local",
                "--profiles-dir",
                str(profiles_dir),
                "--storage-dsn",
                scaffold_dsn,
                "--scores-path",
                str(scores_path),
                "--reviewer",
                "copy-smoke-reviewer",
                "--reason",
                "复制模板人工审核边界验证",
            ],
            cwd=copied,
            env=env,
        )
        eval_result = _run(
            [
                "uv",
                "run",
                "agent-harness",
                "eval",
                "run",
                "--dataset-dir",
                str(dataset_dir),
                "--scores-path",
                str(scores_path),
                "--agent-id",
                "generated.smoke",
            ],
            cwd=copied,
            env=env,
        )
        if '"passed": 1' not in eval_result.stdout:
            raise RuntimeError(f"copied approved eval did not pass:\n{eval_result.stdout}")

        service_output = ""
        if args.service:
            service_env = env.copy()
            postgres_port = _free_port()
            redis_port = _free_port()
            service_env.update(
                {
                    "SERVICE_APP_COMPOSE_PROJECT": f"agent-harness-copy-{os.getpid()}",
                    "SERVICE_APP_POSTGRES_PORT": str(postgres_port),
                    "SERVICE_APP_REDIS_PORT": str(redis_port),
                    "AGENT_HARNESS_STORAGE__DSN": (
                        "postgresql+asyncpg://agent_harness:agent_harness@"
                        f"127.0.0.1:{postgres_port}/agent_harness"
                    ),
                    "AGENT_HARNESS_QUEUE__DSN": f"redis://127.0.0.1:{redis_port}/0",
                }
            )
            try:
                service = _run(["make", "smoke-service"], cwd=copied, env=service_env)
                service_output = service.stdout
            finally:
                _compose_down(copied=copied, env=service_env)
            if "smoke-service: ok" not in service_output:
                raise RuntimeError(f"copied service smoke did not complete:\n{service_output}")

        print(f"smoke-template-copy: wheel={wheel.name}")
        print(f"smoke-template-copy: health={health['status']} profile={health['profile']}")
        print("smoke-template-copy: openapi=ok swagger=ok redoc=ok")
        print(
            "smoke-template-copy: scaffold=generated.smoke "
            "list=ok run=completed approved-eval=passed"
        )
        if args.service:
            for line in service_output.splitlines():
                if line.startswith("smoke-service:"):
                    print(f"smoke-template-copy: copied-{line}")
        print("smoke-template-copy: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
