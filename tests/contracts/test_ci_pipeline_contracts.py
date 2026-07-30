"""GitHub/GitLab pipeline 的公开 YAML 与本地 history guard 合同。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml
from tests.contracts.ci_pipeline_test_support import (
    ROOT,
    copy_contract_surface,
    run_validator,
)


def test_repository_pipeline_contract_is_equivalent_and_fail_closed() -> None:
    """基准合同同时证明 job/DAG/target/artifact/权限与完整 history 语义。"""

    completed = run_validator(ROOT)

    assert completed.returncode == 0, completed.stderr
    assert "ci-contract: ok" in completed.stdout


def test_acceptance_validator_is_a_required_job_in_both_ci_release_dags() -> None:
    """需求验收矩阵必须在 clean hosted run 独立执行，不能依赖聚合任务内部自检。"""

    contract = tomllib.loads((ROOT / "compliance/ci-jobs.toml").read_text(encoding="utf-8"))
    github = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    gitlab = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    jobs = {job["id"]: job for job in contract["jobs"]}
    required_producers = {
        "install",
        "quality-aggregate",
        "test-aggregate",
        "integration",
        "eval",
        "smoke-local",
        "smoke-service",
        "smoke-live-model",
        "smoke-live-model-stream",
        "license",
        "build",
        "release-dry-run",
        "ci-contract",
    }

    assert jobs["acceptance-validate"]["target"] == "ci-acceptance-validate"
    assert set(jobs["acceptance-validate"]["needs"]) == required_producers
    assert set(github["jobs"]["acceptance-validate"]["needs"]) == required_producers
    assert set(gitlab["acceptance-validate"]["needs"]) == required_producers
    downloads = {
        step.get("with", {}).get("name"): step.get("with", {}).get("path")
        for step in github["jobs"]["acceptance-validate"]["steps"]
        if step.get("uses") == contract["platform"]["github_download_artifact"]
    }
    expected_downloads = {
        "ci-install-${{ github.run_id }}": ".artifacts/ci/install",
        "ci-quality-aggregate-${{ github.run_id }}": ".artifacts/ci/quality-aggregate",
        "ci-test-aggregate-${{ github.run_id }}": ".artifacts/ci/test-aggregate",
        "ci-integration-${{ github.run_id }}": ".artifacts",
        "ci-eval-${{ github.run_id }}": ".artifacts",
        "ci-smoke-local-${{ github.run_id }}": ".artifacts",
        "ci-smoke-service-${{ github.run_id }}": ".artifacts",
        "ci-smoke-live-model-${{ github.run_id }}": ".artifacts",
        "ci-smoke-live-model-stream-${{ github.run_id }}": ".artifacts",
        "ci-license-${{ github.run_id }}": ".artifacts",
        "ci-build-${{ github.run_id }}": ".",
        "ci-release-dry-run-${{ github.run_id }}": ".artifacts",
        "ci-contract-${{ github.run_id }}": ".artifacts/ci/ci-contract",
    }
    assert downloads == expected_downloads
    # 需求验收依赖 release dry-run 的真实 evidence，所以它是终端门禁；GitLab
    # promotion 必须显式等待该门禁，不能构造反向依赖造成 DAG 环。
    assert gitlab["promote-plan"]["needs"] == ["acceptance-validate"]


def test_stream_live_smoke_has_independent_ci_producer_and_artifact() -> None:
    """Make、manifest 与双 CI 必须使用独立 stream job，并进入 acceptance 依赖。"""

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    manifest = (ROOT / "compliance" / "ci-jobs.toml").read_text(encoding="utf-8")
    evidence = (ROOT / "scripts" / "ci_evidence.py").read_text(encoding="utf-8")
    github = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    gitlab = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")

    assert "smoke-live-model-stream:" in makefile
    assert "$(UV) run python -m scripts.smoke_live_model_stream" in makefile
    assert "ci-smoke-live-model-stream:" in makefile
    assert 'id = "smoke-live-model-stream"' in manifest
    assert '"smoke-live-model-stream": "smoke-live-model-stream"' in evidence
    assert "smoke-live-model-stream:" in github
    assert "smoke-live-model-stream:" in gitlab
    assert "ci-smoke-live-model-stream-${{ github.run_id }}" in github
    assert ".artifacts/smoke/live-model-stream/result.json" in makefile
    assert "model-stream-live-smoke/v1" in (
        ROOT / "scripts" / "live_model_stream_contract.py"
    ).read_text(encoding="utf-8")


def test_github_artifact_jobs_explicitly_include_hidden_evidence_paths() -> None:
    """固定 action 默认排除点目录时，`.artifacts/**` 必须显式允许上传。"""

    contract = tomllib.loads((ROOT / "compliance/ci-jobs.toml").read_text(encoding="utf-8"))
    upload_action = contract["platform"]["github_upload_artifact"]
    for workflow_name in ("ci.yml", "release.yml"):
        workflow = yaml.safe_load(
            (ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
        )
        for job_name, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                if step.get("uses") == upload_action:
                    assert step.get("with", {}).get("include-hidden-files") is True, (
                        f"{workflow_name}:{job_name} 未允许上传 .artifacts 隐藏目录"
                    )


def test_github_release_handoffs_restore_dot_artifacts_archive_root() -> None:
    """多路径上传以 `.artifacts` 为共同根，release consumer 必须解包回该目录。"""

    contract = tomllib.loads((ROOT / "compliance/ci-jobs.toml").read_text(encoding="utf-8"))
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    download_action = contract["platform"]["github_download_artifact"]
    expected = {
        "promote-plan": "ci-release-dry-run-${{ github.run_id }}",
        "promote-no-release": "release-promotion-plan-${{ github.run_id }}",
        "promote-execute": "release-promotion-plan-${{ github.run_id }}",
        "publish-plan": "release-promotion-execute-${{ github.run_id }}",
        "publish-execute": "registry-publish-plan-${{ github.run_id }}",
    }

    for job_name, artifact_name in expected.items():
        downloads = [
            step
            for step in workflow["jobs"][job_name]["steps"]
            if step.get("uses") == download_action
        ]
        assert len(downloads) == 1
        assert downloads[0].get("with") == {
            "name": artifact_name,
            "path": ".artifacts",
        }


def test_ci_contract_rejects_release_download_archive_root_drift(tmp_path: Path) -> None:
    """CI validator 本身必须拒绝把多路径 artifact 解包到仓库根。"""

    copy_contract_surface(tmp_path)
    release_path = tmp_path / ".github/workflows/release.yml"
    workflow = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    download_action = tomllib.loads(
        (tmp_path / "compliance/ci-jobs.toml").read_text(encoding="utf-8")
    )["platform"]["github_download_artifact"]
    step = next(
        item
        for item in workflow["jobs"]["promote-plan"]["steps"]
        if item.get("uses") == download_action
    )
    step["with"]["path"] = "."
    release_path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")

    rejected = run_validator(tmp_path)

    assert rejected.returncode == 2
    assert "download must restore .artifacts" in rejected.stderr


def test_eval_and_smoke_native_artifacts_are_stable_in_all_archive_sets() -> None:
    """四个稳定原生产物必须同时进入 manifest 与双 CI 归档集合，拒绝 pending 占位。"""

    contract_text = (ROOT / "compliance/ci-jobs.toml").read_text(encoding="utf-8")
    contract = tomllib.loads(contract_text)
    github_text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    gitlab_text = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    stable = {
        ".artifacts/eval/scores.jsonl",
        ".artifacts/eval/traces.jsonl",
        ".artifacts/smoke/local/trace.jsonl",
        ".artifacts/smoke/service/trace.jsonl",
    }
    declared = {path for job in contract["jobs"] for path in job.get("native_artifacts", [])}
    assert "native_artifacts_pending" not in contract_text
    assert stable <= declared
    for path in stable:
        assert path in github_text
        assert path in gitlab_text
