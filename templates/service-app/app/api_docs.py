"""按类型化配置装配 Swagger UI / Redoc 文档表面。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Literal, cast

from fastapi import FastAPI
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

ApiDocsAssetMode = Literal["offline", "online"]

API_DOCS_ASSET_ROOT = Path(__file__).resolve().parent / "static" / "api-docs"
EXACT_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
COMPONENT_PACKAGES = {"swagger_ui": "swagger-ui-dist", "redoc": "redoc"}
COMPONENT_FILES = {
    "swagger_ui": {
        "swagger-ui/swagger-ui-bundle.js",
        "swagger-ui/swagger-ui-bundle.js.LICENSE.txt",
        "swagger-ui/swagger-ui.css",
        "swagger-ui/LICENSE",
        "swagger-ui/NOTICE",
    },
    "redoc": {
        "redoc/redoc.standalone.js",
        "redoc/redoc.standalone.js.LICENSE.txt",
        "redoc/LICENSE",
    },
}
COMPONENT_FIELDS = {"version", "source_url", "integrity", "source_sha256", "files"}
API_DOCS_FAVICON_URL = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' rx='12' fill='%23111827'/%3E"
    "%3Cpath d='M18 17h28v8H26v7h16v8H26v7h20v8H18z' fill='%2334d399'/%3E%3C/svg%3E"
)


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝 JSON 后值覆盖，保持 manifest 语义唯一。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"duplicate API docs asset manifest key: {key}")
        result[key] = value
    return result


def _resolved_asset_root() -> Path:
    """拒绝 service root 以下任一目录 symlink，并返回受边界约束的真实路径。"""

    try:
        service_root = API_DOCS_ASSET_ROOT.parents[2]
    except IndexError as exc:
        raise RuntimeError("API docs asset root lacks service boundary") from exc
    try:
        resolved_service_root = service_root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RuntimeError("API docs service root is missing") from exc
    current = service_root
    for part in API_DOCS_ASSET_ROOT.relative_to(service_root).parts:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"API docs asset ancestor is a symlink: {current}")
    try:
        resolved_root = current.resolve(strict=True)
        resolved_root.relative_to(resolved_service_root)
    except FileNotFoundError as exc:
        raise RuntimeError("API docs asset root is missing") from exc
    except ValueError as exc:
        raise RuntimeError("API docs asset root escapes service boundary") from exc
    return resolved_root


def _validated_component(
    manifest: dict[str, object],
    component: str,
) -> tuple[dict[str, object], dict[str, str]]:
    """封闭 manifest 组件，online 模式也不得接受浮动版本或来源注入。"""

    raw_entry = manifest.get(component)
    if not isinstance(raw_entry, dict):
        raise RuntimeError(f"invalid API docs asset manifest component: {component}")
    entry = cast(dict[str, object], raw_entry)
    if set(entry) != COMPONENT_FIELDS:
        raise RuntimeError(f"invalid API docs asset manifest fields: {component}")
    version = entry.get("version")
    if not isinstance(version, str) or EXACT_VERSION.fullmatch(version) is None:
        raise RuntimeError(f"invalid API docs asset manifest version: {component}")
    package = COMPONENT_PACKAGES[component]
    expected_source = f"https://registry.npmjs.org/{package}/-/{package}-{version}.tgz"
    if entry.get("source_url") != expected_source:
        raise RuntimeError(f"invalid API docs asset manifest source: {component}")
    source_sha256 = entry.get("source_sha256")
    if not isinstance(source_sha256, str) or SHA256_HEX.fullmatch(source_sha256) is None:
        raise RuntimeError(f"invalid API docs asset manifest source hash: {component}")
    integrity = entry.get("integrity")
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise RuntimeError(f"invalid API docs asset manifest integrity: {component}")
    try:
        decoded_integrity = base64.b64decode(
            integrity.removeprefix("sha512-"),
            validate=True,
        )
    except ValueError as exc:
        raise RuntimeError(f"invalid API docs asset manifest integrity: {component}") from exc
    if len(decoded_integrity) != hashlib.sha512().digest_size:
        raise RuntimeError(f"invalid API docs asset manifest integrity: {component}")
    raw_files = entry.get("files")
    if not isinstance(raw_files, dict):
        raise RuntimeError(f"API docs asset manifest has no files: {component}")
    files = cast(dict[object, object], raw_files)
    if set(files) != COMPONENT_FILES[component]:
        raise RuntimeError(f"invalid API docs asset manifest file set: {component}")
    validated: dict[str, str] = {}
    for raw_path, expected_sha256 in files.items():
        if (
            not isinstance(raw_path, str)
            or not isinstance(expected_sha256, str)
            or SHA256_HEX.fullmatch(expected_sha256) is None
        ):
            raise RuntimeError(f"invalid API docs asset record: {component}")
        validated[raw_path] = expected_sha256
    return entry, validated


def _manifest(*, verify_files: bool) -> dict[str, object]:
    """读取锁定版本；离线模式还在启动前逐文件校验。"""

    resolved_root = _resolved_asset_root()
    manifest_path = API_DOCS_ASSET_ROOT / "manifest.json"
    if manifest_path.is_symlink():
        raise RuntimeError("API docs asset manifest must not be a symlink")
    try:
        manifest_path.resolve(strict=True).relative_to(resolved_root)
    except FileNotFoundError as exc:
        raise RuntimeError("API docs asset manifest is missing") from exc
    except ValueError as exc:
        raise RuntimeError("API docs asset manifest escapes root") from exc
    raw_manifest: object = json.loads(
        manifest_path.read_text(encoding="utf-8"),
        object_pairs_hook=_object_without_duplicate_keys,
    )
    if not isinstance(raw_manifest, dict):
        raise RuntimeError("unsupported API docs asset manifest")
    manifest = cast(dict[str, object], raw_manifest)
    if set(manifest) != {"schema_version", "swagger_ui", "redoc"}:
        raise RuntimeError("unsupported API docs asset manifest fields")
    if manifest.get("schema_version") != "api-docs-assets/v1":
        raise RuntimeError("unsupported API docs asset manifest")
    declared = {"manifest.json"}
    for component in ("swagger_ui", "redoc"):
        _, files = _validated_component(manifest, component)
        if not verify_files:
            continue
        for raw_path, expected_sha256 in files.items():
            unresolved_path = API_DOCS_ASSET_ROOT / raw_path
            internal_parents: list[Path] = []
            for parent in unresolved_path.parents:
                if parent == API_DOCS_ASSET_ROOT:
                    break
                internal_parents.append(parent)
            if unresolved_path.is_symlink() or any(
                parent.is_symlink() for parent in internal_parents
            ):
                raise RuntimeError(f"API docs asset path contains symlink: {raw_path}")
            try:
                asset_path = unresolved_path.resolve(strict=True)
                asset_path.relative_to(resolved_root)
            except FileNotFoundError as exc:
                raise RuntimeError(f"API docs asset is missing: {raw_path}") from exc
            except ValueError as exc:
                raise RuntimeError(f"API docs asset path escapes root: {raw_path}") from exc
            if not asset_path.is_file():
                raise RuntimeError(f"API docs asset is missing: {raw_path}")
            digest = hashlib.sha256(asset_path.read_bytes()).hexdigest()
            if digest != expected_sha256:
                raise RuntimeError(f"API docs asset checksum mismatch: {raw_path}")
            declared.add(raw_path)
    if verify_files:
        entries = list(API_DOCS_ASSET_ROOT.rglob("*"))
        symlinks = [
            path.relative_to(API_DOCS_ASSET_ROOT).as_posix()
            for path in entries
            if path.is_symlink()
        ]
        if symlinks:
            raise RuntimeError(f"API docs asset tree contains symlink: {sorted(symlinks)}")
        actual = {
            path.relative_to(API_DOCS_ASSET_ROOT).as_posix() for path in entries if path.is_file()
        }
        if actual != declared:
            raise RuntimeError(
                "API docs asset file set mismatch: "
                f"missing={sorted(declared - actual)} unexpected={sorted(actual - declared)}"
            )
    return manifest


def _asset_urls(asset_mode: ApiDocsAssetMode) -> dict[str, str]:
    """两种模式复用同一 manifest 版本，只改变浏览器加载位置。"""

    # online 只切换浏览器传输位置；本地锁定资源仍是“同版本”的真相源，
    # 因此两种模式都先校验完整本地文件集。
    manifest = _manifest(verify_files=True)
    swagger = cast(dict[str, object], manifest["swagger_ui"])
    redoc = cast(dict[str, object], manifest["redoc"])
    swagger_version = cast(str, swagger["version"])
    redoc_version = cast(str, redoc["version"])
    if asset_mode == "online":
        return {
            "swagger_js": (
                "https://cdn.jsdelivr.net/npm/"
                f"swagger-ui-dist@{swagger_version}/swagger-ui-bundle.js"
            ),
            "swagger_css": (
                f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{swagger_version}/swagger-ui.css"
            ),
            "redoc_js": (
                f"https://cdn.jsdelivr.net/npm/redoc@{redoc_version}/bundles/redoc.standalone.js"
            ),
        }
    return {
        "swagger_js": "/static/api-docs/swagger-ui/swagger-ui-bundle.js",
        "swagger_css": "/static/api-docs/swagger-ui/swagger-ui.css",
        "redoc_js": "/static/api-docs/redoc/redoc.standalone.js",
    }


def configure_api_docs(app: FastAPI, asset_mode: ApiDocsAssetMode) -> None:
    """注册文档路由，并保证离线模式不依赖浏览器访问外部站点。"""

    asset_urls = _asset_urls(asset_mode)
    if asset_mode == "offline":
        app.mount(
            "/static/api-docs",
            StaticFiles(directory=API_DOCS_ASSET_ROOT),
            name="api-docs-assets",
        )

    async def swagger_ui() -> HTMLResponse:
        """提供与当前 OpenAPI 绑定的 Swagger UI。"""

        return get_swagger_ui_html(
            openapi_url=cast(str, app.openapi_url),
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
            swagger_js_url=asset_urls["swagger_js"],
            swagger_css_url=asset_urls["swagger_css"],
            swagger_favicon_url=API_DOCS_FAVICON_URL,
            # Swagger UI bundle 默认调用在线 schema validator；资源本地化并不会
            # 自动关闭该请求，因此离线/在线资源模式都显式禁用远程校验器。
            swagger_ui_parameters={"validatorUrl": None},
        )

    async def swagger_ui_redirect() -> HTMLResponse:
        """保留 Swagger OAuth2 redirect 公开 seam，自定义文档页不得丢失。"""

        return get_swagger_ui_oauth2_redirect_html()

    async def redoc_ui() -> HTMLResponse:
        """提供不加载 Google Fonts 的 Redoc，避免离线模式残留外网请求。"""

        return get_redoc_html(
            openapi_url=cast(str, app.openapi_url),
            title=f"{app.title} - ReDoc",
            redoc_js_url=asset_urls["redoc_js"],
            redoc_favicon_url=API_DOCS_FAVICON_URL,
            with_google_fonts=False,
        )

    app.add_api_route("/docs", swagger_ui, include_in_schema=False)
    app.add_api_route(
        cast(str, app.swagger_ui_oauth2_redirect_url),
        swagger_ui_redirect,
        include_in_schema=False,
    )
    app.add_api_route("/redoc", redoc_ui, include_in_schema=False)
