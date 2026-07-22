"""GitHub/GitLab pipeline 的公开 YAML 与本地 history guard 合同。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts" / "ci_contract.py"
HISTORY = ROOT / "scripts" / "ci_history.py"
GITLAB_RELEASE_GENERATOR = ROOT / "scripts" / "release_gitlab_pipeline.py"


def _promotion_plan(status: str) -> dict[str, object]:
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


def _run_validator(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(root)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _copy_contract_surface(destination: Path) -> None:
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


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)


def test_repository_pipeline_contract_is_equivalent_and_fail_closed() -> None:
    """基准合同同时证明 job/DAG/target/artifact/权限与完整 history 语义。"""

    completed = _run_validator(ROOT)

    assert completed.returncode == 0, completed.stderr
    assert "ci-contract: ok" in completed.stdout


def test_p0_validator_is_a_required_job_in_both_ci_release_dags() -> None:
    """P0 矩阵必须在 clean hosted run 独立执行，不能依赖 test-aggregate 内部自检。"""

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
        "license",
        "build",
        "release-dry-run",
        "ci-contract",
    }

    assert jobs["p0-validate"]["target"] == "ci-p0-validate"
    assert set(jobs["p0-validate"]["needs"]) == required_producers
    assert set(github["jobs"]["p0-validate"]["needs"]) == required_producers
    assert set(gitlab["p0-validate"]["needs"]) == required_producers
    downloads = {
        step.get("with", {}).get("name"): step.get("with", {}).get("path")
        for step in github["jobs"]["p0-validate"]["steps"]
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
        "ci-license-${{ github.run_id }}": ".artifacts",
        "ci-build-${{ github.run_id }}": ".",
        "ci-release-dry-run-${{ github.run_id }}": ".artifacts",
        "ci-contract-${{ github.run_id }}": ".artifacts/ci/ci-contract",
    }
    assert downloads == expected_downloads
    # P0 依赖 release dry-run 的真实 evidence，所以它是终端门禁；GitLab
    # promotion 必须显式等待该门禁，不能构造反向依赖造成 DAG 环。
    assert gitlab["promote-plan"]["needs"] == ["p0-validate"]


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

    _copy_contract_surface(tmp_path)
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

    rejected = _run_validator(tmp_path)

    assert rejected.returncode == 2
    assert "download must restore .artifacts" in rejected.stderr


def test_local_runner_acceptance_excludes_incompatible_artifact_service() -> None:
    """本地 artifact backend 不兼容时，只验仓库 gate，且不得冒充完整 job PASS。"""

    change = ROOT / "openspec/changes/ci-p0-evidence-closure"
    spec = (change / "specs/dual-ci-p0-evidence/spec.md").read_text(encoding="utf-8")
    design = (change / "design.md").read_text(encoding="utf-8")
    tasks = (change / "tasks.md").read_text(encoding="utf-8")

    assert "本地 artifact service 不属于仓库 gate 的验收依赖" in spec
    assert "artifact server 能力受限" in spec
    assert "不得把整个 job 记为 PASS" in spec
    assert "artifact service 不属于本地 ready-to-archive 的验收依赖" in design
    assert "仓库 Make gate" in tasks


def test_contract_rejects_history_depth_target_and_permission_drift(tmp_path: Path) -> None:
    """任一平台扩大权限或漂移入口都必须由同一 validator 封闭失败。"""

    _copy_contract_surface(tmp_path)
    github = tmp_path / ".github/workflows/ci.yml"
    gitlab = tmp_path / ".gitlab-ci.yml"

    github.write_text(
        github.read_text().replace("fetch-depth: 0", "fetch-depth: 1", 1), encoding="utf-8"
    )
    first = _run_validator(tmp_path)
    assert first.returncode == 2
    assert "fetch-depth" in first.stderr

    _copy_contract_surface(tmp_path)
    gitlab.write_text(
        gitlab.read_text().replace('GIT_DEPTH: "0"', 'GIT_DEPTH: "1"', 1), encoding="utf-8"
    )
    second = _run_validator(tmp_path)
    assert second.returncode == 2
    assert "GIT_DEPTH" in second.stderr

    _copy_contract_surface(tmp_path)
    github.write_text(
        github.read_text().replace("make ci-quality-aggregate", "make ci-ruff-lint", 1),
        encoding="utf-8",
    )
    third = _run_validator(tmp_path)
    assert third.returncode == 2
    assert "quality-aggregate" in third.stderr

    _copy_contract_surface(tmp_path)
    github.write_text(
        github.read_text().replace("contents: read", "contents: write", 1), encoding="utf-8"
    )
    fourth = _run_validator(tmp_path)
    assert fourth.returncode == 2
    assert "read-only" in fourth.stderr


def test_contract_rejects_missing_gitlab_runtime_tools(tmp_path: Path) -> None:
    """固定镜像缺工具时必须在合同层失败，不能等 hosted job 以 127 才暴露。"""

    _copy_contract_surface(tmp_path)
    gitlab = tmp_path / ".gitlab-ci.yml"
    gitlab.write_text(
        gitlab.read_text().replace(" ca-certificates git make", " ca-certificates git", 1),
        encoding="utf-8",
    )

    base_runtime = _run_validator(tmp_path)

    assert base_runtime.returncode == 2
    assert "make" in base_runtime.stderr

    _copy_contract_surface(tmp_path)
    gitlab.write_text(
        gitlab.read_text().replace(" docker.io docker-compose", " docker.io", 1),
        encoding="utf-8",
    )

    service_runtime = _run_validator(tmp_path)

    assert service_runtime.returncode == 2
    assert "docker-compose" in service_runtime.stderr


def test_release_ci_evidence_uses_repo_relative_manifest_path() -> None:
    """release 原生产物必须用仓库相对路径交给 evidence runner，避免安全门禁误拒绝。"""

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert (
        "--artifact release-preview=.artifacts/release-preview/phase15-current/manifest.json"
        in makefile
    )


def test_unit_contract_target_uses_existing_test_roots_and_writes_coverage() -> None:
    """测试入口必须带 release 工具并只指向真实目录，稳定生成 JUnit/coverage。"""

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    unit_contract = makefile.split("unit-contract:\n", 1)[1].split("\nintegration:\n", 1)[0]
    full_test = makefile.split("test:\n", 1)[1].split("\n\n# CI", 1)[0]

    assert (ROOT / "tests/contracts").is_dir()
    assert "$(UV) run --group release pytest" in full_test
    assert "-m pytest tests/contracts" in unit_contract
    assert "pytest tests/unit" not in unit_contract
    assert unit_contract.count("$(UV) run --group release coverage") == 3
    assert "--junitxml=.artifacts/tests/unit-contract-junit.xml" in unit_contract
    assert "coverage xml -o .artifacts/tests/coverage.xml" in unit_contract


def test_license_target_selects_its_conflicting_dependency_group() -> None:
    """license gate 必须显式选择隔离工具组，不能依赖本机残留的可执行文件。"""

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    license_target = makefile.split("license-check:\n", 1)[1].split("\n\n# release change", 1)[0]

    assert "$(UV) run --group license python scripts/license_check.py" in license_target


def test_license_precommit_hook_selects_the_same_dependency_group() -> None:
    """pre-commit 与 Make 必须使用同一 licensecheck 环境，不能因嵌套 uv 漂移观察值。"""

    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = {
        hook["id"]: hook for repository in config["repos"] for hook in repository.get("hooks", [])
    }

    assert hooks["license-check"]["entry"] == (
        "uv run --group license python scripts/license_check.py"
    )


def test_depth_one_clone_is_rejected_until_history_and_tags_are_fetched(tmp_path: Path) -> None:
    """真实 shallow clone 必须在 release wrapper 前被拒绝，补全后才可继续。"""

    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    shallow = tmp_path / "shallow"
    source.mkdir()
    assert _git(source, "init", "-b", "main").returncode == 0
    assert _git(source, "config", "user.name", "CI History Contract").returncode == 0
    assert _git(source, "config", "user.email", "ci-history@example.invalid").returncode == 0
    # 隔离 fixture 不继承维护者机器的签名策略，否则无 GPG agent 的 CI 无法建立历史。
    assert _git(source, "config", "commit.gpgsign", "false").returncode == 0
    assert _git(source, "config", "tag.gpgSign", "false").returncode == 0
    (source / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    assert _git(source, "add", "tracked.txt").returncode == 0
    assert _git(source, "commit", "-m", "feat: baseline").returncode == 0
    assert _git(source, "tag", "agent-harness-v0.1.0").returncode == 0
    (source / "tracked.txt").write_text("baseline\nnext\n", encoding="utf-8")
    assert _git(source, "commit", "-am", "fix: next").returncode == 0
    assert _git(tmp_path, "clone", "--bare", str(source), str(remote)).returncode == 0
    assert (
        _git(
            tmp_path,
            "clone",
            "--depth=1",
            f"file://{remote}",
            str(shallow),
        ).returncode
        == 0
    )

    rejected = subprocess.run(
        [
            sys.executable,
            str(HISTORY),
            "--repo",
            str(shallow),
            "--expected-tag",
            "agent-harness-v0.1.0",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert "shallow" in rejected.stderr

    fetched = _git(shallow, "fetch", "--unshallow", "--tags", "origin")
    assert fetched.returncode == 0, fetched.stderr
    accepted = subprocess.run(
        [
            sys.executable,
            str(HISTORY),
            "--repo",
            str(shallow),
            "--expected-tag",
            "agent-harness-v0.1.0",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert "history=complete" in accepted.stdout


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
    plan.write_text(json.dumps(_promotion_plan("no-release")), encoding="utf-8")

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

    payload = _promotion_plan(status)
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
