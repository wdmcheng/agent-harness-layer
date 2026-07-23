"""无副作用计算下一版本，并在隔离副本生成发布预演 artifact。"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from release_models import (
    ReleaseContractError,
    required_psr_executable,
    run_git,
    source_identity,
    write_json,
)
from release_preview_build import build_preview_artifacts

TAG_PREFIX = "agent-harness-v"
CONVENTIONAL = re.compile(
    r"^(?P<type>[a-zA-Z][a-zA-Z0-9-]*)(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?:\s+(?P<subject>.+)$"
)


def _project_version(repo: Path) -> str:
    """读取核心包版本；root/template 只由 promotion 同步，不充当版本真相。"""

    path = repo / "packages/agent-harness/pyproject.toml"
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseContractError(f"cannot read core package version: {path}") from exc
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ReleaseContractError("core package version must be stable SemVer")
    return value


def _parse_commits(repo: Path, base_tag: str | None) -> list[dict[str, Any]]:
    """分类基线后的全部提交，保留非发布提交以解释 no-release 决策。"""

    revision = f"{base_tag}..HEAD" if base_tag else "HEAD"
    raw = run_git(repo, "log", "--format=%H%x1f%B%x1e", revision)
    commits: list[dict[str, Any]] = []
    for entry in raw.split("\x1e"):
        entry = entry.strip()
        if not entry:
            continue
        sha, _, message = entry.partition("\x1f")
        lines = message.strip().splitlines()
        header = lines[0] if lines else ""
        match = CONVENTIONAL.match(header)
        commit_type = match.group("type").lower() if match else "unmatched"
        breaking = bool(match and match.group("breaking")) or any(
            line.startswith("BREAKING CHANGE:") or line.startswith("BREAKING-CHANGE:")
            for line in lines[1:]
        )
        bump = (
            "major"
            if breaking
            else "minor"
            if commit_type == "feat"
            else "patch"
            if commit_type in {"fix", "perf"}
            else None
        )
        commits.append(
            {
                "sha": sha,
                "type": commit_type,
                "scope": match.group("scope") if match else None,
                "subject": match.group("subject") if match else header,
                "breaking": breaking,
                "bump": bump,
            }
        )
    return commits


def _decision(commits: list[dict[str, Any]]) -> tuple[str | None, str]:
    """以 major>minor>patch 聚合提交；原因列出实际触发类型而非黑盒结论。"""

    bumps = [item["bump"] for item in commits if item["bump"] is not None]
    for bump in ("major", "minor", "patch"):
        if bump in bumps:
            sources = sorted({str(item["type"]) for item in commits if item["bump"] == bump})
            return bump, f"{bump} bump from Conventional Commits: {', '.join(sources)}"
    return None, "no feat, fix, or breaking commit after the release baseline"


def _bump(version: str, bump: str) -> str:
    """计算 PSR 稳定 SemVer 的预期值，随后必须与 PSR noop 输出交叉校验。"""

    major, minor, patch = (int(part) for part in version.split("."))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _psr_noop(repo: Path) -> tuple[str | None, str | None]:
    """用临时无 credential remote 配置调用 PSR 顶层 --noop，禁止正常 version 副作用路径。"""

    executable = required_psr_executable()
    config = """[semantic_release]
commit_parser = "conventional"
tag_format = "agent-harness-v{version}"
allow_zero_version = true
version_toml = ["packages/agent-harness/pyproject.toml:project.version"]

[semantic_release.branches.release_preview]
# dry-run 只计算本地历史，分支保护由 promotion/publish seam 独立验证。
match = ".*"
prerelease = false

