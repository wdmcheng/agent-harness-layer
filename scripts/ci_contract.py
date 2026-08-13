"""比较 GitHub/GitLab job、DAG、artifact、history 与权限语义。"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from ci_contract_support import (
    ContractError,
    mapping,
    path_values,
    permission,
    sequence,
    strings,
    target_block,
    toml_document,
    workflow_triggers,
    yaml_document,
)

ACCEPTANCE_GITHUB_DOWNLOADS = {
    "ci-lock-${{ github.run_id }}": ".artifacts/ci/lock",
    "ci-install-${{ github.run_id }}": ".artifacts/ci/install",
    "ci-ruff-format-${{ github.run_id }}": ".artifacts/ci/ruff-format",
    "ci-ruff-lint-${{ github.run_id }}": ".artifacts/ci/ruff-lint",
    "ci-pyright-${{ github.run_id }}": ".artifacts/ci/pyright",
    "ci-import-boundary-${{ github.run_id }}": ".artifacts/ci/import-boundary",
    "ci-quality-aggregate-${{ github.run_id }}": ".artifacts/ci/quality-aggregate",
    "ci-unit-contract-${{ github.run_id }}": ".artifacts",
    "ci-test-aggregate-${{ github.run_id }}": ".artifacts/ci/test-aggregate",
    "ci-integration-${{ github.run_id }}": ".artifacts",
    "ci-eval-${{ github.run_id }}": ".artifacts",
    "ci-smoke-local-${{ github.run_id }}": ".artifacts",
    "ci-smoke-service-${{ github.run_id }}": ".artifacts",
    "ci-smoke-live-model-${{ github.run_id }}": ".artifacts",
    "ci-smoke-live-model-stream-${{ github.run_id }}": ".artifacts",
    "ci-smoke-live-model-failover-${{ github.run_id }}": ".artifacts",
    "ci-license-${{ github.run_id }}": ".artifacts",
    "ci-build-${{ github.run_id }}": ".",
    "ci-release-dry-run-${{ github.run_id }}": ".artifacts",
    "ci-contract-${{ github.run_id }}": ".artifacts/ci/ci-contract",
}


def _validate_github_acceptance_downloads(
    steps: Sequence[Mapping[str, Any]], platform: Mapping[str, Any]
) -> None:
    """锁住 acceptance 所消费的完整 artifact 集合及其证据还原目录。"""

    actual: dict[str, str] = {}
    for step in steps:
        if step.get("uses") != platform["github_download_artifact"]:
            continue
        inputs = mapping(step.get("with"), "GitHub acceptance-validate download inputs")
        name = str(inputs.get("name", ""))
        path = str(inputs.get("path", ""))
        if not name or not path or name in actual:
            raise ContractError("GitHub acceptance-validate download set drift")
        actual[name] = path
    if actual != ACCEPTANCE_GITHUB_DOWNLOADS:
        raise ContractError("GitHub acceptance-validate download set drift")


def _validate_github_job(
    *,
    identifier: str,
    contract: Mapping[str, Any],
    job: Mapping[str, Any],
    workflow: Mapping[str, Any],
    platform: Mapping[str, Any],
) -> None:
    """核对单个 GitHub job 的依赖、只读权限、工具版本、命令与证据归档。"""

    expected_needs = set(strings(contract.get("needs"), f"{identifier}.needs"))
    actual_needs = set(strings(job.get("needs"), f"GitHub {identifier}.needs"))
    if actual_needs != expected_needs:
        raise ContractError(f"GitHub {identifier} needs drift: {sorted(actual_needs)}")
    if permission(job, workflow) != {"contents": "read"}:
        raise ContractError(f"GitHub ordinary job {identifier} is not read-only")
    steps = [
        mapping(item, f"GitHub {identifier}.steps")
        for item in sequence(job.get("steps"), f"GitHub {identifier}.steps")
    ]
    checkout = [step for step in steps if step.get("uses") == platform["github_checkout"]]
    setup = [step for step in steps if step.get("uses") == platform["github_setup_uv"]]
    upload = [step for step in steps if step.get("uses") == platform["github_upload_artifact"]]
    if len(checkout) != 1 or len(setup) != 1 or len(upload) != 1:
        raise ContractError(f"GitHub {identifier} must pin one checkout/setup-uv/upload action")
    checkout_with = mapping(checkout[0].get("with"), f"GitHub {identifier} checkout inputs")
    if str(checkout_with.get("fetch-depth")) != "0":
        raise ContractError(f"GitHub {identifier} fetch-depth must be 0")
    if str(checkout_with.get("persist-credentials")).lower() != "false":
        raise ContractError(f"GitHub {identifier} checkout must not persist credentials")
    setup_with = mapping(setup[0].get("with"), f"GitHub {identifier} setup-uv inputs")
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
    upload_with = mapping(upload[0].get("with"), f"GitHub {identifier} upload inputs")
    if upload_with.get("include-hidden-files") is not True:
        raise ContractError(f"GitHub {identifier} must upload hidden .artifacts evidence")
    actual_paths = path_values(upload_with.get("path"))
    required_paths = {
        str(contract["artifact"]).rstrip("/"),
        *[
            item.rstrip("/")
            for item in strings(contract.get("native_artifacts"), "native artifacts")
        ],
    }
    if not required_paths <= actual_paths:
        raise ContractError(f"GitHub {identifier} artifact paths are incomplete")
    if identifier == "license":
        downloads = [
            step for step in steps if step.get("uses") == platform["github_download_artifact"]
        ]
        if len(downloads) != 1:
            raise ContractError("GitHub license must download one smoke-service artifact")
        download_with = mapping(downloads[0].get("with"), "GitHub license download inputs")
        if download_with != {
            "name": "ci-smoke-service-${{ github.run_id }}",
            "path": ".artifacts",
        }:
            raise ContractError("GitHub license download must restore .artifacts")
    if identifier == "acceptance-validate":
        _validate_github_acceptance_downloads(steps, platform)


def _validate_gitlab_job(
    identifier: str, contract: Mapping[str, Any], job: Mapping[str, Any]
) -> None:
    """核对单个 GitLab job 的依赖、命令、证据归档与凭据隔离。"""

    expected_needs = set(strings(contract.get("needs"), f"{identifier}.needs"))
    actual_needs = set(strings(job.get("needs"), f"GitLab {identifier}.needs"))
    if actual_needs != expected_needs:
        raise ContractError(f"GitLab {identifier} needs drift: {sorted(actual_needs)}")
    scripts = strings(job.get("script"), f"GitLab {identifier}.script")
    expected_command = f"make {contract['target']}"
    if scripts != [expected_command]:
        raise ContractError(
            f"GitLab {identifier} target drift: expected {expected_command}, got {scripts}"
        )
    artifacts = mapping(job.get("artifacts"), f"GitLab {identifier}.artifacts")
    if artifacts.get("when") != "always":
        raise ContractError(f"GitLab {identifier} artifacts must use when: always")
    actual_paths = path_values(artifacts.get("paths"))
    required_paths = {
        str(contract["artifact"]).rstrip("/"),
        *[
            item.rstrip("/")
            for item in strings(contract.get("native_artifacts"), "native artifacts")
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

    default = mapping(pipeline.get("default"), "GitLab default")
    base_packages = strings(platform.get("gitlab_base_packages"), "GitLab base packages")
    smoke_packages = strings(platform.get("gitlab_smoke_packages"), "GitLab smoke packages")
    base_commands = strings(default.get("before_script"), "GitLab default.before_script")
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

    smoke = mapping(pipeline.get("smoke-service"), "GitLab smoke-service")
    smoke_commands = strings(smoke.get("before_script"), "GitLab smoke-service.before_script")
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


def validate(root: Path) -> tuple[int, int]:
    """对照版本化 job 合同校验两个 CI 平台及发布交接。"""

    # 发布验证器在入口执行时再加载：它们复用本模块的严格解析 helper，
    # 同时避免 CLI 薄入口重新导出平台实现细节。
    from ci_github_release_contract import validate_github_release
    from ci_gitlab_release_contract import validate_gitlab_release

    root = root.resolve()
    contract = toml_document(root / "compliance/ci-jobs.toml")
    if contract.get("schema_version") != "ci-job-contract/v1":
        raise ContractError("unsupported job contract schema_version")
    platform = mapping(contract.get("platform"), "platform")
    triggers = mapping(contract.get("triggers"), "triggers")
    release = mapping(contract.get("release"), "release")
    github = yaml_document(root / str(platform["github_workflow"]))
    github_release = yaml_document(root / str(platform["github_release_workflow"]))
    gitlab = yaml_document(root / str(platform["gitlab_pipeline"]))
    gitlab_release_child = yaml_document(root / str(platform["gitlab_release_child_template"]))
    expected_github = set(strings(triggers.get("github"), "GitHub triggers"))
    actual_github = workflow_triggers(github)
    if not expected_github <= actual_github:
        raise ContractError(f"GitHub triggers drift: {sorted(actual_github)}")
    workflow_rules = repr(mapping(gitlab.get("workflow"), "GitLab workflow").get("rules"))
    for source in strings(triggers.get("gitlab"), "GitLab triggers"):
        if source not in workflow_rules:
            raise ContractError(f"GitLab trigger is missing: {source}")
    if mapping(github.get("permissions"), "GitHub permissions") != {"contents": "read"}:
        raise ContractError("GitHub ordinary CI is not read-only")
    default = mapping(gitlab.get("default"), "GitLab default")
    if default.get("image") != platform["gitlab_image"]:
        raise ContractError("GitLab image digest drift")
    _validate_gitlab_runtime(gitlab, platform)
    variables = mapping(gitlab.get("variables"), "GitLab variables")
    if str(variables.get("GIT_DEPTH")) != "0":
        raise ContractError("GitLab GIT_DEPTH must be 0")
    jobs_raw = sequence(contract.get("jobs"), "jobs")
    jobs = [mapping(item, "job") for item in jobs_raw]
    identifiers = [str(item["id"]) for item in jobs]
    if len(identifiers) != len(set(identifiers)):
        raise ContractError("job contract contains duplicate ids")
    github_jobs = mapping(github.get("jobs"), "GitHub jobs")
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
        target_block(makefile, target)
        _validate_github_job(
            identifier=identifier,
            contract=item,
            job=mapping(github_jobs[identifier], f"GitHub {identifier}"),
            workflow=github,
            platform=platform,
        )
        _validate_gitlab_job(identifier, item, mapping(gitlab[identifier], f"GitLab {identifier}"))
    if "$(MAKE) quality" not in target_block(makefile, "quality-aggregate"):
        raise ContractError("quality-aggregate must execute make quality")
    if "$(MAKE) test" not in target_block(makefile, "test-aggregate"):
        raise ContractError("test-aggregate must execute make test")
    if re.search(r"(?m)^ci-release-dry-run\s*:\s*ci-history\s*$", makefile) is None:
        raise ContractError("release dry-run lacks the history guard")
    if "scripts/release_gitlab_pipeline.py" not in target_block(makefile, "release-promote-plan"):
        raise ContractError("promotion plan does not generate the GitLab child pipeline")
    validate_github_release(github_release, platform, release)
    validate_gitlab_release(gitlab, gitlab_release_child, platform, release)
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
