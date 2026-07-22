"""校验 release provider endpoint 的凭据与传输边界。"""

from __future__ import annotations

import os
import urllib.parse

from release_models import ReleaseContractError


def validate_provider_endpoint(value: str) -> None:
    """拒绝 URL credential；生产仅允许 HTTPS，测试只放行 loopback HTTP。"""

    try:
        parsed = urllib.parse.urlparse(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ReleaseContractError("provider endpoint is malformed") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ReleaseContractError("provider endpoint must not contain URL credentials")
    if parsed.scheme == "https" and parsed.netloc:
        return
    if (
        os.environ.get("RELEASE_TEST_MODE", "").lower() == "true"
        and parsed.scheme == "http"
        and hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        return
    raise ReleaseContractError("provider endpoint must use HTTPS (loopback HTTP is test-only)")


__all__ = ["validate_provider_endpoint"]
