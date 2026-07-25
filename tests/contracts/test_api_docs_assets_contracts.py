"""API 文档静态资源的版本、完整性与更新入口合同。"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "service-app"
ASSET_ROOT = TEMPLATE / "app" / "static" / "api-docs"
UPDATER = TEMPLATE / "scripts" / "update_api_docs_assets.py"


def _load_updater() -> Any:
    """按文件加载模板脚本，测试事务式替换边界而不污染项目包命名空间。"""

    spec = importlib.util.spec_from_file_location("service_app_api_docs_updater", UPDATER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_api_docs_manifest_matches_all_vendored_assets() -> None:
    """锁定资源必须存在且与 manifest 中的 SHA-256 逐一一致。"""

    manifest = cast(
        dict[str, Any],
        json.loads((ASSET_ROOT / "manifest.json").read_text(encoding="utf-8")),
    )
    assert manifest["schema_version"] == "api-docs-assets/v1"
    assert set(manifest) == {"schema_version", "swagger_ui", "redoc"}

    for component in ("swagger_ui", "redoc"):
        entry = cast(dict[str, Any], manifest[component])
        assert isinstance(entry["version"], str)
        assert entry["version"]
        assert str(entry["source_url"]).startswith("https://registry.npmjs.org/")
        files = cast(dict[str, str], entry["files"])
        assert files
        for relative_path, expected_sha256 in files.items():
            payload = (ASSET_ROOT / relative_path).read_bytes()
            assert hashlib.sha256(payload).hexdigest() == expected_sha256

    expected_files = {
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
    assert set(manifest["swagger_ui"]["files"]) == expected_files["swagger_ui"]
    assert set(manifest["redoc"]["files"]) == expected_files["redoc"]
    assert _load_updater().COMPONENT_FILES == expected_files
    for component_files in expected_files.values():
        for relative_path in component_files:
            assert (ASSET_ROOT / relative_path).is_file()

    swagger_bundle = (ASSET_ROOT / "swagger-ui" / "swagger-ui-bundle.js").read_text(
        encoding="utf-8"
    )
    assert "swagger-ui-bundle.js.LICENSE.txt" in swagger_bundle.splitlines()[0]
    swagger_sidecar = (ASSET_ROOT / "swagger-ui" / "swagger-ui-bundle.js.LICENSE.txt").read_text(
        encoding="utf-8"
    )
    assert "DOMPurify" in swagger_sidecar

    redoc_sidecar = (ASSET_ROOT / "redoc" / "redoc.standalone.js.LICENSE.txt").read_text(
        encoding="utf-8"
    )
    assert "React" in redoc_sidecar
    redoc_bundle = (ASSET_ROOT / "redoc" / "redoc.standalone.js").read_text(encoding="utf-8")
    assert "redoc.standalone.js.LICENSE.txt" in redoc_bundle.splitlines()[0]
    assert "https://cdn.redoc.ly/redoc/logo-mini.svg" not in redoc_bundle
    assert "data:image/svg+xml" in redoc_bundle


def test_api_docs_asset_updater_has_offline_integrity_check() -> None:
    """日常校验不下载；只有显式 update 才访问固定来源。"""

    result = subprocess.run(
        [
            sys.executable,
            str(UPDATER),
            "--check",
            "--app-root",
            str(TEMPLATE),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "api-docs-assets: check=ok" in result.stdout


@pytest.mark.parametrize(
    "corruption",
    [
        "floating-version",
        "missing-provenance",
        "unexpected-root-field",
        "unexpected-component-field",
        "foreign-source-url",
    ],
)
def test_api_docs_asset_check_rejects_malformed_manifest(
    tmp_path: Path,
    corruption: str,
) -> None:
    """checker 必须封闭 manifest schema、精确版本和可复现来源字段。"""

    updater = _load_updater()
    app_root = tmp_path / "service-app"
    copied_assets = app_root / updater.ASSET_RELATIVE_ROOT
    shutil.copytree(ASSET_ROOT, copied_assets)
    manifest_path = copied_assets / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if corruption == "floating-version":
        manifest["swagger_ui"]["version"] = "latest"
    elif corruption == "missing-provenance":
        del manifest["swagger_ui"]["integrity"]
    elif corruption == "unexpected-root-field":
        manifest["unexpected"] = True
    elif corruption == "unexpected-component-field":
        manifest["redoc"]["unexpected"] = True
    else:
        manifest["redoc"]["source_url"] = "https://example.invalid/redoc.tgz"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(updater.AssetUpdateError, match="manifest|version|source|integrity"):
        updater.check_assets(app_root)


@pytest.mark.parametrize("link_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_api_docs_asset_updater_rejects_link_tar_members(link_type: bytes) -> None:
    """精确成员名也必须是普通文件，不跟随 tar symlink 或 hardlink。"""

    updater = _load_updater()
    buffer = io.BytesIO()
    payload = b"unreviewed payload"
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        unreviewed = tarfile.TarInfo("package/unreviewed")
        unreviewed.size = len(payload)
        archive.addfile(unreviewed, io.BytesIO(payload))
        reviewed = tarfile.TarInfo("package/reviewed")
        reviewed.type = link_type
        reviewed.linkname = "unreviewed" if link_type == tarfile.SYMTYPE else "package/unreviewed"
        archive.addfile(reviewed)

    with pytest.raises(updater.AssetUpdateError, match="regular file"):
        updater._read_members(buffer.getvalue(), {"package/reviewed": "reviewed"})


def test_api_docs_asset_updater_rejects_duplicate_tar_members() -> None:
    """同名普通成员也存在解析歧义，不能静默选择 tar 中最后一个。"""

    updater = _load_updater()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for payload in (b"first", b"second"):
            duplicate = tarfile.TarInfo("package/reviewed")
            duplicate.size = len(payload)
            archive.addfile(duplicate, io.BytesIO(payload))

    with pytest.raises(updater.AssetUpdateError, match="duplicate"):
        updater._read_members(buffer.getvalue(), {"package/reviewed": "reviewed"})


def test_api_docs_asset_check_rejects_duplicate_manifest_keys(tmp_path: Path) -> None:
    """checker 必须拒绝 JSON 重复键，不依赖后值覆盖解释 manifest。"""

    updater = _load_updater()
    app_root = tmp_path / "service-app"
    copied_assets = app_root / updater.ASSET_RELATIVE_ROOT
    shutil.copytree(ASSET_ROOT, copied_assets)
    manifest_path = copied_assets / "manifest.json"
    original = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        original.replace("{", '{"schema_version":"invalid",', 1),
        encoding="utf-8",
    )

    with pytest.raises(updater.AssetUpdateError, match="duplicate"):
        updater.check_assets(app_root)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"name":"swagger-ui-dist","name":"redoc"}',
        b'{"dist":{"integrity":"first","integrity":"second"}}',
    ],
)
def test_api_docs_asset_updater_rejects_duplicate_npm_metadata_keys(
    payload: bytes,
    monkeypatch: Any,
) -> None:
    """npm metadata 的根对象和嵌套对象都不得依赖 JSON 后值覆盖。"""

    updater = _load_updater()

    def open_payload(_request: object, *, timeout: int) -> io.BytesIO:
        assert timeout == 30
        return io.BytesIO(payload)

    monkeypatch.setattr(updater.urllib.request, "urlopen", open_payload)

    with pytest.raises(updater.AssetUpdateError, match="duplicate"):
        updater._download_json("https://registry.npmjs.org/example/1.0.0")


def test_api_docs_asset_check_rejects_symlink_that_escapes_asset_root(
    tmp_path: Path,
) -> None:
    """同内容 symlink 也不得让完整性校验读取资源根外的文件。"""

    updater = _load_updater()
    app_root = tmp_path / "service-app"
    copied_assets = app_root / updater.ASSET_RELATIVE_ROOT
    shutil.copytree(ASSET_ROOT, copied_assets)
    escaped_license = tmp_path / "outside-license"
    escaped_license.write_bytes((ASSET_ROOT / "redoc" / "LICENSE").read_bytes())
    linked_license = copied_assets / "redoc" / "LICENSE"
    linked_license.unlink()
    linked_license.symlink_to(escaped_license)

    with pytest.raises(updater.AssetUpdateError, match="symlink|escapes root"):
        updater.check_assets(app_root)


def test_api_docs_asset_check_rejects_manifest_symlink(tmp_path: Path) -> None:
    """manifest 自身不得通过 symlink 把完整性声明移到资源根外。"""

    updater = _load_updater()
    app_root = tmp_path / "service-app"
    copied_assets = app_root / updater.ASSET_RELATIVE_ROOT
    shutil.copytree(ASSET_ROOT, copied_assets)
    escaped_manifest = tmp_path / "outside-manifest.json"
    escaped_manifest.write_bytes((ASSET_ROOT / "manifest.json").read_bytes())
    linked_manifest = copied_assets / "manifest.json"
    linked_manifest.unlink()
    linked_manifest.symlink_to(escaped_manifest)

    with pytest.raises(updater.AssetUpdateError, match="symlink|escapes root"):
        updater.check_assets(app_root)


def test_api_docs_asset_check_rejects_symlinked_asset_root(tmp_path: Path) -> None:
    """资源根不得整体指向应用目录外，即使目标树内容和哈希均合法。"""

    updater = _load_updater()
    app_root = tmp_path / "service-app"
    asset_root = app_root / updater.ASSET_RELATIVE_ROOT
    asset_root.parent.mkdir(parents=True)
    escaped_assets = tmp_path / "outside-assets"
    shutil.copytree(ASSET_ROOT, escaped_assets)
    asset_root.symlink_to(escaped_assets, target_is_directory=True)

    with pytest.raises(updater.AssetUpdateError, match="symlink"):
        updater.check_assets(app_root)


def test_api_docs_asset_check_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    """app/static 等资源根祖先不得把 checker 边界移到应用目录外。"""

    updater = _load_updater()
    app_root = tmp_path / "service-app"
    (app_root / "app").mkdir(parents=True)
    escaped_static = tmp_path / "outside-static"
    shutil.copytree(ASSET_ROOT, escaped_static / "api-docs")
    (app_root / "app" / "static").symlink_to(
        escaped_static,
        target_is_directory=True,
    )

    with pytest.raises(updater.AssetUpdateError, match="symlink|escapes"):
        updater.check_assets(app_root)


def test_api_docs_asset_update_rejects_symlinked_ancestor_before_staging(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """update 不得在应用目录外的 symlink 目标中 staging 或替换。"""

    updater = _load_updater()
    app_root = tmp_path / "service-app"
    (app_root / "app").mkdir(parents=True)
    escaped_static = tmp_path / "outside-static"
    escaped_static.mkdir()
    (app_root / "app" / "static").symlink_to(
        escaped_static,
        target_is_directory=True,
    )

    def must_not_stage(_stage: Path, _swagger: str, _redoc: str) -> None:
        raise AssertionError("staging crossed the application boundary")

    monkeypatch.setattr(updater, "_write_stage", must_not_stage)

    with pytest.raises(updater.AssetUpdateError, match="symlink|escapes"):
        updater.update_assets(app_root, "5.32.11", "2.5.3")


def test_api_docs_asset_check_rejects_unexpected_symlink_directory(tmp_path: Path) -> None:
    """未声明的目录 symlink 也属于非法文件集，不得被 rglob 静默忽略。"""

    updater = _load_updater()
    app_root = tmp_path / "service-app"
    copied_assets = app_root / updater.ASSET_RELATIVE_ROOT
    shutil.copytree(ASSET_ROOT, copied_assets)
    escaped_directory = tmp_path / "outside-directory"
    escaped_directory.mkdir()
    (copied_assets / "unexpected-link").symlink_to(
        escaped_directory,
        target_is_directory=True,
    )

    with pytest.raises(updater.AssetUpdateError, match="symlink"):
        updater.check_assets(app_root)


def test_api_docs_asset_update_restores_previous_tree_after_final_check_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """事务式替换后的最终校验失败时，已提交的旧资源必须完整恢复。"""

    updater = _load_updater()
    asset_root = tmp_path / updater.ASSET_RELATIVE_ROOT
    asset_root.mkdir(parents=True)
    (asset_root / "old.txt").write_text("old\n", encoding="utf-8")

    def write_stage(stage: Path, _swagger: str, _redoc: str) -> None:
        (stage / "new.txt").write_text("new\n", encoding="utf-8")

    checks = 0

    def fail_after_swap(_app_root: Path) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise updater.AssetUpdateError("injected final check failure")

    monkeypatch.setattr(updater, "_write_stage", write_stage)
    monkeypatch.setattr(updater, "check_assets", fail_after_swap)

    with pytest.raises(updater.AssetUpdateError, match="injected final check failure"):
        updater.update_assets(tmp_path, "5.32.11", "2.5.3")

    assert (asset_root / "old.txt").read_text(encoding="utf-8") == "old\n"
    assert not (asset_root / "new.txt").exists()


def test_api_docs_asset_update_restores_previous_tree_after_swap_interruption(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """旧树移入备份后即使收到进程中断，也必须先恢复再传播中断。"""

    updater = _load_updater()
    asset_root = tmp_path / updater.ASSET_RELATIVE_ROOT
    asset_root.mkdir(parents=True)
    (asset_root / "old.txt").write_text("old\n", encoding="utf-8")

    def write_stage(stage: Path, _swagger: str, _redoc: str) -> None:
        (stage / "new.txt").write_text("new\n", encoding="utf-8")

    real_replace = updater.os.replace
    replace_calls = 0

    def interrupt_second_replace(source: Path, destination: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise KeyboardInterrupt("injected swap interruption")
        real_replace(source, destination)

    def accept_staged_tree(_app_root: Path) -> None:
        """隔离本测试到 swap/rollback，不重复 manifest 合同。"""

    monkeypatch.setattr(updater, "_write_stage", write_stage)
    monkeypatch.setattr(updater, "check_assets", accept_staged_tree)
    monkeypatch.setattr(updater.os, "replace", interrupt_second_replace)

    with pytest.raises(KeyboardInterrupt, match="injected swap interruption"):
        updater.update_assets(tmp_path, "5.32.11", "2.5.3")

    assert (asset_root / "old.txt").read_text(encoding="utf-8") == "old\n"
    assert not (asset_root / "new.txt").exists()
