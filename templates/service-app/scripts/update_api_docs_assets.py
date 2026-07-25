"""校验或事务式更新 Swagger UI / Redoc 离线静态资源。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

SCHEMA_VERSION = "api-docs-assets/v1"
BOOTSTRAP_SWAGGER_UI_VERSION = "5.32.11"
BOOTSTRAP_REDOC_VERSION = "2.5.3"
ASSET_RELATIVE_ROOT = Path("app/static/api-docs")
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
REDOC_REMOTE_LOGO_URL = b"https://cdn.redoc.ly/redoc/logo-mini.svg"
REDOC_LOCAL_LOGO_DATA_URI = (
    b"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
    b"width='1' height='1' viewBox='0 0 1 1'/%3E"
)


class AssetUpdateError(RuntimeError):
    """表示资源下载、完整性或替换边界失败。"""


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝 JSON 后值覆盖，保持离线 checker 的 manifest 语义唯一。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AssetUpdateError(f"duplicate API docs asset manifest key: {key}")
        result[key] = value
    return result


def _asset_root_within_app(
    app_root: Path,
    *,
    require_asset_root: bool,
    create_parents: bool,
) -> Path:
    """解析 app/static/api-docs，拒绝 app root 以下任一祖先 symlink。"""

    try:
        resolved_app_root = app_root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise AssetUpdateError("service-app root is missing") from exc
    current = app_root
    parts = ASSET_RELATIVE_ROOT.parts
    for index, part in enumerate(parts):
        current /= part
        if current.is_symlink():
            raise AssetUpdateError(f"asset ancestor contains symlink: {current}")
        is_asset_root = index == len(parts) - 1
        if not current.exists():
            if create_parents and not is_asset_root:
                current.mkdir()
            elif require_asset_root or not is_asset_root:
                raise AssetUpdateError(f"asset path is missing: {current}")
            continue
        try:
            current.resolve(strict=True).relative_to(resolved_app_root)
        except ValueError as exc:
            raise AssetUpdateError(f"asset path escapes service-app root: {current}") from exc
    return current


def _sha256(payload: bytes) -> str:
    """返回 manifest 和合规清单共用的小写 SHA-256。"""

    return hashlib.sha256(payload).hexdigest()


def _download_json(url: str) -> dict[str, object]:
    """只读取 npm 公开 metadata，并拒绝非 JSON 载荷。"""

    request = urllib.request.Request(url, headers={"User-Agent": "agent-harness-assets/1"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - 固定 npm HTTPS
        payload = response.read()
    parsed: object = json.loads(
        payload,
        object_pairs_hook=_object_without_duplicate_keys,
    )
    if not isinstance(parsed, dict):
        raise AssetUpdateError(f"npm metadata is not an object: {url}")
    return cast(dict[str, object], parsed)


def _download_bytes(url: str) -> bytes:
    """下载锁定版本 tarball，设置超时避免更新入口无限挂起。"""

    request = urllib.request.Request(url, headers={"User-Agent": "agent-harness-assets/1"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - metadata 固定 HTTPS
        return response.read()


def _verified_package(name: str, version: str) -> tuple[bytes, dict[str, object]]:
    """根据 npm 发布 metadata 的 sha512 integrity 校验整个 tarball。"""

    if EXACT_VERSION.fullmatch(version) is None:
        raise AssetUpdateError(f"npm package version must be exact: {name}@{version}")
    metadata_url = f"https://registry.npmjs.org/{name}/{version}"
    metadata = _download_json(metadata_url)
    if metadata.get("name") != name or metadata.get("version") != version:
        raise AssetUpdateError(f"npm package identity mismatch: {name}@{version}")
    raw_dist = metadata.get("dist")
    if not isinstance(raw_dist, dict):
        raise AssetUpdateError(f"npm package lacks dist metadata: {name}@{version}")
    dist = cast(dict[str, object], raw_dist)
    tarball_url = dist.get("tarball")
    integrity = dist.get("integrity")
    expected_tarball_url = f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz"
    if not isinstance(tarball_url, str) or tarball_url != expected_tarball_url:
        raise AssetUpdateError(f"unexpected npm tarball URL: {name}@{version}")
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise AssetUpdateError(f"npm package lacks sha512 integrity: {name}@{version}")
    payload = _download_bytes(tarball_url)
    expected = base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
    if len(expected) != hashlib.sha512().digest_size:
        raise AssetUpdateError(f"invalid npm tarball integrity: {name}@{version}")
    if hashlib.sha512(payload).digest() != expected:
        raise AssetUpdateError(f"npm tarball integrity mismatch: {name}@{version}")
    return payload, {
        "version": version,
        "source_url": tarball_url,
        "integrity": integrity,
        "source_sha256": _sha256(payload),
    }


def _read_members(payload: bytes, members: dict[str, str]) -> dict[str, bytes]:
    """按精确成员名读取 tarball，不解压上游未审核路径。"""

    extracted: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        archive_members = archive.getmembers()
        for source_name, target_name in members.items():
            matches = [member for member in archive_members if member.name == source_name]
            if not matches:
                raise AssetUpdateError(f"npm tarball lacks {source_name}")
            if len(matches) != 1:
                raise AssetUpdateError(f"npm tarball has duplicate member: {source_name}")
            member = matches[0]
            if not member.isfile():
                raise AssetUpdateError(f"npm tarball member is not a regular file: {source_name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise AssetUpdateError(f"npm tarball member is not a file: {source_name}")
            content = stream.read()
            if not content:
                raise AssetUpdateError(f"npm tarball member is empty: {source_name}")
            extracted[target_name] = content
    return extracted


def _patch_redoc_external_logo(files: dict[str, bytes]) -> dict[str, bytes]:
    """确定性移除 Redoc 挂载后注入的 Redocly CDN logo 请求。"""

    bundle_name = "redoc/redoc.standalone.js"
    bundle = files[bundle_name]
    if bundle.count(REDOC_REMOTE_LOGO_URL) != 1:
        raise AssetUpdateError("Redoc bundle external logo seam changed; review update")
    return {
        **files,
        bundle_name: bundle.replace(REDOC_REMOTE_LOGO_URL, REDOC_LOCAL_LOGO_DATA_URI),
    }


def _component_manifest(
    package: dict[str, object],
    files: dict[str, bytes],
) -> dict[str, object]:
    """为运行时和离线 check 产出同一份逐文件完整性记录。"""

    return {
        **package,
        "files": {name: _sha256(payload) for name, payload in sorted(files.items())},
    }


def _validated_manifest_component(
    manifest: dict[str, object],
    component: str,
) -> dict[str, str]:
    """封闭组件 schema，并验证精确版本、npm 来源和摘要格式。"""

    raw_entry = manifest.get(component)
    if not isinstance(raw_entry, dict):
        raise AssetUpdateError(f"invalid manifest component: {component}")
    entry = cast(dict[str, object], raw_entry)
    if set(entry) != COMPONENT_FIELDS:
        raise AssetUpdateError(f"invalid manifest fields: {component}")
    version = entry.get("version")
    if not isinstance(version, str) or EXACT_VERSION.fullmatch(version) is None:
        raise AssetUpdateError(f"invalid manifest version: {component}")
    package = COMPONENT_PACKAGES[component]
    expected_source = f"https://registry.npmjs.org/{package}/-/{package}-{version}.tgz"
    if entry.get("source_url") != expected_source:
        raise AssetUpdateError(f"invalid manifest source URL: {component}")
    source_sha256 = entry.get("source_sha256")
    if not isinstance(source_sha256, str) or SHA256_HEX.fullmatch(source_sha256) is None:
        raise AssetUpdateError(f"invalid manifest source SHA-256: {component}")
    integrity = entry.get("integrity")
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise AssetUpdateError(f"invalid manifest integrity: {component}")
    try:
        decoded_integrity = base64.b64decode(
            integrity.removeprefix("sha512-"),
            validate=True,
        )
    except ValueError as exc:
        raise AssetUpdateError(f"invalid manifest integrity: {component}") from exc
    if len(decoded_integrity) != hashlib.sha512().digest_size:
        raise AssetUpdateError(f"invalid manifest integrity: {component}")
    raw_files = entry.get("files")
    if not isinstance(raw_files, dict):
        raise AssetUpdateError(f"manifest component has no files: {component}")
    files = cast(dict[object, object], raw_files)
    if set(files) != COMPONENT_FILES[component]:
        raise AssetUpdateError(f"invalid manifest file set: {component}")
    validated: dict[str, str] = {}
    for relative_name, expected_hash in files.items():
        if (
            not isinstance(relative_name, str)
            or not isinstance(expected_hash, str)
            or SHA256_HEX.fullmatch(expected_hash) is None
        ):
            raise AssetUpdateError(f"invalid manifest asset record: {component}")
        validated[relative_name] = expected_hash
    return validated


def _write_stage(stage: Path, swagger_version: str, redoc_version: str) -> None:
    """两个组件全部校验后才写入待替换目录。"""

    swagger_tarball, swagger_package = _verified_package("swagger-ui-dist", swagger_version)
    redoc_tarball, redoc_package = _verified_package("redoc", redoc_version)
    swagger_files = _read_members(
        swagger_tarball,
        {
            "package/swagger-ui-bundle.js": "swagger-ui/swagger-ui-bundle.js",
            "package/swagger-ui-bundle.js.LICENSE.txt": (
                "swagger-ui/swagger-ui-bundle.js.LICENSE.txt"
            ),
            "package/swagger-ui.css": "swagger-ui/swagger-ui.css",
            "package/LICENSE": "swagger-ui/LICENSE",
            "package/NOTICE": "swagger-ui/NOTICE",
        },
    )
    redoc_files = _patch_redoc_external_logo(
        _read_members(
            redoc_tarball,
            {
                "package/bundles/redoc.standalone.js": "redoc/redoc.standalone.js",
                "package/bundles/redoc.standalone.js.LICENSE.txt": (
                    "redoc/redoc.standalone.js.LICENSE.txt"
                ),
                "package/LICENSE": "redoc/LICENSE",
            },
        )
    )
    for name, payload in {**swagger_files, **redoc_files}.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "swagger_ui": _component_manifest(swagger_package, swagger_files),
        "redoc": _component_manifest(redoc_package, redoc_files),
    }
    (stage / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def check_assets(app_root: Path) -> None:
    """完全离线地校验 manifest、文件集和逐文件 SHA-256。"""

    asset_root = _asset_root_within_app(
        app_root,
        require_asset_root=True,
        create_parents=False,
    )
    resolved_root = asset_root.resolve(strict=True)
    manifest_path = asset_root / "manifest.json"
    if manifest_path.is_symlink():
        raise AssetUpdateError("API docs asset manifest must not be a symlink")
    try:
        manifest_path.resolve(strict=True).relative_to(resolved_root)
    except FileNotFoundError as exc:
        raise AssetUpdateError("API docs asset manifest is missing") from exc
    except ValueError as exc:
        raise AssetUpdateError("API docs asset manifest escapes root") from exc
    raw_manifest: object = json.loads(
        manifest_path.read_text(encoding="utf-8"),
        object_pairs_hook=_object_without_duplicate_keys,
    )
    if not isinstance(raw_manifest, dict):
        raise AssetUpdateError("unsupported API docs asset manifest")
    manifest = cast(dict[str, object], raw_manifest)
    if set(manifest) != {"schema_version", "swagger_ui", "redoc"}:
        raise AssetUpdateError("unsupported API docs asset manifest fields")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise AssetUpdateError("unsupported API docs asset manifest")
    declared = {"manifest.json"}
    for component in ("swagger_ui", "redoc"):
        files = _validated_manifest_component(manifest, component)
        for relative_name, expected_hash in files.items():
            path = asset_root / relative_name
            internal_parents: list[Path] = []
            for parent in path.parents:
                if parent == asset_root:
                    break
                internal_parents.append(parent)
            if path.is_symlink() or any(parent.is_symlink() for parent in internal_parents):
                raise AssetUpdateError(f"asset path contains symlink: {relative_name}")
            try:
                resolved_path = path.resolve(strict=True)
                resolved_path.relative_to(resolved_root)
            except FileNotFoundError as exc:
                raise AssetUpdateError(f"asset is missing: {relative_name}") from exc
            except ValueError as exc:
                raise AssetUpdateError(f"asset path escapes root: {relative_name}") from exc
            if not path.is_file() or _sha256(path.read_bytes()) != expected_hash:
                raise AssetUpdateError(f"asset checksum mismatch: {relative_name}")
            declared.add(relative_name)
    entries = list(asset_root.rglob("*"))
    symlinks = [path.relative_to(asset_root).as_posix() for path in entries if path.is_symlink()]
    if symlinks:
        raise AssetUpdateError(f"asset tree contains symlink: {sorted(symlinks)}")
    actual = {path.relative_to(asset_root).as_posix() for path in entries if path.is_file()}
    if actual != declared:
        raise AssetUpdateError(
            f"asset file set mismatch: missing={sorted(declared - actual)} "
            f"unexpected={sorted(actual - declared)}"
        )


def update_assets(app_root: Path, swagger_version: str, redoc_version: str) -> None:
    """在同一父目录构建完整新版，可捕获故障或中断时恢复原目录。"""

    asset_root = _asset_root_within_app(
        app_root,
        require_asset_root=False,
        create_parents=True,
    )
    with tempfile.TemporaryDirectory(prefix="api-docs-assets.", dir=asset_root.parent) as temp_dir:
        temporary = Path(temp_dir)
        stage = temporary / "next"
        backup = temporary / "previous"
        stage.mkdir()
        _write_stage(stage, swagger_version, redoc_version)
        staged_root = temporary / "app-root"
        staged_root.mkdir()
        shutil.copytree(stage, staged_root / ASSET_RELATIVE_ROOT)
        check_assets(staged_root)
        had_previous = asset_root.exists()
        try:
            if had_previous:
                os.replace(asset_root, backup)
            os.replace(stage, asset_root)
            check_assets(app_root)
        except BaseException:
            # 目录树无法用可移植的单次 rename 覆盖非空目录。这里明确提供事务式
            # 回滚：只有旧树已移入备份时才移除候选树并恢复；第一次 rename 尚未
            # 成功则保持原树不动。BaseException 让 KeyboardInterrupt/SystemExit
            # 也先恢复后再传播，但 SIGKILL 等不可捕获终止不属于此保证。
            if had_previous and backup.exists():
                if asset_root.exists():
                    shutil.rmtree(asset_root)
                os.replace(backup, asset_root)
            elif not had_previous and asset_root.exists():
                shutil.rmtree(asset_root)
            raise


def parse_args() -> argparse.Namespace:
    """要求显式选择离线 check 或联网 update，避免默认触网。"""

    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="verify committed assets offline")
    action.add_argument(
        "--update",
        action="store_true",
        help="download, validate, and transactionally replace assets",
    )
    parser.add_argument("--app-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--swagger-ui-version", default=BOOTSTRAP_SWAGGER_UI_VERSION)
    parser.add_argument("--redoc-version", default=BOOTSTRAP_REDOC_VERSION)
    return parser.parse_args()


def main() -> int:
    """执行资源闭环，失败时只输出可操作的脱敏错误。"""

    args = parse_args()
    app_root = args.app_root.resolve()
    try:
        if args.update:
            update_assets(app_root, args.swagger_ui_version, args.redoc_version)
            print(
                "api-docs-assets: update=ok "
                f"swagger-ui={args.swagger_ui_version} redoc={args.redoc_version}"
            )
        else:
            check_assets(app_root)
            print("api-docs-assets: check=ok")
    except (
        AssetUpdateError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        tarfile.TarError,
        urllib.error.URLError,
    ) as exc:
        print(f"api-docs-assets: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
