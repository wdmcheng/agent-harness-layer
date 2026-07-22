"""校验 GitLab 父子发布 pipeline 的凭据隔离和 artifact 交接。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ci_contract_support import ContractError, mapping, path_values, sequence, strings


def validate_gitlab_release(
    pipeline: Mapping[str, Any],
    child: Mapping[str, Any],
    platform: Mapping[str, Any],
    release: Mapping[str, Any],
) -> None:
    """验证父 pipeline 动态分派与 child 中的最小凭据可见面。"""

    plan = mapping(pipeline.get("promote-plan"), "GitLab promote-plan")
    dispatch = mapping(pipeline.get("promote-dispatch"), "GitLab promote-dispatch")
    if strings(plan.get("needs"), "GitLab promote-plan.needs") != ["acceptance-validate"]:
        raise ContractError("GitLab promote-plan must wait for acceptance-validate")
    if strings(plan.get("script"), "GitLab promote-plan.script") != [
        f"make {release['promote_plan_target']}"
    ]:
        raise ContractError("GitLab promote-plan target drift")
    plan_rules = repr(sequence(plan.get("rules"), "GitLab promote-plan.rules"))
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
    plan_artifacts = mapping(plan.get("artifacts"), "GitLab promote-plan.artifacts")
    if plan_artifacts.get("when") != "always" or not {
        ".artifacts/release-preview",
        ".artifacts/release-promotion/plan.json",
        child_artifact,
    } <= path_values(plan_artifacts.get("paths")):
        raise ContractError("GitLab promote-plan artifact handoff is incomplete")

    if strings(dispatch.get("needs"), "GitLab promote-dispatch.needs") != ["promote-plan"]:
        raise ContractError("GitLab promote-dispatch must consume promote-plan")
    trigger = mapping(dispatch.get("trigger"), "GitLab promote-dispatch.trigger")
    includes = sequence(trigger.get("include"), "GitLab promote-dispatch.trigger.include")
    if includes != [{"artifact": child_artifact, "job": "promote-plan"}]:
        raise ContractError("GitLab promote-dispatch must use the generated child artifact")
    if trigger.get("strategy") != "mirror":
        raise ContractError("GitLab promote-dispatch must mirror child pipeline status")
    dispatch_variables = mapping(dispatch.get("variables"), "GitLab promote-dispatch.variables")
    if dispatch_variables.get("PARENT_PIPELINE_ID") != "$CI_PIPELINE_ID":
        raise ContractError("GitLab child pipeline lacks its parent artifact identity")
    dispatch_rules = repr(sequence(dispatch.get("rules"), "GitLab promote-dispatch.rules"))
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
    if mapping(child.get("default"), "GitLab release child default") != mapping(
        pipeline.get("default"), "GitLab default"
    ):
        raise ContractError("GitLab release child runtime drift")
    if (
        str(mapping(child.get("variables"), "GitLab release child variables").get("GIT_DEPTH"))
        != "0"
    ):
        raise ContractError("GitLab release child must keep complete history")

    noop = mapping(child.get(".release-promote-no-release"), "GitLab no-release template")
    promote = mapping(child.get(".release-promote-execute"), "GitLab promote template")
    publish_plan = mapping(child.get(".release-publish-plan"), "GitLab publish-plan template")
    publish = mapping(child.get(".release-publish-execute"), "GitLab publish template")
    parent_need = [{"pipeline": "$PARENT_PIPELINE_ID", "job": "promote-plan"}]
    for label, job in (("no-release", noop), ("promote", promote)):
        if sequence(job.get("needs"), f"GitLab {label}.needs") != parent_need:
            raise ContractError(f"GitLab {label} must consume the reviewed parent plan")
    if strings(noop.get("script"), "GitLab no-release.script") != [
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
        env = mapping(job.get("environment"), f"GitLab {label}.environment")
        if env.get("name") != environment:
            raise ContractError(f"GitLab {label} protected environment drift")
        if job.get("when") != "manual" or job.get("allow_failure") is not False:
            raise ContractError(f"GitLab {label} must be a blocking manual gate")
    if strings(publish_plan.get("needs"), "GitLab publish-plan.needs") != ["promote-execute"]:
        raise ContractError("GitLab publish-plan bypasses promotion")
    if strings(publish.get("needs"), "GitLab publish.needs") != ["publish-plan"]:
        raise ContractError("GitLab publish bypasses its reviewed plan")
    if "environment" in publish_plan:
        raise ContractError("GitLab publish-plan must not enter a credential environment")
    if strings(publish_plan.get("script"), "GitLab publish-plan.script") != [
        f"make {release['publish_plan_target']}"
    ] or strings(publish.get("script"), "GitLab publish.script") != [
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
        artifacts = mapping(job.get("artifacts"), f"GitLab {label}.artifacts")
        if artifacts.get("when") != "always" or not required_artifacts[label] <= path_values(
            artifacts.get("paths")
        ):
            raise ContractError(f"GitLab {label} artifact handoff is incomplete")
