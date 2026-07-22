"""发布预演、推广与 registry 共享的版本化 artifact 合同。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
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


def _valid_git_object_id(value: object) -> bool:
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


def validate_preview(manifest: dict[str, Any]) -> None:
    """验证 v1 全部必填解释字段及 release/no-release 状态不变量。"""

    if manifest.get("schema_version") != PREVIEW_SCHEMA:
        raise ReleaseContractError("preview schema_version must be release-preview/v1")
    status = manifest.get("status")
    if status not in {"release", "no-release"}:
        raise ReleaseContractError("preview status must be release or no-release")
    source = manifest.get("source")
    decision = manifest.get("decision")
    if not isinstance(source, dict):
        raise ReleaseContractError("preview source identity is incomplete")
    typed_source = cast(dict[str, object], source)
    if not {"commit_sha", "dirty_diff_sha256", "base_tag"} <= typed_source.keys():
        raise ReleaseContractError("preview source identity is incomplete")
    commit_sha = typed_source["commit_sha"]
    dirty_diff_sha256 = typed_source["dirty_diff_sha256"]
    base_tag = typed_source["base_tag"]
    if not _valid_git_object_id(commit_sha):
        raise ReleaseContractError("preview source commit_sha is invalid")
    if not isinstance(dirty_diff_sha256, str) or SHA256.fullmatch(dirty_diff_sha256) is None:
        raise ReleaseContractError("preview source dirty_diff_sha256 is invalid")
    if base_tag is not None and (not isinstance(base_tag, str) or not base_tag.strip()):
        raise ReleaseContractError("preview source base_tag must be null or a non-empty string")
    if not isinstance(decision, dict):
        raise ReleaseContractError("preview decision is incomplete")
    typed_decision = cast(dict[str, object], decision)
    if not {"bump", "reason", "commits"} <= typed_decision.keys():
        raise ReleaseContractError("preview decision is incomplete")
    bump = typed_decision["bump"]
    reason = typed_decision["reason"]
    commits = typed_decision["commits"]
    if not isinstance(reason, str) or not reason.strip():
        raise ReleaseContractError("preview decision reason must be a non-empty string")
    if not isinstance(commits, list):
        raise ReleaseContractError("preview decision commits must be a list")
    required_commit_fields = {"sha", "type", "scope", "subject", "breaking", "bump"}
    for index, raw in enumerate(cast(list[object], commits)):
        if not isinstance(raw, dict):
            raise ReleaseContractError(f"preview decision commit {index} must be an object")
        item = cast(dict[str, object], raw)
        if not required_commit_fields <= item.keys():
            raise ReleaseContractError(f"preview decision commit {index} is incomplete")
        item_sha = item["sha"]
        item_type = item["type"]
        item_scope = item["scope"]
        item_subject = item["subject"]
        item_breaking = item["breaking"]
        item_bump = item["bump"]
        if not isinstance(item_sha, str) or GIT_OBJECT_ID.fullmatch(item_sha) is None:
            raise ReleaseContractError(f"preview decision commit {index} sha is invalid")
        if not isinstance(item_type, str) or not item_type.strip():
            raise ReleaseContractError(f"preview decision commit {index} type is invalid")
        if item_scope is not None and (not isinstance(item_scope, str) or not item_scope.strip()):
            raise ReleaseContractError(f"preview decision commit {index} scope is invalid")
        if not isinstance(item_subject, str) or not item_subject.strip():
            raise ReleaseContractError(f"preview decision commit {index} subject is invalid")
        if type(item_breaking) is not bool:
            raise ReleaseContractError(f"preview decision commit {index} breaking is invalid")
        if item_bump is not None and (not isinstance(item_bump, str) or item_bump not in BUMPS):
            raise ReleaseContractError(f"preview decision commit {index} bump is invalid")
    current_version = manifest.get("current_version")
    if not isinstance(current_version, str) or SEMVER.fullmatch(current_version) is None:
        raise ReleaseContractError("preview current_version must be stable SemVer")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseContractError("preview artifacts must be a list")
    typed_artifacts = cast(list[object], artifacts)
    if status == "no-release":
        if bump is not None:
            raise ReleaseContractError("no-release preview decision bump must be null")
        if (
            "next_version" not in manifest
            or "tag" not in manifest
            or manifest.get("next_version") is not None
            or manifest.get("tag") is not None
            or typed_artifacts
        ):
            raise ReleaseContractError(
                "no-release preview cannot authorize version, tag, or artifacts"
            )
        return
    if not isinstance(bump, str) or bump not in BUMPS:
        raise ReleaseContractError("release preview decision bump must be major, minor, or patch")
    next_version = manifest.get("next_version")
    tag = manifest.get("tag")
    if not isinstance(next_version, str) or SEMVER.fullmatch(next_version) is None:
        raise ReleaseContractError("release preview next_version must be stable SemVer")
    if not isinstance(tag, str) or tag != f"agent-harness-v{next_version}":
        raise ReleaseContractError("release preview tag must match next_version")
    if len(typed_artifacts) != len(RELEASE_ARTIFACT_KINDS):
        raise ReleaseContractError("release preview must contain exactly five artifacts")
    kinds: set[str] = set()
    paths: set[str] = set()
    for raw in typed_artifacts:
        if not isinstance(raw, dict):
            raise ReleaseContractError("preview artifact entry must be an object")
        item = cast(dict[str, object], raw)
        kind = item.get("kind")
        path = item.get("path")
        if not isinstance(kind, str) or kind not in RELEASE_ARTIFACT_KINDS:
            raise ReleaseContractError("release preview artifact kind is invalid")
        if kind in kinds:
            raise ReleaseContractError(f"release preview artifact kind is duplicated: {kind}")
        if not isinstance(path, str) or not path:
            raise ReleaseContractError("release preview artifact path is invalid")
        normalized = PurePosixPath(path)
        if (
            normalized.is_absolute()
            or normalized.as_posix() != path
            or any(part in {".", ".."} for part in normalized.parts)
        ):
            raise ReleaseContractError("release preview artifact path must be canonical relative")
        if path in paths:
            raise ReleaseContractError(f"release preview artifact path is duplicated: {path}")
        kinds.add(kind)
        paths.add(path)
    if kinds != RELEASE_ARTIFACT_KINDS:
        raise ReleaseContractError("release preview artifacts are incomplete")


def resolve_artifact(base: Path, item: dict[str, Any]) -> Path:
    """把 artifact 相对路径约束在 manifest 根下，阻止 path traversal 与绝对路径。"""

    raw = item.get("path")
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise ReleaseContractError("artifact path must be repo-relative")
    root = base.resolve()
    path = (root / raw).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ReleaseContractError("artifact path escapes manifest root") from exc
    return path


def verify_artifacts(manifest: dict[str, Any], *, base: Path) -> list[dict[str, Any]]:
    """复算全部预演产物，但只把 wheel/sdist 返回给 registry upload。"""

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseContractError("preview artifacts must be a list")
    items: list[dict[str, Any]] = []
    publishable: list[dict[str, Any]] = []
    resolved_items: list[tuple[dict[str, Any], Path]] = []
    kinds: set[str] = set()
    paths: set[Path] = set()
    for raw in cast(list[object], artifacts):
        if not isinstance(raw, dict):
            raise ReleaseContractError("preview artifact entry must be an object")
        item = cast(dict[str, Any], raw)
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in RELEASE_ARTIFACT_KINDS:
            raise ReleaseContractError("preview artifact kind is invalid")
        if kind in kinds:
            raise ReleaseContractError(f"preview artifact kind is duplicated: {kind}")
        checksum = item.get("sha256")
        size = item.get("size")
        if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
            raise ReleaseContractError("preview artifact sha256 is invalid")
        if type(size) is not int or size < 0:
            raise ReleaseContractError("preview artifact size is invalid")
        path = resolve_artifact(base, item)
        if path in paths:
            raise ReleaseContractError(f"preview artifact path is duplicated: {item.get('path')}")
        items.append(item)
        resolved_items.append((item, path))
        kinds.add(kind)
        paths.add(path)
        if kind in {"wheel", "sdist"}:
            publishable.append(item)
    if manifest.get("status") == "release" and (
        len(items) != len(RELEASE_ARTIFACT_KINDS) or kinds != RELEASE_ARTIFACT_KINDS
    ):
        raise ReleaseContractError("release preview must contain each required artifact once")
    if manifest.get("status") == "no-release" and items:
        raise ReleaseContractError("no-release preview cannot contain artifacts")
    for item, path in resolved_items:
        if not path.is_file():
            raise ReleaseContractError(f"artifact is missing: {item.get('path')}")
        if item.get("sha256") != sha256_file(path):
            raise ReleaseContractError(f"artifact checksum drift: {item.get('path')}")
        if item.get("size") != path.stat().st_size:
            raise ReleaseContractError(f"artifact size drift: {item.get('path')}")
    return publishable


def validate_promotion(
    preview: dict[str, Any],
    receipt: dict[str, Any],
    build: dict[str, Any],
    *,
    preview_path: Path,
    build_path: Path,
) -> None:
    """闭合 preview 与 promoted receipt 的前后身份，禁止仅凭 tag/job 顺序授权。"""

    validate_preview(preview)
    if receipt.get("schema_version") != PROMOTION_SCHEMA:
        raise ReleaseContractError("promotion schema_version must be release-promotion/v1")
    if receipt.get("status") != "promoted":
        raise ReleaseContractError("promotion receipt status is not promoted")
    if receipt.get("preview_manifest_sha256") != sha256_file(preview_path):
        raise ReleaseContractError("preview manifest checksum drift")
    if receipt.get("source") != preview.get("source"):
        raise ReleaseContractError("promotion source identity drift")
    if receipt.get("version") != preview.get("next_version"):
        raise ReleaseContractError("promotion version identity drift")
    if receipt.get("tag") != preview.get("tag"):
        raise ReleaseContractError("promotion tag identity drift")
    release_commit = receipt.get("release_commit_sha")
    if not _valid_git_object_id(release_commit):
        raise ReleaseContractError("promotion release_commit_sha is not a valid Git object ID")
    if receipt.get("tag_target_sha") != release_commit:
        raise ReleaseContractError("promotion release commit/tag target identity drift")
    validate_release_build(build)
    if receipt.get("release_build_manifest_sha256") != sha256_file(build_path):
        raise ReleaseContractError("promotion release build manifest checksum drift")
    if build.get("version") != receipt.get("version"):
        raise ReleaseContractError("release build version identity drift")
    if build.get("tag") != receipt.get("tag"):
        raise ReleaseContractError("release build tag identity drift")
    if build.get("tag_target_sha") != release_commit:
        raise ReleaseContractError("release build tag target identity drift")
    if receipt.get("artifacts") != release_artifacts(build):
        raise ReleaseContractError("promotion formal artifact identity drift")
    release_notes_sha256 = receipt.get("release_notes_sha256")
    if (
        not isinstance(release_notes_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", release_notes_sha256) is None
    ):
        raise ReleaseContractError("promotion release_notes_sha256 is missing or invalid")
    preview_artifacts = cast(list[object], preview["artifacts"])
    release_notes: list[dict[str, Any]] = []
    for raw in preview_artifacts:
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, Any], raw)
        if item.get("kind") == "release-notes":
            release_notes.append(item)
    if len(release_notes) != 1:
        raise ReleaseContractError(
            "preview must contain exactly one release-notes artifact for promotion binding"
        )
    preview_release_notes_sha256 = release_notes[0].get("sha256")
    if (
        not isinstance(preview_release_notes_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", preview_release_notes_sha256) is None
    ):
        raise ReleaseContractError("preview release-notes artifact sha256 is missing or invalid")
    if release_notes_sha256 != preview_release_notes_sha256:
        raise ReleaseContractError("promotion release_notes_sha256 identity drift")
    for field in ("provider", "provider_release_id"):
        value = receipt.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ReleaseContractError(f"promotion {field} is missing or empty")
    provider_release_url = receipt.get("provider_release_url")
    if not isinstance(provider_release_url, str) or not provider_release_url.strip():
        raise ReleaseContractError("promotion provider_release_url is missing or empty")
    try:
        parsed_url = urllib.parse.urlparse(provider_release_url)
        provider_hostname = parsed_url.hostname
    except ValueError as exc:
        raise ReleaseContractError("promotion provider_release_url is malformed") from exc
    loopback_http = parsed_url.scheme == "http" and provider_hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    if not parsed_url.netloc or (parsed_url.scheme != "https" and not loopback_http):
        raise ReleaseContractError("promotion provider_release_url is not a valid provider URL")


def validate_promotion_plan(plan: dict[str, Any]) -> None:
    """验证跨 job promotion plan 的版本、状态与动态审批身份。"""

    if plan.get("schema_version") != PROMOTION_PLAN_SCHEMA:
        raise ReleaseContractError(
            "promotion plan schema_version must be release-promotion-plan/v1"
        )
    preview_manifest_sha256 = plan.get("preview_manifest_sha256")
    if (
        not isinstance(preview_manifest_sha256, str)
        or SHA256.fullmatch(preview_manifest_sha256) is None
    ):
        raise ReleaseContractError("promotion plan preview checksum is missing or invalid")
    source = plan.get("source")
    if not isinstance(source, dict):
        raise ReleaseContractError("promotion plan source identity is incomplete")
    typed_source = cast(dict[str, object], source)
    if not {"commit_sha", "dirty_diff_sha256", "base_tag"} <= typed_source.keys():
        raise ReleaseContractError("promotion plan source identity is incomplete")
    if not _valid_git_object_id(typed_source["commit_sha"]):
        raise ReleaseContractError("promotion plan source commit_sha is invalid")
    dirty_diff_sha256 = typed_source["dirty_diff_sha256"]
    if not isinstance(dirty_diff_sha256, str) or SHA256.fullmatch(dirty_diff_sha256) is None:
        raise ReleaseContractError("promotion plan source dirty_diff_sha256 is invalid")
    base_tag = typed_source["base_tag"]
    if base_tag is not None and (not isinstance(base_tag, str) or not base_tag.strip()):
        raise ReleaseContractError("promotion plan source base_tag must be null or non-empty")
    version = plan.get("version")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise ReleaseContractError("promotion plan version must be stable SemVer")
    artifacts = plan.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseContractError("promotion plan artifacts must be a list")
    artifact_kinds: set[str] = set()
    for raw in cast(list[object], artifacts):
        if not isinstance(raw, dict):
            raise ReleaseContractError("promotion plan artifact entry must be an object")
        item = cast(dict[str, object], raw)
        kind = item.get("kind")
        path = item.get("path")
        checksum = item.get("sha256")
        size = item.get("size")
        if not isinstance(kind, str) or kind not in {"wheel", "sdist"}:
            raise ReleaseContractError("promotion plan artifact kind is invalid")
        if kind in artifact_kinds:
            raise ReleaseContractError("promotion plan artifact kind is duplicated")
        if not isinstance(path, str) or not path:
            raise ReleaseContractError("promotion plan artifact path is invalid")
        normalized = PurePosixPath(path)
        if (
            normalized.is_absolute()
            or normalized.as_posix() != path
            or any(part in {".", ".."} for part in normalized.parts)
        ):
            raise ReleaseContractError("promotion plan artifact path must be canonical relative")
        if not isinstance(checksum, str) or SHA256.fullmatch(checksum) is None:
            raise ReleaseContractError("promotion plan artifact checksum is invalid")
        if type(size) is not int or size < 0:
            raise ReleaseContractError("promotion plan artifact size is invalid")
        artifact_kinds.add(kind)
    status = plan.get("status")
    if status == "no-release":
        if plan.get("tag") is not None:
            raise ReleaseContractError("no-release promotion plan tag must be null")
        if "approval" in plan or "approval_sha256" in plan:
            raise ReleaseContractError("no-release promotion plan cannot authorize side effects")
        if artifacts:
            raise ReleaseContractError("no-release promotion plan cannot contain artifacts")
        return
    if status != "planned":
        raise ReleaseContractError("promotion plan status must be planned or no-release")
    if plan.get("tag") != f"agent-harness-v{version}":
        raise ReleaseContractError("promotion plan tag must match version")
    if artifact_kinds != {"wheel", "sdist"}:
        raise ReleaseContractError("planned promotion plan artifacts are incomplete")
    approval = plan.get("approval")
    if not isinstance(approval, dict):
        raise ReleaseContractError("promotion plan approval is incomplete")
    typed_approval = cast(dict[str, object], approval)
    required_approval = {
        "schema_version",
        "operation",
        "preview_manifest_sha256",
        "source",
        "tag",
        "version",
        "origin_endpoint_sha256",
        "provider_endpoint_sha256",
        "protected_default_branch",
        "release_notes_sha256",
        "artifacts",
    }
    if not required_approval <= typed_approval.keys():
        raise ReleaseContractError("promotion plan approval is incomplete")
    if (
        typed_approval["schema_version"] != "release-approval/v1"
        or typed_approval["operation"] != "promotion"
        or typed_approval["preview_manifest_sha256"] != preview_manifest_sha256
        or typed_approval["source"] != source
        or typed_approval["tag"] != plan.get("tag")
        or typed_approval["version"] != version
        or typed_approval["artifacts"] != artifacts
    ):
        raise ReleaseContractError("promotion plan approval identity drift")
    for field in (
        "origin_endpoint_sha256",
        "provider_endpoint_sha256",
        "release_notes_sha256",
    ):
        value = typed_approval[field]
        if not isinstance(value, str) or SHA256.fullmatch(value) is None:
            raise ReleaseContractError(f"promotion plan approval {field} is invalid")
    protected_default_branch = typed_approval["protected_default_branch"]
    if (
        not isinstance(protected_default_branch, str)
        or not protected_default_branch
        or protected_default_branch.strip() != protected_default_branch
    ):
        raise ReleaseContractError("promotion plan protected default branch is invalid")
    approval_digest = plan.get("approval_sha256")
    if not isinstance(approval_digest, str) or SHA256.fullmatch(approval_digest) is None:
        raise ReleaseContractError("promotion plan approval checksum is missing or invalid")
    if approval_digest != approval_sha256(typed_approval):
        raise ReleaseContractError("promotion plan approval checksum drift")


def validate_release_build(build: dict[str, Any]) -> None:
    """验证 tag 后正式构建身份；状态必须先于 credential 和网络检查。"""

    if build.get("status") != "built":
        raise ReleaseContractError("release build status must be built")
    if build.get("schema_version") != BUILD_SCHEMA:
        raise ReleaseContractError("release build schema_version must be release-build/v1")
    version = build.get("version")
    tag = build.get("tag")
    if not isinstance(version, str) or SEMVER.fullmatch(version) is None:
        raise ReleaseContractError("release build version must be stable SemVer")
    if tag != f"agent-harness-v{version}":
        raise ReleaseContractError("release build tag must match version")
    if not _valid_git_object_id(build.get("tag_target_sha")):
        raise ReleaseContractError("release build tag_target_sha is invalid")
    if build.get("uv_version") != UV_VERSION:
        raise ReleaseContractError(f"release build uv_version must be {UV_VERSION}")
    artifacts = build.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseContractError("release build artifacts must be a list")
    artifact_values = cast(list[object], artifacts)
    kinds: list[str] = []
    for item in artifact_values:
        if not isinstance(item, dict):
            raise ReleaseContractError("release build artifact entries must contain a string kind")
        artifact = cast(dict[str, object], item)
        kind = artifact.get("kind")
        if not isinstance(kind, str):
            raise ReleaseContractError("release build artifact entries must contain a string kind")
        kinds.append(kind)
    if sorted(kinds) != ["checksums", "sdist", "wheel"]:
        raise ReleaseContractError(
            "release build artifacts must contain wheel, sdist, and checksums"
        )


def validate_registry_plan(plan: dict[str, Any]) -> None:
    """验证 publish execute 消费的同一份无凭据计划。"""

    if plan.get("schema_version") != REGISTRY_PLAN_SCHEMA:
        raise ReleaseContractError("registry plan schema_version must be registry-publish-plan/v1")
    if plan.get("status") != "planned":
        raise ReleaseContractError("registry plan status must be planned")
    approval = plan.get("approval")
    if not isinstance(approval, dict):
        raise ReleaseContractError("registry plan approval is incomplete")
    if plan.get("approval_sha256") != approval_sha256(cast(dict[str, object], approval)):
        raise ReleaseContractError("registry plan approval checksum drift")
