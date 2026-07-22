"""Release pipeline 的 DAG、凭据隔离与 GitLab child 合同。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from tests.contracts.ci_pipeline_test_support import (
    GITLAB_RELEASE_GENERATOR,
    ROOT,
    promotion_plan,
)


def test_release_workflows_use_plan_execute_dag_and_scoped_credentials() -> None:
    """发布 pipeline 必须隔离 plan/no-release 与受保护 execute 的凭据边界。"""

    github = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    gitlab = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    gitlab_child = yaml.safe_load((ROOT / ".gitlab/release-child.yml").read_text(encoding="utf-8"))
    expected = {
        "promote-plan",
        "promote-no-release",
        "promote-execute",
        "publish-plan",
        "publish-execute",
    }
    assert expected <= set(github["jobs"])
    assert {"promote-plan", "promote-dispatch"} <= set(gitlab)
    assert github["jobs"]["promote-plan"]["permissions"] == {"contents": "read"}
    assert github["jobs"]["promote-no-release"]["permissions"] == {"contents": "read"}
    assert github["jobs"]["promote-execute"]["permissions"] == {"contents": "write"}
    assert github["jobs"]["publish-plan"]["permissions"] == {"contents": "read"}
    assert github["jobs"]["publish-execute"]["permissions"] == {"contents": "read"}
    assert github["jobs"]["promote-execute"]["needs"] == ["promote-plan"]
    assert github["jobs"]["publish-plan"]["needs"] == ["promote-plan", "promote-execute"]
    assert github["jobs"]["publish-execute"]["needs"] == ["promote-plan", "publish-plan"]
    assert "PRIVATE_REGISTRY_TOKEN" not in repr(github["jobs"]["promote-execute"])
    assert "RELEASE_PUSH_TOKEN" in repr(github["jobs"]["promote-execute"])
    assert "PRIVATE_REGISTRY_TOKEN" in repr(github["jobs"]["publish-execute"])
    assert "CI_JOB_TOKEN" not in repr(gitlab_child[".release-publish-execute"])
    assert "RELEASE_PUSH_TOKEN" in repr(gitlab_child[".release-promote-execute"])


def test_no_release_ci_path_never_enters_credential_jobs() -> None:
    """no-release 必须由无 environment、无 secret 的路径终止，且不得进入 registry DAG。"""

    github = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    gitlab = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    github_jobs = github["jobs"]

    assert github_jobs["promote-plan"]["outputs"]["release_required"]
    noop = github_jobs["promote-no-release"]
    assert "environment" not in noop
    assert "SECRET" not in repr(noop).upper()
    assert "TOKEN" not in repr(noop).upper()
    assert "needs.promote-plan.outputs.release_required == 'false'" in noop["if"]
    for job_name in ("promote-execute", "publish-plan", "publish-execute"):
        assert (
            "needs.promote-plan.outputs.release_required == 'true'" in github_jobs[job_name]["if"]
        )

    assert "promote-execute" not in gitlab
    assert "publish-plan" not in gitlab
    assert "publish-execute" not in gitlab
    child_path = ".artifacts/release-promotion/gitlab-child.yml"
    assert child_path in gitlab["promote-plan"]["artifacts"]["paths"]
    trigger = gitlab["promote-dispatch"]["trigger"]
    assert trigger["strategy"] == "mirror"
    assert trigger["include"] == [{"artifact": child_path, "job": "promote-plan"}]
    assert gitlab["promote-dispatch"]["variables"]["PARENT_PIPELINE_ID"] == "$CI_PIPELINE_ID"


def test_gitlab_no_release_child_contains_only_noncredential_execution(tmp_path: Path) -> None:
    """动态 child config 的 no-release 形态不得包含任何可实例化的发布 job。"""

    plan = tmp_path / "plan.json"
    output = tmp_path / "child.yml"
    plan.write_text(json.dumps(promotion_plan("no-release")), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(GITLAB_RELEASE_GENERATOR),
            "--plan",
            str(plan),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    child = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert set(child) == {"include", "promote-no-release"}
    assert child["promote-no-release"] == {"extends": ".release-promote-no-release"}
    assert "TOKEN" not in output.read_text(encoding="utf-8").upper()
    assert "environment" not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("status", "missing_field"),
    [
        ("planned", "preview_manifest_sha256"),
        ("planned", "protected_default_branch"),
        ("no-release", "source"),
    ],
)
def test_gitlab_child_rejects_incomplete_promotion_plan_before_job_instantiation(
    tmp_path: Path,
    status: str,
    missing_field: str,
) -> None:
    """不完整计划不得决定 child DAG，尤其不能实例化携带凭据的执行节点。"""

    payload = promotion_plan(status)
    if missing_field == "protected_default_branch":
        approval = payload["approval"]
        assert isinstance(approval, dict)
        del approval[missing_field]
        canonical = (
            json.dumps(
                approval,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        payload["approval_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    else:
        del payload[missing_field]
    plan = tmp_path / f"{status}-{missing_field}.json"
    output = tmp_path / f"{status}-{missing_field}.yml"
    plan.write_text(json.dumps(payload), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(GITLAB_RELEASE_GENERATOR),
            "--plan",
            str(plan),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert not output.exists()
