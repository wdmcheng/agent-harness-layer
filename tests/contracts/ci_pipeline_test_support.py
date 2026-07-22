"""CI pipeline 合同测试共享的路径、突变复制与计划夹具。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts" / "ci_contract.py"
HISTORY = ROOT / "scripts" / "ci_history.py"
GITLAB_RELEASE_GENERATOR = ROOT / "scripts" / "release_gitlab_pipeline.py"


def promotion_plan(status: str) -> dict[str, object]:
    """构造 consumer 合同的完整计划，突变用例只移除一个受审字段。"""

    source = {
        "commit_sha": "a" * 40,
        "dirty_diff_sha256": "b" * 64,
        "base_tag": "agent-harness-v0.1.0",
    }
    plan: dict[str, object] = {
        "schema_version": "release-promotion-plan/v1",
        "status": status,
        "preview_manifest_sha256": "c" * 64,
        "source": source,
        "version": "0.2.0" if status == "planned" else "0.1.0",
        "artifacts": [],
        "tag": "agent-harness-v0.2.0" if status == "planned" else None,
    }
    if status == "planned":
        artifacts = [
            {
                "path": ".artifacts/release-preview/test/dist/agent_harness-0.2.0.whl",
                "kind": "wheel",
                "sha256": "d" * 64,
                "size": 1,
            },
            {
                "path": ".artifacts/release-preview/test/dist/agent_harness-0.2.0.tar.gz",
                "kind": "sdist",
                "sha256": "e" * 64,
                "size": 1,
            },
        ]
        plan["artifacts"] = artifacts
        approval: dict[str, object] = {
            "schema_version": "release-approval/v1",
            "operation": "promotion",
            "preview_manifest_sha256": plan["preview_manifest_sha256"],
            "source": source,
            "tag": plan["tag"],
            "version": plan["version"],
            "origin_endpoint_sha256": "f" * 64,
            "provider_endpoint_sha256": "0" * 64,
            "protected_default_branch": "main",
            "release_notes_sha256": "1" * 64,
            "artifacts": artifacts,
        }
        plan["approval"] = approval
        canonical = (
            json.dumps(
                approval,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        plan["approval_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    return plan


def run_validator(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(root)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def copy_contract_surface(destination: Path) -> None:
    """只复制 validator 的公开输入，突变测试不触碰共享工作树。"""

    for relative in (
        "Makefile",
        "compliance/ci-jobs.toml",
        ".github/workflows/ci.yml",
        ".github/workflows/release.yml",
        ".gitlab-ci.yml",
        ".gitlab/release-child.yml",
    ):
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)


__all__ = [
    "GITLAB_RELEASE_GENERATOR",
    "HISTORY",
    "ROOT",
    "copy_contract_surface",
    "git",
    "promotion_plan",
    "run_validator",
]
