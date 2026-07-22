"""发布 promotion、provider 调用与回执闭合行为合同。"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
import os
import sys
import tomllib
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

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

from scripts import release_promote as release_promote_module  # noqa: E402


@pytest.fixture
def loopback_server() -> Generator[tuple[str, type[RegistryHandler]], None, None]:
    """复用共享 loopback 生命周期，同时让 pytest 在当前测试模块发现 fixture。"""

    yield from loopback_server_fixture()


@pytest.mark.parametrize("artifact_kind", ["changelog", "release-notes"])
def test_promotion_rejects_tampered_document_artifact_before_side_effects(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    artifact_kind: str,
) -> None:
    """变更日志或发布说明漂移时，promotion 必须在 commit、tag、push、provider 前拒绝。"""

    endpoint, handler = loopback_server
    repo = tmp_path / "repo"
    write_release_repo(repo)
    output = repo / ".artifacts" / "release-preview" / "tampered-document"
    assert dry_run(repo, output).returncode == 0
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_item = next(item for item in manifest["artifacts"] if item["kind"] == artifact_kind)
    (repo / artifact_item["path"]).write_text("tampered\n", encoding="utf-8")
    bare = tmp_path / "remote.git"
    add_seeded_origin(repo, bare)
    before = (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "tag", "--list"),
        git(repo, "ls-remote", "origin"),
    )

    result = run(
        sys.executable,
        str(PROMOTE),
        "--repo",
        str(repo),
        "--manifest",
        str(manifest_path),
        "--output-dir",
        str(tmp_path / "promotion"),
        "--execute",
        cwd=ROOT,
        env={
            "RELEASE_PROMOTION_APPROVED": "true",
            "RELEASE_PROTECTED_REF": "true",
            "RELEASE_TEST_MODE": "true",
            "RELEASE_PROVIDER_URL": endpoint,
            "RELEASE_PROVIDER_TOKEN": "fixture-provider-token",
        },
    )

    assert result.returncode != 0
    assert "checksum drift" in result.stderr
    assert before == (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "tag", "--list"),
        git(repo, "ls-remote", "origin"),
    )
    assert handler.requests == []


def test_isolated_promotion_orders_commit_tag_provider_and_writes_closed_receipt(
    tmp_path: Path, loopback_server: tuple[str, type[RegistryHandler]]
) -> None:
    """一次性 repo/bare/provider 替身验证完整 promotion 顺序与 receipt 闭合。"""

    endpoint, handler = loopback_server
    repo = tmp_path / "repo"
    write_release_repo(repo)
    git(repo, "tag", "-a", "agent-harness-v0.1.0", "-m", "0.1.0")
    (repo / "feature.txt").write_text("promotion feature\n", encoding="utf-8")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-m", "feat: promotion feature")
    output = repo / ".artifacts" / "release-preview" / "promotion"
    assert dry_run(repo, output).returncode == 0
    preview = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    release_notes_item = next(
        item for item in preview["artifacts"] if item["kind"] == "release-notes"
    )
    release_notes = (repo / release_notes_item["path"]).read_text(encoding="utf-8")
    bare = tmp_path / "remote.git"
    add_seeded_origin(repo, bare)
    receipt_dir = tmp_path / "promotion"
    result = promotion_execute(
        repo,
        output / "manifest.json",
        receipt_dir,
        endpoint=endpoint,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "release-promotion/v1"
    assert receipt["status"] == "promoted"
    assert receipt["release_commit_sha"] == receipt["tag_target_sha"]
    assert git(repo, "rev-list", "-n", "1", receipt["tag"]) == receipt["release_commit_sha"]
    assert git(repo, "cat-file", "-t", receipt["tag"]) == "tag"
    assert git(bare, "rev-parse", "refs/heads/main") == receipt["release_commit_sha"]
    assert (
        git(bare, "rev-list", "-n", "1", f"refs/tags/{receipt['tag']}")
        == receipt["release_commit_sha"]
    )
    assert git(bare, "cat-file", "-t", f"refs/tags/{receipt['tag']}") == "tag"
    lock_check = run(os.environ["UV"], "lock", "--check", cwd=repo)
    assert lock_check.returncode == 0, lock_check.stderr
    assert (
        "uv.lock"
        in git(
            repo,
            "show",
            "--format=",
            "--name-only",
            receipt["release_commit_sha"],
        ).splitlines()
    )
    with (repo / "packages/agent-harness/pyproject.toml").open("rb") as stream:
        assert tomllib.load(stream)["project"]["version"] == receipt["version"]
    with (repo / "pyproject.toml").open("rb") as stream:
        root_project = tomllib.load(stream)
    with (repo / "templates/service-app/pyproject.toml").open("rb") as stream:
        template_project = tomllib.load(stream)
    assert "agent-harness==0.2.0" in root_project["project"]["dependencies"]
    assert "agent-harness==0.2.*" in template_project["project"]["dependencies"]
    assert len(handler.requests) == 1 and handler.requests[0]["path"] == "/"
    provider_payload_value = handler.requests[0]["json_payload"]
    assert isinstance(provider_payload_value, dict)
    provider_payload = cast(dict[str, object], provider_payload_value)
    assert provider_payload == {
        "tag": preview["tag"],
        "target": receipt["release_commit_sha"],
        "version": preview["next_version"],
        "release_notes": release_notes,
    }
    provider_release_notes = provider_payload.get("release_notes")
    assert isinstance(provider_release_notes, str)
    assert (
        hashlib.sha256(provider_release_notes.encode()).hexdigest()
        == receipt["release_notes_sha256"]
    )
    assert receipt["provider_release_id"] == "local-release"
    assert receipt["provider_release_url"] == "http://127.0.0.1/release"


def test_promotion_uses_reviewed_release_notes_bytes_after_preflight(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预检后路径内容变化时，provider 与回执仍只能消费已校验的说明 bytes。"""

    endpoint, handler = loopback_server
    repo = tmp_path / "repo"
    write_release_repo(repo)
    preview_dir = repo / ".artifacts" / "release-preview" / "frozen-release-notes"
    assert dry_run(repo, preview_dir).returncode == 0
    manifest_path = preview_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    notes_item = next(item for item in manifest["artifacts"] if item["kind"] == "release-notes")
    notes_path = repo / notes_item["path"]
    reviewed_notes = notes_path.read_text(encoding="utf-8")
    bare = tmp_path / "remote.git"
    add_seeded_origin(repo, bare)
    plan = promotion_plan(repo, manifest_path, endpoint=endpoint)
    assert plan.returncode == 0, plan.stderr
    approval_sha256 = str(json.loads(plan.stdout)["approval_sha256"])
    original_update = release_promote_module._update_release_files

    def replace_notes_after_preflight(
        repo_value: Path,
        preview_value: dict[str, Any],
        *,
        changelog_preview: str,
    ) -> Path:
        """在生产更新 seam 返回前替换路径内容，固定 provider 前的竞争窗口。"""

        path = original_update(
            repo_value,
            preview_value,
            changelog_preview=changelog_preview,
        )
        path.write_text("unreviewed replacement\n", encoding="utf-8")
        return path

    monkeypatch.setattr(
        release_promote_module,
        "_update_release_files",
        replace_notes_after_preflight,
    )
    monkeypatch.setenv("RELEASE_PROMOTION_APPROVED", "true")
    monkeypatch.setenv("RELEASE_PROMOTION_APPROVAL_SHA256", approval_sha256)
    monkeypatch.setenv("RELEASE_PROTECTED_REF", "true")
    monkeypatch.setenv("RELEASE_PROTECTED_DEFAULT_BRANCH", "main")
    monkeypatch.setenv("RELEASE_TEST_MODE", "true")
    monkeypatch.setenv("RELEASE_PROVIDER_URL", endpoint)
    monkeypatch.setenv("RELEASE_PROVIDER_TOKEN", "fixture-provider-token")

    receipt = release_promote_module.promote(
        repo=repo,
        manifest_path=manifest_path,
        output_dir=tmp_path / "promotion",
        execute=True,
        plan_path=repo / ".artifacts/release-promotion/test-plan.json",
    )

    assert receipt["status"] == "promoted"
    provider_payload = cast(dict[str, object], handler.requests[0]["json_payload"])
    assert provider_payload["release_notes"] == reviewed_notes
    assert receipt["release_notes_sha256"] == notes_item["sha256"]
