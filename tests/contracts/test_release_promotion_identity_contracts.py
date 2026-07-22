"""发布 promotion、provider 调用与回执闭合行为合同。"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from release_contract_test_support import (
    PROMOTE,
    ROOT,
    RegistryHandler,
    add_seeded_origin,
    dry_run,
    git,
    loopback_server_fixture,
    promotion_execute,
    promotion_plan,
    run,
    write_release_repo,
)


@pytest.fixture
def loopback_server() -> Generator[tuple[str, type[RegistryHandler]], None, None]:
    """复用共享 loopback 生命周期，同时让 pytest 在当前测试模块发现 fixture。"""

    yield from loopback_server_fixture()


def test_promotion_default_plan_and_missing_gates_have_zero_git_side_effects(
    tmp_path: Path,
) -> None:
    """promotion 缺审批/保护/credential 时不改 version、commit、tag 或远端。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    output = repo / ".artifacts" / "release-preview" / "plan"
    assert dry_run(repo, output).returncode == 0
    manifest = output / "manifest.json"
    bare = tmp_path / "remote.git"
    add_seeded_origin(repo, bare)
    before = (git(repo, "rev-parse", "HEAD"), git(repo, "tag", "--list"))
    plan = promotion_plan(
        repo,
        manifest,
        endpoint="https://provider.invalid/releases",
    )
    assert plan.returncode == 0, plan.stderr
    planned = json.loads(plan.stdout)
    assert planned["status"] == "planned"
    assert len(planned["approval_sha256"]) == 64
    assert "provider.invalid" not in plan.stdout + plan.stderr
    rejected = run(
        sys.executable,
        str(PROMOTE),
        "--repo",
        str(repo),
        "--manifest",
        str(manifest),
        "--execute",
        cwd=ROOT,
        env={
            "RELEASE_PROVIDER_TOKEN": "provider-secret",
            "RELEASE_PROVIDER_URL": "https://provider.invalid",
        },
    )
    assert rejected.returncode != 0
    assert "provider-secret" not in rejected.stdout + rejected.stderr
    assert before == (git(repo, "rev-parse", "HEAD"), git(repo, "tag", "--list"))


