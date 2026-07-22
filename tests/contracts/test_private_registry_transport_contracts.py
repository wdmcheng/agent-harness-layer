"""固定 uv、package-index multipart、查重与 no-redirect relay 合同。"""

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


def test_registry_retries_transient_response_with_identical_checksum(
    tmp_path: Path, loopback_server: tuple[str, type[RegistryHandler]]
) -> None:
    """loopback registry 的 429/5xx 只允许有界重试完全相同 bytes/checksum。"""

    endpoint, handler = loopback_server
    handler.status_sequence = [429, 503, 200]
    preview, receipt, wheel = write_publish_inputs(tmp_path)
    token = "fixture-registry-token"
    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        token=token,
    )
    assert result.returncode == 0, result.stderr
    uploads = [request for request in handler.requests if request["method"] == "POST"]
    assert len(uploads) == 4
    expected = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert [request["upload_sha256"] for request in uploads[:3]] == [expected] * 3
    assert [request["checksum"] for request in uploads[:3]] == [expected] * 3
    assert {request["auth_scheme"] for request in uploads} == {"Basic"}
    assert {request["basic_username"] for request in uploads} == {"__token__"}
    assert token not in result.stdout + result.stderr


def test_registry_execute_uses_uv_publish_package_index_protocol(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """execute 必须由固定 uv 发送标准 multipart distribution，而非自定义原始 POST。"""

    endpoint, handler = loopback_server
    handler.require_package_index = True
    preview, receipt, wheel = write_publish_inputs(tmp_path)
    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-package-index-token",
    )

    assert result.returncode == 0, result.stderr
    uploads = [request for request in handler.requests if request["method"] == "POST"]
    assert len(uploads) == 2
    assert all(
        str(request["content_type"]).startswith("multipart/form-data") for request in uploads
    )
    assert {request["auth_scheme"] for request in uploads} == {"Basic"}
    assert {request["basic_username"] for request in uploads} == {"__token__"}
    wheel_upload = next(request for request in uploads if request["upload_filename"] == wheel.name)
    assert wheel_upload["upload_sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    fields = wheel_upload["form_fields"]
    assert isinstance(fields, dict)
    assert fields[":action"] == "file_upload"
    assert fields["name"] == "agent-harness"
    assert fields["version"] == "0.2.0"


def test_registry_zero_exit_without_positive_hash_check_is_not_confirmed(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """即使 uv 返回零，simple index 未确认同名同 hash 时也必须失败。"""

    endpoint, handler = loopback_server
    handler.persist_successful_uploads = False
    preview, receipt, _wheel = write_publish_inputs(tmp_path)

    result = registry_execute(preview, receipt, cwd=tmp_path, endpoint=endpoint)

    assert result.returncode != 0
    assert "manual review" in result.stderr.lower()
    assert any(request["method"] == "GET" for request in handler.requests)


def test_registry_safe_retry_confirms_persisted_file_without_duplicate_post(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """registry 已持久化但返回 503 时，check hash 必须确认成功且不得重复 POST 同文件。"""

    endpoint, handler = loopback_server
    handler.status_sequence = [503, 200]
    handler.persist_upload_on_statuses = {503}
    preview, receipt, wheel = write_publish_inputs(tmp_path)
    result = registry_execute(preview, receipt, cwd=tmp_path, endpoint=endpoint)

    assert result.returncode == 0, result.stderr
    uploads = [request for request in handler.requests if request["method"] == "POST"]
    assert [request["upload_filename"] for request in uploads].count(wheel.name) == 1
    assert len(uploads) == 2


def test_registry_unknown_upload_response_never_reposts_without_positive_check(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """服务端读完 distribution 后断连且 check 为空时，必须停在 uncertain 且只 POST 一次。"""

    endpoint, handler = loopback_server
    handler.disconnect_upload_response_count = 1
    preview, receipt, wheel = write_publish_inputs(tmp_path)
    output = tmp_path / "registry-failed.json"

    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        output=output,
    )

    assert result.returncode != 0
    uploads = [request for request in handler.requests if request["method"] == "POST"]
    assert [request["upload_filename"] for request in uploads].count(wheel.name) == 1
    failed = json.loads(output.read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["unconfirmed_uploads"][0]["status"] == "uncertain"
    assert "manual review" in result.stderr.lower()


def test_registry_unknown_response_accepts_positive_hash_check_without_repost(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """回包丢失但 check 已确认同名同 SHA 时可成功，仍不得向外部 registry 重发。"""

    endpoint, handler = loopback_server
    handler.disconnect_upload_response_count = 1
    handler.persist_upload_before_disconnect = True
    preview, receipt, wheel = write_publish_inputs(tmp_path)

    result = registry_execute(preview, receipt, cwd=tmp_path, endpoint=endpoint)

    assert result.returncode == 0, result.stderr
    uploads = [request for request in handler.requests if request["method"] == "POST"]
    assert [request["upload_filename"] for request in uploads].count(wheel.name) == 1
    assert len(uploads) == 2


def test_registry_unknown_response_accepts_pep691_hash_without_repost(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """PEP 691 JSON 的同名同 SHA 也构成正向证据，且不得因此重发 distribution。"""

    endpoint, handler = loopback_server
    handler.disconnect_upload_response_count = 1
    handler.persist_upload_before_disconnect = True
    handler.simple_index_json = True
    preview, receipt, wheel = write_publish_inputs(tmp_path)

    result = registry_execute(preview, receipt, cwd=tmp_path, endpoint=endpoint)

    assert result.returncode == 0, result.stderr
    uploads = [request for request in handler.requests if request["method"] == "POST"]
    assert [request["upload_filename"] for request in uploads].count(wheel.name) == 1


def test_registry_truncated_success_response_is_uncertain_without_traceback(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """registry 成功头后的截断 body 仍是未知结果，receipt 不得误标 not-started。"""

    endpoint, handler = loopback_server
    handler.truncate_response_body_count = 1
    preview, receipt, wheel = write_publish_inputs(tmp_path)
    output = tmp_path / "registry-truncated.json"

    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        output=output,
    )

    assert result.returncode != 0
    uploads = [request for request in handler.requests if request["method"] == "POST"]
    assert [request["upload_filename"] for request in uploads].count(wheel.name) == 1
    failed = json.loads(output.read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["unconfirmed_uploads"][0]["status"] == "uncertain"
    assert "traceback" not in result.stderr.lower()
    assert "exception occurred during processing" not in result.stderr.lower()


def test_registry_truncated_upload_and_unavailable_check_never_reposts(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """upload 响应截断且事后 check 也断连时必须只 POST 一次并进入人工复核。"""

    endpoint, handler = loopback_server
    handler.truncate_response_body_count = 1
    handler.disconnect_check_after_upload_count = 1
    preview, receipt, wheel = write_publish_inputs(tmp_path)
    output = tmp_path / "registry-check-unavailable.json"

    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        output=output,
    )

    assert result.returncode != 0
    uploads = [request for request in handler.requests if request["method"] == "POST"]
    assert [request["upload_filename"] for request in uploads].count(wheel.name) == 1
    failed = json.loads(output.read_text(encoding="utf-8"))
    assert failed["unconfirmed_uploads"][0]["status"] == "uncertain"
    assert "traceback" not in result.stderr.lower()


def test_registry_ignores_inherited_uv_username_password_identity(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """机器继承的 uv 用户名/密码不得覆盖本次批准 token 身份或写入日志。"""

    endpoint, handler = loopback_server
    monkeypatch.setenv("UV_PUBLISH_USERNAME", "inherited-user")
    monkeypatch.setenv("UV_PUBLISH_PASSWORD", "inherited-password-secret")
    preview, receipt, _ = write_publish_inputs(tmp_path)

    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        token="approved-fixture-token",
    )

    assert result.returncode == 0, result.stderr
    uploads = [request for request in handler.requests if request["method"] == "POST"]
    assert {request["basic_username"] for request in uploads} == {"__token__"}
    assert "inherited-password-secret" not in result.stdout + result.stderr


def test_registry_check_hash_mismatch_fails_before_upload(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """同名文件存在但 hash 不同时，uv/check relay 必须在 distribution POST 前 fail closed。"""

    endpoint, handler = loopback_server
    preview, receipt, wheel = write_publish_inputs(tmp_path)
    handler.uploaded_files[wheel.name] = "d" * 64
    result = registry_execute(preview, receipt, cwd=tmp_path, endpoint=endpoint)

    assert result.returncode != 0
    assert [request for request in handler.requests if request["method"] == "POST"] == []
    assert "manual review" in result.stderr.lower()


def test_registry_execute_rejects_uv_version_drift_before_network(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """publish 使用 `--no-config` 时仍须自行核验 uv pin，旧版本不得启动 relay 网络。"""

    endpoint, handler = loopback_server
    preview, receipt, _ = write_publish_inputs(tmp_path)
    fake_uv = tmp_path / "uv-version-drift"
    fake_uv.write_text(
        "#!/bin/sh\nprintf 'uv 0.11.28 (fixture)\\n'\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o700)
    monkeypatch.setenv("UV", str(fake_uv))

    result = registry_execute(preview, receipt, cwd=tmp_path, endpoint=endpoint)

    assert result.returncode != 0
    assert "required uv version is 0.11.29" in result.stderr
    assert handler.requests == []


def test_registry_redirect_fails_closed_without_following_get(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """registry 30x 不得把 artifact POST 改成 GET 或转发认证后误报发布成功。"""

    endpoint, handler = loopback_server
    handler.redirect_location = f"{endpoint}/redirected"
    preview, receipt, _ = write_publish_inputs(tmp_path)

    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-redirect-token",
    )

    assert result.returncode != 0
    uploads = [request for request in handler.requests if request["method"] == "POST"]
    assert len(uploads) == 1
    assert all(request["path"] != "/redirected" for request in handler.requests)
    assert all(
        request.get("authorization") is None
        for request in handler.requests
        if request["method"] == "GET"
    )
    assert "manual review" in result.stderr.lower()


def test_registry_check_redirect_fails_before_distribution_upload(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """check endpoint 的 30x 也不得跟随，且失败时不能向 registry 发送 distribution。"""

    endpoint, handler = loopback_server
    handler.check_redirect_location = f"{endpoint}/redirected-check"
    preview, receipt, _ = write_publish_inputs(tmp_path)
    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-check-redirect-token",
    )

    assert result.returncode != 0
    assert [request for request in handler.requests if request["method"] == "POST"] == []
    assert all(request["path"] != "/redirected-check" for request in handler.requests)
    assert "manual review" in result.stderr.lower()
