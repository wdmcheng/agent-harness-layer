"""私有 registry 的 schema、回执、路径与类型校验合同。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator
from pathlib import Path

import pytest
from release_contract_test_support import (
    RegistryHandler,
    loopback_server_fixture,
    registry_execute,
    write_publish_inputs,
)


@pytest.fixture
def loopback_server() -> Generator[tuple[str, type[RegistryHandler]], None, None]:
    """复用共享 loopback 生命周期，同时让 pytest 在当前测试模块发现 fixture。"""

    yield from loopback_server_fixture()


def test_registry_rejects_receipt_checksum_drift_before_network(
    tmp_path: Path, loopback_server: tuple[str, type[RegistryHandler]]
) -> None:
    """陈旧 preview/receipt/artifact 任一 checksum 漂移必须在上传请求前具体拒绝。"""

    endpoint, handler = loopback_server
    preview, receipt, wheel = write_publish_inputs(tmp_path)
    wheel.write_bytes(b"changed-after-promotion")
    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-token",
    )
    assert result.returncode != 0
    assert handler.requests == []
    assert "checksum" in (result.stdout + result.stderr).lower()


@pytest.mark.parametrize("suffix", ["?token=query-secret", "#token=fragment-secret"])
def test_registry_rejects_endpoint_query_or_fragment_credential_before_plan(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    suffix: str,
) -> None:
    """registry 身份 URL 不承载 query/fragment，凭据只能来自专用环境变量。"""

    endpoint, handler = loopback_server
    preview, receipt, _ = write_publish_inputs(tmp_path)
    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=f"{endpoint}{suffix}",
        token="fixture-token",
    )

    assert result.returncode != 0
    assert handler.requests == []
    assert suffix.removeprefix("?").removeprefix("#") not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "failed"),
        ("status", "no-release"),
        ("preview_manifest_sha256", "0" * 64),
        ("version", "0.3.0"),
        ("tag", "agent-harness-v0.3.0"),
        ("tag_target_sha", "e" * 40),
    ],
)
def test_registry_rejects_non_promoted_or_stale_identity_before_network(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    field: str,
    value: str,
) -> None:
    """非 promoted 或陈旧 promotion 身份均不能借完整 job 顺序越过网络前校验。"""

    endpoint, handler = loopback_server
    preview, receipt, _ = write_publish_inputs(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload[field] = value
    receipt.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-token",
    )
    assert result.returncode != 0
    assert handler.requests == []


@pytest.mark.parametrize(
    "invalid_oid",
    [
        "",
        "not-a-git-object-id",
        "a" * 39,
        "a" * 41,
        "g" * 40,
        "a" * 63,
        "a" * 65,
        "A" * 40,
        "A" * 64,
    ],
)
def test_registry_rejects_equal_but_invalid_promotion_git_oid_before_network(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    invalid_oid: str,
) -> None:
    """release commit 与 tag target 即使相等，也必须是 producer 兼容的小写 Git OID。"""

    endpoint, handler = loopback_server
    preview, receipt, _ = write_publish_inputs(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["release_commit_sha"] = invalid_oid
    payload["tag_target_sha"] = invalid_oid
    receipt.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-token",
    )

    assert result.returncode != 0
    assert handler.requests == []


@pytest.mark.parametrize("valid_oid", ["c" * 40, "c" * 64])
def test_registry_accepts_lowercase_sha1_and_sha256_promotion_git_oid(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    valid_oid: str,
) -> None:
    """共享 validator 接受 Git SHA-1 与 SHA-256 producer 的小写 object identity。"""

    endpoint, handler = loopback_server
    preview, receipt, _ = write_publish_inputs(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["release_commit_sha"] = valid_oid
    payload["tag_target_sha"] = valid_oid
    build_path = tmp_path / payload["release_build_manifest"]
    build = json.loads(build_path.read_text(encoding="utf-8"))
    build["tag_target_sha"] = valid_oid
    build_path.write_text(json.dumps(build, sort_keys=True), encoding="utf-8")
    payload["release_build_manifest_sha256"] = hashlib.sha256(build_path.read_bytes()).hexdigest()
    receipt.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-token",
    )

    assert result.returncode == 0, result.stderr
    assert len([request for request in handler.requests if request["method"] == "POST"]) == 2


@pytest.mark.parametrize(
    "case",
    [
        "duplicate-wheel",
        "duplicate-sdist",
        "duplicate-changelog",
        "duplicate-release-notes",
        "duplicate-checksums",
        "duplicate-path",
    ],
)
def test_registry_rejects_duplicate_preview_artifact_kind_or_path_before_network(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    case: str,
) -> None:
    """release preview 的五类 artifact 与规范化路径都必须一对一，禁止重复上传。"""

    endpoint, handler = loopback_server
    preview_path, receipt_path, _ = write_publish_inputs(tmp_path)
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    artifacts = preview["artifacts"]
    if case == "duplicate-path":
        wheel = next(item for item in artifacts if item["kind"] == "wheel")
        sdist = next(item for item in artifacts if item["kind"] == "sdist")
        sdist.update({"path": wheel["path"], "sha256": wheel["sha256"], "size": wheel["size"]})
    else:
        duplicate_kind = case.removeprefix("duplicate-")
        duplicate = next(item for item in artifacts if item["kind"] == duplicate_kind)
        artifacts.append(dict(duplicate))
    receipt["artifacts"] = [item for item in artifacts if item["kind"] in {"wheel", "sdist"}]
    preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
    receipt["preview_manifest_sha256"] = hashlib.sha256(preview_path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

    result = registry_execute(
        preview_path,
        receipt_path,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-token",
    )

    assert result.returncode != 0
    assert handler.requests == []


@pytest.mark.parametrize(
    "missing_field",
    [
        "release_notes_sha256",
        "provider",
        "provider_release_id",
        "provider_release_url",
    ],
)
def test_registry_rejects_promoted_receipt_missing_authority_field_before_network(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    missing_field: str,
) -> None:
    """promoted receipt 缺任一 provider/notes 授权字段时必须在 upload 前 fail closed。"""

    endpoint, handler = loopback_server
    preview, receipt, _ = write_publish_inputs(tmp_path)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    del payload[missing_field]
    receipt.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-token",
    )
    assert result.returncode != 0
    assert handler.requests == []
    assert missing_field in (result.stdout + result.stderr)


@pytest.mark.parametrize("case", ["mismatched-receipt", "missing-artifact", "duplicate-artifact"])
def test_registry_requires_unique_release_notes_checksum_binding_before_network(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    case: str,
) -> None:
    """receipt 必须精确绑定唯一发布说明；合法错值、缺失或重复均不得上传。"""

    endpoint, handler = loopback_server
    preview_path, receipt_path, _ = write_publish_inputs(tmp_path)
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if case == "mismatched-receipt":
        receipt["release_notes_sha256"] = "0" * 64
    elif case == "missing-artifact":
        preview["artifacts"] = [
            item for item in preview["artifacts"] if item["kind"] != "release-notes"
        ]
    else:
        release_notes = next(
            item for item in preview["artifacts"] if item["kind"] == "release-notes"
        )
        preview["artifacts"].append(dict(release_notes))
    preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
    receipt["preview_manifest_sha256"] = hashlib.sha256(preview_path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

    result = registry_execute(
        preview_path,
        receipt_path,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-token",
    )

    assert result.returncode != 0
    assert handler.requests == []


@pytest.mark.parametrize(
    "container,missing_field",
    [("source", "base_tag"), ("decision", "bump"), ("decision", "reason")],
)
def test_registry_rejects_preview_missing_declared_v1_field_before_network(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    container: str,
    missing_field: str,
) -> None:
    """v1 source/decision 声明的必填字段缺失时，consumer 必须在 upload 前拒绝。"""

    endpoint, handler = loopback_server
    preview_path, receipt_path, _ = write_publish_inputs(tmp_path)
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    del preview[container][missing_field]
    if container == "source":
        receipt["source"] = preview["source"]
    preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
    receipt["preview_manifest_sha256"] = hashlib.sha256(preview_path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

    result = registry_execute(
        preview_path,
        receipt_path,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-token",
    )

    assert result.returncode != 0
    assert handler.requests == []


@pytest.mark.parametrize(
    "case",
    [
        "commit-sha-format",
        "dirty-sha-format",
        "base-tag-type",
        "decision-bump-type",
        "decision-reason-type",
        "decision-commits-type",
        "commit-entry-missing",
        "commit-entry-field-type",
        "current-version-format",
        "next-version-format",
        "tag-mismatch",
        "no-release-bump",
    ],
)
def test_registry_rejects_preview_wrong_v1_field_type_before_network(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    case: str,
) -> None:
    """v1 字段类型、格式或状态语义错误时，consumer 必须在 upload 前拒绝。"""

    endpoint, handler = loopback_server
    preview_path, receipt_path, _ = write_publish_inputs(tmp_path)
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if case == "commit-sha-format":
        preview["source"]["commit_sha"] = "not-a-git-object-id"
        receipt["source"] = preview["source"]
    elif case == "dirty-sha-format":
        preview["source"]["dirty_diff_sha256"] = "not-a-sha256"
        receipt["source"] = preview["source"]
    elif case == "base-tag-type":
        preview["source"]["base_tag"] = 1
        receipt["source"] = preview["source"]
    elif case == "decision-bump-type":
        preview["decision"]["bump"] = []
    elif case == "decision-reason-type":
        preview["decision"]["reason"] = []
    elif case == "decision-commits-type":
        preview["decision"]["commits"] = {}
    elif case == "commit-entry-missing":
        preview["decision"]["commits"] = [{"sha": "a" * 40}]
    elif case == "commit-entry-field-type":
        preview["decision"]["commits"] = [
            {
                "sha": "a" * 40,
                "type": "feat",
                "scope": None,
                "subject": "fixture",
                "breaking": "false",
                "bump": "minor",
            }
        ]
    elif case == "current-version-format":
        preview["current_version"] = "v0.1"
    elif case == "next-version-format":
        preview["next_version"] = "0.2"
    elif case == "tag-mismatch":
        preview["tag"] = "agent-harness-v9.9.9"
    else:
        preview.update({"status": "no-release", "next_version": None, "tag": None})
        preview["artifacts"] = []
    preview_path.write_text(json.dumps(preview, sort_keys=True), encoding="utf-8")
    receipt["preview_manifest_sha256"] = hashlib.sha256(preview_path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

    result = registry_execute(
        preview_path,
        receipt_path,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-token",
    )

    assert result.returncode != 0
    assert handler.requests == []
