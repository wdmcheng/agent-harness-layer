"""验证 annotated tag 后正式构建与 registry plan 的版本化合同。"""

from __future__ import annotations

from typing import Any, cast

from release_contract_support import (
    BUILD_SCHEMA,
    REGISTRY_PLAN_SCHEMA,
    SEMVER,
    UV_VERSION,
    ReleaseContractError,
    approval_sha256,
    valid_git_object_id,
    validate_build_backend_identity,
)


def validate_release_build(build: dict[str, Any]) -> None:
    """验证 tag 后正式构建身份；状态必须先于 credential 和网络检查。"""

    if build.get("status") != "built":
        raise ReleaseContractError("release build status must be built")
    if build.get("schema_version") != BUILD_SCHEMA:
        raise ReleaseContractError("release build schema_version must be release-build/v1")
    version = build.get("version")
    tag = build.get("tag")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise ReleaseContractError("release build version must be stable SemVer")
    if tag != f"agent-harness-v{version}":
        raise ReleaseContractError("release build tag must match version")
    if not valid_git_object_id(build.get("tag_target_sha")):
        raise ReleaseContractError("release build tag_target_sha is invalid")
    if build.get("uv_version") != UV_VERSION:
        raise ReleaseContractError(f"release build uv_version must be {UV_VERSION}")
    validate_build_backend_identity(build.get("build_backend"))
    artifacts = build.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseContractError("release build artifacts must be a list")
    artifact_values = cast(list[object], artifacts)
    kinds: list[str] = []
    for item in artifact_values:
        if not isinstance(item, dict):
            raise ReleaseContractError("release build artifact entries must contain a string kind")
        artifact = cast(dict[str, object], item)
        kind = artifact.get("kind")
        if not isinstance(kind, str):
            raise ReleaseContractError("release build artifact entries must contain a string kind")
        kinds.append(kind)
    if sorted(kinds) != ["checksums", "sdist", "wheel"]:
        raise ReleaseContractError(
            "release build artifacts must contain wheel, sdist, and checksums"
        )


def validate_registry_plan(plan: dict[str, Any]) -> None:
    """验证 publish execute 消费的同一份无凭据计划。"""

    if plan.get("schema_version") != REGISTRY_PLAN_SCHEMA:
        raise ReleaseContractError("registry plan schema_version must be registry-publish-plan/v1")
    if plan.get("status") != "planned":
        raise ReleaseContractError("registry plan status must be planned")
    approval = plan.get("approval")
    if not isinstance(approval, dict):
        raise ReleaseContractError("registry plan approval is incomplete")
    if plan.get("approval_sha256") != approval_sha256(cast(dict[str, object], approval)):
        raise ReleaseContractError("registry plan approval checksum drift")
