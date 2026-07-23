"""私有 registry 发布门禁、重试、回执与上传行为合同。"""

# pyright: reportPrivateUsage=false
# TOCTOU 合同必须在实际 bytes 读取边界注入竞争；公开 CLI 的子进程无法提供该确定性时序。

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

# 生产脚本不是安装包；合同测试显式暴露 scripts seam，避免复制上传逻辑自证。
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from release_contract_test_support import (  # noqa: E402
    PUBLISH,
    ROOT,
    RegistryHandler,
    loopback_server_fixture,
    registry_execute,
    run,
    write_publish_inputs,
)
from scripts.registry_publish import (  # noqa: E402  # pyright: ignore[reportPrivateUsage]
    RegistryPublishFailure,
    RegistryUploadError,
    _upload,
    publish,
)
from scripts.release_models import ReleaseContractError, redact  # noqa: E402

REGISTRY_MODULE = importlib.import_module("scripts.registry_publish")


@pytest.fixture
def loopback_server() -> Generator[tuple[str, type[RegistryHandler]], None, None]:
    """复用共享 loopback 生命周期，同时让 pytest 在当前测试模块发现 fixture。"""

    yield from loopback_server_fixture()


def test_registry_plan_is_default_and_execute_requires_all_gates(tmp_path: Path) -> None:
    """registry 默认仅给去敏计划，execute 缺批准或 protected ref 时必须在网络前拒绝。"""

    preview, receipt, _ = write_publish_inputs(tmp_path)
    promotion = json.loads(receipt.read_text(encoding="utf-8"))
    endpoint = "https://registry.invalid/legacy/"
    check_endpoint = "https://registry.invalid/simple"
    plan = run(
        sys.executable,
        str(PUBLISH),
        "--manifest",
        str(preview),
        "--promotion-receipt",
        str(receipt),
        "--artifact-root",
        str(tmp_path),
        cwd=ROOT,
        env={
            "UV_PUBLISH_URL": endpoint,
            "UV_PUBLISH_CHECK_URL": check_endpoint,
            "RELEASE_PROTECTED_REF_NAME": f"refs/tags/{promotion['tag']}",
            "RELEASE_PROTECTED_REF_SHA": promotion["release_commit_sha"],
        },
    )
    assert plan.returncode == 0, plan.stderr
    planned = json.loads(plan.stdout)
    assert planned["status"] == "planned"
    assert len(planned["approval_sha256"]) == 64
    assert endpoint not in plan.stdout + plan.stderr
    rejected = run(
        sys.executable,
        str(PUBLISH),
        "--manifest",
        str(preview),
        "--promotion-receipt",
        str(receipt),
        "--artifact-root",
        str(tmp_path),
        "--execute",
        cwd=ROOT,
        env={"UV_PUBLISH_TOKEN": "fixture-secret", "UV_PUBLISH_URL": "https://registry.invalid"},
    )
    assert rejected.returncode != 0
    assert "fixture-secret" not in rejected.stdout + rejected.stderr


