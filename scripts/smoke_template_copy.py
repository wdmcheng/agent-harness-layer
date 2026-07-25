"""把 service-app 复制到仓库外，验证 wheel-only bootstrap 与应用表面。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

from template_copy_agent_smoke import run_agent_surface_smoke

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


def _read_dev_surface(
    *,
    copied: Path,
    state: Path,
    env: dict[str, str],
    log_path: Path,
) -> dict[str, Any]:
    """启动一次复制项目，读取 health、OpenAPI 和两个文档页后回收进程。"""

    port = _free_port()
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
            swagger_status, swagger_type, swagger_text = _read_url(port, "/docs")
            redoc_status, redoc_type, redoc_text = _read_url(port, "/redoc")
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
    return {
        "port": port,
        "health": health,
        "openapi_status": openapi_status,
        "openapi_type": openapi_type,
        "openapi_text": openapi_text,
        "swagger_status": swagger_status,
        "swagger_type": swagger_type,
        "swagger_text": swagger_text,
        "redoc_status": redoc_status,
        "redoc_type": redoc_type,
        "redoc_text": redoc_text,
    }


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
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    # 复制验收必须从封闭配置基线开始，不能继承宿主的 direct、`_FILE`
    # 或测试专用 Harness 变量；本轮所需值在下方逐项重新注入。
    for key in tuple(env):
        if key.startswith("AGENT_HARNESS_"):
            env.pop(key)
    env["UV_NO_PROGRESS"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    # 复制 smoke 必须自带 local profile 的 fail-closed 前置值，不能依赖开发者 shell；
    # 固定测试值仅用于本轮临时 SQLite 状态，不写入模板或产物。
    env["AGENT_HARNESS_BUDGET__FINGERPRINT_KEY"] = "copy-smoke-ephemeral-fingerprint-key"
    with tempfile.TemporaryDirectory(prefix="agent-harness-template-copy.") as temp_dir:
        temp_root = Path(temp_dir)
        dist = temp_root / "dist"
        # 复制目录刻意包含空格，防止 Make/shell 引用只在简单路径假通过。
        copied = temp_root / "service app"
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
        copied_pyproject = tomllib.loads((copied / "pyproject.toml").read_text(encoding="utf-8"))
        if copied_pyproject["tool"]["pyright"] != {"venvPath": ".", "venv": ".venv"}:
            raise RuntimeError("copied bootstrap did not normalize the Pyright environment")
        _run(
            ["make", "test"],
            cwd=copied,
            env=env,
        )
        # 复制项目的测试必须与合法本机覆盖共存。用户显式开启 API 文档后，
        # 测试仍应自己声明关闭场景，不能把真实 `.env` 当成 profile 默认值。
        configured_env_path = copied / ".env"
        configured_env_content = "AGENT_HARNESS_SERVICE__API_DOCS__ENABLED=true\n"
        configured_env_path.write_text(configured_env_content, encoding="utf-8")
        configured_docs = _run(
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-c",
                (
                    "from pathlib import Path; "
                    "from agent_harness.config import load_settings; "
                    "settings = load_settings(profile='service', "
                    "profiles_dir=Path('configs/profiles')); "
                    "print('configured-service-api-docs=' + "
                    "('enabled' if settings.service.api_docs.enabled else 'disabled'))"
                ),
            ],
            cwd=copied,
            env=env,
        )
        if configured_docs.stdout.strip() != "configured-service-api-docs=enabled":
            raise RuntimeError(
                "copied service profile did not load API docs enablement from .env"
            )
        _run(
            ["make", "test"],
            cwd=copied,
            env=env,
        )
        if configured_env_path.read_text(encoding="utf-8") != configured_env_content:
            raise RuntimeError("copied bootstrap rewrote the user-owned .env")
        _run(
            ["make", "quality"],
            cwd=copied,
            env=env,
        )
        if (copied / "pyrightconfig.json").exists():
            raise RuntimeError("copied quality generated an unexpected pyrightconfig.json")

        # 真实切换到含空格的项目外环境，证明文档示例不是只通过字符串合同。
        custom_environment = temp_root / "python envs" / "service-app"
        custom_config = (
            "{\n"
            "  // Pyright 接受 JSONC；质量门禁必须接受相同语法。\n"
            '  "extends": "./pyproject.toml",\n'
            f'  "venvPath": "{custom_environment.parent}",\n'
            f'  "venv": "{custom_environment.name}",\n'
            '  "exclude": ["https://example.test//types", "/* literal */"],\n'
            "}\n"
        )
        pyright_config = copied / "pyrightconfig.json"
        pyright_config.write_text(custom_config, encoding="utf-8")
        custom_env = env.copy()
        custom_env["UV_PROJECT_ENVIRONMENT"] = str(custom_environment)
        _run(
            ["make", "bootstrap", f"AGENT_HARNESS_SOURCE={wheel}"],
            cwd=copied,
            env=custom_env,
        )
        if pyright_config.read_text(encoding="utf-8") != custom_config:
            raise RuntimeError("bootstrap rewrote the user-owned pyrightconfig.json")
        _run(
            ["make", "test"],
            cwd=copied,
            env=custom_env,
        )
        _run(
            ["make", "quality"],
            cwd=copied,
            env=custom_env,
        )
        pyright_config.unlink()

        state.mkdir(exist_ok=True)
        eval_result = _run(
            ["make", "eval-ticket", f"STATE_DIR={state}"],
            cwd=copied,
            env=env,
        )
        if "agent=examples.ticket_triage status=completed cases=2" not in eval_result.stdout:
            raise RuntimeError(f"copied eval did not complete:\n{eval_result.stdout}")
        if not (state / "eval-ticket" / "eval.db").is_file():
            raise RuntimeError("copied eval did not migrate its isolated database")

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
        offline_surface = _read_dev_surface(
            copied=copied,
            state=state,
            env=env,
            log_path=temp_root / "dev-offline.log",
        )
        health = offline_surface["health"]
        if health.get("status") != "ok" or health.get("profile") != "local":
            raise RuntimeError(f"unexpected health payload: {health}")
        openapi = json.loads(offline_surface["openapi_text"])
        if (
            offline_surface["openapi_status"] != 200
            or "application/json" not in offline_surface["openapi_type"]
        ):
            raise RuntimeError("copied OpenAPI endpoint is not available")
        if "/api/v1/health" not in openapi.get("paths", {}):
            raise RuntimeError("copied OpenAPI schema is missing /api/v1/health")
        if (
            offline_surface["swagger_status"] != 200
            or "text/html" not in offline_surface["swagger_type"]
        ):
            raise RuntimeError("copied Swagger endpoint is not available")
        if (
            offline_surface["redoc_status"] != 200
            or "text/html" not in offline_surface["redoc_type"]
        ):
            raise RuntimeError("copied Redoc endpoint is not available")
        for page in (offline_surface["swagger_text"], offline_surface["redoc_text"]):
            if "https://" in page:
                raise RuntimeError("copied offline API docs retained an external asset")
            asset_paths = re.findall(r'(?:href|src)="(/[^"]+)"', page)
            if not asset_paths:
                raise RuntimeError("copied offline API docs expose no local assets")
            for asset_path in asset_paths:
                status, _, payload = _read_url(cast(int, offline_surface["port"]), asset_path)
                if status != 200 or not payload:
                    raise RuntimeError(f"copied API docs asset is unavailable: {asset_path}")
                if (
                    asset_path.endswith("/redoc/redoc.standalone.js")
                    and "https://cdn.redoc.ly/redoc/logo-mini.svg" in payload
                ):
                    raise RuntimeError("copied Redoc bundle retained its runtime CDN logo")
        if '"validatorUrl": null' not in offline_surface["swagger_text"]:
            raise RuntimeError("copied offline Swagger UI retained the remote validator")

        manifest = json.loads(
            (copied / "app/static/api-docs/manifest.json").read_text(encoding="utf-8")
        )
        license_sidecars = {
            "swagger_ui": (
                "swagger-ui/swagger-ui-bundle.js",
                "swagger-ui/swagger-ui-bundle.js.LICENSE.txt",
            ),
            "redoc": (
                "redoc/redoc.standalone.js",
                "redoc/redoc.standalone.js.LICENSE.txt",
            ),
        }
        for component, (bundle_path, sidecar_path) in license_sidecars.items():
            if sidecar_path not in manifest[component]["files"]:
                raise RuntimeError(f"copied API docs manifest omitted {sidecar_path}")
            sidecar = copied / "app/static/api-docs" / sidecar_path
            if not sidecar.is_file() or not sidecar.read_text(encoding="utf-8").strip():
                raise RuntimeError(
                    f"copied API docs license sidecar is unavailable: {sidecar_path}"
                )
            bundle_header = (
                (copied / "app/static/api-docs" / bundle_path)
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            if Path(sidecar_path).name not in bundle_header:
                raise RuntimeError(f"copied bundle does not reference {sidecar_path}")
        online_env = env.copy()
        online_env["AGENT_HARNESS_SERVICE__API_DOCS__ASSET_MODE"] = "online"
        online_surface = _read_dev_surface(
            copied=copied,
            state=state,
            env=online_env,
            log_path=temp_root / "dev-online.log",
        )
        if (
            f"swagger-ui-dist@{manifest['swagger_ui']['version']}"
            not in online_surface["swagger_text"]
            or f"redoc@{manifest['redoc']['version']}" not in online_surface["redoc_text"]
        ):
            raise RuntimeError("copied online API docs did not keep pinned asset versions")
        if any(
            "/static/api-docs/" in page
            for page in (online_surface["swagger_text"], online_surface["redoc_text"])
        ):
            raise RuntimeError("copied online API docs retained local asset URLs")
        if '"validatorUrl": null' not in online_surface["swagger_text"]:
            raise RuntimeError("copied online Swagger UI retained the remote validator")
        if "workspace = true" in (copied / "pyproject.toml").read_text(encoding="utf-8"):
            raise RuntimeError("copied pyproject retained a workspace-only source")
        if str(ROOT) in (copied / "pyproject.toml").read_text(encoding="utf-8"):
            raise RuntimeError("copied pyproject retained a monorepo source path")

        run_agent_surface_smoke(
            copied=copied,
            state=state,
            env=env,
            run_command=_run,
        )

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
        print("smoke-template-copy: make-test=ok")
        print("smoke-template-copy: configured-service-api-docs=enabled")
        print("smoke-template-copy: configured-make-test=ok")
        print("smoke-template-copy: custom-make-test=ok")
        print("smoke-template-copy: make-quality=ok spaced-path=ok custom-environment=ok")
        print("smoke-template-copy: eval-ticket=migrated-and-passed")
        print(f"smoke-template-copy: health={health['status']} profile={health['profile']}")
        print("smoke-template-copy: openapi=ok swagger=offline-ok redoc=offline-ok")
        print("smoke-template-copy: swagger=online-pinned redoc=online-pinned")
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
