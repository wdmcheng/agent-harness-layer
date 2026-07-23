"""验证 release preview 及其仓库相对 artifact 身份。"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, cast

from release_contract_support import (
    BUMPS,
    GIT_OBJECT_ID,
    PREVIEW_SCHEMA,
    RELEASE_ARTIFACT_KINDS,
    SEMVER,
    SHA256,
    ReleaseContractError,
    sha256_file,
    valid_git_object_id,
    validate_build_backend_identity,
    validate_uv_version,
)


def validate_preview(manifest: dict[str, Any]) -> None:
    """验证 v1 全部必填解释字段及 release/no-release 状态不变量。"""

    if manifest.get("schema_version") != PREVIEW_SCHEMA:
        raise ReleaseContractError("preview schema_version must be release-preview/v1")
    status = manifest.get("status")
    if status not in {"release", "no-release"}:
        raise ReleaseContractError("preview status must be release or no-release")
    source = manifest.get("source")
    decision = manifest.get("decision")
    if not isinstance(source, dict):
        raise ReleaseContractError("preview source identity is incomplete")
    typed_source = cast(dict[str, object], source)
    if not {"commit_sha", "dirty_diff_sha256", "base_tag"} <= typed_source.keys():
        raise ReleaseContractError("preview source identity is incomplete")
    commit_sha = typed_source["commit_sha"]
    dirty_diff_sha256 = typed_source["dirty_diff_sha256"]
    base_tag = typed_source["base_tag"]
    if not valid_git_object_id(commit_sha):
        raise ReleaseContractError("preview source commit_sha is invalid")
    if not isinstance(dirty_diff_sha256, str) or SHA256.fullmatch(dirty_diff_sha256) is None:
        raise ReleaseContractError("preview source dirty_diff_sha256 is invalid")
    if base_tag is not None and (not isinstance(base_tag, str) or not base_tag.strip()):
        raise ReleaseContractError("preview source base_tag must be null or a non-empty string")
    if not isinstance(decision, dict):
        raise ReleaseContractError("preview decision is incomplete")
    typed_decision = cast(dict[str, object], decision)
    if not {"bump", "reason", "commits"} <= typed_decision.keys():
        raise ReleaseContractError("preview decision is incomplete")
    bump = typed_decision["bump"]
    reason = typed_decision["reason"]
    commits = typed_decision["commits"]
    if not isinstance(reason, str) or not reason.strip():
        raise ReleaseContractError("preview decision reason must be a non-empty string")
    if not isinstance(commits, list):
        raise ReleaseContractError("preview decision commits must be a list")
    required_commit_fields = {"sha", "type", "scope", "subject", "breaking", "bump"}
    for index, raw in enumerate(cast(list[object], commits)):
        if not isinstance(raw, dict):
            raise ReleaseContractError(f"preview decision commit {index} must be an object")
        item = cast(dict[str, object], raw)
        if not required_commit_fields <= item.keys():
            raise ReleaseContractError(f"preview decision commit {index} is incomplete")
        item_sha = item["sha"]
        item_type = item["type"]
        item_scope = item["scope"]
        item_subject = item["subject"]
        item_breaking = item["breaking"]
        item_bump = item["bump"]
        if not isinstance(item_sha, str) or GIT_OBJECT_ID.fullmatch(item_sha) is None:
            raise ReleaseContractError(f"preview decision commit {index} sha is invalid")
        if not isinstance(item_type, str) or not item_type.strip():
            raise ReleaseContractError(f"preview decision commit {index} type is invalid")
        if item_scope is not None and (not isinstance(item_scope, str) or not item_scope.strip()):
            raise ReleaseContractError(f"preview decision commit {index} scope is invalid")
        if not isinstance(item_subject, str) or not item_subject.strip():
            raise ReleaseContractError(f"preview decision commit {index} subject is invalid")
        if type(item_breaking) is not bool:
            raise ReleaseContractError(f"preview decision commit {index} breaking is invalid")
        if item_bump is not None and (not isinstance(item_bump, str) or item_bump not in BUMPS):
            raise ReleaseContractError(f"preview decision commit {index} bump is invalid")
    current_version = manifest.get("current_version")
    if not isinstance(current_version, str) or SEMVER.fullmatch(current_version) is None:
        raise ReleaseContractError("preview current_version must be stable SemVer")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseContractError("preview artifacts must be a list")
    typed_artifacts = cast(list[object], artifacts)
    if status == "no-release":
        if bump is not None:
            raise ReleaseContractError("no-release preview decision bump must be null")
        if (
            "next_version" not in manifest
            or "tag" not in manifest
            or "uv_version" not in manifest
            or manifest.get("next_version") is not None
            or manifest.get("tag") is not None
            or manifest.get("uv_version") is not None
            or typed_artifacts
        ):
            raise ReleaseContractError(
                "no-release preview cannot authorize version, tag, or artifacts"
            )
        return
    if not isinstance(bump, str) or bump not in BUMPS:
        raise ReleaseContractError("release preview decision bump must be major, minor, or patch")
    next_version = manifest.get("next_version")
    tag = manifest.get("tag")
    if not isinstance(next_version, str) or SEMVER.fullmatch(next_version) is None:
        raise ReleaseContractError("release preview next_version must be stable SemVer")
    if not isinstance(tag, str) or tag != f"agent-harness-v{next_version}":
        raise ReleaseContractError("release preview tag must match next_version")
    validate_uv_version(manifest.get("uv_version"))
    validate_build_backend_identity(manifest.get("build_backend"))
    if len(typed_artifacts) != len(RELEASE_ARTIFACT_KINDS):
        raise ReleaseContractError("release preview must contain exactly five artifacts")
    kinds: set[str] = set()
    paths: set[str] = set()
    for raw in typed_artifacts:
        if not isinstance(raw, dict):
            raise ReleaseContractError("preview artifact entry must be an object")
        item = cast(dict[str, object], raw)
        kind = item.get("kind")
        path = item.get("path")
        if not isinstance(kind, str) or kind not in RELEASE_ARTIFACT_KINDS:
            raise ReleaseContractError("release preview artifact kind is invalid")
        if kind in kinds:
            raise ReleaseContractError(f"release preview artifact kind is duplicated: {kind}")
        if not isinstance(path, str) or not path:
            raise ReleaseContractError("release preview artifact path is invalid")
        normalized = PurePosixPath(path)
        if (
            normalized.is_absolute()
            or normalized.as_posix() != path
            or any(part in {".", ".."} for part in normalized.parts)
        ):
            raise ReleaseContractError("release preview artifact path must be canonical relative")
        if path in paths:
            raise ReleaseContractError(f"release preview artifact path is duplicated: {path}")
        kinds.add(kind)
        paths.add(path)
    if kinds != RELEASE_ARTIFACT_KINDS:
        raise ReleaseContractError("release preview artifacts are incomplete")


def resolve_artifact(base: Path, item: dict[str, Any]) -> Path:
    """把 artifact 相对路径约束在 manifest 根下，阻止 path traversal 与绝对路径。"""

    raw = item.get("path")
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise ReleaseContractError("artifact path must be repo-relative")
    root = base.resolve()
    path = (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ReleaseContractError("artifact path escapes manifest root") from exc
    return path


def verify_artifacts(manifest: dict[str, Any], *, base: Path) -> list[dict[str, Any]]:
    """复算全部预演产物，但只把 wheel/sdist 返回给 registry upload。"""

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseContractError("preview artifacts must be a list")
    items: list[dict[str, Any]] = []
    publishable: list[dict[str, Any]] = []
    resolved_items: list[tuple[dict[str, Any], Path]] = []
    kinds: set[str] = set()
    paths: set[Path] = set()
    for raw in cast(list[object], artifacts):
        if not isinstance(raw, dict):
            raise ReleaseContractError("preview artifact entry must be an object")
        item = cast(dict[str, Any], raw)
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in RELEASE_ARTIFACT_KINDS:
            raise ReleaseContractError("preview artifact kind is invalid")
        if kind in kinds:
            raise ReleaseContractError(f"preview artifact kind is duplicated: {kind}")
        checksum = item.get("sha256")
        size = item.get("size")
        if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
            raise ReleaseContractError("preview artifact sha256 is invalid")
        if type(size) is not int or size < 0:
            raise ReleaseContractError("preview artifact size is invalid")
        path = resolve_artifact(base, item)
        if path in paths:
            raise ReleaseContractError(f"preview artifact path is duplicated: {item.get('path')}")
        items.append(item)
        resolved_items.append((item, path))
        kinds.add(kind)
        paths.add(path)
        if kind in {"wheel", "sdist"}:
            publishable.append(item)
    if manifest.get("status") == "release" and (
        len(items) != len(RELEASE_ARTIFACT_KINDS) or kinds != RELEASE_ARTIFACT_KINDS
    ):
        raise ReleaseContractError("release preview must contain each required artifact once")
    if manifest.get("status") == "no-release" and items:
        raise ReleaseContractError("no-release preview cannot contain artifacts")
    for item, path in resolved_items:
        if not path.is_file():
            raise ReleaseContractError(f"artifact is missing: {item.get('path')}")
        if item.get("sha256") != sha256_file(path):
            raise ReleaseContractError(f"artifact checksum drift: {item.get('path')}")
        if item.get("size") != path.stat().st_size:
            raise ReleaseContractError(f"artifact size drift: {item.get('path')}")
    return publishable