def test_cli_plan_drift_error_does_not_read_registry_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI 入口必须先让 publish 校验 plan identity，普通合同失败不得读取 token。"""

    token_reads: list[str] = []
    original_get = REGISTRY_MODULE.os.environ.get

    def tracking_get(key: str, default: str | None = None) -> str | None:
        """记录敏感键读取，区分“未泄漏”与“根本未读取”。"""

        if key == "UV_PUBLISH_TOKEN":
            token_reads.append(key)
        return original_get(key, default)

    def reject_plan_drift(**_kwargs: object) -> dict[str, object]:
        """模拟 publish 在 credential 门禁前发现受审 plan identity 漂移。"""

        raise ReleaseContractError("registry approval plan identity drift")

    monkeypatch.setenv("UV_PUBLISH_TOKEN", "must-not-be-read")
    monkeypatch.setattr(REGISTRY_MODULE.os.environ, "get", tracking_get)
    monkeypatch.setattr(REGISTRY_MODULE, "publish", reject_plan_drift)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "registry_publish.py",
            "--manifest",
            str(tmp_path / "preview.json"),
            "--promotion-receipt",
            str(tmp_path / "receipt.json"),
        ],
    )

    assert REGISTRY_MODULE.main() == 2
    assert token_reads == []


def test_registry_execute_rejects_approved_uv_patch_drift_before_credential_or_network(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """plan 绑定实际 uv patch；execute 换成另一受支持 patch 也必须重新审批。"""

    endpoint, handler = loopback_server
    preview, receipt, _ = write_publish_inputs(tmp_path)
    promotion = json.loads(receipt.read_text(encoding="utf-8"))
    build = tmp_path / str(promotion["release_build_manifest"])
    plan_path = tmp_path / "registry-plan.json"
    uv_29 = tmp_path / "uv-0.11.29"
    uv_31 = tmp_path / "uv-0.11.31"
    credential_probe = tmp_path / "uv-version-probe-credential.txt"
    for executable, version in ((uv_29, "0.11.29"), (uv_31, "0.11.31")):
        credential_check = ""
        if version == "0.11.31":
            # execute 阶段会把 registry token 放入父进程环境；待验证的 uv 身份探测
            # 不得继承该凭据，否则漂移拒绝发生前 executable 已经能够读取密钥。
            credential_check = (
                'if [ -n "${UV_PUBLISH_TOKEN+x}" ]; then '
                f"printf '%s' \"$UV_PUBLISH_TOKEN\" > '{credential_probe}'; fi\n"
            )
        executable.write_text(
            f"#!/bin/sh\n{credential_check}printf 'uv {version} (fixture)\\n'\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
    arguments = (
        sys.executable,
        str(PUBLISH),
        "--manifest",
        str(preview),
        "--promotion-receipt",
        str(receipt),
        "--build-manifest",
        str(build),
        "--artifact-root",
        str(tmp_path),
    )
    evidence = {
        "RELEASE_TEST_MODE": "true",
        "UV_PUBLISH_URL": endpoint,
        "UV_PUBLISH_CHECK_URL": f"{endpoint}/simple",
        "RELEASE_PROTECTED_REF_NAME": f"refs/tags/{promotion['tag']}",
        "RELEASE_PROTECTED_REF_SHA": str(promotion["release_commit_sha"]),
    }
    planned = run(
        *arguments,
        "--plan-output",
        str(plan_path),
        cwd=ROOT,
        env={**evidence, "UV": str(uv_29)},
    )
    assert planned.returncode == 0, planned.stderr
    plan = json.loads(planned.stdout)
    assert plan["uv_version"] == "0.11.29"
    assert plan["approval"]["uv_version"] == "0.11.29"

    executed = run(
        *arguments,
        "--plan-input",
        str(plan_path),
        "--execute",
        cwd=ROOT,
        env={
            **evidence,
            "UV": str(uv_31),
            "REGISTRY_PUBLISH_APPROVED": "true",
            "REGISTRY_PUBLISH_APPROVAL_SHA256": str(plan["approval_sha256"]),
            "RELEASE_PROTECTED_REF": "true",
            "UV_PUBLISH_TOKEN": "must-not-be-read",
        },
    )

    assert executed.returncode != 0
    assert "plan identity drift" in executed.stderr
    assert "must-not-be-read" not in executed.stdout + executed.stderr
    assert not credential_probe.exists()
    assert handler.requests == []


def test_registry_rejects_cross_authority_check_endpoint_before_network(tmp_path: Path) -> None:
    """upload 与 check 不得分属不同 registry authority。"""

    preview, receipt, _ = write_publish_inputs(tmp_path)
    promotion = json.loads(receipt.read_text(encoding="utf-8"))
    result = run(
        sys.executable,
        str(PUBLISH),
        "--manifest",
        str(preview),
        "--promotion-receipt",
        str(receipt),
        "--artifact-root",
        str(tmp_path),
        cwd=ROOT,
        env={
            "RELEASE_TEST_MODE": "true",
            "UV_PUBLISH_URL": "http://127.0.0.1:8001/legacy/",
            "UV_PUBLISH_CHECK_URL": "http://127.0.0.1:8002/simple",
            "RELEASE_PROTECTED_REF_NAME": f"refs/tags/{promotion['tag']}",
            "RELEASE_PROTECTED_REF_SHA": promotion["release_commit_sha"],
        },
    )

    assert result.returncode != 0
    assert "share an authority" in result.stderr


def test_registry_rejects_endpoint_or_protected_release_ref_drift_before_network(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
) -> None:
    """批准身份必须闭合 preview、promotion receipt、endpoint、artifacts 与 release ref。"""

    endpoint, handler = loopback_server
    preview, receipt, _ = write_publish_inputs(tmp_path)
    promotion = json.loads(receipt.read_text(encoding="utf-8"))
    base_env = {
        "REGISTRY_PUBLISH_APPROVED": "true",
        "RELEASE_PROTECTED_REF": "true",
        "RELEASE_PROTECTED_REF_NAME": f"refs/tags/{promotion['tag']}",
        "RELEASE_PROTECTED_REF_SHA": promotion["release_commit_sha"],
        "RELEASE_TEST_MODE": "true",
        "UV_PUBLISH_URL": endpoint,
        "UV_PUBLISH_CHECK_URL": f"{endpoint}/simple",
        "UV_PUBLISH_TOKEN": "fixture-token",
    }
    arguments = [
        sys.executable,
        str(PUBLISH),
        "--manifest",
        str(preview),
        "--promotion-receipt",
        str(receipt),
        "--artifact-root",
        str(tmp_path),
    ]
    plan_path = tmp_path / "registry-plan.json"
    plan = run(*arguments, "--plan-output", str(plan_path), cwd=ROOT, env=base_env)
    assert plan.returncode == 0, plan.stderr
    approval_sha256 = str(json.loads(plan.stdout)["approval_sha256"])

    endpoint_drift = run(
        *arguments,
        "--plan-input",
        str(plan_path),
        "--execute",
        cwd=ROOT,
        env={
            **base_env,
            "REGISTRY_PUBLISH_APPROVAL_SHA256": approval_sha256,
            "UV_PUBLISH_URL": f"{endpoint}/changed",
        },
    )
    assert endpoint_drift.returncode != 0
    assert "approval" in endpoint_drift.stderr.lower()
    assert handler.requests == []

    check_endpoint_drift = run(
        *arguments,
        "--plan-input",
        str(plan_path),
        "--execute",
        cwd=ROOT,
        env={
            **base_env,
            "REGISTRY_PUBLISH_APPROVAL_SHA256": approval_sha256,
            "UV_PUBLISH_CHECK_URL": f"{endpoint}/replacement-simple",
        },
    )
    assert check_endpoint_drift.returncode != 0
    assert "approval" in check_endpoint_drift.stderr.lower()
    assert handler.requests == []

    original_receipt = receipt.read_text(encoding="utf-8")
    changed_receipt = json.loads(original_receipt)
    changed_receipt["provider_release_id"] = "replacement-release"
    receipt.write_text(json.dumps(changed_receipt, sort_keys=True), encoding="utf-8")
    receipt_drift = run(
        *arguments,
        "--plan-input",
        str(plan_path),
        "--execute",
        cwd=ROOT,
        env={
            **base_env,
            "REGISTRY_PUBLISH_APPROVAL_SHA256": approval_sha256,
        },
    )
    assert receipt_drift.returncode != 0
    assert "approval" in receipt_drift.stderr.lower()
    assert handler.requests == []
    receipt.write_text(original_receipt, encoding="utf-8")

    ref_drift = run(
        *arguments,
        "--plan-input",
        str(plan_path),
        "--execute",
        cwd=ROOT,
        env={
            **base_env,
            "REGISTRY_PUBLISH_APPROVAL_SHA256": approval_sha256,
            "RELEASE_PROTECTED_REF_SHA": "d" * 40,
        },
    )
    assert ref_drift.returncode != 0
    assert "protected ref" in ref_drift.stderr.lower()
    assert handler.requests == []


def test_registry_rejects_bytes_changed_between_preflight_and_upload(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上传函数必须校验实际发送的同一份 bytes，不能让路径二次读取产生 TOCTOU。"""

    endpoint, handler = loopback_server
    artifact = tmp_path / "artifact.whl"
    reviewed = b"reviewed-wheel-bytes"
    artifact.write_bytes(reviewed)
    checksum = hashlib.sha256(reviewed).hexdigest()
    original_read_bytes = Path.read_bytes

    def substitute_unreviewed_bytes(path: Path) -> bytes:
        """只在 upload 读取目标 artifact 时注入与磁盘受审内容不同的 bytes。"""

        if path == artifact:
            return b"unreviewed-wheel-bytes"
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", substitute_unreviewed_bytes)

    with pytest.raises(RegistryUploadError, match="checksum drift"):
        _upload(
            endpoint,
            f"{endpoint}/simple",
            "fixture-token",
            artifact,
            checksum,
            "0.2.0",
            "uv",
        )
    assert handler.requests == []


