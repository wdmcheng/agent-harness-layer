"""闭合 release promotion receipt 与跨 job plan 的授权身份。"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any, cast

from release_build_contract import validate_release_build
from release_contract_support import (
    PROMOTION_PLAN_SCHEMA,
    PROMOTION_SCHEMA,
    SEMVER,
    SHA256,
    ReleaseContractError,
    approval_sha256,
    release_artifacts,
    sha256_file,
    valid_git_object_id,
)
from release_preview_contract import validate_preview


def validate_promotion(
    preview: dict[str, Any],
    receipt: dict[str, Any],
    build: dict[str, Any],
    *,
    preview_path: Path,
    build_path: Path,
) -> None:
    """闭合 preview 与 promoted receipt 的前后身份，禁止仅凭 tag/job 顺序授权。"""

    validate_preview(preview)
    if receipt.get("schema_version") != PROMOTION_SCHEMA:
        raise ReleaseContractError("promotion schema_version must be release-promotion/v1")
    if receipt.get("status") != "promoted":
        raise ReleaseContractError("promotion receipt status is not promoted")
    if receipt.get("preview_manifest_sha256") != sha256_file(preview_path):
        raise ReleaseContractError("preview manifest checksum drift")
    if receipt.get("source") != preview.get("source"):
        raise ReleaseContractError("promotion source identity drift")
    if receipt.get("version") != preview.get("next_version"):
        raise ReleaseContractError("promotion version identity drift")
    if receipt.get("tag") != preview.get("tag"):
        raise ReleaseContractError("promotion tag identity drift")
    release_commit = receipt.get("release_commit_sha")
    if not valid_git_object_id(release_commit):
        raise ReleaseContractError("promotion release_commit_sha is not a valid Git object ID")
    if receipt.get("tag_target_sha") != release_commit:
        raise ReleaseContractError("promotion release commit/tag target identity drift")
    validate_release_build(build)
    if receipt.get("release_build_manifest_sha256") != sha256_file(build_path):
        raise ReleaseContractError("promotion release build manifest checksum drift")
    if build.get("version") != receipt.get("version"):
        raise ReleaseContractError("release build version identity drift")
    if build.get("tag") != receipt.get("tag"):
        raise ReleaseContractError("release build tag identity drift")
    if build.get("tag_target_sha") != release_commit:
        raise ReleaseContractError("release build tag target identity drift")
    if receipt.get("artifacts") != release_artifacts(build):
        raise ReleaseContractError("promotion formal artifact identity drift")
    release_notes_sha256 = receipt.get("release_notes_sha256")
    if (
        not isinstance(release_notes_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", release_notes_sha256) is None
    ):
        raise ReleaseContractError("promotion release_notes_sha256 is missing or invalid")
    preview_artifacts = cast(list[object], preview["artifacts"])
    release_notes: list[dict[str, Any]] = []
    for raw in preview_artifacts:
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, Any], raw)
        if item.get("kind") == "release-notes":
            release_notes.append(item)
    if len(release_notes) != 1:
        raise ReleaseContractError(
            "preview must contain exactly one release-notes artifact for promotion binding"
        )
    preview_release_notes_sha256 = release_notes[0].get("sha256")
    if (
        not isinstance(preview_release_notes_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", preview_release_notes_sha256) is None
    ):
        raise ReleaseContractError("preview release-notes artifact sha256 is missing or invalid")
    if release_notes_sha256 != preview_release_notes_sha256:
        raise ReleaseContractError("promotion release_notes_sha256 identity drift")
    for field in ("provider", "provider_release_id"):
        value = receipt.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ReleaseContractError(f"promotion {field} is missing or empty")
    provider_release_url = receipt.get("provider_release_url")
    if not isinstance(provider_release_url, str) or not provider_release_url.strip():
        raise ReleaseContractError("promotion provider_release_url is missing or empty")
    try:
        parsed_url = urllib.parse.urlparse(provider_release_url)
        provider_hostname = parsed_url.hostname
    except ValueError as exc:
        raise ReleaseContractError("promotion provider_release_url is malformed") from exc
    loopback_http = parsed_url.scheme == "http" and provider_hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    if not parsed_url.netloc or (parsed_url.scheme != "https" and not loopback_http):
        raise ReleaseContractError("promotion provider_release_url is not a valid provider URL")


def validate_promotion_plan(plan: dict[str, Any]) -> None:
    """验证跨 job promotion plan 的版本、状态与动态审批身份。"""

    if plan.get("schema_version") != PROMOTION_PLAN_SCHEMA:
        raise ReleaseContractError(
            "promotion plan schema_version must be release-promotion-plan/v1"
        )
    preview_manifest_sha256 = plan.get("preview_manifest_sha256")
    if (
        not isinstance(preview_manifest_sha256, str)
        or SHA256.fullmatch(preview_manifest_sha256) is None
    ):
        raise ReleaseContractError("promotion plan preview checksum is missing or invalid")
    source = plan.get("source")
    if not isinstance(source, dict):
        raise ReleaseContractError("promotion plan source identity is incomplete")
    typed_source = cast(dict[str, object], source)
    if not {"commit_sha", "dirty_diff_sha256", "base_tag"} <= typed_source.keys():
        raise ReleaseContractError("promotion plan source identity is incomplete")
    if not valid_git_object_id(typed_source["commit_sha"]):
        raise ReleaseContractError("promotion plan source commit_sha is invalid")
    dirty_diff_sha256 = typed_source["dirty_diff_sha256"]
    if not isinstance(dirty_diff_sha256, str) or SHA256.fullmatch(dirty_diff_sha256) is None:
        raise ReleaseContractError("promotion plan source dirty_diff_sha256 is invalid")
    base_tag = typed_source["base_tag"]
    if base_tag is not None and (not isinstance(base_tag, str) or not base_tag.strip()):
        raise ReleaseContractError("promotion plan source base_tag must be null or non-empty")
    version = plan.get("version")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise ReleaseContractError("promotion plan version must be stable SemVer")
    artifacts = plan.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseContractError("promotion plan artifacts must be a list")
    artifact_kinds: set[str] = set()
    for raw in cast(list[object], artifacts):
        if not isinstance(raw, dict):
            raise ReleaseContractError("promotion plan artifact entry must be an object")
        item = cast(dict[str, object], raw)
        kind = item.get("kind")
        path = item.get("path")
        checksum = item.get("sha256")
        size = item.get("size")
        if not isinstance(kind, str) or kind not in {"wheel", "sdist"}:
            raise ReleaseContractError("promotion plan artifact kind is invalid")
        if kind in artifact_kinds:
            raise ReleaseContractError("promotion plan artifact kind is duplicated")
        if not isinstance(path, str) or not path:
            raise ReleaseContractError("promotion plan artifact path is invalid")
        normalized = PurePosixPath(path)
        if (
            normalized.is_absolute()
            or normalized.as_posix() != path
            or any(part in {".", ".."} for part in normalized.parts)
        ):
            raise ReleaseContractError("promotion plan artifact path must be canonical relative")
        if not isinstance(checksum, str) or SHA256.fullmatch(checksum) is None:
            raise ReleaseContractError("promotion plan artifact checksum is invalid")
        if type(size) is not int or size < 0:
            raise ReleaseContractError("promotion plan artifact size is invalid")
        artifact_kinds.add(kind)
    status = plan.get("status")
    if status == "no-release":
        if plan.get("tag") is not None:
            raise ReleaseContractError("no-release promotion plan tag must be null")
        if "approval" in plan or "approval_sha256" in plan:
            raise ReleaseContractError("no-release promotion plan cannot authorize side effects")
        if artifacts:
            raise ReleaseContractError("no-release promotion plan cannot contain artifacts")
        return
    if status != "planned":
        raise ReleaseContractError("promotion plan status must be planned or no-release")
    if plan.get("tag") != f"agent-harness-v{version}":
        raise ReleaseContractError("promotion plan tag must match version")
    if artifact_kinds != {"wheel", "sdist"}:
        raise ReleaseContractError("planned promotion plan artifacts are incomplete")
    approval = plan.get("approval")
    if not isinstance(approval, dict):
        raise ReleaseContractError("promotion plan approval is incomplete")
    typed_approval = cast(dict[str, object], approval)
    required_approval = {
        "schema_version",
        "operation",
        "preview_manifest_sha256",
        "source",
        "tag",
        "version",
        "origin_endpoint_sha256",
        "provider_endpoint_sha256",
        "protected_default_branch",
        "release_notes_sha256",
        "artifacts",
    }
    if not required_approval <= typed_approval.keys():
        raise ReleaseContractError("promotion plan approval is incomplete")
    if (
        typed_approval["schema_version"] != "release-approval/v1"
        or typed_approval["operation"] != "promotion"
        or typed_approval["preview_manifest_sha256"] != preview_manifest_sha256
        or typed_approval["source"] != source
        or typed_approval["tag"] != plan.get("tag")
        or typed_approval["version"] != version
        or typed_approval["artifacts"] != artifacts
    ):
        raise ReleaseContractError("promotion plan approval identity drift")
    for field in (
        "origin_endpoint_sha256",
        "provider_endpoint_sha256",
        "release_notes_sha256",
    ):
        value = typed_approval[field]
        if not isinstance(value, str) or SHA256.fullmatch(value) is None:
            raise ReleaseContractError(f"promotion plan approval {field} is invalid")
    protected_default_branch = typed_approval["protected_default_branch"]
    if (
        not isinstance(protected_default_branch, str)
        or not protected_default_branch
        or protected_default_branch.strip() != protected_default_branch
    ):
        raise ReleaseContractError("promotion plan protected default branch is invalid")
    approval_digest = plan.get("approval_sha256")
    if not isinstance(approval_digest, str) or SHA256.fullmatch(approval_digest) is None:
        raise ReleaseContractError("promotion plan approval checksum is missing or invalid")
    if approval_digest != approval_sha256(typed_approval):
        raise ReleaseContractError("promotion plan approval checksum drift")
