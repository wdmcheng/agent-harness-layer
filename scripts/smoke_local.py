"""Local smoke check for the workspace and service-app shell."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

from agent_harness.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
SERVICE_APP = ROOT / "templates" / "service-app"


REQUIRED_TEMPLATE_PATHS = [
    "app/api",
    "app/cli",
    "app/workers",
    "agents/examples",
    "configs/profiles",
    "eval-cases/drafts",
    "eval-cases/approved",
    "tests",
    "docs",
    ".env.example",
    "Makefile",
    "README.md",
    "pyproject.toml",
]


def _fail(message: str) -> int:
    print(f"smoke-local: {message}", file=sys.stderr)
    return 1


def check_import() -> int:
    module = importlib.import_module("agent_harness")
    version = getattr(module, "__version__", "")
    if not isinstance(version, str) or not version:
        return _fail("agent_harness.__version__ is missing.")
    return 0


def check_template_layout() -> int:
    missing = [path for path in REQUIRED_TEMPLATE_PATHS if not (SERVICE_APP / path).exists()]
    if missing:
        return _fail(f"missing template paths: {', '.join(missing)}")
    return 0


def check_local_profile() -> int:
    settings = load_settings(profile="local", profiles_dir=SERVICE_APP / "configs" / "profiles")
    if settings.profile != "local":
        return _fail("local profile must declare profile=local.")
    if settings.model.requires_api_key:
        return _fail("local profile must not require real provider keys.")
    return 0


def check_doctor() -> int:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "doctor",
            "--profile",
            "local",
            "--profiles-dir",
            str(SERVICE_APP / "configs" / "profiles"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return _fail(f"doctor failed: {result.stderr.strip()}")
    if "profile: local" not in result.stdout:
        return _fail("doctor output did not report local profile.")
    return 0


def main() -> int:
    checks = [check_import, check_template_layout, check_local_profile, check_doctor]
    for check in checks:
        result = check()
        if result != 0:
            return result
    print("smoke-local: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
