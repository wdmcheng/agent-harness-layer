"""校验 GitHub 发布工作流的只读计划、受保护执行和 artifact 交接。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ci_contract_support import (
    ContractError,
    mapping,
    path_values,
    sequence,
    strings,
    workflow_triggers,
)


def validate_github_release(
    workflow: Mapping[str, Any], platform: Mapping[str, Any], release: Mapping[str, Any]
) -> None:
    """验证 GitHub 发布 DAG、权限、凭据隔离与 artifact 交接边界。"""

    if workflow_triggers(workflow) != {release["github_trigger"]}:
        raise ContractError("GitHub release workflow must be workflow_dispatch only")
    if mapping(workflow.get("permissions"), "release permissions") != {"contents": "read"}:
        raise ContractError("GitHub release workflow default permission is not read-only")
    jobs = mapping(workflow.get("jobs"), "GitHub release jobs")
    release_jobs = {
        "dry-run",
        "promote-plan",
        "promote-no-release",
        "promote-execute",
        "publish-plan",
        "publish-execute",
    }
    if set(jobs) != release_jobs:
        raise ContractError("GitHub release jobs drift")
    dry_run = mapping(jobs["dry-run"], "GitHub dry-run")
    if dry_run.get("uses") != "./.github/workflows/ci.yml":
        raise ContractError("GitHub release dry-run must reuse the complete CI workflow")
    promote_plan = mapping(jobs["promote-plan"], "GitHub promote-plan")
    promote_no_release = mapping(jobs["promote-no-release"], "GitHub promote-no-release")
    promote_execute = mapping(jobs["promote-execute"], "GitHub promote-execute")
    publish_plan = mapping(jobs["publish-plan"], "GitHub publish-plan")
    publish_execute = mapping(jobs["publish-execute"], "GitHub publish-execute")
    if promote_execute.get("environment") != release["github_promote_environment"]:
        raise ContractError("GitHub promotion environment drift")
    if publish_execute.get("environment") != release["github_publish_environment"]:
        raise ContractError("GitHub publish environment drift")
    if any("environment" in job for job in (promote_plan, promote_no_release, publish_plan)):
        raise ContractError("GitHub plan jobs must not enter a protected credential environment")
    if mapping(promote_plan.get("permissions"), "promote-plan permissions") != {"contents": "read"}:
        raise ContractError("GitHub promotion plan must remain read-only")
    if mapping(promote_execute.get("permissions"), "promote-execute permissions") != {
        "contents": "write"
    }:
        raise ContractError("GitHub promotion must exclusively request contents: write")
    if mapping(promote_no_release.get("permissions"), "promote-no-release permissions") != {
        "contents": "read"
    }:
        raise ContractError("GitHub no-release execution must remain read-only")
    if mapping(publish_plan.get("permissions"), "publish-plan permissions") != {
        "contents": "read"
    } or mapping(publish_execute.get("permissions"), "publish-execute permissions") != {
        "contents": "read"
    }:
        raise ContractError("GitHub publish must restore contents: read")
    expected_needs = {
        "promote-plan": ["dry-run"],
        "promote-no-release": ["promote-plan"],
        "promote-execute": ["promote-plan"],
        "publish-plan": ["promote-plan", "promote-execute"],
        "publish-execute": ["promote-plan", "publish-plan"],
    }
    for identifier, expected in expected_needs.items():
        job = mapping(jobs[identifier], f"GitHub {identifier}")
        if strings(job.get("needs"), f"GitHub {identifier}.needs") != expected:
            raise ContractError(f"GitHub {identifier} bypasses the release DAG")
    if any(
        "inputs.promote" not in str(mapping(jobs[name], name).get("if"))
        for name in (
            "promote-plan",
            "promote-no-release",
            "promote-execute",
            "publish-plan",
            "publish-execute",
        )
    ) or any(
        "inputs.publish" not in str(mapping(jobs[name], name).get("if"))
        for name in ("publish-plan", "publish-execute")
    ):
        raise ContractError("GitHub promotion/publish must require explicit manual inputs")
    plan_outputs = mapping(promote_plan.get("outputs"), "GitHub promote-plan outputs")
    if "steps.promotion-plan.outputs.release_required" not in str(
        plan_outputs.get("release_required")
    ):
        raise ContractError("GitHub promotion plan must expose release_required")
    no_release_if = str(promote_no_release.get("if"))
    if "needs.promote-plan.outputs.release_required == 'false'" not in no_release_if:
        raise ContractError("GitHub no-release job is not selected by the reviewed plan")
    for identifier in ("promote-execute", "publish-plan", "publish-execute"):
        condition = str(mapping(jobs[identifier], identifier).get("if"))
        if "needs.promote-plan.outputs.release_required == 'true'" not in condition:
            raise ContractError(f"GitHub {identifier} is not limited to planned releases")
    plan_serialized = (repr(promote_plan) + repr(publish_plan)).upper()
    credential_names = (
        "RELEASE_PUSH_TOKEN",
        "RELEASE_PROVIDER_TOKEN",
        "PRIVATE_REGISTRY_TOKEN",
        "UV_PUBLISH_TOKEN",
    )
    if any(name in plan_serialized for name in credential_names):
        raise ContractError("GitHub plan job reads an execute credential")
    no_release_serialized = repr(promote_no_release).upper()
    if any(name in no_release_serialized for name in credential_names):
        raise ContractError("GitHub no-release job reads an execute credential")
    promote_serialized = repr(promote_execute).upper()
    if "REGISTRY" in promote_serialized or "UV_PUBLISH" in promote_serialized:
        raise ContractError("GitHub promotion can see a registry credential")
    if not {"RELEASE_PUSH_TOKEN", "RELEASE_PROVIDER_TOKEN"} <= {
        name for name in credential_names if name in promote_serialized
    }:
        raise ContractError("GitHub promotion lacks its isolated push/provider credentials")
    publish_serialized = repr(publish_execute).upper()
    if "PRIVATE_REGISTRY_TOKEN" not in publish_serialized:
        raise ContractError("GitHub publish lacks its isolated registry credential")
    if "RELEASE_PUSH_TOKEN" in publish_serialized or "RELEASE_PROVIDER_TOKEN" in publish_serialized:
        raise ContractError("GitHub publish can see a promotion credential")
    required_artifacts = {
        "promote-plan": {
            ".artifacts/release-preview",
            ".artifacts/release-promotion/plan.json",
            str(platform["gitlab_release_child_artifact"]),
        },
        "promote-no-release": {
            ".artifacts/release-preview",
            ".artifacts/release-promotion",
        },
        "promote-execute": {
            ".artifacts/release-preview",
            ".artifacts/release-promotion",
            ".artifacts/release-build",
        },
        "publish-plan": {
            ".artifacts/release-preview",
            ".artifacts/release-promotion",
            ".artifacts/release-build",
            ".artifacts/registry-publish/plan.json",
        },
        "publish-execute": {
            ".artifacts/release-preview",
            ".artifacts/release-promotion",
            ".artifacts/release-build",
            ".artifacts/registry-publish",
        },
    }
    expected_download_names = {
        "promote-plan": "ci-release-dry-run-${{ github.run_id }}",
        "promote-no-release": "release-promotion-plan-${{ github.run_id }}",
        "promote-execute": "release-promotion-plan-${{ github.run_id }}",
        "publish-plan": "release-promotion-execute-${{ github.run_id }}",
        "publish-execute": "registry-publish-plan-${{ github.run_id }}",
    }
    for identifier, job, target in (
        ("promote-plan", promote_plan, str(release["promote_plan_target"])),
        (
            "promote-no-release",
            promote_no_release,
            str(release["promote_execute_target"]),
        ),
        ("promote-execute", promote_execute, str(release["promote_execute_target"])),
        ("publish-plan", publish_plan, str(release["publish_plan_target"])),
        ("publish-execute", publish_execute, str(release["publish_execute_target"])),
    ):
        steps = [
            mapping(item, f"release {identifier}.steps")
            for item in sequence(job.get("steps"), f"release {identifier}.steps")
        ]
        commands = [str(step["run"]).strip() for step in steps if "run" in step]
        if commands != [f"make {target}"]:
            raise ContractError(f"GitHub release {identifier} target drift")
        checkout = [step for step in steps if step.get("uses") == platform["github_checkout"]]
        setup = [step for step in steps if step.get("uses") == platform["github_setup_uv"]]
        download = [
            step for step in steps if step.get("uses") == platform["github_download_artifact"]
        ]
        upload = [step for step in steps if step.get("uses") == platform["github_upload_artifact"]]
        if len(checkout) != 1 or len(setup) != 1 or len(download) != 1 or len(upload) != 1:
            raise ContractError(f"GitHub release {identifier} action pins are incomplete")
        checkout_with = mapping(checkout[0].get("with"), f"release {identifier} checkout")
        if str(checkout_with.get("fetch-depth")) != "0":
            raise ContractError(f"GitHub release {identifier} fetch-depth must be 0")
        if str(upload[0].get("if")) != "always()":
            raise ContractError(f"GitHub release {identifier} must retain failure artifacts")
        download_with = mapping(download[0].get("with"), f"release {identifier} download")
        if download_with != {
            "name": expected_download_names[identifier],
            "path": ".artifacts",
        }:
            raise ContractError(
                f"GitHub release {identifier} download must restore .artifacts archive root"
            )
        upload_with = mapping(upload[0].get("with"), f"release {identifier} upload")
        if upload_with.get("include-hidden-files") is not True:
            raise ContractError(
                f"GitHub release {identifier} must upload hidden .artifacts evidence"
            )
        if not required_artifacts[identifier] <= path_values(upload_with.get("path")):
            raise ContractError(f"GitHub release {identifier} artifact handoff is incomplete")
