"""比较 GitHub/GitLab job、DAG、artifact、history 与权限语义。"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml


class ContractError(RuntimeError):
    """表示 pipeline 配置扩大权限、漂移入口或缺失失败证据。"""


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a mapping")
    return cast(dict[str, Any], value)


def _sequence(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a list")
    return cast(list[Any], value)


def _strings(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    result: list[str] = []
    for item in _sequence(value, label):
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            item_mapping = cast(dict[str, Any], item)
            job_value = item_mapping.get("job")
            if isinstance(job_value, str):
                result.append(job_value)
                continue
            raise ContractError(f"{label} contains a non-job value")
        else:
            raise ContractError(f"{label} contains a non-job value")
    return result


def _yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"pipeline file is missing: {path}")
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _mapping(loaded, str(path))


def _toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"job contract is missing: {path}")
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _workflow_triggers(workflow: Mapping[str, Any]) -> set[str]:
    # 仓库显式引用 on，避免 PyYAML 1.1 将它解释为布尔值后掩盖 trigger 漂移。
    raw = workflow.get("on")
    if isinstance(raw, str):
        return {raw}
    return set(_mapping(raw, "GitHub on"))


def _permission(job: Mapping[str, Any], workflow: Mapping[str, Any]) -> dict[str, Any]:
    raw = job.get("permissions", workflow.get("permissions"))
    return _mapping(raw, "GitHub permissions")


def _target_block(makefile: str, target: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(target)}\s*:[^\n]*\n(?P<body>(?:\t[^\n]*\n)+)", makefile)
    if match is None:
        raise ContractError(f"Make target is missing: {target}")
    return match.group("body")


def _path_values(value: object) -> set[str]:
    if isinstance(value, str):
        return {line.strip().rstrip("/") for line in value.splitlines() if line.strip()}
    return {item.rstrip("/") for item in _strings(value, "artifact paths")}


def _validate_github_job(
    *,
    identifier: str,
    contract: Mapping[str, Any],
    job: Mapping[str, Any],
    workflow: Mapping[str, Any],
    platform: Mapping[str, Any],
) -> None:
    expected_needs = set(_strings(contract.get("needs"), f"{identifier}.needs"))
    actual_needs = set(_strings(job.get("needs"), f"GitHub {identifier}.needs"))
    if actual_needs != expected_needs:
        raise ContractError(f"GitHub {identifier} needs drift: {sorted(actual_needs)}")
    if _permission(job, workflow) != {"contents": "read"}:
        raise ContractError(f"GitHub ordinary job {identifier} is not read-only")
    steps = [
        _mapping(item, f"GitHub {identifier}.steps")
        for item in _sequence(job.get("steps"), f"GitHub {identifier}.steps")
    ]
    checkout = [step for step in steps if step.get("uses") == platform["github_checkout"]]
    setup = [step for step in steps if step.get("uses") == platform["github_setup_uv"]]
    upload = [step for step in steps if step.get("uses") == platform["github_upload_artifact"]]
    if len(checkout) != 1 or len(setup) != 1 or len(upload) != 1:
        raise ContractError(f"GitHub {identifier} must pin one checkout/setup-uv/upload action")
    checkout_with = _mapping(checkout[0].get("with"), f"GitHub {identifier} checkout inputs")
    if str(checkout_with.get("fetch-depth")) != "0":
        raise ContractError(f"GitHub {identifier} fetch-depth must be 0")
    if str(checkout_with.get("persist-credentials")).lower() != "false":
        raise ContractError(f"GitHub {identifier} checkout must not persist credentials")
    setup_with = _mapping(setup[0].get("with"), f"GitHub {identifier} setup-uv inputs")
    if str(setup_with.get("version")) != str(platform["uv_version"]):
        raise ContractError(f"GitHub {identifier} uv version drift")
    commands = [str(step["run"]).strip() for step in steps if "run" in step]
    expected_command = f"make {contract['target']}"
    if commands != [expected_command]:
        raise ContractError(
            f"GitHub {identifier} target drift: expected {expected_command}, got {commands}"
        )
    upload_if = str(upload[0].get("if", "")).replace("${{", "").replace("}}", "").strip()
    if upload_if != "always()":
        raise ContractError(f"GitHub {identifier} artifact upload must use always()")
    upload_with = _mapping(upload[0].get("with"), f"GitHub {identifier} upload inputs")
    if upload_with.get("include-hidden-files") is not True:
        raise ContractError(f"GitHub {identifier} must upload hidden .artifacts evidence")
    actual_paths = _path_values(upload_with.get("path"))
    required_paths = {
        str(contract["artifact"]).rstrip("/"),
        *[
            item.rstrip("/")
            for item in _strings(contract.get("native_artifacts"), "native artifacts")
        ],
    }
    if not required_paths <= actual_paths:
        raise ContractError(f"GitHub {identifier} artifact paths are incomplete")


def _validate_gitlab_job(
    identifier: str, contract: Mapping[str, Any], job: Mapping[str, Any]
) -> None:
    expected_needs = set(_strings(contract.get("needs"), f"{identifier}.needs"))
    actual_needs = set(_strings(job.get("needs"), f"GitLab {identifier}.needs"))
    if actual_needs != expected_needs:
        raise ContractError(f"GitLab {identifier} needs drift: {sorted(actual_needs)}")
    scripts = _strings(job.get("script"), f"GitLab {identifier}.script")
    expected_command = f"make {contract['target']}"
    if scripts != [expected_command]:
        raise ContractError(
            f"GitLab {identifier} target drift: expected {expected_command}, got {scripts}"
        )
    artifacts = _mapping(job.get("artifacts"), f"GitLab {identifier}.artifacts")
    if artifacts.get("when") != "always":
        raise ContractError(f"GitLab {identifier} artifacts must use when: always")
    actual_paths = _path_values(artifacts.get("paths"))
    required_paths = {
        str(contract["artifact"]).rstrip("/"),
        *[
            item.rstrip("/")
            for item in _strings(contract.get("native_artifacts"), "native artifacts")
        ],
    }
    if not required_paths <= actual_paths:
        raise ContractError(f"GitLab {identifier} artifact paths are incomplete")
    serialized = repr(job).upper()
    if any(
        name in serialized
        for name in ("PRIVATE_REGISTRY_TOKEN", "UV_PUBLISH_TOKEN", "RELEASE_PROVIDER_TOKEN")
    ):
        raise ContractError(f"GitLab ordinary job {identifier} reads a release credential")


def _validate_gitlab_runtime(pipeline: Mapping[str, Any], platform: Mapping[str, Any]) -> None:
    """锁住 slim image 缺失的基础工具与真实 service smoke 的 CLI 前置条件。"""

    default = _mapping(pipeline.get("default"), "GitLab default")
    base_packages = _strings(platform.get("gitlab_base_packages"), "GitLab base packages")
    smoke_packages = _strings(platform.get("gitlab_smoke_packages"), "GitLab smoke packages")
    base_commands = _strings(default.get("before_script"), "GitLab default.before_script")
    expected_base_install = "apt-get install --yes --no-install-recommends " + " ".join(
        base_packages
    )
    if base_commands != [
        "apt-get update",
        expected_base_install,
        "rm -rf /var/lib/apt/lists/*",
        "uv --version",
    ]:
        raise ContractError("GitLab default runtime must install " + ", ".join(base_packages))

    smoke = _mapping(pipeline.get("smoke-service"), "GitLab smoke-service")
    smoke_commands = _strings(smoke.get("before_script"), "GitLab smoke-service.before_script")
    expected_smoke_install = "apt-get install --yes --no-install-recommends " + " ".join(
        [*base_packages, *smoke_packages]
    )
    if smoke_commands != [
        "apt-get update",
        expected_smoke_install,
        "rm -rf /var/lib/apt/lists/*",
        "uv --version",
        "docker --version",
        "docker compose version",
    ]:
        raise ContractError(
            "GitLab smoke-service runtime must install " + ", ".join(smoke_packages)
        )


def _validate_github_release(
    workflow: Mapping[str, Any], platform: Mapping[str, Any], release: Mapping[str, Any]
) -> None:
    if _workflow_triggers(workflow) != {release["github_trigger"]}:
        raise ContractError("GitHub release workflow must be workflow_dispatch only")
    if _mapping(workflow.get("permissions"), "release permissions") != {"contents": "read"}:
        raise ContractError("GitHub release workflow default permission is not read-only")
    jobs = _mapping(workflow.get("jobs"), "GitHub release jobs")
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
    dry_run = _mapping(jobs["dry-run"], "GitHub dry-run")
    if dry_run.get("uses") != "./.github/workflows/ci.yml":
        raise ContractError("GitHub release dry-run must reuse the complete CI workflow")
    promote_plan = _mapping(jobs["promote-plan"], "GitHub promote-plan")
    promote_no_release = _mapping(jobs["promote-no-release"], "GitHub promote-no-release")
    promote_execute = _mapping(jobs["promote-execute"], "GitHub promote-execute")
    publish_plan = _mapping(jobs["publish-plan"], "GitHub publish-plan")
    publish_execute = _mapping(jobs["publish-execute"], "GitHub publish-execute")
    if promote_execute.get("environment") != release["github_promote_environment"]:
        raise ContractError("GitHub promotion environment drift")
    if publish_execute.get("environment") != release["github_publish_environment"]:
        raise ContractError("GitHub publish environment drift")
    if any("environment" in job for job in (promote_plan, promote_no_release, publish_plan)):
        raise ContractError("GitHub plan jobs must not enter a protected credential environment")
    if _mapping(promote_plan.get("permissions"), "promote-plan permissions") != {
        "contents": "read"
    }:
        raise ContractError("GitHub promotion plan must remain read-only")
    if _mapping(promote_execute.get("permissions"), "promote-execute permissions") != {
        "contents": "write"
    }:
        raise ContractError("GitHub promotion must exclusively request contents: write")
    if _mapping(promote_no_release.get("permissions"), "promote-no-release permissions") != {
        "contents": "read"
    }:
        raise ContractError("GitHub no-release execution must remain read-only")
    if _mapping(publish_plan.get("permissions"), "publish-plan permissions") != {
        "contents": "read"
    } or _mapping(publish_execute.get("permissions"), "publish-execute permissions") != {
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
        job = _mapping(jobs[identifier], f"GitHub {identifier}")
        if _strings(job.get("needs"), f"GitHub {identifier}.needs") != expected:
            raise ContractError(f"GitHub {identifier} bypasses the release DAG")
    if any(
        "inputs.promote" not in str(_mapping(jobs[name], name).get("if"))
        for name in (
            "promote-plan",
            "promote-no-release",
            "promote-execute",
            "publish-plan",
            "publish-execute",
        )
    ) or any(
        "inputs.publish" not in str(_mapping(jobs[name], name).get("if"))
        for name in ("publish-plan", "publish-execute")
    ):
        raise ContractError("GitHub promotion/publish must require explicit manual inputs")
    plan_outputs = _mapping(promote_plan.get("outputs"), "GitHub promote-plan outputs")
    if "steps.promotion-plan.outputs.release_required" not in str(
        plan_outputs.get("release_required")
    ):
        raise ContractError("GitHub promotion plan must expose release_required")
    no_release_if = str(promote_no_release.get("if"))
    if "needs.promote-plan.outputs.release_required == 'false'" not in no_release_if:
        raise ContractError("GitHub no-release job is not selected by the reviewed plan")
    for identifier in ("promote-execute", "publish-plan", "publish-execute"):
        condition = str(_mapping(jobs[identifier], identifier).get("if"))
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
            _mapping(item, f"release {identifier}.steps")
            for item in _sequence(job.get("steps"), f"release {identifier}.steps")
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
        checkout_with = _mapping(checkout[0].get("with"), f"release {identifier} checkout")
        if str(checkout_with.get("fetch-depth")) != "0":
            raise ContractError(f"GitHub release {identifier} fetch-depth must be 0")
        if str(upload[0].get("if")) != "always()":
            raise ContractError(f"GitHub release {identifier} must retain failure artifacts")
        download_with = _mapping(download[0].get("with"), f"release {identifier} download")
        if download_with != {
            "name": expected_download_names[identifier],
            "path": ".artifacts",
        }:
            raise ContractError(
                f"GitHub release {identifier} download must restore .artifacts archive root"
            )
        upload_with = _mapping(upload[0].get("with"), f"release {identifier} upload")
        if upload_with.get("include-hidden-files") is not True:
            raise ContractError(
                f"GitHub release {identifier} must upload hidden .artifacts evidence"
            )
        if not required_artifacts[identifier] <= _path_values(upload_with.get("path")):
            raise ContractError(f"GitHub release {identifier} artifact handoff is incomplete")


def _validate_gitlab_release(
    pipeline: Mapping[str, Any],
    child: Mapping[str, Any],
    platform: Mapping[str, Any],
    release: Mapping[str, Any],
) -> None:
    """验证父 pipeline 动态分派与 child 中的最小凭据可见面。"""

    plan = _mapping(pipeline.get("promote-plan"), "GitLab promote-plan")
    dispatch = _mapping(pipeline.get("promote-dispatch"), "GitLab promote-dispatch")
    if _strings(plan.get("needs"), "GitLab promote-plan.needs") != ["p0-validate"]:
        raise ContractError("GitLab promote-plan must wait for p0-validate")
    if _strings(plan.get("script"), "GitLab promote-plan.script") != [
        f"make {release['promote_plan_target']}"
    ]:
        raise ContractError("GitLab promote-plan target drift")
    plan_rules = repr(_sequence(plan.get("rules"), "GitLab promote-plan.rules"))
    if "CI_DEFAULT_BRANCH" not in plan_rules or "manual" in plan_rules:
        raise ContractError("GitLab promote-plan must run automatically on the default branch")
    if "environment" in plan:
        raise ContractError("GitLab promote-plan must not enter a credential environment")
    credential_names = (
        "RELEASE_PUSH_TOKEN",
        "RELEASE_PROVIDER_TOKEN",
        "PRIVATE_REGISTRY_TOKEN",
        "UV_PUBLISH_TOKEN",
        "CI_JOB_TOKEN",
    )
    if any(name in repr(plan).upper() for name in credential_names):
        raise ContractError("GitLab promote-plan reads an execute credential")
    child_artifact = str(platform["gitlab_release_child_artifact"])
    plan_artifacts = _mapping(plan.get("artifacts"), "GitLab promote-plan.artifacts")
    if plan_artifacts.get("when") != "always" or not {
        ".artifacts/release-preview",
        ".artifacts/release-promotion/plan.json",
        child_artifact,
    } <= _path_values(plan_artifacts.get("paths")):
        raise ContractError("GitLab promote-plan artifact handoff is incomplete")

    if _strings(dispatch.get("needs"), "GitLab promote-dispatch.needs") != ["promote-plan"]:
        raise ContractError("GitLab promote-dispatch must consume promote-plan")
    trigger = _mapping(dispatch.get("trigger"), "GitLab promote-dispatch.trigger")
    includes = _sequence(trigger.get("include"), "GitLab promote-dispatch.trigger.include")
    if includes != [{"artifact": child_artifact, "job": "promote-plan"}]:
        raise ContractError("GitLab promote-dispatch must use the generated child artifact")
    if trigger.get("strategy") != "mirror":
        raise ContractError("GitLab promote-dispatch must mirror child pipeline status")
    dispatch_variables = _mapping(dispatch.get("variables"), "GitLab promote-dispatch.variables")
    if dispatch_variables.get("PARENT_PIPELINE_ID") != "$CI_PIPELINE_ID":
        raise ContractError("GitLab child pipeline lacks its parent artifact identity")
    dispatch_rules = repr(_sequence(dispatch.get("rules"), "GitLab promote-dispatch.rules"))
    if "CI_DEFAULT_BRANCH" not in dispatch_rules or "manual" in dispatch_rules:
        raise ContractError("GitLab promote-dispatch must run automatically on the default branch")

    expected_child_keys = {
        "stages",
        "default",
        "variables",
        ".release-promote-no-release",
        ".release-promote-execute",
        ".release-publish-plan",
        ".release-publish-execute",
    }
    if set(child) != expected_child_keys:
        raise ContractError("GitLab release child template surface drift")
    if _mapping(child.get("default"), "GitLab release child default") != _mapping(
        pipeline.get("default"), "GitLab default"
    ):
        raise ContractError("GitLab release child runtime drift")
    if (
        str(_mapping(child.get("variables"), "GitLab release child variables").get("GIT_DEPTH"))
        != "0"
    ):
        raise ContractError("GitLab release child must keep complete history")

    noop = _mapping(child.get(".release-promote-no-release"), "GitLab no-release template")
    promote = _mapping(child.get(".release-promote-execute"), "GitLab promote template")
    publish_plan = _mapping(child.get(".release-publish-plan"), "GitLab publish-plan template")
    publish = _mapping(child.get(".release-publish-execute"), "GitLab publish template")
    parent_need = [{"pipeline": "$PARENT_PIPELINE_ID", "job": "promote-plan"}]
    for label, job in (("no-release", noop), ("promote", promote)):
        if _sequence(job.get("needs"), f"GitLab {label}.needs") != parent_need:
            raise ContractError(f"GitLab {label} must consume the reviewed parent plan")
    if _strings(noop.get("script"), "GitLab no-release.script") != [
        f"make {release['promote_execute_target']}"
    ]:
        raise ContractError("GitLab no-release target drift")
    noop_serialized = repr(noop).upper()
    if "environment" in noop or any(name in noop_serialized for name in credential_names):
        raise ContractError("GitLab no-release template can see a credential boundary")

    expected_jobs = (
        ("promote", promote, release["gitlab_promote_environment"]),
        ("publish", publish, release["gitlab_publish_environment"]),
    )
    for label, job, environment in expected_jobs:
        env = _mapping(job.get("environment"), f"GitLab {label}.environment")
        if env.get("name") != environment:
            raise ContractError(f"GitLab {label} protected environment drift")
        if job.get("when") != "manual" or job.get("allow_failure") is not False:
            raise ContractError(f"GitLab {label} must be a blocking manual gate")
    if _strings(publish_plan.get("needs"), "GitLab publish-plan.needs") != ["promote-execute"]:
        raise ContractError("GitLab publish-plan bypasses promotion")
    if _strings(publish.get("needs"), "GitLab publish.needs") != ["publish-plan"]:
        raise ContractError("GitLab publish bypasses its reviewed plan")
    if "environment" in publish_plan:
        raise ContractError("GitLab publish-plan must not enter a credential environment")
    if _strings(publish_plan.get("script"), "GitLab publish-plan.script") != [
        f"make {release['publish_plan_target']}"
    ] or _strings(publish.get("script"), "GitLab publish.script") != [
        f"make {release['publish_execute_target']}"
    ]:
        raise ContractError("GitLab publish target drift")

    promote_serialized = repr(promote).upper()
    publish_serialized = repr(publish).upper()
    if not {"RELEASE_PUSH_TOKEN", "RELEASE_PROVIDER_TOKEN"} <= {
        name for name in credential_names if name in promote_serialized
    }:
        raise ContractError("GitLab promotion lacks isolated push/provider credentials")
    if "UV_PUBLISH_TOKEN" in promote_serialized or "CI_JOB_TOKEN" in promote_serialized:
        raise ContractError("GitLab promotion can see a registry credential")
    if "HTTPS://${CI_SERVER_HOST}/${CI_PROJECT_PATH}.GIT" not in promote_serialized:
        raise ContractError("GitLab promotion push endpoint is not credential-free HTTPS")
    if "PRIVATE_REGISTRY_TOKEN" not in publish_serialized:
        raise ContractError("GitLab publish lacks its environment-scoped registry credential")
    if "RELEASE_PUSH_TOKEN" in publish_serialized or "RELEASE_PROVIDER_TOKEN" in publish_serialized:
        raise ContractError("GitLab publish can see a promotion credential")
    if "CI_JOB_TOKEN" in repr(child).upper():
        raise ContractError("GitLab release must not use CI_JOB_TOKEN as a registry credential")

    required_artifacts = {
        "no-release": {".artifacts/release-preview", ".artifacts/release-promotion"},
        "promote": {
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
        "publish": {
            ".artifacts/release-preview",
            ".artifacts/release-promotion",
            ".artifacts/release-build",
            ".artifacts/registry-publish",
        },
    }
    for label, job in (
        ("no-release", noop),
        ("promote", promote),
        ("publish-plan", publish_plan),
        ("publish", publish),
    ):
        artifacts = _mapping(job.get("artifacts"), f"GitLab {label}.artifacts")
        if artifacts.get("when") != "always" or not required_artifacts[label] <= _path_values(
            artifacts.get("paths")
        ):
            raise ContractError(f"GitLab {label} artifact handoff is incomplete")


def validate(root: Path) -> tuple[int, int]:
    root = root.resolve()
    contract = _toml(root / "compliance/ci-jobs.toml")
    if contract.get("schema_version") != "ci-job-contract/v1":
        raise ContractError("unsupported job contract schema_version")
    platform = _mapping(contract.get("platform"), "platform")
    triggers = _mapping(contract.get("triggers"), "triggers")
    release = _mapping(contract.get("release"), "release")
    github = _yaml(root / str(platform["github_workflow"]))
    github_release = _yaml(root / str(platform["github_release_workflow"]))
    gitlab = _yaml(root / str(platform["gitlab_pipeline"]))
    gitlab_release_child = _yaml(root / str(platform["gitlab_release_child_template"]))
    expected_github = set(_strings(triggers.get("github"), "GitHub triggers"))
    actual_github = _workflow_triggers(github)
    if not expected_github <= actual_github:
        raise ContractError(f"GitHub triggers drift: {sorted(actual_github)}")
    workflow_rules = repr(_mapping(gitlab.get("workflow"), "GitLab workflow").get("rules"))
    for source in _strings(triggers.get("gitlab"), "GitLab triggers"):
        if source not in workflow_rules:
            raise ContractError(f"GitLab trigger is missing: {source}")
    if _mapping(github.get("permissions"), "GitHub permissions") != {"contents": "read"}:
        raise ContractError("GitHub ordinary CI is not read-only")
    default = _mapping(gitlab.get("default"), "GitLab default")
    if default.get("image") != platform["gitlab_image"]:
        raise ContractError("GitLab image digest drift")
    _validate_gitlab_runtime(gitlab, platform)
    variables = _mapping(gitlab.get("variables"), "GitLab variables")
    if str(variables.get("GIT_DEPTH")) != "0":
        raise ContractError("GitLab GIT_DEPTH must be 0")
    jobs_raw = _sequence(contract.get("jobs"), "jobs")
    jobs = [_mapping(item, "job") for item in jobs_raw]
    identifiers = [str(item["id"]) for item in jobs]
    if len(identifiers) != len(set(identifiers)):
        raise ContractError("job contract contains duplicate ids")
    github_jobs = _mapping(github.get("jobs"), "GitHub jobs")
    gitlab_reserved = {
        "workflow",
        "default",
        "variables",
        "stages",
        "promote-plan",
        "promote-dispatch",
    }
    gitlab_job_names = {
        key for key in gitlab if key not in gitlab_reserved and not key.startswith(".")
    }
    if set(github_jobs) != set(identifiers) or gitlab_job_names != set(identifiers):
        raise ContractError("GitHub/GitLab required job set drift")
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    for item in jobs:
        identifier = str(item["id"])
        target = str(item["target"])
        _target_block(makefile, target)
        _validate_github_job(
            identifier=identifier,
            contract=item,
            job=_mapping(github_jobs[identifier], f"GitHub {identifier}"),
            workflow=github,
            platform=platform,
        )
        _validate_gitlab_job(identifier, item, _mapping(gitlab[identifier], f"GitLab {identifier}"))
    if "$(MAKE) quality" not in _target_block(makefile, "quality-aggregate"):
        raise ContractError("quality-aggregate must execute make quality")
    if "$(MAKE) test" not in _target_block(makefile, "test-aggregate"):
        raise ContractError("test-aggregate must execute make test")
    if re.search(r"(?m)^ci-release-dry-run\s*:\s*ci-history\s*$", makefile) is None:
        raise ContractError("release dry-run lacks the history guard")
    if "scripts/release_gitlab_pipeline.py" not in _target_block(makefile, "release-promote-plan"):
        raise ContractError("promotion plan does not generate the GitLab child pipeline")
    _validate_github_release(github_release, platform, release)
    _validate_gitlab_release(gitlab, gitlab_release_child, platform, release)
    return len(jobs), len(jobs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        github_jobs, gitlab_jobs = validate(args.root)
    except (ContractError, OSError, tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
        print(f"ci-contract: {exc}", file=sys.stderr)
        return 2
    print(f"ci-contract: ok github={github_jobs} gitlab={gitlab_jobs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
