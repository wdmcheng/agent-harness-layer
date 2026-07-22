"""准备受审发布文件并刷新锁文件，隔离 promotion 的工作区写入职责。"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, cast

from release_models import (
    ReleaseContractError,
    required_uv_executable,
    resolve_artifact,
    sha256_bytes,
)


def _replace_project_version(path: Path, version: str) -> None:
    """同步各发布 package 的 project.version，不改依赖兼容范围或其他版本文本。"""

    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    replaced, count = re.subn(
        r"(?ms)(^\[project\]\s*.*?^version\s*=\s*)\"[^\"]+\"",
        rf'\g<1>"{version}"',
        text,
        count=1,
    )
    if count != 1:
        raise ReleaseContractError(f"cannot update project.version: {path}")
    path.write_text(replaced, encoding="utf-8")


def _replace_core_dependency(path: Path, version: str, *, compatible: bool) -> None:
    """同步 root exact 与模板兼容依赖，避免 promotion 后 workspace 无法解析。"""

    if not path.exists():
        return
    major, minor, _patch = (int(part) for part in version.split("."))
    requirement = f"agent-harness=={major}.{minor}.*" if compatible else f"agent-harness=={version}"
    text = path.read_text(encoding="utf-8")
    replaced, count = re.subn(
        r"agent-harness(?:==|>=)[^\"\s,]+(?:,<[^\"\s,]+)?",
        requirement,
        text,
        count=1,
    )
    if count != 1:
        raise ReleaseContractError(f"cannot update agent-harness dependency: {path}")
    path.write_text(replaced, encoding="utf-8")


def freeze_release_documents(repo: Path, preview: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """一次读取并校验发布文档，后续 Git/provider 步骤只消费这份冻结 bytes。"""

    artifacts = cast(list[object], preview["artifacts"])
    frozen: dict[str, tuple[str, str]] = {}
    for kind in ("changelog", "release-notes"):
        matches: list[dict[str, Any]] = []
        for raw in artifacts:
            if not isinstance(raw, dict):
                continue
            item = cast(dict[str, Any], raw)
            if item.get("kind") == kind:
                matches.append(item)
        if len(matches) != 1:
            raise ReleaseContractError(f"preview {kind} artifact is incomplete")
        item = matches[0]
        path = resolve_artifact(repo, item)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ReleaseContractError(f"cannot freeze reviewed {kind} artifact") from exc
        checksum = sha256_bytes(data)
        if checksum != item.get("sha256") or len(data) != item.get("size"):
            raise ReleaseContractError(f"reviewed {kind} artifact identity drift")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseContractError(f"reviewed {kind} artifact must be UTF-8") from exc
        frozen[kind] = (text, checksum)
    return frozen


def update_release_files(
    repo: Path,
    preview: dict[str, Any],
    *,
    changelog_preview: str,
) -> Path:
    """按固定顺序更新 version 与 CHANGELOG，并返回已审 release notes 文件。"""

    version = str(preview["next_version"])
    for relative in (
        "pyproject.toml",
        "packages/agent-harness/pyproject.toml",
        "templates/service-app/pyproject.toml",
    ):
        _replace_project_version(repo / relative, version)
    _replace_core_dependency(repo / "pyproject.toml", version, compatible=False)
    _replace_core_dependency(
        repo / "templates/service-app/pyproject.toml", version, compatible=True
    )
    init_file = repo / "packages/agent-harness/src/agent_harness/__init__.py"
    if init_file.exists():
        init_file.write_text(
            re.sub(
                r'(?m)^__version__\s*=\s*"[^"]+"',
                f'__version__ = "{version}"',
                init_file.read_text(encoding="utf-8"),
            ),
            encoding="utf-8",
        )
    artifacts_raw = preview["artifacts"]
    if not isinstance(artifacts_raw, list):
        raise ReleaseContractError("preview artifacts must be a list")
    artifacts = [
        cast(dict[str, Any], item)
        for item in cast(list[object], artifacts_raw)
        if isinstance(item, dict)
    ]
    notes_item = next(
        (item for item in artifacts if item.get("kind") == "release-notes"),
        None,
    )
    if notes_item is None:
        raise ReleaseContractError("preview release-notes artifact is incomplete")
    notes = resolve_artifact(repo, notes_item)
    changelog = repo / "CHANGELOG.md"
    existing = changelog.read_text(encoding="utf-8") if changelog.exists() else "# Changelog\n"
    section = changelog_preview.split("\n", 2)[-1]
    if not existing.endswith("\n"):
        existing += "\n"
    changelog.write_text(existing.rstrip() + "\n\n" + section.lstrip(), encoding="utf-8")
    return notes


def refresh_lock(repo: Path) -> None:
    """在 release commit 前更新并复核 uv.lock，使 tag 指向可解析的完整版本状态。"""

    uv = required_uv_executable()
    for arguments in ((uv, "lock"), (uv, "lock", "--check")):
        result = subprocess.run(
            arguments,
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise ReleaseContractError(
                f"{' '.join(arguments)} failed while preparing the release commit"
            )
    if not (repo / "uv.lock").is_file():
        raise ReleaseContractError("uv lock completed without creating uv.lock")


__all__ = [
    "freeze_release_documents",
    "refresh_lock",
    "update_release_files",
]
