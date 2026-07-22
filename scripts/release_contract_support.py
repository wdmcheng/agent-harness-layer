"""发布合同共享的 schema 常量、I/O、摘要、脱敏与 source identity 工具。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, cast

PREVIEW_SCHEMA = "release-preview/v1"
PROMOTION_PLAN_SCHEMA = "release-promotion-plan/v1"
BUILD_SCHEMA = "release-build/v1"
PROMOTION_SCHEMA = "release-promotion/v1"
REGISTRY_PLAN_SCHEMA = "registry-publish-plan/v1"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:token|password|secret|credential)\s*[=:]\s*)[^\s,;]+"),
)
# Git/HTTP 客户端可能把 remote URL 原样写进诊断；调用方通常不知道其中密码，
# 因此必须在共享边界整体清除 userinfo，同时保留 URL 的 `@host` 结构供排障。
URL_USERINFO_PATTERN = re.compile(r"(?i)((?:git\+)?https?://)[^/@\s]+@")
SEMVER = re.compile(r"\d+\.\d+\.\d+")
GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SHA256 = re.compile(r"[0-9a-f]{64}")
BUMPS = {"major", "minor", "patch"}
RELEASE_ARTIFACT_KINDS = {"wheel", "sdist", "changelog", "release-notes", "checksums"}
UV_VERSION = "0.11.29"
PSR_VERSION = "10.6.1"


class ReleaseContractError(RuntimeError):
    """表示发布身份、schema 或安全门禁不能形成闭合证据。"""


def required_uv_executable() -> str:
    """返回精确项目 pin 的 uv；子进程不得绕回 PATH 中的旧版本。"""

    executable = os.environ.get("UV") or shutil.which("uv")
    if executable is None:
        raise ReleaseContractError("required uv executable is unavailable")
    result = subprocess.run(
        [executable, "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    parts = result.stdout.split()
    if result.returncode != 0 or len(parts) < 2 or parts[:2] != ["uv", UV_VERSION]:
        raise ReleaseContractError(f"required uv version is {UV_VERSION}")
    return executable


def required_psr_executable() -> str:
    """返回精确项目 pin 的 semantic-release，拒绝 PATH 中碰巧输出相同版本的替身。"""

    executable = os.environ.get("SEMANTIC_RELEASE") or shutil.which("semantic-release")
    if executable is None:
        raise ReleaseContractError("required semantic-release executable is unavailable")
    result = subprocess.run(
        [executable, "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != (
        f"semantic-release, version {PSR_VERSION}"
    ):
        raise ReleaseContractError(f"required semantic-release version is {PSR_VERSION}")
    return executable


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """让 30x 保持为 HTTPError，调用方不得隐式改变已审批请求的目标或方法。"""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        """拒绝构造跳转请求；参数只用于遵循标准库 handler seam。"""

        del req, fp, code, msg, headers, newurl
        return None


def urlopen_no_redirect(
    request: urllib.request.Request,
    *,
    timeout: int,
    bypass_proxy: bool = False,
) -> Any:
    """执行一次不跟随 redirect 的请求；loopback 替身可显式绕过机器代理。"""

    handlers: list[Any] = []
    if bypass_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    handlers.append(_RejectRedirects())
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)


def run_git(repo: Path, *args: str) -> str:
    """读取或操作明确传入的一次性 git 仓库，并把失败转换为稳定诊断。"""

    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise ReleaseContractError(redact(f"git {' '.join(args)} failed: {result.stderr}"))
    return result.stdout.strip()


def sha256_bytes(value: bytes) -> str:
    """返回 artifact schema 统一使用的小写 SHA-256。"""

    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """流式计算文件身份，避免把 wheel/sdist 全部读入常驻内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    """生成稳定 JSON bytes，供 preview checksum 与 consumer 复算共享。"""

    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def approval_sha256(payload: dict[str, object]) -> str:
    """计算人工审批对象的规范 JSON 身份，禁止布尔门禁跨计划复用。"""

    return sha256_bytes(canonical_json_bytes(payload))


def endpoint_sha256(endpoint: str) -> str:
    """只暴露 endpoint 的摘要，plan 不持久化或输出可能含租户信息的 URL。"""

    return sha256_bytes(endpoint.encode())


def require_approval_digest(name: str, expected: str) -> None:
    """要求执行环境携带与当前规范计划完全一致的人工批准摘要。"""

    actual = os.environ.get(name, "")
    if SHA256.fullmatch(actual) is None:
        raise ReleaseContractError(f"execute requires a valid {name}")
    if actual != expected:
        raise ReleaseContractError("approval identity drift; review and approve the current plan")


def write_json(path: Path, value: object) -> None:
    """用同目录不可预测临时文件原子替换 JSON，且不跟随预置 symlink。

    固定的 `<name>.tmp` 会允许同机攻击者预置 symlink，把 manifest/receipt 内容
    写进任意可写目标；`mkstemp` 以 O_EXCL 建立唯一 inode，再由 replace 原子发布。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict[str, Any]:
    """读取 JSON 对象；数组或标量不能伪装为版本化发布合同。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseContractError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseContractError(f"JSON artifact must be an object: {path}")
    return cast(dict[str, Any], value)


def redact(value: str, *secrets: str) -> str:
    """清除显式 secret 与常见 credential 形态，保证异常也不泄漏环境凭据。"""

    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = URL_USERINFO_PATTERN.sub(r"\1[REDACTED]@", result)
    for pattern in SECRET_PATTERNS:
        result = pattern.sub(r"\1[REDACTED]", result)
    return result


def valid_git_object_id(value: object) -> bool:
    """接受 producer 可能返回的 SHA-1/SHA-256 小写完整 Git object identity。"""

    return isinstance(value, str) and GIT_OBJECT_ID.fullmatch(value) is not None


def source_identity(repo: Path) -> dict[str, str | None]:
    """冻结 source commit、tracked dirty diff 与 release tag 基线。"""

    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"], cwd=repo, capture_output=True, check=False
    )
    if diff.returncode != 0:
        raise ReleaseContractError("cannot calculate tracked dirty diff")
    tags = run_git(repo, "tag", "--list", "agent-harness-v*", "--sort=-version:refname")
    return {
        "commit_sha": run_git(repo, "rev-parse", "HEAD"),
        "dirty_diff_sha256": sha256_bytes(diff.stdout),
        "base_tag": tags.splitlines()[0] if tags else None,
    }


def artifact_record(path: Path, *, root: Path, kind: str) -> dict[str, object]:
    """生成 repo-relative artifact 引用；绝对/越界路径在生产者处直接拒绝。"""

    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ReleaseContractError(f"artifact is outside artifact root: {path}") from exc
    return {
        "path": relative.as_posix(),
        "kind": kind,
        "sha256": sha256_file(resolved),
        "size": resolved.stat().st_size,
    }


def release_artifacts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """仅返回可发布 wheel/sdist，文档/checksum artifact 不得作为 registry upload 输入。"""

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseContractError("preview artifacts must be a list")
    result: list[dict[str, Any]] = []
    for raw in cast(list[object], artifacts):
        if not isinstance(raw, dict):
            raise ReleaseContractError("preview artifact entry must be an object")
        item = cast(dict[str, Any], raw)
        if item.get("kind") in {"wheel", "sdist"}:
            result.append(item)
    return result
