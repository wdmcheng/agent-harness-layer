"""GitHub/GitLab release job 之间的 preview 与 promotion artifact 接力合同。"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _makefile() -> str:
    return (ROOT / "Makefile").read_text(encoding="utf-8")


def test_dry_run_writes_the_manifest_at_the_ci_consumption_path() -> None:
    """dry-run 的输出目录必须和 promotion/publish 默认输入保持一致。"""

    makefile = _makefile()
    assert "RELEASE_PREVIEW_DIR ?= $(CURDIR)/.artifacts/release-preview/phase15-current" in makefile
    assert (
        "release-dry-run:\n\t$(UV) run --group release python "
        'scripts/release_dry_run.py --output-dir "$(RELEASE_PREVIEW_DIR)"' in makefile
    )
    assert "RELEASE_MANIFEST ?= $(RELEASE_PREVIEW_DIR)/manifest.json" in makefile


def test_github_downloads_release_archives_into_dot_artifacts() -> None:
    """多路径 archive 去掉共同根后，consumer 必须解包回 `.artifacts`。"""

    ci_workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    workflow_text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    assert "name: ci-release-dry-run-${{ github.run_id }}" in ci_workflow
    assert 'name: "ci-release-dry-run-${{ github.run_id }}"' in workflow_text
    expected_downloads = {
        "promote-plan": "ci-release-dry-run-${{ github.run_id }}",
        "promote-no-release": "release-promotion-plan-${{ github.run_id }}",
        "promote-execute": "release-promotion-plan-${{ github.run_id }}",
        "publish-plan": "release-promotion-execute-${{ github.run_id }}",
        "publish-execute": "registry-publish-plan-${{ github.run_id }}",
    }
    for job_name, artifact_name in expected_downloads.items():
        downloads = [
            step
            for step in workflow["jobs"][job_name]["steps"]
            if str(step.get("uses", "")).startswith("actions/download-artifact@")
        ]
        assert len(downloads) == 1
        assert downloads[0]["with"] == {"name": artifact_name, "path": ".artifacts"}


def test_gitlab_dynamic_release_artifacts_match_make_defaults() -> None:
    """GitLab 父 plan 与动态 child 必须归档 Make 默认输入。"""

    makefile = _makefile()
    parent = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    child = (ROOT / ".gitlab/release-child.yml").read_text(encoding="utf-8")
    manifest = re.search(r"RELEASE_MANIFEST \?= (?P<path>[^\n]+)", makefile)
    promotion_plan = re.search(r"RELEASE_PROMOTION_PLAN \?= (?P<path>[^\n]+)", makefile)
    receipt = re.search(r"RELEASE_PROMOTION_RECEIPT \?= (?P<path>[^\n]+)", makefile)
    build_manifest = re.search(r"RELEASE_BUILD_MANIFEST \?= (?P<path>[^\n]+)", makefile)
    registry_plan = re.search(r"REGISTRY_PLAN \?= (?P<path>[^\n]+)", makefile)
    assert all(
        value is not None
        for value in (manifest, promotion_plan, receipt, build_manifest, registry_plan)
    )
    assert "RELEASE_MANIFEST: .artifacts/release-preview/phase15-current/manifest.json" in parent
    assert ".artifacts/release-promotion/plan.json" in parent
    assert ".artifacts/release-promotion/gitlab-child.yml" in parent
    assert "RELEASE_MANIFEST: .artifacts/release-preview/phase15-current/manifest.json" in child
    assert ".artifacts/release-promotion" in child
    assert ".artifacts/release-build" in child
    assert ".artifacts/registry-publish/plan.json" in child
    assert manifest is not None
    assert promotion_plan is not None
    assert receipt is not None
    assert build_manifest is not None
    assert registry_plan is not None
    assert manifest.group("path") == "$(RELEASE_PREVIEW_DIR)/manifest.json"
    assert promotion_plan.group("path").endswith(".artifacts/release-promotion/plan.json")
    assert receipt.group("path") == "$(RELEASE_OUTPUT_DIR)/receipt.json"
    assert build_manifest.group("path").endswith(".artifacts/release-build/execute/manifest.json")
    assert registry_plan.group("path").endswith(".artifacts/registry-publish/plan.json")
