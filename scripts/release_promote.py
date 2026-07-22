"""默认 plan-only、仅在隔离受保护 checkout 执行 git/provider promotion。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, cast

from release_build import build_release
from release_models import (
    ReleaseContractError,
    approval_sha256,
    endpoint_sha256,
    read_json,
    redact,
    require_approval_digest,
    required_uv_executable,
    resolve_artifact,
    run_git,
    sha256_bytes,
    sha256_file,
    source_identity,
    validate_preview,
    validate_promotion_plan,
    verify_artifacts,
    write_json,
)
from release_provider_transport import create_provider_release


def _approved(name: str) -> bool:
    """副作用授权只接受显式 true，避免继承的非空环境变量误触发。"""

    return os.environ.get(name, "").lower() == "true"


def _validate_provider_endpoint(value: str) -> None:
    """provider endpoint 禁止 URL credential；生产仅接受 HTTPS。"""

    try:
        parsed = urllib.parse.urlparse(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ReleaseContractError("provider endpoint is malformed") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ReleaseContractError("provider endpoint must not contain URL credentials")
    if parsed.scheme == "https" and parsed.netloc:
        return
    if (
        _approved("RELEASE_TEST_MODE")
        and parsed.scheme == "http"
        and hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        return
    raise ReleaseContractError("provider endpoint must use HTTPS (loopback HTTP is test-only)")


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


def _freeze_release_documents(repo: Path, preview: dict[str, Any]) -> dict[str, tuple[str, str]]:
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


def _update_release_files(
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


def _refresh_lock(repo: Path) -> None:
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


def _base_receipt(preview: dict[str, Any], manifest: Path) -> dict[str, Any]:
    """建立所有终态共享身份，failed/no-release 也不能丢掉受审输入。"""

    artifacts_raw = preview.get("artifacts")
    if not isinstance(artifacts_raw, list):
        raise ReleaseContractError("preview artifacts must be a list")
    publishable: list[dict[str, Any]] = []
    for raw in cast(list[object], artifacts_raw):
        if isinstance(raw, dict):
            item = cast(dict[str, Any], raw)
            if item.get("kind") in {"wheel", "sdist"}:
                publishable.append(item)
    return {
        "schema_version": "release-promotion/v1",
        "preview_manifest_sha256": sha256_file(manifest),
        "source": preview["source"],
        "version": preview.get("next_version") or preview["current_version"],
        # preview distribution 只用于审批和可复现性对照，正式回执必须在 tag 后
        # 用 release-build/v1 覆盖该字段，避免下游误发 dry-run 产物。
        "artifacts": publishable,
    }


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


def _origin_push_endpoint(repo: Path) -> tuple[str, str]:
    """冻结唯一实际 push endpoint 及摘要，使校验、审批和写入使用同一目标。

    remote URL 可能携带受限 userinfo，计划只持久化 SHA-256，不输出原值。这里读取
    push URL 而不是 fetch URL，因为后续副作用明确执行 `git push origin`。
    """

    endpoints = [
        line
        for line in run_git(repo, "remote", "get-url", "--push", "--all", "origin").splitlines()
        if line
    ]
    if len(endpoints) != 1:
        raise ReleaseContractError("origin must declare exactly one push endpoint")
    endpoint = endpoints[0]
    return endpoint, endpoint_sha256(endpoint)


def _verify_push_default_branch(repo: Path, push_endpoint: str, expected_branch: str) -> None:
    """从实际写入 endpoint 读取默认分支，并绑定当前受审 source commit。

    平台环境变量只声明期望值，不构成保护证据；真正的远端 symref 与 HEAD OID
    必须同时匹配，避免任意同名本地分支消费 promotion approval。
    """

    remote_head = run_git(repo, "ls-remote", "--symref", push_endpoint, "HEAD")
    lines = [line.split() for line in remote_head.splitlines() if line.strip()]
    symref = next((parts for parts in lines if parts[0] == "ref:"), None)
    identity = next(
        (parts for parts in lines if parts[-1] == "HEAD" and parts[0] != "ref:"),
        None,
    )
    expected_ref = f"refs/heads/{expected_branch}"
    if symref is None or len(symref) != 3 or symref[1:] != [expected_ref, "HEAD"]:
        raise ReleaseContractError(
            "origin default branch does not match the declared protected default branch"
        )
    if identity is None or len(identity) != 2:
        raise ReleaseContractError("origin default branch identity is unavailable")
    if identity[0] != run_git(repo, "rev-parse", "HEAD"):
        raise ReleaseContractError(
            "origin default branch does not point to the reviewed source commit"
        )


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
