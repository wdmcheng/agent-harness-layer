"""从 annotated tag target 的隔离 checkout 生成正式发布构建。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any, cast

from release_build_backend import prepare_build_backend
from release_models import (
    ReleaseContractError,
    artifact_record,
    required_uv_identity,
    run_git,
    sha256_file,
    write_json,
)


def _run(arguments: list[str], *, cwd: Path) -> None:
    """运行构建子进程，并把路径无关的摘要转换为稳定发布诊断。"""

    result = subprocess.run(arguments, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise ReleaseContractError(
            f"isolated release build command failed: {' '.join(arguments[:2])}"
        )


def _validate_output(repo: Path, output: Path) -> Path:
    """返回未跟随 symlink 的安全输出路径，并拒绝仓库外清理目标。"""

    root = repo / ".artifacts/release-build"
    # `resolve()` 会把仓库内 symlink 转成仓库外真实路径；若再对真实路径执行
    # rmtree，就会越过发布目录边界。这里先按词法路径定界，再逐段拒绝 symlink。
    if root.is_symlink() or root.resolve() != root:
        raise ReleaseContractError("release build root has an unsafe symlink boundary")
    if root.exists() and not root.is_dir():
        raise ReleaseContractError("release build root has an unsafe file type")
    normalized = Path(os.path.abspath(output))
    try:
        relative = normalized.relative_to(root)
    except ValueError as exc:
        raise ReleaseContractError(
            "release build output must be below .artifacts/release-build"
        ) from exc
    if not relative.parts:
        raise ReleaseContractError("release build output must include an identity directory")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ReleaseContractError("release build output has an unsafe symlink boundary")
    return normalized


def _remove_output(repo: Path, output: Path, *, ignore_errors: bool) -> None:
    """在每次递归删除前重新核对词法根和 symlink 边界。"""

    validated = _validate_output(repo, output)
    if not validated.exists() and not validated.is_symlink():
        return
    if validated.is_symlink() or not validated.is_dir():
        raise ReleaseContractError("release build output has an unsafe file type")
    shutil.rmtree(validated, ignore_errors=ignore_errors)


def _project_version(source: Path) -> str:
    """从 tag 内容读取核心包版本，避免用调用环境猜测正式产物版本。"""

    try:
        document = tomllib.loads(
            (source / "packages/agent-harness/pyproject.toml").read_text(encoding="utf-8")
        )
        value = document["project"]["version"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise ReleaseContractError("tag target does not declare core project.version") from exc
    if not isinstance(value, str):
        raise ReleaseContractError("tag target project.version must be a string")
    return value


def _verify_wheel_metadata(wheel: Path, version: str) -> None:
    """检查正式 wheel 的版本与依赖，不允许 workspace/path source 泄漏。"""

    try:
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ReleaseContractError("formal wheel must contain one METADATA file")
            metadata = archive.read(names[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise ReleaseContractError("formal wheel metadata is unreadable") from exc
    if f"Version: {version}\n" not in metadata:
        raise ReleaseContractError("formal wheel version does not match tag version")
    lowered = metadata.lower()
    if "file://" in lowered or " @ ../" in lowered or " @ ./" in lowered:
        raise ReleaseContractError("formal wheel contains a workspace path dependency")


def _verify_sdist_metadata(sdist: Path, version: str) -> None:
    """独立检查正式 sdist 内的版本和 uv source，不能借用 wheel 结论。"""

    try:
        with tarfile.open(sdist, "r:gz") as archive:
            metadata_members = [
                member
                for member in archive.getmembers()
                if member.isfile() and member.name.endswith("/PKG-INFO")
            ]
            pyproject_members = [
                member
                for member in archive.getmembers()
                if member.isfile() and member.name.endswith("/pyproject.toml")
            ]
            if len(metadata_members) != 1 or len(pyproject_members) != 1:
                raise ReleaseContractError(
                    "formal sdist must contain one PKG-INFO and one pyproject.toml"
                )
            metadata_stream = archive.extractfile(metadata_members[0])
            pyproject_stream = archive.extractfile(pyproject_members[0])
            if metadata_stream is None or pyproject_stream is None:
                raise ReleaseContractError("formal sdist metadata is unreadable")
            metadata = metadata_stream.read().decode("utf-8")
            pyproject = tomllib.loads(pyproject_stream.read().decode("utf-8"))
    except ReleaseContractError:
        raise
    except (OSError, UnicodeDecodeError, tarfile.TarError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseContractError("formal sdist metadata is unreadable") from exc
    if f"Version: {version}\n" not in metadata:
        raise ReleaseContractError("formal sdist version does not match tag version")
    lowered = metadata.lower()
    if "file://" in lowered or " @ ../" in lowered or " @ ./" in lowered:
        raise ReleaseContractError("formal sdist contains a workspace path dependency")
    raw_tool = pyproject.get("tool", {})
    tool = cast(dict[str, Any], raw_tool) if isinstance(raw_tool, dict) else {}
    raw_uv = tool.get("uv", {})
    uv = cast(dict[str, Any], raw_uv) if isinstance(raw_uv, dict) else {}
    raw_sources = uv.get("sources", {})
    sources = cast(dict[str, Any], raw_sources) if isinstance(raw_sources, dict) else {}
    if any(
        isinstance(raw_source, dict)
        and (
            "path" in cast(dict[str, Any], raw_source)
            or cast(dict[str, Any], raw_source).get("workspace") is True
        )
        for raw_source in sources.values()
    ):
        raise ReleaseContractError("formal sdist contains a workspace path dependency")


def build_release(
    *,
    repo: Path,
    tag: str,
    expected_version: str,
    expected_target: str,
    output: Path,
) -> dict[str, Any]:
    """复制 tag target、运行固定 uv，并原子发布 `release-build/v1` manifest。"""

    repo = repo.resolve()
    output = _validate_output(repo, output)
    if run_git(repo, "cat-file", "-t", tag) != "tag":
        raise ReleaseContractError("formal build requires an annotated release tag")
    tag_target = run_git(repo, "rev-parse", f"{tag}^{{commit}}")
    if tag_target != expected_target:
        raise ReleaseContractError("formal build tag target identity drift")
    uv, uv_version = required_uv_identity()

    _remove_output(repo, output, ignore_errors=False)
    output.mkdir(parents=True)
    dist = output / "dist"
    try:
        with tempfile.TemporaryDirectory(prefix="agent-harness-tag-build-") as directory:
            source = Path(directory) / "source"
            _run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--no-checkout",
                    "--no-hardlinks",
                    str(repo),
                    str(source),
                ],
                cwd=repo,
            )
            _run(["git", "checkout", "--quiet", "--detach", tag_target], cwd=source)
            version = _project_version(source)
            if version != expected_version:
                raise ReleaseContractError("formal build project.version does not match promotion")
            backend = prepare_build_backend(source, uv)
            _run(
                [
                    uv,
                    "build",
                    "--package",
                    "agent-harness",
                    "--out-dir",
                    str(dist),
                    "--no-build-isolation",
                ],
                cwd=source,
            )
        wheels = sorted(dist.glob("*.whl"))
        sdists = sorted(dist.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ReleaseContractError("formal build must produce exactly one wheel and one sdist")
        _verify_wheel_metadata(wheels[0], expected_version)
        _verify_sdist_metadata(sdists[0], expected_version)
        checksums = output / "SHA256SUMS"
        checksums.write_text(
            "".join(f"{sha256_file(path)}  {path.name}\n" for path in [*wheels, *sdists]),
            encoding="utf-8",
        )
        manifest: dict[str, Any] = {
            "schema_version": "release-build/v1",
            "status": "built",
            "version": expected_version,
            "tag": tag,
            "tag_target_sha": tag_target,
            "uv_version": uv_version,
            "build_backend": backend,
            "artifacts": [
                artifact_record(wheels[0], root=repo, kind="wheel"),
                artifact_record(sdists[0], root=repo, kind="sdist"),
                artifact_record(checksums, root=repo, kind="checksums"),
            ],
        }
        write_json(output / "manifest.json", manifest)
        return manifest
    except BaseException:
        try:
            _remove_output(repo, output, ignore_errors=True)
        except ReleaseContractError:
            # 若构建期间边界被替换为 symlink，宁可保留局部产物，也不能越界清理。
            pass
        raise


def main() -> int:
    """提供可由 protected execute job 复用的独立正式构建 CLI。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag-target", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest = build_release(
            repo=args.repo,
            tag=args.tag,
            expected_version=args.version,
            expected_target=args.tag_target,
            output=args.output_dir,
        )
    except ReleaseContractError as exc:
        print(f"release build failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
