"""执行冻结 distribution 的私有 registry 上传状态机。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from release_models import (
    ReleaseContractError,
    required_uv_executable,
    urlopen_no_redirect,
)
from release_registry_transport import RegistryRelay


class RegistryUploadError(ReleaseContractError):
    """携带当前 artifact 的确定性上传状态，供失败清单区分不确定与未开始。"""

    def __init__(self, message: str, *, upload_status: str) -> None:
        super().__init__(message)
        self.upload_status = upload_status


def approved(name: str) -> bool:
    """只接受明确的 true，防止非空字符串或继承环境意外授权副作用。"""

    return os.environ.get(name, "").lower() == "true"


def validate_endpoint(value: str) -> None:
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
        approved("RELEASE_TEST_MODE")
        and parsed.scheme == "http"
        and hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        return
    raise ReleaseContractError("registry endpoint must use HTTPS (loopback HTTP is test-only)")


def validate_same_registry(endpoint: str, check_endpoint: str) -> None:
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


def upload(
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


__all__ = [
    "RegistryUploadError",
    "approved",
    "upload",
    "validate_endpoint",
    "validate_same_registry",
]
