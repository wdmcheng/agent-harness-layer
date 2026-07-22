"""许可证门禁共享的常量、错误、路径脱敏与原子报告工具。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
import urllib.parse
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "license-report/v1"
LICENSECHECK_VERSION = "2026.0.8"
POSTGRES_IMAGE = (
    "postgres:18.4@sha256:3a82e1f56c8f0f5616a11103ac3d47e632c3938698946a7ad26da0df1334744a"
)
REDIS_IMAGE = "redis:7.2.14@sha256:f0707c78ea880b293ccdeb410c9c0a8ccae93fe7128799b751333a698b0a39a7"
POSTGRES_SECURITY_BASIS = "https://www.postgresql.org/support/security/"
REDIS_LICENSE_BASIS = "https://raw.githubusercontent.com/redis/redis/7.2.14/COPYING"
REDIS_SECURITY_BASIS = "https://github.com/redis/redis/releases/tag/7.2.14"
REDIS_SERVER_LICENSE = "BSD-3-Clause"
NOTICE_RUNTIME_MARKERS = (
    "PostgreSQL actual server version: 18.4",
    f"PostgreSQL security basis: {POSTGRES_SECURITY_BASIS}",
    "Redis actual server version: 7.2.14",
    f"Redis security advisory basis: {REDIS_SECURITY_BASIS}",
    f"Redis server license boundary: {REDIS_SERVER_LICENSE}",
    "redis-py client license boundary: MIT",
)
VENDORED_DIR_NAMES = {"third_party", "third-party", "vendor", "vendored"}
REQUIRED_VENDORED_FIELDS = {
    "path",
    "source_url",
    "source_revision",
    "source_sha256",
    "license_expression",
    "license_ref",
    "notice_ref",
    "modified",
    "modification_summary",
    "modification_summary_sha256",
    "adr_ref",
}
APPROVAL_FIELDS = {
    "path",
    "source_url",
    "source_revision",
    "source_sha256",
    "license_expression",
    "modified",
    "modification_summary_sha256",
}

PackageIdentity = tuple[str, str, str]
PUBLISHED_RUNTIME_ROOT_SOURCES = {
    "agent-harness": "editable:packages/agent-harness",
    "agent-harness-service-app": "editable:templates/service-app",
}
METADATA_LICENSE_ALIASES = {
    "zlib": "Zlib",
    "zlib/libpng": "Zlib",
    "zlib_libpng": "Zlib",
    "zlib/libpng license": "Zlib",
}


class LicenseError(RuntimeError):
    """表示许可证输入不完整、漂移或需要人工复核。"""


def normalize_metadata_license(value: str) -> str:
    """归一同一许可证的工具拼写，同时保留报告中的原始观察值。"""

    compact = " ".join(value.strip().split())
    return METADATA_LICENSE_ALIASES.get(compact.casefold(), compact)


def issue(message: str) -> str:
    """为所有发现附加稳定的 CLI 前缀。"""

    return f"license-check: {message}"


def relative_path(root: Path, path: Path) -> str:
    """把诊断路径收敛为仓库相对值，避免泄漏宿主目录。"""

    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def source_url_has_credentials(parsed: urllib.parse.SplitResult) -> bool:
    """识别 userinfo 及常见签名查询字段，避免凭据进入合规归档。"""

    if parsed.username is not None or parsed.password is not None:
        return True
    credential_keys = {
        "apikey",
        "authorization",
        "auth",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
    }
    for component in (parsed.query, parsed.fragment):
        for raw_key, _ in urllib.parse.parse_qsl(component, keep_blank_values=True):
            normalized = re.sub(r"[^a-z0-9]", "", raw_key.casefold())
            if normalized in credential_keys or any(
                marker in normalized
                for marker in ("credential", "password", "secret", "signature", "token")
            ):
                return True
    return False


def valid_source_url(value: object) -> bool:
    """只接受带主机且不携带凭据的明确网络来源。"""

    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urllib.parse.urlsplit(value)
        return (
            parsed.scheme in {"https", "ssh", "git", "git+https", "git+ssh"}
            and bool(parsed.hostname)
            and not source_url_has_credentials(parsed)
        )
    except ValueError:
        return False


def report_source_url(value: object) -> str:
    """报告只保留不含凭据的合法来源；其他输入统一脱敏。"""

    if not isinstance(value, str):
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
        if source_url_has_credentials(parsed):
            return "[REDACTED SOURCE URL]"
        return value if valid_source_url(value) else "[INVALID SOURCE URL]"
    except ValueError:
        return "[INVALID SOURCE URL]"


def report_repository_path(value: object) -> str:
    """报告只保留仓库相对路径，拒绝泄漏本机绝对目录。"""

    if not isinstance(value, str) or not value.strip():
        return ""
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return "[INVALID REPOSITORY PATH]"
    return value


def sha256_file(path: Path) -> str:
    """流式计算合规输入摘要，避免一次性载入大文件。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写入报告，失败时不留下可被误判为当前结果的半文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_toml(path: Path) -> dict[str, Any]:
    """读取必需策略文件；缺失时保持 fail-closed。"""

    if not path.is_file():
        raise LicenseError("policy is missing: compliance/third-party.toml")
    with path.open("rb") as stream:
        return tomllib.load(stream)
