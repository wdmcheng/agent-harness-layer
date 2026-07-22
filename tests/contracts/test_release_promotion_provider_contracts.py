"""发布 promotion、provider 调用与回执闭合行为合同。"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Generator
from pathlib import Path
from typing import cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

PROMOTE_MODULE = importlib.import_module("release_promote")
PROMOTE_FUNCTION = vars(PROMOTE_MODULE)["promote"]
RELEASE_CONTRACT_ERROR = cast(
    type[Exception], vars(importlib.import_module("release_models"))["ReleaseContractError"]
)

from release_contract_test_support import (  # noqa: E402 - scripts path must precede helper import
    PROMOTE,
    ROOT,
    RegistryHandler,
    add_seeded_origin,
    dry_run,
    git,
    loopback_server_fixture,
    promotion_execute,
    run,
    write_release_repo,
)


@pytest.fixture
def loopback_server() -> Generator[tuple[str, type[RegistryHandler]], None, None]:
    """复用共享 loopback 生命周期，同时让 pytest 在当前测试模块发现 fixture。"""

    yield from loopback_server_fixture()


def test_promotion_redacts_provider_reflected_credential_from_receipt_and_output(
    tmp_path: Path, loopback_server: tuple[str, type[RegistryHandler]]
) -> None:
    """provider 反射 credential 时，持久化回执和 CLI 输出都不得保留原始 secret。"""

    endpoint, handler = loopback_server
    token = "provider-reflected-secret"
    handler.response_body = json.dumps(
        {
            "id": f"release-token={token}",
            "url": f"http://127.0.0.1/release?credential={token}",
        }
    ).encode()
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview = repo / ".artifacts" / "release-preview" / "provider-redaction"
    assert dry_run(repo, preview).returncode == 0
    bare = tmp_path / "remote.git"
    add_seeded_origin(repo, bare)
    receipt_dir = tmp_path / "promotion"

    result = promotion_execute(
        repo,
        preview / "manifest.json",
        receipt_dir,
        endpoint=endpoint,
        token=token,
    )

    assert result.returncode == 0, result.stderr
    serialized = (receipt_dir / "receipt.json").read_text(encoding="utf-8")
    assert token not in serialized + result.stdout + result.stderr
    assert "[REDACTED]" in serialized


def test_provider_release_url_preserves_valid_endpoint_prefix(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """合法 release URL 可复用审批 endpoint 前缀，去敏后仍须是 consumer 可用 URL。"""

    endpoint, handler = loopback_server
    release_url = f"{endpoint}/release"
    handler.response_body = json.dumps({"id": "release-1", "url": release_url}).encode()
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview = repo / ".artifacts" / "release-preview" / "endpoint-prefix"
    assert dry_run(repo, preview).returncode == 0
    bare = tmp_path / "remote.git"
    add_seeded_origin(repo, bare)
    receipt_dir = tmp_path / "promotion"

    result = promotion_execute(
        repo,
        preview / "manifest.json",
        receipt_dir,
        endpoint=endpoint,
    )

    assert result.returncode == 0, result.stderr
    promoted = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))
    assert promoted["status"] == "promoted"
    assert promoted["provider_release_url"] == release_url


def test_provider_redirect_writes_failed_receipt_without_following_get(
    tmp_path: Path, loopback_server: tuple[str, type[RegistryHandler]]
) -> None:
    """provider 30x 不得改写 release POST 或把跳转终点响应记为 promoted。"""

    endpoint, handler = loopback_server
    handler.redirect_location = f"{endpoint}/redirected"
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview = repo / ".artifacts" / "release-preview" / "provider-redirect"
    assert dry_run(repo, preview).returncode == 0
    bare = tmp_path / "remote.git"
    add_seeded_origin(repo, bare)
    receipt_dir = tmp_path / "promotion"

    result = promotion_execute(
        repo,
        preview / "manifest.json",
        receipt_dir,
        endpoint=endpoint,
        token="fixture-provider-redirect-token",
    )

    assert result.returncode != 0
    assert [request["method"] for request in handler.requests] == ["POST"]
    failed = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed"


def test_promotion_redacts_git_remote_userinfo_from_failed_receipt_and_log(
    tmp_path: Path, loopback_server: tuple[str, type[RegistryHandler]]
) -> None:
    """push 失败诊断可能回显 remote userinfo，失败回执与日志必须统一去敏。"""

    endpoint, _handler = loopback_server
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview = repo / ".artifacts" / "release-preview" / "remote-redaction"
    assert dry_run(repo, preview).returncode == 0
    remote_secret = "remote-password-secret"
    git(
        repo,
        "remote",
        "add",
        "origin",
        f"https://release-user:{remote_secret}@127.0.0.1:9/repository.git",
    )
    receipt_dir = tmp_path / "promotion"

    result = promotion_execute(
        repo,
        preview / "manifest.json",
        receipt_dir,
        endpoint=endpoint,
    )

    assert result.returncode != 0
    serialized = (receipt_dir / "receipt.json").read_text(encoding="utf-8")
    assert remote_secret not in serialized + result.stdout + result.stderr
    assert "release-user:" not in serialized + result.stdout + result.stderr


def test_no_release_promotion_writes_non_publishable_receipt_without_git_changes(
    tmp_path: Path,
) -> None:
    """no-release 生成不可发布回执，且不要求凭据、不创建 commit/tag/provider 请求。"""

    repo = tmp_path / "repo"
    write_release_repo(repo)
    git(repo, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")
    (repo / "README.md").write_text("docs\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "docs: no release")
    preview = repo / ".artifacts" / "release-preview" / "none"
    assert dry_run(repo, preview).returncode == 0
    before = git(repo, "rev-parse", "HEAD")
    receipt_dir = tmp_path / "promotion"
    plan_path = tmp_path / "promotion-plan.json"
    plan_result = run(
        sys.executable,
        str(PROMOTE),
        "--repo",
        str(repo),
        "--manifest",
        str(preview / "manifest.json"),
        "--plan-output",
        str(plan_path),
        cwd=ROOT,
    )
    assert plan_result.returncode == 0, plan_result.stderr
    result = run(
        sys.executable,
        str(PROMOTE),
        "--repo",
        str(repo),
        "--manifest",
        str(preview / "manifest.json"),
        "--output-dir",
        str(receipt_dir),
        "--plan-input",
        str(plan_path),
        "--execute",
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "no-release"
    assert git(repo, "rev-parse", "HEAD") == before


def test_partial_provider_failure_writes_failed_receipt_not_publish_authority(
    tmp_path: Path, loopback_server: tuple[str, type[RegistryHandler]]
) -> None:
    """provider 不确定结果必须写 failed 回执，保留已确认 commit/tag 身份且禁止 registry 消费。"""

    endpoint, handler = loopback_server
    handler.status_sequence = [202]
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview = repo / ".artifacts" / "release-preview" / "failure"
    assert dry_run(repo, preview).returncode == 0
    bare = tmp_path / "remote.git"
    add_seeded_origin(repo, bare)
    receipt_dir = tmp_path / "promotion"
    result = promotion_execute(
        repo,
        preview / "manifest.json",
        receipt_dir,
        endpoint=endpoint,
    )
    assert result.returncode != 0
    receipt = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    assert receipt["release_commit_sha"] == receipt["tag_target_sha"]


def test_formal_build_failure_preserves_confirmed_provider_identity(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 已创建后正式 build 失败，failed receipt 仍须可定位外部 release。"""

    endpoint, _handler = loopback_server
    repo = tmp_path / "repo"
    write_release_repo(repo)
    git(repo, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")
    (repo / "feature.txt").write_text("provider then build failure\n", encoding="utf-8")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "feat: provider identity recovery")
    preview = repo / ".artifacts" / "release-preview" / "provider-build-failure"
    assert dry_run(repo, preview).returncode == 0
    add_seeded_origin(repo, tmp_path / "remote.git")
    manifest = preview / "manifest.json"
    plan_path = repo / ".artifacts" / "release-promotion" / "plan.json"
    output_dir = repo / ".artifacts" / "release-promotion" / "execute"
    monkeypatch.setenv("RELEASE_PROVIDER_URL", endpoint)
    monkeypatch.setenv("RELEASE_PROTECTED_DEFAULT_BRANCH", "main")
    monkeypatch.setenv("RELEASE_TEST_MODE", "true")
    plan = PROMOTE_FUNCTION(
        repo=repo,
        manifest_path=manifest,
        output_dir=output_dir,
        execute=False,
        plan_path=plan_path,
    )
    monkeypatch.setenv("RELEASE_PROMOTION_APPROVED", "true")
    monkeypatch.setenv("RELEASE_PROMOTION_APPROVAL_SHA256", plan["approval_sha256"])
    monkeypatch.setenv("RELEASE_PROTECTED_REF", "true")
    monkeypatch.setenv("RELEASE_PROVIDER_TOKEN", "fixture-provider-token")

    def fail_build(**_kwargs: object) -> dict[str, object]:
        raise RELEASE_CONTRACT_ERROR("forced formal build failure")

    monkeypatch.setattr(PROMOTE_MODULE, "build_release", fail_build)

    with pytest.raises(RELEASE_CONTRACT_ERROR, match="forced formal build failure"):
        PROMOTE_FUNCTION(
            repo=repo,
            manifest_path=manifest,
            output_dir=output_dir,
            execute=True,
            plan_path=plan_path,
        )

    receipt = json.loads((output_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    assert receipt["provider"] == "configured-http-provider"
    assert receipt["provider_release_id"]
    assert receipt["provider_release_url"]


def test_truncated_provider_success_writes_failed_receipt_with_confirmed_git_identity(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """provider 成功头后的截断 JSON 必须写 failed 回执并保留已推送 commit/tag 身份。"""

    endpoint, handler = loopback_server
    handler.truncate_response_body_count = 1
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview = repo / ".artifacts" / "release-preview" / "truncated-provider"
    assert dry_run(repo, preview).returncode == 0
    bare = tmp_path / "remote.git"
    add_seeded_origin(repo, bare)
    receipt_dir = tmp_path / "promotion"

    result = promotion_execute(
        repo,
        preview / "manifest.json",
        receipt_dir,
        endpoint=endpoint,
    )

    assert result.returncode != 0
    assert [request["method"] for request in handler.requests] == ["POST"]
    failed = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["release_commit_sha"] == failed["tag_target_sha"]
    assert failed["tag"].startswith("agent-harness-v")
    assert "traceback" not in result.stderr.lower()


@pytest.mark.parametrize(
    "response_body",
    [
        b'{"id":"","url":"http://127.0.0.1/release"}',
        b'{"id":"release-1","url":"not-a-provider-url"}',
        b'{"id":"release-1","url":"https://[::1"}',
    ],
    ids=["empty-id", "invalid-url", "malformed-ipv6-url"],
)
def test_provider_success_without_release_identity_writes_failed_receipt(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    response_body: bytes,
) -> None:
    """完整 2xx JSON 仍须携带非空 ID 与合法 URL，否则 promotion 不得误报成功。"""

    endpoint, handler = loopback_server
    handler.response_body = response_body
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview = repo / ".artifacts" / "release-preview" / "missing-provider-identity"
    assert dry_run(repo, preview).returncode == 0
    bare = tmp_path / "remote.git"
    add_seeded_origin(repo, bare)
    receipt_dir = tmp_path / "promotion"

    result = promotion_execute(
        repo,
        preview / "manifest.json",
        receipt_dir,
        endpoint=endpoint,
    )

    assert result.returncode != 0
    assert [request["method"] for request in handler.requests] == ["POST"]
    failed = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["release_commit_sha"] == failed["tag_target_sha"]
    assert "provider" in failed["failure"]
    assert "traceback" not in result.stderr.lower()
