"""CI 本地执行边界、Make 入口与 history guard 合同。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml
from tests.contracts.ci_pipeline_test_support import (
    HISTORY,
    ROOT,
    copy_contract_surface,
    git,
    run_validator,
)


def test_local_runner_acceptance_excludes_incompatible_artifact_service() -> None:
    """本地 artifact backend 不兼容时，只验仓库 gate，且不得冒充完整 job PASS。"""

    spec = (ROOT / "openspec/specs/dual-ci-acceptance-evidence/spec.md").read_text(encoding="utf-8")
    release_process = (ROOT / "docs/release-process.md").read_text(encoding="utf-8")
    release_process_zh = (ROOT / "docs/release-process.zh-CN.md").read_text(encoding="utf-8")

    assert "本地 artifact service 不属于仓库 gate 的验收依赖" in spec
    assert "artifact server 能力受限" in spec
    assert "不得把整个 job 记为 PASS" in spec
    assert (
        "artifact-service behavior is outside local ready-to-archive acceptance" in release_process
    )
    assert "Repository Make gates that exited zero" in release_process
    assert "artifact service 不属于本地 ready-to-archive 的验收依赖" in release_process_zh
    assert "仓库 Make gate" in release_process_zh


def test_contract_rejects_history_depth_target_and_permission_drift(tmp_path: Path) -> None:
    """任一平台扩大权限或漂移入口都必须由同一 validator 封闭失败。"""

    copy_contract_surface(tmp_path)
    github = tmp_path / ".github/workflows/ci.yml"
    gitlab = tmp_path / ".gitlab-ci.yml"

    github.write_text(
        github.read_text().replace("fetch-depth: 0", "fetch-depth: 1", 1), encoding="utf-8"
    )
    first = run_validator(tmp_path)
    assert first.returncode == 2
    assert "fetch-depth" in first.stderr

    copy_contract_surface(tmp_path)
    gitlab.write_text(
        gitlab.read_text().replace('GIT_DEPTH: "0"', 'GIT_DEPTH: "1"', 1), encoding="utf-8"
    )
    second = run_validator(tmp_path)
    assert second.returncode == 2
    assert "GIT_DEPTH" in second.stderr

    copy_contract_surface(tmp_path)
    github.write_text(
        github.read_text().replace("make ci-quality-aggregate", "make ci-ruff-lint", 1),
        encoding="utf-8",
    )
    third = run_validator(tmp_path)
    assert third.returncode == 2
    assert "quality-aggregate" in third.stderr

    copy_contract_surface(tmp_path)
    github.write_text(
        github.read_text().replace("contents: read", "contents: write", 1), encoding="utf-8"
    )
    fourth = run_validator(tmp_path)
    assert fourth.returncode == 2
    assert "read-only" in fourth.stderr


def test_contract_rejects_missing_gitlab_runtime_tools(tmp_path: Path) -> None:
    """固定镜像缺工具时必须在合同层失败，不能等 hosted job 以 127 才暴露。"""

    copy_contract_surface(tmp_path)
    gitlab = tmp_path / ".gitlab-ci.yml"
    gitlab.write_text(
        gitlab.read_text().replace(" ca-certificates git make", " ca-certificates git", 1),
        encoding="utf-8",
    )

    base_runtime = run_validator(tmp_path)

    assert base_runtime.returncode == 2
    assert "make" in base_runtime.stderr

    copy_contract_surface(tmp_path)
    gitlab.write_text(
        gitlab.read_text().replace(" docker.io docker-compose", " docker.io", 1),
        encoding="utf-8",
    )

    service_runtime = run_validator(tmp_path)

    assert service_runtime.returncode == 2
    assert "docker-compose" in service_runtime.stderr


def test_release_ci_evidence_uses_repo_relative_manifest_path() -> None:
    """release 原生产物必须用仓库相对路径交给 evidence runner，避免安全门禁误拒绝。"""

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "--artifact release-preview=.artifacts/release-preview/current/manifest.json" in makefile


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
    assert git(source, "init", "-b", "main").returncode == 0
    assert git(source, "config", "user.name", "CI History Contract").returncode == 0
    assert git(source, "config", "user.email", "ci-history@example.invalid").returncode == 0
    # 隔离 fixture 不继承维护者机器的签名策略，否则无 GPG agent 的 CI 无法建立历史。
    assert git(source, "config", "commit.gpgsign", "false").returncode == 0
    assert git(source, "config", "tag.gpgSign", "false").returncode == 0
    (source / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    assert git(source, "add", "tracked.txt").returncode == 0
    assert git(source, "commit", "-m", "feat: baseline").returncode == 0
    assert git(source, "tag", "agent-harness-v0.1.0").returncode == 0
    (source / "tracked.txt").write_text("baseline\nnext\n", encoding="utf-8")
    assert git(source, "commit", "-am", "fix: next").returncode == 0
    assert git(tmp_path, "clone", "--bare", str(source), str(remote)).returncode == 0
    assert (
        git(
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

    fetched = git(shallow, "fetch", "--unshallow", "--tags", "origin")
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