def test_registry_unreadable_artifact_after_preflight_writes_upload_inventory(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二件产物预检后不可读时，须保留首件确认状态和机器可读失败清单。"""

    endpoint, handler = loopback_server
    preview_path, receipt_path, _ = write_publish_inputs(tmp_path)
    promotion = json.loads(receipt_path.read_text(encoding="utf-8"))
    publishable = promotion["artifacts"]
    second_path = tmp_path / publishable[1]["path"]
    output = tmp_path / "unreadable-artifact-failed.json"
    plan_path = tmp_path / "registry-plan.json"
    monkeypatch.setenv("RELEASE_TEST_MODE", "true")
    monkeypatch.setenv("UV_PUBLISH_URL", endpoint)
    monkeypatch.setenv("UV_PUBLISH_CHECK_URL", f"{endpoint}/simple")
    monkeypatch.setenv("RELEASE_PROTECTED_REF_NAME", f"refs/tags/{promotion['tag']}")
    monkeypatch.setenv("RELEASE_PROTECTED_REF_SHA", str(promotion["release_commit_sha"]))
    plan = publish(
        preview_path=preview_path,
        receipt_path=receipt_path,
        execute=False,
        artifact_root=tmp_path,
        output_path=None,
        plan_path=plan_path,
    )
    monkeypatch.setenv("REGISTRY_PUBLISH_APPROVED", "true")
    monkeypatch.setenv("REGISTRY_PUBLISH_APPROVAL_SHA256", str(plan["approval_sha256"]))
    monkeypatch.setenv("RELEASE_PROTECTED_REF", "true")
    monkeypatch.setenv("UV_PUBLISH_TOKEN", "fixture-token")
    original_read_bytes = Path.read_bytes

    def fail_second_artifact(path: Path) -> bytes:
        """只在 upload 二次读取 sdist 时模拟文件消失，预检仍读取真实文件。"""

        if path == second_path:
            raise FileNotFoundError(second_path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_second_artifact)

    with pytest.raises(RegistryPublishFailure, match="unreadable"):
        publish(
            preview_path=preview_path,
            receipt_path=receipt_path,
            execute=True,
            artifact_root=tmp_path,
            output_path=output,
            plan_path=plan_path,
        )

    assert len([request for request in handler.requests if request["method"] == "POST"]) == 1
    failed = json.loads(output.read_text(encoding="utf-8"))
    assert [item["status"] for item in failed["confirmed_uploads"]] == ["confirmed"]
    assert [item["status"] for item in failed["unconfirmed_uploads"]] == ["not-started"]


def test_release_diagnostics_redact_https_remote_userinfo() -> None:
    """Git push 诊断中的 URL userinfo 必须整体去敏，不能依赖调用方知道 remote 密码。"""

    remote = "https://release-user:opaque-value@provider.invalid/repository.git"
    sanitized = redact(f"git push failed: fatal: unable to access {remote}")
    assert "release-user" not in sanitized
    assert "opaque-value" not in sanitized
    assert "https://[REDACTED]@provider.invalid/repository.git" in sanitized


@pytest.mark.parametrize("status", [400, 401, 409, 202])
def test_registry_non_retryable_or_partial_result_fails_closed(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    status: int,
) -> None:
    """认证、冲突、输入错误与部分上传不确定状态不得自动重试或伪装成功。"""

    endpoint, handler = loopback_server
    handler.status_sequence = [status]
    preview, receipt, _ = write_publish_inputs(tmp_path)
    result = registry_execute(
        preview,
        receipt,
        cwd=tmp_path,
        endpoint=endpoint,
        token="fixture-token",
    )
    assert result.returncode != 0
    assert len([request for request in handler.requests if request["method"] == "POST"]) == 1
    assert "manual review" in (result.stdout + result.stderr).lower()


@pytest.mark.parametrize(
    ("status_sequence", "confirmed_count", "unconfirmed_statuses"),
    [([202], 0, ["uncertain", "not-started"]), ([200, 202], 1, ["uncertain"])],
)
def test_registry_partial_failure_writes_machine_readable_upload_inventory(
    tmp_path: Path,
    loopback_server: tuple[str, type[RegistryHandler]],
    status_sequence: list[int],
    confirmed_count: int,
    unconfirmed_statuses: list[str],
) -> None:
    """首件或第二件不确定时必须停止，并持久化已确认/未确认 artifact 清单。"""

    endpoint, handler = loopback_server
    handler.status_sequence = status_sequence.copy()
    preview_path, receipt_path, _ = write_publish_inputs(tmp_path)
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    promotion = json.loads(receipt_path.read_text(encoding="utf-8"))
    publishable = promotion["artifacts"]
    output = tmp_path / "registry-failed.json"
    token = "fixture-sensitive-token"
    result = registry_execute(
        preview_path,
        receipt_path,
        cwd=tmp_path,
        endpoint=endpoint,
        token=token,
        output=output,
    )

    assert result.returncode != 0
    uploads = [request for request in handler.requests if request["method"] == "POST"]
    assert len(uploads) == confirmed_count + 1
    assert [request["checksum"] for request in uploads] == [
        item["sha256"] for item in publishable[: confirmed_count + 1]
    ]
    failed = json.loads(output.read_text(encoding="utf-8"))
    assert failed["schema_version"] == "registry-publish/v1"
    assert failed["status"] == "failed"
    assert (failed["version"], failed["tag"]) == (preview["next_version"], preview["tag"])
    assert [item["path"] for item in failed["confirmed_uploads"]] == [
        item["path"] for item in publishable[:confirmed_count]
    ]
    assert [item["status"] for item in failed["confirmed_uploads"]] == [
        "confirmed"
    ] * confirmed_count
    assert [item["path"] for item in failed["unconfirmed_uploads"]] == [
        item["path"] for item in publishable[confirmed_count:]
    ]
    assert [item["status"] for item in failed["unconfirmed_uploads"]] == unconfirmed_statuses
    for item in [*failed["confirmed_uploads"], *failed["unconfirmed_uploads"]]:
        assert {"path", "kind", "sha256", "size", "status"} <= item.keys()
    serialized = output.read_text(encoding="utf-8") + result.stdout + result.stderr
    assert token not in serialized
    assert endpoint not in serialized


def test_registry_partial_failure_without_output_prints_safe_upload_inventory(
    tmp_path: Path, loopback_server: tuple[str, type[RegistryHandler]]
) -> None:
    """未传 output 时，stderr 仍须列出已确认与未确认 path+SHA 供人工复核。"""

    endpoint, handler = loopback_server
    handler.status_sequence = [200, 202]
    preview_path, receipt_path, _ = write_publish_inputs(tmp_path)
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    publishable = [item for item in preview["artifacts"] if item["kind"] in {"wheel", "sdist"}]
    token = "fixture-sensitive-token"
    result = registry_execute(
        preview_path,
        receipt_path,
        cwd=tmp_path,
        endpoint=endpoint,
        token=token,
    )

    assert result.returncode != 0
    assert len([request for request in handler.requests if request["method"] == "POST"]) == 2
    assert "confirmed_uploads" in result.stderr
    assert "unconfirmed_uploads" in result.stderr
    for item in publishable:
        assert item["path"] in result.stderr
        assert item["sha256"] in result.stderr
    assert token not in result.stdout + result.stderr
    assert endpoint not in result.stdout + result.stderr