@pytest.mark.parametrize(
    ("endpoint", "credential"),
    [
        ("https://provider-user@provider.invalid/releases", "provider-user"),
        (
            "https://provider-user:provider-secret@provider.invalid/releases",
            "provider-secret",
        ),
    ],
)
def test_promotion_plan_rejects_provider_endpoint_url_credentials(
    tmp_path: Path,
    endpoint: str,
    credential: str,
) -> None:
    """无凭据 plan job 必须拒绝嵌入 provider URL userinfo 的认证材料。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview = repo / ".artifacts" / "release-preview" / "provider-url-credentials"
    assert dry_run(repo, preview).returncode == 0
    bare = tmp_path / "remote.git"
    add_seeded_origin(repo, bare)
    before = (git(repo, "rev-parse", "HEAD"), git(repo, "tag", "--list"))

    result = promotion_plan(repo, preview / "manifest.json", endpoint=endpoint)

    assert result.returncode != 0
    assert "provider endpoint must not contain URL credentials" in result.stderr
    assert credential not in result.stdout + result.stderr
    assert before == (git(repo, "rev-parse", "HEAD"), git(repo, "tag", "--list"))


def test_promotion_rejects_approval_endpoint_or_default_branch_drift_before_git_side_effects(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """人工批准必须绑定 preview/source/tag/provider endpoint，且只能在声明的默认分支消费。"""

    endpoint, handler = loopback_server
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview_dir = repo / ".artifacts" / "release-preview" / "approval-binding"
    assert dry_run(repo, preview_dir).returncode == 0
    manifest = preview_dir / "manifest.json"
    bare = tmp_path / "remote.git"
    add_seeded_origin(repo, bare)
    reviewed = promotion_plan(repo, manifest, endpoint=endpoint)
    assert reviewed.returncode == 0, reviewed.stderr
    approval_sha256 = str(json.loads(reviewed.stdout)["approval_sha256"])
    before = (git(repo, "rev-parse", "HEAD"), git(repo, "tag", "--list"))

    endpoint_drift = promotion_execute(
        repo,
        manifest,
        tmp_path / "endpoint-drift",
        endpoint="https://changed-provider.invalid/releases",
        approval_sha256=approval_sha256,
    )
    assert endpoint_drift.returncode != 0
    assert "approval" in endpoint_drift.stderr.lower()
    assert before == (git(repo, "rev-parse", "HEAD"), git(repo, "tag", "--list"))
    assert handler.requests == []

    original_manifest = manifest.read_text(encoding="utf-8")
    changed_preview = json.loads(original_manifest)
    changed_preview["decision"]["reason"] = "reviewed payload was replaced"
    manifest.write_text(json.dumps(changed_preview, sort_keys=True), encoding="utf-8")
    preview_drift = promotion_execute(
        repo,
        manifest,
        tmp_path / "preview-drift",
        endpoint=endpoint,
        approval_sha256=approval_sha256,
    )
    assert preview_drift.returncode != 0
    assert "approval" in preview_drift.stderr.lower()
    assert before == (git(repo, "rev-parse", "HEAD"), git(repo, "tag", "--list"))
    assert handler.requests == []
    manifest.write_text(original_manifest, encoding="utf-8")

    git(repo, "switch", "-c", "release-candidate")
    wrong_branch = promotion_execute(
        repo,
        manifest,
        tmp_path / "wrong-branch",
        endpoint=endpoint,
        protected_branch="main",
        approval_sha256=approval_sha256,
    )
    assert wrong_branch.returncode != 0
    assert "default branch" in wrong_branch.stderr.lower()
    assert before == (git(repo, "rev-parse", "HEAD"), git(repo, "tag", "--list"))
    assert handler.requests == []


def test_promotion_rejects_environment_claim_when_origin_default_branch_differs(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """环境自声明 main 不足以授权；origin/HEAD 指向其他分支时必须在 Git 写前拒绝。"""

    endpoint, handler = loopback_server
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview = repo / ".artifacts" / "release-preview" / "origin-default-branch"
    assert dry_run(repo, preview).returncode == 0
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(bare, "symbolic-ref", "HEAD", "refs/heads/release-candidate")
    git(repo, "push", "origin", "HEAD:refs/heads/release-candidate")
    manifest = preview / "manifest.json"
    before = (git(repo, "rev-parse", "HEAD"), git(repo, "tag", "--list"))

    result = promotion_execute(
        repo,
        manifest,
        tmp_path / "rejected-origin",
        endpoint=endpoint,
    )

    assert result.returncode != 0
    assert "origin default branch" in result.stderr
    assert before == (git(repo, "rev-parse", "HEAD"), git(repo, "tag", "--list"))
    assert handler.requests == []


def test_promotion_rejects_approval_reused_after_origin_endpoint_swap(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """审批必须绑定 origin endpoint；相同 source/HEAD 不能授权另一个远端。"""

    endpoint, handler = loopback_server
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview = repo / ".artifacts" / "release-preview" / "origin-endpoint"
    assert dry_run(repo, preview).returncode == 0
    manifest = preview / "manifest.json"
    reviewed_origin = tmp_path / "reviewed.git"
    add_seeded_origin(repo, reviewed_origin)
    reviewed = promotion_plan(repo, manifest, endpoint=endpoint)
    assert reviewed.returncode == 0, reviewed.stderr
    assert str(reviewed_origin) not in reviewed.stdout
    assert len(json.loads(reviewed.stdout)["approval"]["origin_endpoint_sha256"]) == 64
    approval_sha256 = str(json.loads(reviewed.stdout)["approval_sha256"])

    substituted_origin = tmp_path / "substituted.git"
    git(tmp_path, "init", "--bare", str(substituted_origin))
    git(substituted_origin, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repo, "push", str(substituted_origin), "HEAD:refs/heads/main")
    git(repo, "remote", "set-url", "origin", str(substituted_origin))
    before = (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "tag", "--list"),
        git(repo, "ls-remote", "origin"),
    )

    result = promotion_execute(
        repo,
        manifest,
        tmp_path / "origin-endpoint-rejected",
        endpoint=endpoint,
        approval_sha256=approval_sha256,
    )

    assert result.returncode != 0
    assert "approval" in result.stderr.lower()
    assert before == (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "tag", "--list"),
        git(repo, "ls-remote", "origin"),
    )
    assert handler.requests == []


def test_promotion_rejects_unreviewed_additional_origin_push_endpoint(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """唯一 push endpoint 才可审批；追加目标不能搭便车接收 release ref。"""

    endpoint, handler = loopback_server
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview = repo / ".artifacts" / "release-preview" / "multiple-push-endpoints"
    assert dry_run(repo, preview).returncode == 0
    manifest = preview / "manifest.json"
    reviewed_origin = tmp_path / "reviewed.git"
    add_seeded_origin(repo, reviewed_origin)
    git(repo, "remote", "set-url", "--add", "--push", "origin", str(reviewed_origin))
    reviewed = promotion_plan(repo, manifest, endpoint=endpoint)
    assert reviewed.returncode == 0, reviewed.stderr
    approval_sha256 = str(json.loads(reviewed.stdout)["approval_sha256"])

    unreviewed_origin = tmp_path / "unreviewed.git"
    git(tmp_path, "init", "--bare", str(unreviewed_origin))
    git(unreviewed_origin, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repo, "push", str(unreviewed_origin), "HEAD:refs/heads/main")
    git(repo, "remote", "set-url", "--add", "--push", "origin", str(unreviewed_origin))
    before = (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "tag", "--list"),
        git(repo, "ls-remote", str(reviewed_origin)),
        git(repo, "ls-remote", str(unreviewed_origin)),
    )

    result = run(
        sys.executable,
        str(PROMOTE),
        "--repo",
        str(repo),
        "--manifest",
        str(manifest),
        "--output-dir",
        str(tmp_path / "multiple-push-endpoints-rejected"),
        "--execute",
        cwd=ROOT,
        env={
            "RELEASE_PROMOTION_APPROVED": "true",
            "RELEASE_PROMOTION_APPROVAL_SHA256": approval_sha256,
            "RELEASE_PROTECTED_REF": "true",
            "RELEASE_PROTECTED_DEFAULT_BRANCH": "main",
            "RELEASE_TEST_MODE": "true",
            "RELEASE_PROVIDER_URL": endpoint,
            "RELEASE_PROVIDER_TOKEN": "fixture-provider-token",
        },
    )

    assert result.returncode != 0
    assert "push endpoint" in result.stderr.lower()
    assert before == (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "tag", "--list"),
        git(repo, "ls-remote", str(reviewed_origin)),
        git(repo, "ls-remote", str(unreviewed_origin)),
    )
    assert handler.requests == []


def test_promotion_validates_actual_push_endpoint_default_branch(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """fetch 与 push URL 分离时，保护分支证明必须读取实际写入目标。"""

    endpoint, handler = loopback_server
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview = repo / ".artifacts" / "release-preview" / "push-target-default-branch"
    assert dry_run(repo, preview).returncode == 0
    fetch_origin = tmp_path / "fetch.git"
    add_seeded_origin(repo, fetch_origin)
    push_origin = tmp_path / "push.git"
    git(tmp_path, "init", "--bare", str(push_origin))
    git(push_origin, "symbolic-ref", "HEAD", "refs/heads/release-candidate")
    git(repo, "push", str(push_origin), "HEAD:refs/heads/release-candidate")
    git(repo, "remote", "set-url", "--add", "--push", "origin", str(push_origin))
    before = git(repo, "ls-remote", str(push_origin))

    result = promotion_execute(
        repo,
        preview / "manifest.json",
        tmp_path / "push-target-rejected",
        endpoint=endpoint,
    )

    assert result.returncode != 0
    assert "default branch" in result.stderr.lower()
    assert git(repo, "ls-remote", str(push_origin)) == before
    assert handler.requests == []
