"""默认只计划、经双重授权后才上传私有 registry 的发布 wrapper。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from release_models import (
    ReleaseContractError,
    approval_sha256,
    endpoint_sha256,
    read_json,
    redact,
    require_approval_digest,
    required_uv_executable,
    resolve_artifact,
    sha256_file,
    urlopen_no_redirect,
    validate_preview,
    validate_promotion,
    validate_registry_plan,
    validate_release_build,
    verify_artifacts,
    write_json,
)
from release_registry_transport import RegistryRelay


class RegistryUploadError(ReleaseContractError):
    """携带当前 artifact 的确定性上传状态，供失败清单区分不确定与未开始。"""

    def __init__(self, message: str, *, upload_status: str) -> None:
        super().__init__(message)
        self.upload_status = upload_status


class RegistryPublishFailure(ReleaseContractError):
    """携带已去敏的 failed receipt，使 CLI 在无 output 时仍能打印复核清单。"""

    def __init__(self, message: str, *, receipt: dict[str, Any]) -> None:
        super().__init__(message)
        self.receipt = receipt


def _approved(name: str) -> bool:
    """只接受明确的 true，防止非空字符串或继承环境意外授权副作用。"""

    return os.environ.get(name, "").lower() == "true"


def _validate_endpoint(value: str) -> None:
    """生产只接受 HTTPS；loopback HTTP 仅供显式 test mode 的本地替身。"""

    try:
        parsed = urllib.parse.urlparse(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ReleaseContractError("registry endpoint is malformed") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ReleaseContractError("registry endpoint must not contain URL credentials")
    if parsed.query or parsed.fragment:
        raise ReleaseContractError("registry endpoint must not contain query or fragment")
    if parsed.scheme == "https" and parsed.netloc:
        return
    if (
        _approved("RELEASE_TEST_MODE")
        and parsed.scheme == "http"
        and hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        return
    raise ReleaseContractError("registry endpoint must use HTTPS (loopback HTTP is test-only)")


def _validate_same_registry(endpoint: str, check_endpoint: str) -> None:
    """上传和查重必须落在同一 registry authority，避免跨站伪造确认。"""

    try:
        upload = urllib.parse.urlparse(endpoint)
        check = urllib.parse.urlparse(check_endpoint)
        upload_host = upload.hostname
        check_host = check.hostname
        upload_port = upload.port or (443 if upload.scheme == "https" else 80)
        check_port = check.port or (443 if check.scheme == "https" else 80)
    except ValueError as exc:
        raise ReleaseContractError("registry endpoint authority is malformed") from exc
    if (
        upload.scheme.lower() != check.scheme.lower()
        or upload_host is None
        or check_host is None
        or upload_host.lower() != check_host.lower()
        or upload_port != check_port
    ):
        raise ReleaseContractError("registry upload and check endpoints must share an authority")


def _frozen_distribution(directory: Path, *, name: str, body: bytes) -> Path:
    """以 0600、排他创建写入受审 bytes，uv 永远不重新读取可变原路径。"""

    target = directory / name
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return target


def _project_name(filename: str, version: str) -> str:
    """从受审 wheel/sdist 文件名提取 PEP 503 规范化项目名。"""

    wheel_marker = f"-{version}-"
    sdist_marker = f"-{version}.tar.gz"
    if wheel_marker in filename and filename.endswith(".whl"):
        raw = filename.split(wheel_marker, 1)[0]
    elif filename.endswith(sdist_marker):
        raw = filename[: -len(sdist_marker)]
    else:
        raise RegistryUploadError(
            f"artifact filename does not match release version: {filename}",
            upload_status="not-started",
        )
    normalized = "-".join(part for part in raw.replace("_", "-").split("-") if part).lower()
    if not normalized or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in normalized
    ):
        raise RegistryUploadError(
            f"artifact project name is invalid: {filename}",
            upload_status="not-started",
        )
    return normalized


def _request_positive_check(relay: RegistryRelay, *, project: str) -> None:
    """在 uv 零退出后主动触发受限 simple-index check，由 relay 解析 hash。"""

    request = urllib.request.Request(
        f"{relay.check_url}/{project}/",
        headers={"Accept": "application/vnd.pypi.simple.v1+json, text/html"},
    )
    try:
        with urlopen_no_redirect(request, timeout=10, bypass_proxy=True) as response:
            response.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        # relay state 已记录外部 check 的 redirect/connection/status；调用方统一
        # 以“未取得同名同 hash 正向证据”收口，不能把 check 异常当作上传失败重试。
        return


def _upload(
    endpoint: str,
    check_endpoint: str,
    token: str,
    path: Path,
    checksum: str,
    version: str,
) -> dict[str, object]:
    """用固定 uv 上传冻结 distribution；relay 禁止 redirect 并限制安全重试。"""

    # 同一份 bytes 同时用于校验和上传，避免路径在两次读取间被替换后产生
    # “未受审 body + 受审 checksum header”的 TOCTOU 窗口。
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise RegistryUploadError(
            f"artifact became unreadable before upload: {path.name}",
            upload_status="not-started",
        ) from exc
    if hashlib.sha256(body).hexdigest() != checksum:
        raise RegistryUploadError(
            f"artifact checksum drift before upload: {path.name}",
            upload_status="not-started",
        )
    project = _project_name(path.name, version)
    try:
        executable = required_uv_executable()
    except ReleaseContractError as exc:
        raise RegistryUploadError(str(exc), upload_status="not-started") from exc
    with TemporaryDirectory(prefix="agent-harness-publish-") as temporary:
        frozen = _frozen_distribution(Path(temporary), name=path.name, body=body)
        with RegistryRelay(
            upload_endpoint=endpoint,
            check_endpoint=check_endpoint,
            expected_filename=path.name,
            expected_sha256=checksum,
        ) as relay:
            environment = os.environ.copy()
            for inherited_identity in (
                "UV_PUBLISH_USERNAME",
                "UV_PUBLISH_PASSWORD",
                "UV_PUBLISH_INDEX",
                "UV_KEYRING_PROVIDER",
            ):
                # 发布身份只允许来自本次冻结 plan 的 token/endpoint；继承的 uv
                # 用户名、密码或 index 不得改变认证语义或造成机器相关失败。
                environment.pop(inherited_identity, None)
            environment.update(
                {
                    "UV_PUBLISH_URL": relay.publish_url,
                    "UV_PUBLISH_CHECK_URL": relay.check_url,
                    "UV_PUBLISH_TOKEN": token,
                    "UV_INSECURE_HOST": relay.authority,
                    "UV_HTTP_RETRIES": "0",
                    "UV_NO_PROGRESS": "1",
                }
            )
            no_proxy = environment.get("NO_PROXY", environment.get("no_proxy", ""))
            environment["NO_PROXY"] = ",".join(
                value for value in (no_proxy, "127.0.0.1", "localhost", "::1") if value
            )
            for attempt in range(1, 4):
                before = relay.state.snapshot()
                try:
                    result = subprocess.run(
                        [
                            executable,
                            "publish",
                            "--trusted-publishing",
                            "never",
                            "--no-attestations",
                            "--no-progress",
                            "--no-config",
                            str(frozen),
                        ],
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=60,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RegistryUploadError(
                        f"manual review required: uv publish timed out for {path.name}",
                        upload_status="uncertain",
                    ) from exc
                after = relay.state.snapshot()
                new_upload_statuses = relay.state.upload_statuses[before[0] : after[0]]
                new_check_statuses = relay.state.check_statuses[before[1] : after[1]]
                connection_error_routes = relay.state.connection_error_routes[before[3] : after[3]]
                if after[2] > before[2]:
                    raise RegistryUploadError(
                        f"manual review required: registry redirect rejected for {path.name}",
                        upload_status="rejected",
                    )
                if after[4] > before[4]:
                    raise RegistryUploadError(
                        f"manual review required: uncertain partial upload for {path.name}",
                        upload_status="uncertain",
                    )
                if after[5] > before[5]:
                    raise RegistryUploadError(
                        f"manual review required: unsafe upload replay blocked for {path.name}",
                        upload_status="uncertain",
                    )
                if result.returncode == 0:
                    if after[6] == before[6]:
                        _request_positive_check(relay, project=project)
                        after = relay.state.snapshot()
                    if after[6] == before[6]:
                        # uv 的零退出码只说明客户端流程没有报错，不能证明 registry
                        # 已持久化目标 bytes。必须由 relay 在上传后的 check 中解析出
                        # 同 filename + SHA-256，才能把冻结 artifact 标记为 confirmed。
                        raise RegistryUploadError(
                            f"manual review required: registry did not confirm {path.name}",
                            upload_status="uncertain",
                        )
                    return {"status": "confirmed"}
                if "upload" in connection_error_routes:
                    # POST body 可能已被 registry 持久化；即使随后 check 为空，也
                    # 不能把最终一致性窗口误当作“确认不存在”并自动重放。
                    raise RegistryUploadError(
                        f"manual review required: upload response is unknown for {path.name}",
                        upload_status="uncertain",
                    )
                if "check" in connection_error_routes and new_upload_statuses:
                    raise RegistryUploadError(
                        f"manual review required: upload could not be confirmed for {path.name}",
                        upload_status="uncertain",
                    )
                status = new_upload_statuses[-1] if new_upload_statuses else None
                # uv 在确定的 429/5xx 后会再次查询 check URL。只有第二次 check
                # 明确成功且仍未找到同名同 hash 时，才允许重放相同冻结 bytes。
                checked_absent_after_upload = (
                    len(new_check_statuses) >= 2 and new_check_statuses[-1] == 200
                )
                transient = (
                    status == 429 or (status is not None and 500 <= status < 600)
                ) and checked_absent_after_upload
                transient = transient or (
                    connection_error_routes == ["check"] and not new_upload_statuses
                )
                if transient and attempt < 3:
                    continue
                if status is not None and 400 <= status < 500:
                    upload_status = "rejected"
                    detail = f"registry status {status}"
                elif transient and new_upload_statuses:
                    upload_status = "uncertain"
                    detail = "registry transient failure without confirmed upload"
                elif transient:
                    upload_status = "not-started"
                    detail = "registry check unavailable before upload"
                elif not new_upload_statuses:
                    upload_status = "not-started"
                    detail = "uv rejected distribution before upload"
                else:
                    upload_status = "uncertain"
                    detail = "registry result could not be confirmed"
                raise RegistryUploadError(
                    f"manual review required: {detail} for {path.name}",
                    upload_status=upload_status,
                )
    raise RegistryUploadError(
        "manual review required: publish state unavailable",
        upload_status="uncertain",
    )


def _inventory_item(item: dict[str, Any], *, status: str) -> dict[str, object]:
    """复制公开 artifact 身份并附上传状态，不携带 endpoint、响应正文或 credential。"""

    return {
        "path": item["path"],
        "kind": item["kind"],
        "sha256": item["sha256"],
        "size": item["size"],
        "status": status,
    }


def _inventory_summary(receipt: dict[str, Any]) -> list[str]:
    """把 failed receipt 压成稳定的 path@SHA 清单，供无 output 的人工复核。"""

    lines: list[str] = []
    for field in ("confirmed_uploads", "unconfirmed_uploads"):
        raw_items = receipt.get(field, [])
        items = cast(list[object], raw_items) if isinstance(raw_items, list) else []
        values: list[str] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            item = cast(dict[str, object], raw)
            values.append(f"{item.get('path')}@{item.get('sha256')}[{item.get('status')}]")
        lines.append(f"{field}: {', '.join(values) if values else '(none)'}")
    return lines


def _protected_ref_evidence(promotion: dict[str, Any]) -> dict[str, str]:
    """把 CI 的 protected-ref 证明闭合到 promotion 的 release commit 与完整 tag ref。"""

    name = os.environ.get("RELEASE_PROTECTED_REF_NAME", "")
    sha = os.environ.get("RELEASE_PROTECTED_REF_SHA", "")
    expected_name = f"refs/tags/{promotion['tag']}"
    expected_sha = str(promotion["release_commit_sha"])
    if name != expected_name or sha != expected_sha or sha != promotion["tag_target_sha"]:
        raise ReleaseContractError(
            "protected ref evidence does not match the promoted release commit/ref"
        )
    return {"name": name, "sha": sha}


def _approval_payload(
    *,
    preview_path: Path,
    receipt_path: Path,
    build_path: Path,
    promotion: dict[str, Any],
    endpoint: str,
    check_endpoint: str,
    protected_ref: dict[str, str],
    artifacts: list[dict[str, Any]],
) -> dict[str, object]:
    """冻结 publish 的跨 job 身份；endpoint 只以摘要进入 plan，credential 永不入 payload。"""

    return {
        "schema_version": "release-approval/v1",
        "operation": "registry-publish",
        "preview_manifest_sha256": sha256_file(preview_path),
        "promotion_receipt_sha256": sha256_file(receipt_path),
        "release_build_manifest_sha256": sha256_file(build_path),
        "release_commit_sha": str(promotion["release_commit_sha"]),
        "tag": str(promotion["tag"]),
        "registry_endpoint_sha256": endpoint_sha256(endpoint),
        "registry_check_endpoint_sha256": endpoint_sha256(check_endpoint),
        "protected_ref": protected_ref,
        "artifacts": cast(list[dict[str, object]], artifacts),
    }


def publish(
    *,
    preview_path: Path,
    receipt_path: Path,
    build_path: Path | None = None,
    execute: bool,
    artifact_root: Path,
    output_path: Path | None,
    plan_path: Path | None = None,
) -> dict[str, Any]:
    """验证所有身份后输出 plan，或在显式 execute 下顺序上传并保存去敏回执。"""

    # build 状态先于 preview、credential 和 endpoint 读取，非法正式产物必须在
    # 所有敏感输入与网络 seam 之前 fail closed。
    promotion_hint: dict[str, Any] | None = None
    if build_path is None:
        promotion_hint = read_json(receipt_path)
        raw_build_path = promotion_hint.get("release_build_manifest")
        if not isinstance(raw_build_path, str) or not raw_build_path:
            raise ReleaseContractError(
                "promotion receipt does not identify a release build manifest"
            )
        build_path = (artifact_root / raw_build_path).resolve()
        try:
            build_path.relative_to(artifact_root.resolve())
        except ValueError as exc:
            raise ReleaseContractError("release build manifest path escapes artifact root") from exc
    build = read_json(build_path)
    validate_release_build(build)
    preview = read_json(preview_path)
    promotion = promotion_hint or read_json(receipt_path)
    validate_preview(preview)
    validate_promotion(
        preview,
        promotion,
        build,
        preview_path=preview_path,
        build_path=build_path,
    )
    artifacts = verify_artifacts(build, base=artifact_root)
    artifacts = [item for item in artifacts if item.get("kind") in {"wheel", "sdist"}]
    endpoint = os.environ.get("UV_PUBLISH_URL", "")
    if not endpoint:
        raise ReleaseContractError("UV_PUBLISH_URL is required to identify the plan")
    _validate_endpoint(endpoint)
    check_endpoint = os.environ.get("UV_PUBLISH_CHECK_URL", "")
    if not check_endpoint:
        raise ReleaseContractError("UV_PUBLISH_CHECK_URL is required to identify the plan")
    _validate_endpoint(check_endpoint)
    _validate_same_registry(endpoint, check_endpoint)
    protected_ref = _protected_ref_evidence(promotion)
    approval = _approval_payload(
        preview_path=preview_path,
        receipt_path=receipt_path,
        build_path=build_path,
        promotion=promotion,
        endpoint=endpoint,
        check_endpoint=check_endpoint,
        protected_ref=protected_ref,
        artifacts=artifacts,
    )
    reviewed_approval = approval_sha256(approval)
    plan: dict[str, Any] = {
        "schema_version": "registry-publish-plan/v1",
        "status": "planned",
        "version": preview["next_version"],
        "tag": preview["tag"],
        "artifacts": artifacts,
        "approval": approval,
        "approval_sha256": reviewed_approval,
    }
    if not execute:
        if plan_path is not None:
            write_json(plan_path, plan)
        return plan
    if plan_path is None:
        raise ReleaseContractError("registry execute requires --plan-input")
    reviewed_plan = read_json(plan_path)
    validate_registry_plan(reviewed_plan)
    if reviewed_plan != plan:
        raise ReleaseContractError("registry approval plan identity drift")
    if not _approved("REGISTRY_PUBLISH_APPROVED"):
        raise ReleaseContractError("registry execute requires REGISTRY_PUBLISH_APPROVED=true")
    require_approval_digest("REGISTRY_PUBLISH_APPROVAL_SHA256", reviewed_approval)
    if not _approved("RELEASE_PROTECTED_REF"):
        raise ReleaseContractError("registry execute requires protected ref evidence")
    token = os.environ.get("UV_PUBLISH_TOKEN", "")
    if not token:
        raise ReleaseContractError("restricted UV_PUBLISH_TOKEN environment credential is required")
    results: list[dict[str, object]] = []
    confirmed: list[dict[str, object]] = []
    for index, item in enumerate(artifacts):
        path = resolve_artifact(artifact_root, item)
        try:
            result = _upload(
                endpoint,
                check_endpoint,
                token,
                path,
                str(item["sha256"]),
                str(preview["next_version"]),
            )
        except RegistryUploadError as exc:
            failure = redact(str(exc), token, endpoint)
            unconfirmed = [_inventory_item(item, status=exc.upload_status)]
            unconfirmed.extend(
                _inventory_item(pending, status="not-started") for pending in artifacts[index + 1 :]
            )
            failed: dict[str, Any] = {
                "schema_version": "registry-publish/v1",
                "status": "failed",
                "version": preview["next_version"],
                "tag": preview["tag"],
                "confirmed_uploads": confirmed,
                "unconfirmed_uploads": unconfirmed,
                "failure": failure,
            }
            if output_path is not None:
                write_json(output_path, failed)
            raise RegistryPublishFailure(failure, receipt=failed) from exc
        results.append(
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "status": result["status"],
            }
        )
        confirmed.append(_inventory_item(item, status="confirmed"))
    published = {**plan, "status": "published", "uploads": results}
    if output_path is not None:
        write_json(output_path, published)
    return published


def main() -> int:
    """提供 CI 可复用 CLI；所有 credential 仅从环境读取并在异常路径二次脱敏。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--promotion-receipt", required=True, type=Path)
    parser.add_argument("--build-manifest", type=Path)
    parser.add_argument("--artifact-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    plan_group = parser.add_mutually_exclusive_group()
    plan_group.add_argument("--plan-output", type=Path)
    plan_group.add_argument("--plan-input", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("UV_PUBLISH_TOKEN", "")
    try:
        result = publish(
            preview_path=args.manifest.resolve(),
            receipt_path=args.promotion_receipt.resolve(),
            build_path=args.build_manifest.resolve() if args.build_manifest else None,
            execute=args.execute,
            artifact_root=args.artifact_root.resolve(),
            output_path=args.output.resolve() if args.output else None,
            plan_path=(args.plan_input or args.plan_output).resolve()
            if (args.plan_input or args.plan_output)
            else None,
        )
    except RegistryPublishFailure as exc:
        print(redact(f"registry publish failed: {exc}", token), file=sys.stderr)
        for line in _inventory_summary(exc.receipt):
            print(redact(line, token), file=sys.stderr)
        return 2
    except ReleaseContractError as exc:
        print(redact(f"registry publish failed: {exc}", token), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
