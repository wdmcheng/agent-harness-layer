"""Phase 1 local smoke check for the workspace and service-app shell."""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

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
    profile_path = SERVICE_APP / "configs" / "profiles" / "local.yaml"
    try:
        profile_data: object = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _fail(f"local profile is not JSON-compatible YAML: {exc}")
    if not isinstance(profile_data, dict):
        return _fail("local profile must be a mapping.")
    profile = cast(Mapping[str, object], profile_data)
    if profile.get("profile") != "local":
        return _fail("local profile must declare profile=local.")
    model_data = profile.get("model")
    if not isinstance(model_data, dict):
        return _fail("local profile must declare model settings.")
    model = cast(Mapping[str, object], model_data)
    if model.get("requires_api_key") is not False:
        return _fail("local profile must not require real provider keys in Phase 1.")
    return 0


def main() -> int:
    checks = [check_import, check_template_layout, check_local_profile]
    for check in checks:
        result = check()
        if result != 0:
            return result
    print("smoke-local: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
