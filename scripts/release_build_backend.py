"""从 frozen release 环境准备并核对正式构建 backend。"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any, cast

from release_contract_support import (
    ReleaseContractError,
    build_backend_identity,
    validate_build_backend_identity,
)


def locked_build_backend(repo: Path) -> dict[str, object]:
    """读取 lock 中唯一 Hatchling identity，不从宽范围或当前环境猜版本。"""

    try:
        with (repo / "uv.lock").open("rb") as stream:
            document = tomllib.load(stream)
        packages = document["package"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseContractError("reviewed lock is unavailable for build backend") from exc
    if not isinstance(packages, list):
        raise ReleaseContractError("reviewed lock package list is invalid")
    matches: list[dict[str, Any]] = []
    for raw in cast(list[object], packages):
        if not isinstance(raw, dict):
            continue
        package = cast(dict[str, Any], raw)
        if package.get("name") == "hatchling":
            matches.append(package)
    if len(matches) != 1:
        raise ReleaseContractError("reviewed lock must contain one build backend identity")
    package = matches[0]
    identity: dict[str, object] = {
        "name": package.get("name"),
        "version": package.get("version"),
        "source": package.get("source"),
    }
    validate_build_backend_identity(identity)
    return build_backend_identity()


def prepare_build_backend(repo: Path, uv: str) -> dict[str, object]:
    """按 frozen release group 建环境，并逐值核对实际 Hatchling 版本。"""

    expected = locked_build_backend(repo)
    sync = subprocess.run(
        [
            uv,
            "sync",
            "--frozen",
            "--group",
            "release",
            "--no-group",
            "license",
            "--no-install-workspace",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if sync.returncode != 0:
        raise ReleaseContractError("frozen release build backend sync failed")
    observed = subprocess.run(
        [
            uv,
            "run",
            "--no-sync",
            "python",
            "-c",
            "import importlib.metadata as m; print(m.version('hatchling'))",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if observed.returncode != 0 or observed.stdout.strip() != expected["version"]:
        raise ReleaseContractError(
            "prepared build backend identity does not match the reviewed lock"
        )
    return expected


__all__ = ["locked_build_backend", "prepare_build_backend"]
