"""发布 provider 的 no-redirect HTTP transport 与不可信响应收口。"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import cast

from release_models import ReleaseContractError, redact, urlopen_no_redirect


def _validate_provider_release_url(value: str) -> None:
    """校验最终会写入跨 job receipt 的 URL；raw 与去敏结果必须同样合法。"""

    try:
        parsed = urllib.parse.urlparse(value)
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
    except ValueError as exc:
        # 畸形 IPv6 bracket 等 parser 错误发生在 provider POST 之后，必须转成
        # promotion 合同异常，才能写入保留 commit/tag 身份的 failed receipt。
        raise ReleaseContractError("provider success response URL is invalid") from exc
    loopback_http = (
        os.environ.get("RELEASE_TEST_MODE", "").lower() == "true"
        and parsed.scheme == "http"
        and hostname in {"127.0.0.1", "localhost", "::1"}
    )
    if (
        username is not None
        or password is not None
        or not parsed.netloc
        or (parsed.scheme != "https" and not loopback_http)
    ):
        raise ReleaseContractError("provider success response URL is invalid")


def create_provider_release(
    endpoint: str,
    token: str,
    payload: dict[str, object],
) -> dict[str, str]:
    """创建 provider release；任何非确定成功都交由人工复核且不自动重放。"""

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen_no_redirect(request, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
            if (
                response.status == 202
                or response.headers.get("X-Upload-State", "complete") != "complete"
            ):
                raise ReleaseContractError("provider result is uncertain; manual review required")
            if not 200 <= response.status < 300:
                raise ReleaseContractError(
                    f"provider status {response.status} is not a confirmed success; "
                    "manual review required"
                )
    except urllib.error.HTTPError as exc:
        raise ReleaseContractError(
            f"provider status {exc.code} is not a confirmed success; manual review required"
        ) from exc
    except (
        urllib.error.URLError,
        TimeoutError,
        ConnectionError,
        OSError,
        http.client.HTTPException,
    ) as exc:
        # 2xx status 后的截断 body 仍是未知 provider 结果；转换为合同异常后，
        # promotion 外层才能持久化已确认 commit/tag 的 failed receipt。
        raise ReleaseContractError("provider response is unknown; manual review required") from exc
    try:
        decoded_raw = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ReleaseContractError("provider success response is not valid JSON") from exc
    if not isinstance(decoded_raw, dict):
        raise ReleaseContractError("provider success response must be an object")
    decoded = cast(dict[str, object], decoded_raw)
    release_id = decoded.get("id")
    release_url = decoded.get("url")
    if (
        not isinstance(release_id, str)
        or not release_id.strip()
        or not isinstance(release_url, str)
        or not release_url.strip()
        or release_url != release_url.strip()
    ):
        raise ReleaseContractError("provider success response lacks usable id/url")
    _validate_provider_release_url(release_url)
    # provider 回包属于不可信边界，可能反射 Authorization token 或 endpoint；
    # receipt 是跨 job 持久化 artifact，必须在截断和写盘前执行同一去敏策略。
    safe_id = redact(release_id, token, endpoint)
    # endpoint 是已审批的网络身份，不是 credential；整段替换会破坏与 endpoint
    # 同源的合法 release URL。URL userinfo 已在结构校验中拒绝，只需清除 token。
    safe_url = redact(release_url, token)
    _validate_provider_release_url(safe_url)
    if len(safe_id) > 200 or len(safe_url) > 500:
        raise ReleaseContractError("provider success response identity is too long")
    return {"id": safe_id, "url": safe_url}
