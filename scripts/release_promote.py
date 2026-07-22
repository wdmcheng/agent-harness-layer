"""默认 plan-only、仅在隔离受保护 checkout 执行 git/provider promotion。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from release_build import build_release
from release_git_remote_contract import (
    origin_push_endpoint as _origin_push_endpoint,
)
from release_git_remote_contract import (
    verify_push_default_branch as _verify_push_default_branch,
)
from release_models import (
    ReleaseContractError,
    approval_sha256,
    endpoint_sha256,
    read_json,
    redact,
    require_approval_digest,
    run_git,
    sha256_file,
    source_identity,
    validate_preview,
    validate_promotion_plan,
    verify_artifacts,
    write_json,
)
from release_promotion_receipt_contract import base_receipt as _base_receipt
from release_provider_endpoint_contract import (
    validate_provider_endpoint as _validate_provider_endpoint,
)
from release_provider_transport import create_provider_release
from release_workspace_contract import (
    freeze_release_documents as _freeze_release_documents,
)
from release_workspace_contract import (
    refresh_lock as _refresh_lock,
)
from release_workspace_contract import (
    update_release_files as _update_release_files,
)


def _approved(name: str) -> bool:
    """副作用授权只接受显式 true，避免继承的非空环境变量误触发。"""

    return os.environ.get(name, "").lower() == "true"


def _approval_payload(
    preview: dict[str, Any],
    base: dict[str, Any],
    *,
    origin_endpoint_sha256: str,
    provider_endpoint: str,
    protected_default_branch: str,
) -> dict[str, object]:
    """冻结 promotion 的全部受审输入；URL 仅以摘要进入可持久化计划。"""

    release_notes = next(
        (
            item
            for item in cast(list[dict[str, Any]], preview["artifacts"])
            if item.get("kind") == "release-notes"
        ),
        None,
    )
    if release_notes is None:
        raise ReleaseContractError("preview release-notes artifact is incomplete")
    return {
        "schema_version": "release-approval/v1",
        "operation": "promotion",
        "preview_manifest_sha256": str(base["preview_manifest_sha256"]),
        "source": cast(dict[str, object], preview["source"]),
        "tag": str(preview["tag"]),
        "version": str(preview["next_version"]),
        "origin_endpoint_sha256": origin_endpoint_sha256,
        "provider_endpoint_sha256": endpoint_sha256(provider_endpoint),
        "protected_default_branch": protected_default_branch,
        "release_notes_sha256": str(release_notes["sha256"]),
        "artifacts": cast(list[dict[str, object]], base["artifacts"]),
    }


def promote(
    *,
    repo: Path,
    manifest_path: Path,
    output_dir: Path,
    execute: bool,
    plan_path: Path | None = None,
) -> dict[str, Any]:
    """执行 promotion 计划或受保护生命周期，所有外部副作用均晚于完整 preflight。"""

    preview = read_json(manifest_path)
    validate_preview(preview)
    base = _base_receipt(preview, manifest_path)
    if preview["status"] == "no-release":
        plan = {
            **base,
            "schema_version": "release-promotion-plan/v1",
            "status": "no-release",
            "tag": None,
        }
        if not execute:
            if plan_path is not None:
                write_json(plan_path, plan)
            return plan
        if plan_path is None:
            raise ReleaseContractError("promotion execute requires --plan-input")
        reviewed_plan = read_json(plan_path)
        validate_promotion_plan(reviewed_plan)
        if reviewed_plan != plan:
            raise ReleaseContractError("promotion approval plan identity drift")
        receipt = {**base, "status": "no-release"}
        write_json(output_dir / "receipt.json", receipt)
        return receipt
    verify_artifacts(preview, base=repo)
    identity = source_identity(repo)
    if identity != preview["source"]:
        raise ReleaseContractError("promotion source identity does not match reviewed preview")
    if run_git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseContractError("promotion requires a clean tracked checkout")
    provider_url = os.environ.get("RELEASE_PROVIDER_URL", "")
    protected_default_branch = os.environ.get("RELEASE_PROTECTED_DEFAULT_BRANCH", "")
    if not provider_url:
        raise ReleaseContractError("RELEASE_PROVIDER_URL is required to identify the plan")
    if not protected_default_branch or protected_default_branch.strip() != protected_default_branch:
        raise ReleaseContractError(
            "RELEASE_PROTECTED_DEFAULT_BRANCH must declare one non-empty branch"
        )
    _validate_provider_endpoint(provider_url)
    push_endpoint, origin_endpoint_identity = _origin_push_endpoint(repo)
    approval = _approval_payload(
        preview,
        base,
        origin_endpoint_sha256=origin_endpoint_identity,
        provider_endpoint=provider_url,
        protected_default_branch=protected_default_branch,
    )
    reviewed_approval = approval_sha256(approval)
    plan: dict[str, Any] = {
        **base,
        "schema_version": "release-promotion-plan/v1",
        "status": "planned",
        "tag": preview["tag"],
        "approval": approval,
        "approval_sha256": reviewed_approval,
    }
    if not execute:
        if plan_path is not None:
            write_json(plan_path, plan)
        return plan
    if plan_path is None:
        raise ReleaseContractError("promotion execute requires --plan-input")
    reviewed_plan = read_json(plan_path)
    validate_promotion_plan(reviewed_plan)
    if reviewed_plan != plan:
        raise ReleaseContractError("promotion approval plan identity drift")
    if not _approved("RELEASE_PROMOTION_APPROVED"):
        raise ReleaseContractError("promotion execute requires RELEASE_PROMOTION_APPROVED=true")
    require_approval_digest("RELEASE_PROMOTION_APPROVAL_SHA256", reviewed_approval)
    if not _approved("RELEASE_PROTECTED_REF"):
        raise ReleaseContractError("promotion execute requires protected ref evidence")
    current_branch = run_git(repo, "branch", "--show-current")
    if current_branch != protected_default_branch:
        raise ReleaseContractError(
            "promotion checkout is not the declared protected default branch"
        )
    _verify_push_default_branch(repo, push_endpoint, protected_default_branch)
    provider_token = os.environ.get("RELEASE_PROVIDER_TOKEN", "")
    if not provider_token:
        raise ReleaseContractError(
            "restricted RELEASE_PROVIDER_TOKEN environment credential is required"
        )
    documents = _freeze_release_documents(repo, preview)
    changelog_preview, _changelog_sha256 = documents["changelog"]
    release_notes_text, release_notes_sha256 = documents["release-notes"]
    confirmed: dict[str, Any] = {}
    try:
        _update_release_files(repo, preview, changelog_preview=changelog_preview)
        _refresh_lock(repo)
        changed = [
            relative
            for relative in (
                "pyproject.toml",
                "packages/agent-harness/pyproject.toml",
                "packages/agent-harness/src/agent_harness/__init__.py",
                "templates/service-app/pyproject.toml",
                "CHANGELOG.md",
                "uv.lock",
            )
            if (repo / relative).exists()
        ]
        run_git(repo, "add", "--", *changed)
        run_git(repo, "commit", "-m", f"chore(release): {preview['next_version']}")
        release_commit = run_git(repo, "rev-parse", "HEAD")
        confirmed["release_commit_sha"] = release_commit
        tag = str(preview["tag"])
        run_git(repo, "tag", "-a", tag, "-m", f"agent-harness {preview['next_version']}")
        tag_target = run_git(repo, "rev-list", "-n", "1", tag)
        confirmed.update({"tag": tag, "tag_target_sha": tag_target})
        if tag_target != release_commit:
            raise ReleaseContractError("annotated tag target does not equal release commit")
        run_git(
            repo,
            "push",
            push_endpoint,
            f"HEAD:refs/heads/{protected_default_branch}",
        )
        run_git(repo, "push", push_endpoint, f"refs/tags/{tag}:refs/tags/{tag}")
        provider = create_provider_release(
            provider_url,
            provider_token,
            {
                "tag": tag,
                "target": release_commit,
                "version": preview["next_version"],
                "release_notes": release_notes_text,
            },
        )
        confirmed.update(
            {
                "provider": "configured-http-provider",
                "provider_release_id": provider["id"],
                "provider_release_url": provider["url"],
            }
        )
        build_output = repo / ".artifacts/release-build" / output_dir.name
        build = build_release(
            repo=repo,
            tag=tag,
            expected_version=str(preview["next_version"]),
            expected_target=release_commit,
            output=build_output,
        )
        build_manifest = build_output / "manifest.json"
        formal_artifacts = [
            item
            for item in cast(list[dict[str, Any]], build["artifacts"])
            if item.get("kind") in {"wheel", "sdist"}
        ]
        receipt = {
            **base,
            "status": "promoted",
            **confirmed,
            "artifacts": formal_artifacts,
            "release_build_manifest": build_manifest.relative_to(repo).as_posix(),
            "release_build_manifest_sha256": sha256_file(build_manifest),
            "release_notes_sha256": release_notes_sha256,
        }
        write_json(output_dir / "receipt.json", receipt)
        return receipt
    except (ReleaseContractError, OSError) as exc:
        failed = {
            **base,
            "status": "failed",
            **confirmed,
            "failure": redact(str(exc), provider_token),
        }
        write_json(output_dir / "receipt.json", failed)
        raise ReleaseContractError(str(exc)) from exc


def main() -> int:
    """运行 promotion CLI；凭据不接受参数，异常信息使用同一脱敏函数收口。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    plan_group = parser.add_mutually_exclusive_group()
    plan_group.add_argument("--plan-output", type=Path)
    plan_group.add_argument("--plan-input", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    output = (
        args.output_dir
        or repo / ".artifacts/release-promotion" / run_git(repo, "rev-parse", "HEAD")[:12]
    )
    token = os.environ.get("RELEASE_PROVIDER_TOKEN", "")
    try:
        receipt = promote(
            repo=repo,
            manifest_path=args.manifest.resolve(),
            output_dir=output.resolve(),
            execute=args.execute,
            plan_path=(args.plan_input or args.plan_output).resolve()
            if (args.plan_input or args.plan_output)
            else None,
        )
    except ReleaseContractError as exc:
        # preflight 失败也生成 failed receipt，确保 CI 不会把“无文件”误认为可重试成功。
        try:
            preview = read_json(args.manifest.resolve())
            receipt_path = output.resolve() / "receipt.json"
            if (
                args.execute
                and preview.get("schema_version") == "release-preview/v1"
                and not receipt_path.exists()
            ):
                failed = {
                    **_base_receipt(preview, args.manifest.resolve()),
                    "status": "failed",
                    "failure": redact(str(exc), token),
                }
                write_json(receipt_path, failed)
        except ReleaseContractError:
            pass
        print(redact(f"release promotion failed: {exc}", token), file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
