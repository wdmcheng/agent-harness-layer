"""默认只计划、经双重授权后才上传私有 registry 的发布 wrapper。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from registry_publish_upload import (
    RegistryUploadError,
)
from registry_publish_upload import (
    approved as _approved,
)
from registry_publish_upload import (
    upload as _upload,
)
from registry_publish_upload import (
    validate_endpoint as _validate_endpoint,
)
from registry_publish_upload import (
    validate_same_registry as _validate_same_registry,
)
from release_models import (
    ReleaseContractError,
    approval_sha256,
    endpoint_sha256,
    read_json,
    redact,
    require_approval_digest,
    resolve_artifact,
    sha256_file,
    validate_preview,
    validate_promotion,
    validate_registry_plan,
    validate_release_build,
    verify_artifacts,
    write_json,
)


class RegistryPublishFailure(ReleaseContractError):
    """携带已去敏的 failed receipt，使 CLI 在无 output 时仍能打印复核清单。"""

    def __init__(self, message: str, *, receipt: dict[str, Any]) -> None:
        super().__init__(message)
        self.receipt = receipt


def _inventory_item(item: dict[str, Any], *, status: str) -> dict[str, object]:
    """复制公开 artifact 身份并附上传状态，不携带 endpoint、响应正文或 credential。"""

    return {
        "path": item["path"],
        "kind": item["kind"],
        "sha256": item["sha256"],
        "size": item["size"],
        "status": status,
    }


def _inventory_summary(receipt: dict[str, Any]) -> list[str]:
    """把 failed receipt 压成稳定的 path@SHA 清单，供无 output 的人工复核。"""

    lines: list[str] = []
    for field in ("confirmed_uploads", "unconfirmed_uploads"):
        raw_items = receipt.get(field, [])
        items = cast(list[object], raw_items) if isinstance(raw_items, list) else []
        values: list[str] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            item = cast(dict[str, object], raw)
            values.append(f"{item.get('path')}@{item.get('sha256')}[{item.get('status')}]")
        lines.append(f"{field}: {', '.join(values) if values else '(none)'}")
    return lines


def _protected_ref_evidence(promotion: dict[str, Any]) -> dict[str, str]:
    """把 CI 的 protected-ref 证明闭合到 promotion 的 release commit 与完整 tag ref。"""

    name = os.environ.get("RELEASE_PROTECTED_REF_NAME", "")
    sha = os.environ.get("RELEASE_PROTECTED_REF_SHA", "")
    expected_name = f"refs/tags/{promotion['tag']}"
    expected_sha = str(promotion["release_commit_sha"])
    if name != expected_name or sha != expected_sha or sha != promotion["tag_target_sha"]:
        raise ReleaseContractError(
            "protected ref evidence does not match the promoted release commit/ref"
        )
    return {"name": name, "sha": sha}


def _approval_payload(
    *,
    preview_path: Path,
    receipt_path: Path,
    build_path: Path,
    promotion: dict[str, Any],
    endpoint: str,
    check_endpoint: str,
    protected_ref: dict[str, str],
    artifacts: list[dict[str, Any]],
) -> dict[str, object]:
    """冻结 publish 的跨 job 身份；endpoint 只以摘要进入 plan，credential 永不入 payload。"""

    return {
        "schema_version": "release-approval/v1",
        "operation": "registry-publish",
        "preview_manifest_sha256": sha256_file(preview_path),
        "promotion_receipt_sha256": sha256_file(receipt_path),
        "release_build_manifest_sha256": sha256_file(build_path),
        "release_commit_sha": str(promotion["release_commit_sha"]),
        "tag": str(promotion["tag"]),
        "registry_endpoint_sha256": endpoint_sha256(endpoint),
        "registry_check_endpoint_sha256": endpoint_sha256(check_endpoint),
        "protected_ref": protected_ref,
        "artifacts": cast(list[dict[str, object]], artifacts),
    }


def publish(
    *,
    preview_path: Path,
    receipt_path: Path,
    build_path: Path | None = None,
    execute: bool,
    artifact_root: Path,
    output_path: Path | None,
    plan_path: Path | None = None,
) -> dict[str, Any]:
    """验证所有身份后输出 plan，或在显式 execute 下顺序上传并保存去敏回执。"""

    # build 状态先于 preview、credential 和 endpoint 读取，非法正式产物必须在
    # 所有敏感输入与网络 seam 之前 fail closed。
    promotion_hint: dict[str, Any] | None = None
    if build_path is None:
        promotion_hint = read_json(receipt_path)
        raw_build_path = promotion_hint.get("release_build_manifest")
        if not isinstance(raw_build_path, str) or not raw_build_path:
            raise ReleaseContractError(
                "promotion receipt does not identify a release build manifest"
            )
        build_path = (artifact_root / raw_build_path).resolve()
        try:
            build_path.relative_to(artifact_root.resolve())
        except ValueError as exc:
            raise ReleaseContractError("release build manifest path escapes artifact root") from exc
    build = read_json(build_path)
    validate_release_build(build)
    preview = read_json(preview_path)
    promotion = promotion_hint or read_json(receipt_path)
    validate_preview(preview)
    validate_promotion(
        preview,
        promotion,
        build,
        preview_path=preview_path,
        build_path=build_path,
    )
    artifacts = verify_artifacts(build, base=artifact_root)
    artifacts = [item for item in artifacts if item.get("kind") in {"wheel", "sdist"}]
    endpoint = os.environ.get("UV_PUBLISH_URL", "")
    if not endpoint:
        raise ReleaseContractError("UV_PUBLISH_URL is required to identify the plan")
    _validate_endpoint(endpoint)
    check_endpoint = os.environ.get("UV_PUBLISH_CHECK_URL", "")
    if not check_endpoint:
        raise ReleaseContractError("UV_PUBLISH_CHECK_URL is required to identify the plan")
    _validate_endpoint(check_endpoint)
    _validate_same_registry(endpoint, check_endpoint)
    protected_ref = _protected_ref_evidence(promotion)
    approval = _approval_payload(
        preview_path=preview_path,
        receipt_path=receipt_path,
        build_path=build_path,
        promotion=promotion,
        endpoint=endpoint,
        check_endpoint=check_endpoint,
        protected_ref=protected_ref,
        artifacts=artifacts,
    )
    reviewed_approval = approval_sha256(approval)
    plan: dict[str, Any] = {
        "schema_version": "registry-publish-plan/v1",
        "status": "planned",
        "version": preview["next_version"],
        "tag": preview["tag"],
        "artifacts": artifacts,
        "approval": approval,
        "approval_sha256": reviewed_approval,
    }
    if not execute:
        if plan_path is not None:
            write_json(plan_path, plan)
        return plan
    if plan_path is None:
        raise ReleaseContractError("registry execute requires --plan-input")
    reviewed_plan = read_json(plan_path)
    validate_registry_plan(reviewed_plan)
    if reviewed_plan != plan:
        raise ReleaseContractError("registry approval plan identity drift")
    if not _approved("REGISTRY_PUBLISH_APPROVED"):
        raise ReleaseContractError("registry execute requires REGISTRY_PUBLISH_APPROVED=true")
    require_approval_digest("REGISTRY_PUBLISH_APPROVAL_SHA256", reviewed_approval)
    if not _approved("RELEASE_PROTECTED_REF"):
        raise ReleaseContractError("registry execute requires protected ref evidence")
    token = os.environ.get("UV_PUBLISH_TOKEN", "")
    if not token:
        raise ReleaseContractError("restricted UV_PUBLISH_TOKEN environment credential is required")
    results: list[dict[str, object]] = []
    confirmed: list[dict[str, object]] = []
    for index, item in enumerate(artifacts):
        path = resolve_artifact(artifact_root, item)
        try:
            result = _upload(
                endpoint,
                check_endpoint,
                token,
                path,
                str(item["sha256"]),
                str(preview["next_version"]),
            )
        except RegistryUploadError as exc:
            failure = redact(str(exc), token, endpoint)
            unconfirmed = [_inventory_item(item, status=exc.upload_status)]
            unconfirmed.extend(
                _inventory_item(pending, status="not-started") for pending in artifacts[index + 1 :]
            )
            failed: dict[str, Any] = {
                "schema_version": "registry-publish/v1",
                "status": "failed",
                "version": preview["next_version"],
                "tag": preview["tag"],
                "confirmed_uploads": confirmed,
                "unconfirmed_uploads": unconfirmed,
                "failure": failure,
            }
            if output_path is not None:
                write_json(output_path, failed)
            raise RegistryPublishFailure(failure, receipt=failed) from exc
        results.append(
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "status": result["status"],
            }
        )
        confirmed.append(_inventory_item(item, status="confirmed"))
    published = {**plan, "status": "published", "uploads": results}
    if output_path is not None:
        write_json(output_path, published)
    return published


def main() -> int:
    """提供 CI 可复用 CLI；所有 credential 仅从环境读取并在异常路径二次脱敏。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--promotion-receipt", required=True, type=Path)
    parser.add_argument("--build-manifest", type=Path)
    parser.add_argument("--artifact-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    plan_group = parser.add_mutually_exclusive_group()
    plan_group.add_argument("--plan-output", type=Path)
    plan_group.add_argument("--plan-input", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("UV_PUBLISH_TOKEN", "")
    try:
        result = publish(
            preview_path=args.manifest.resolve(),
            receipt_path=args.promotion_receipt.resolve(),
            build_path=args.build_manifest.resolve() if args.build_manifest else None,
            execute=args.execute,
            artifact_root=args.artifact_root.resolve(),
            output_path=args.output.resolve() if args.output else None,
            plan_path=(args.plan_input or args.plan_output).resolve()
            if (args.plan_input or args.plan_output)
            else None,
        )
    except RegistryPublishFailure as exc:
        print(redact(f"registry publish failed: {exc}", token), file=sys.stderr)
        for line in _inventory_summary(exc.receipt):
            print(redact(line, token), file=sys.stderr)
        return 2
    except ReleaseContractError as exc:
        print(redact(f"registry publish failed: {exc}", token), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
