"""在短命工作区生成发布预览 distribution 与说明文档。"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from release_models import (
    ReleaseContractError,
    artifact_record,
    required_uv_executable,
    sha256_file,
)


def _replace_project_version(path: Path, version: str) -> None:
    """只替换 TOML `[project]` 下第一条 version，避免修改依赖版本文本。"""

    text = path.read_text(encoding="utf-8")
    replaced, count = re.subn(
        r"(?ms)(^\[project\]\s*.*?^version\s*=\s*)\"[^\"]+\"",
        rf'\g<1>"{version}"',
        text,
        count=1,
    )
    if count != 1:
        raise ReleaseContractError(f"cannot update project.version in temporary copy: {path}")
    path.write_text(replaced, encoding="utf-8")


def _copy_forbuild_preview_artifacts(repo: Path, destination: Path) -> None:
    """只复制核心构建与版本同步输入，不接触 socket、credential 或本地运行状态。"""

    destination.mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock", "LICENSE", "NOTICE", "README.md", "CHANGELOG.md"):
        source = repo / name
        if source.is_file():
            shutil.copy2(source, destination / name)
    shutil.copytree(
        repo / "packages/agent-harness",
        destination / "packages/agent-harness",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "dist", "*.egg-info"),
    )
    template_source = repo / "templates/service-app/pyproject.toml"
    if template_source.is_file():
        template_destination = destination / "templates/service-app"
        template_destination.mkdir(parents=True)
        shutil.copy2(template_source, template_destination / "pyproject.toml")


def _release_text(version: str, commits: list[dict[str, Any]]) -> tuple[str, str]:
    """生成同一份提交事实驱动的 CHANGELOG preview 与 provider release notes。"""

    lines = [f"## {version}", ""]
    for item in reversed(commits):
        marker = "BREAKING " if item["breaking"] else ""
        scope = f"({item['scope']})" if item["scope"] else ""
        lines.append(f"- {marker}{item['type']}{scope}: {item['subject']} ({str(item['sha'])[:8]})")
    body = "\n".join(lines) + "\n"
    return f"# Changelog preview\n\n{body}", f"# agent-harness {version}\n\n{body}"


def build_preview_artifacts(
    repo: Path, output: Path, version: str, commits: list[dict[str, Any]]
) -> list[dict[str, object]]:
    """在短命副本更新版本并构建，异常或中断由 TemporaryDirectory 统一清理。"""

    uv = required_uv_executable()
    output.mkdir(parents=True, exist_ok=True)
    dist = output / "dist"
    changelog = output / "CHANGELOG.preview.md"
    notes = output / "release-notes.md"
    changelog_text, notes_text = _release_text(version, commits)
    changelog.write_text(changelog_text, encoding="utf-8")
    notes.write_text(notes_text, encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="agent-harness-release-build-") as directory:
        copy = Path(directory) / "source"
        _copy_forbuild_preview_artifacts(repo, copy)
        for relative in (
            "pyproject.toml",
            "packages/agent-harness/pyproject.toml",
            "templates/service-app/pyproject.toml",
        ):
            path = copy / relative
            if path.exists():
                _replace_project_version(path, version)
        init_file = copy / "packages/agent-harness/src/agent_harness/__init__.py"
        if init_file.exists():
            init_file.write_text(
                re.sub(
                    r'(?m)^__version__\s*=\s*"[^"]+"',
                    f'__version__ = "{version}"',
                    init_file.read_text(encoding="utf-8"),
                ),
                encoding="utf-8",
            )
        result = subprocess.run(
            [
                uv,
                "build",
                str(copy / "packages/agent-harness"),
                "--out-dir",
                str(dist),
                "--clear",
                "--no-create-gitignore",
                "--no-build-isolation",
            ],
            cwd=copy,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise ReleaseContractError(
                f"isolated uv build failed: {result.stdout}\n{result.stderr}"
            )
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseContractError("isolated build must produce exactly one wheel and one sdist")
    checksum = output / "SHA256SUMS"
    checksum.write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in [*wheels, *sdists]),
        encoding="utf-8",
    )
    records = [
        artifact_record(wheels[0], root=repo, kind="wheel"),
        artifact_record(sdists[0], root=repo, kind="sdist"),
        artifact_record(changelog, root=repo, kind="changelog"),
        artifact_record(notes, root=repo, kind="release-notes"),
        artifact_record(checksum, root=repo, kind="checksums"),
    ]
    return records


__all__ = ["build_preview_artifacts"]