[semantic_release.remote]
name = "origin"
type = "github"
url = "https://example.invalid/agent-harness/repository.git"
ignore_token_for_push = true
"""
    with tempfile.TemporaryDirectory(prefix="agent-harness-psr-") as directory:
        config_path = Path(directory) / "semantic-release.toml"
        config_path.write_text(config, encoding="utf-8")
        values: list[str | None] = []
        for flag in ("--print", "--print-tag"):
            result = subprocess.run(
                [executable, "--config", str(config_path), "--noop", "version", flag],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            combined = f"{result.stdout}\n{result.stderr}"
            no_release = "No release will be made" in combined
            if result.returncode != 0 and not no_release:
                raise ReleaseContractError(f"PSR noop failed: {combined.strip()}")
            if no_release:
                values.append(None)
                continue
            if flag == "--print":
                matches = re.findall(r"(?<![\w-])(\d+\.\d+\.\d+)(?![\w-])", result.stdout)
            else:
                matches = re.findall(r"agent-harness-v\d+\.\d+\.\d+", result.stdout)
            values.append(matches[-1] if matches else None)
        return values[0], values[1]


def _validate_output_dir(repo: Path, output: Path) -> None:
    """只允许使用仓库内约定的具名预览目录，避免清理范围扩散到项目内容。"""

    preview_root = (repo / ".artifacts/release-preview").resolve()
    try:
        preview_root.relative_to(repo)
        relative = output.relative_to(preview_root)
    except ValueError as exc:
        raise ReleaseContractError(
            "output-dir must be below the repository .artifacts/release-preview directory"
        ) from exc
    if not relative.parts:
        raise ReleaseContractError(
            "output-dir must name a preview below .artifacts/release-preview"
        )


def _clear_generated_output(output: Path) -> None:
    """清理一次预演拥有的固定产物；未知内容保留，异常文件形状 fail closed。"""

    dist = output / "dist"
    files = [
        output / "CHANGELOG.preview.md",
        output / "release-notes.md",
        output / "SHA256SUMS",
        output / "manifest.json",
    ]
    if dist.exists() and not dist.is_dir() and not dist.is_symlink():
        raise ReleaseContractError(f"generated dist target has unexpected type: {dist}")
    for path in files:
        if path.exists() and not path.is_file() and not path.is_symlink():
            raise ReleaseContractError(f"generated file target has unexpected type: {path}")
    if dist.is_symlink():
        dist.unlink()
    elif dist.is_dir():
        shutil.rmtree(dist)
    for path in files:
        if path.is_file() or path.is_symlink():
            path.unlink()


def create_preview(repo: Path, output: Path) -> dict[str, Any]:
    """执行完整 dry-run；任何失败都不修改原 tracked 文件、HEAD、tag 或 refs。"""

    repo = repo.resolve()
    output = output.resolve()
    _validate_output_dir(repo, output)
    if run_git(repo, "rev-parse", "--is-shallow-repository") == "true":
        raise ReleaseContractError(
            "shallow repository is not a valid release input; fetch complete history and tags first"
        )
    before_head = run_git(repo, "rev-parse", "HEAD")
    before_refs = run_git(repo, "show-ref")
    before_status = run_git(repo, "status", "--porcelain", "--untracked-files=no")
    before_diff = run_git(repo, "diff", "--binary", "HEAD")
    source = source_identity(repo)
    current = _project_version(repo)
    base_tag = source["base_tag"]
    if isinstance(base_tag, str):
        tagged = base_tag.removeprefix(TAG_PREFIX)
        if tagged == base_tag or tagged != current:
            raise ReleaseContractError(
                f"package/tag baseline mismatch: package={current} tag={base_tag}"
            )
    commits = _parse_commits(repo, base_tag if isinstance(base_tag, str) else None)
    bump, reason = _decision(commits)
    psr_version, psr_tag = _psr_noop(repo)
    if (psr_version is None) != (psr_tag is None):
        raise ReleaseContractError(
            f"PSR noop drift: version/tag must agree, got {psr_version}/{psr_tag}"
        )
    if bump is None and psr_version is not None:
        raise ReleaseContractError(
            "PSR noop drift: PSR selected a release but the local explanation classified "
            "the history as no-release"
        )
    next_version: str | None = None
    tag: str | None = None
    if bump is not None:
        if base_tag is None:
            expected = _bump("0.0.0", bump)
            if expected != current:
                raise ReleaseContractError(
                    "first-release baseline is ambiguous: "
                    f"PSR from 0.0.0={expected}, package={current}"
                )
            next_version = current
        else:
            next_version = _bump(current, bump)
        tag = f"{TAG_PREFIX}{next_version}"
        if psr_version != next_version or psr_tag != tag:
            raise ReleaseContractError(
                "PSR noop drift: expected version/tag "
                f"{next_version}/{tag}, got {psr_version}/{psr_tag}"
            )
    output.mkdir(parents=True, exist_ok=True)
    _clear_generated_output(output)
    try:
        if bump is None:
            manifest: dict[str, Any] = {
                "schema_version": "release-preview/v1",
                "status": "no-release",
                "source": source,
                "current_version": current,
                "next_version": None,
                "tag": None,
                "uv_version": None,
                "decision": {"bump": None, "reason": reason, "commits": commits},
                "artifacts": [],
            }
        else:
            if next_version is None or tag is None:
                raise ReleaseContractError("release decision lacks calculated version or tag")
            records, backend, uv_version = build_preview_artifacts(
                repo, output, next_version, commits
            )
            manifest = {
                "schema_version": "release-preview/v1",
                "status": "release",
                "source": source,
                "current_version": current,
                "next_version": next_version,
                "tag": tag,
                "uv_version": uv_version,
                "decision": {"bump": bump, "reason": reason, "commits": commits},
                "build_backend": backend,
                "artifacts": records,
            }
        write_json(output / "manifest.json", manifest)
        after = (
            run_git(repo, "rev-parse", "HEAD"),
            run_git(repo, "show-ref"),
            run_git(repo, "status", "--porcelain", "--untracked-files=no"),
            run_git(repo, "diff", "--binary", "HEAD"),
        )
        if after != (before_head, before_refs, before_status, before_diff):
            raise ReleaseContractError("dry-run changed tracked source, HEAD, tag, or refs")
    except BaseException:
        _clear_generated_output(output)
        raise
    return manifest


def main() -> int:
    """解析 CLI 并输出机器可读结果路径；错误仅含去敏诊断。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    identity = run_git(repo, "rev-parse", "HEAD")[:12]
    output = args.output_dir or repo / ".artifacts/release-preview" / identity
    try:
        manifest = create_preview(repo, output)
    except ReleaseContractError as exc:
        print(f"release dry-run failed: {exc}", file=sys.stderr)
        return 2
    print(f"release dry-run {manifest['status']}: {(output / 'manifest.json').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
