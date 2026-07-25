"""复制模板后可直接运行的最小公开表面测试。"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from agent_harness.events import LocalJsonlEventSink
from agent_harness.registry import AgentRegistry
from app import api_docs
from app.main import create_app

APP_ROOT = Path(__file__).resolve().parents[1]


def _create_injected_app(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    profile: str = "local",
) -> Any:
    """构造不连接 provider 或业务存储的模板应用。"""

    # fingerprint key 是设置加载的 fail-closed 前置条件；测试值只用于本进程，
    # 不写入模板配置，也不允许调用方环境中的 `_FILE` 形成冲突。
    monkeypatch.setenv(
        "AGENT_HARNESS_BUDGET__FINGERPRINT_KEY",
        "test-only-template-health-fingerprint-key",
    )
    monkeypatch.delenv("AGENT_HARNESS_BUDGET__FINGERPRINT_KEY_FILE", raising=False)
    return create_app(
        orchestrator=cast(Any, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        # App factory 需要合法 registry，但这些公开表面不执行 Agent。
        # 仍从完整 agents 根加载，保持 dotted schema ref 语义一致。
        registry=AgentRegistry.load_from_directory(APP_ROOT / "agents"),
        approval_service=cast(Any, object()),
        eval_service=cast(Any, object()),
        profile=profile,
        profiles_dir=APP_ROOT / "configs" / "profiles",
    )


def test_local_health_uses_profile_summary_without_external_provider(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """local health 只需类型化配置，不建立外部依赖连接。"""

    app = _create_injected_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/v1/health", headers={"X-Request-Id": "template-test"})

    assert response.status_code == 200
    assert response.json() == {
        "request_id": "template-test",
        "status": "ok",
        "profile": "local",
        "storage": {"kind": "sqlite", "status": "configured"},
        "queue": {"kind": "in-memory", "status": "configured"},
        "observability": {"kind": "local-jsonl", "status": "configured"},
    }


def test_api_docs_default_to_complete_local_assets(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Swagger 与 Redoc 默认不依赖浏览器访问外部 CDN。"""

    monkeypatch.delenv("AGENT_HARNESS_SERVICE__API_DOCS__ASSET_MODE", raising=False)
    app = _create_injected_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        for path in ("/docs", "/redoc"):
            response = client.get(path)
            assert response.status_code == 200
            assert "https://" not in response.text
            if path == "/docs":
                assert '"validatorUrl": null' in response.text
            asset_paths = re.findall(r'(?:href|src)="(/[^"]+)"', response.text)
            assert asset_paths
            for asset_path in asset_paths:
                asset = client.get(asset_path)
                assert asset.status_code == 200, asset_path
                assert asset.content


def test_api_docs_online_mode_uses_the_same_pinned_versions(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """显式 online 模式只切换传输位置，不偷渡浮动版本。"""

    manifest = json.loads(
        (APP_ROOT / "app" / "static" / "api-docs" / "manifest.json").read_text(encoding="utf-8")
    )
    monkeypatch.setenv("AGENT_HARNESS_SERVICE__API_DOCS__ASSET_MODE", "online")
    app = _create_injected_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        swagger = client.get("/docs")
        redoc = client.get("/redoc")

    assert swagger.status_code == 200
    assert redoc.status_code == 200
    assert f"swagger-ui-dist@{manifest['swagger_ui']['version']}" in swagger.text
    assert f"redoc@{manifest['redoc']['version']}" in redoc.text
    assert '"validatorUrl": null' in swagger.text
    assert "/static/api-docs/" not in swagger.text
    assert "/static/api-docs/" not in redoc.text


@pytest.mark.parametrize("profile", ["local", "service"])
def test_api_docs_can_be_disabled_without_reading_assets(
    tmp_path: Path,
    monkeypatch: Any,
    profile: str,
) -> None:
    """显式关闭或 service 默认值都会隐藏全部公开 seam 且不触碰静态树。"""

    if profile == "local":
        monkeypatch.setenv("AGENT_HARNESS_SERVICE__API_DOCS__ENABLED", "false")
    else:
        monkeypatch.delenv("AGENT_HARNESS_SERVICE__API_DOCS__ENABLED", raising=False)
    monkeypatch.setattr(api_docs, "API_DOCS_ASSET_ROOT", tmp_path / "missing-assets")
    app = _create_injected_app(tmp_path, monkeypatch, profile=profile)

    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        for path in (
            "/docs",
            "/redoc",
            "/openapi.json",
            "/docs/oauth2-redirect",
            "/static/api-docs/swagger-ui/swagger-ui.css",
        ):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.json()["error"]["code"] == "api.not_found"


@pytest.mark.parametrize("symlink_kind", ["ancestor", "root", "manifest", "file"])
def test_api_docs_runtime_rejects_symlinked_asset_boundaries(
    tmp_path: Path,
    monkeypatch: Any,
    symlink_kind: str,
) -> None:
    """应用启动时也必须拒绝资源根、manifest 和资源文件 symlink。"""

    monkeypatch.delenv("AGENT_HARNESS_SERVICE__API_DOCS__ASSET_MODE", raising=False)
    copied_assets = tmp_path / "copied-assets"
    if symlink_kind == "ancestor":
        service_root = tmp_path / "service-app"
        (service_root / "app").mkdir(parents=True)
        escaped_static = tmp_path / "outside-static"
        shutil.copytree(api_docs.API_DOCS_ASSET_ROOT, escaped_static / "api-docs")
        (service_root / "app" / "static").symlink_to(
            escaped_static,
            target_is_directory=True,
        )
        copied_assets = service_root / "app" / "static" / "api-docs"
    elif symlink_kind == "root":
        escaped_assets = tmp_path / "outside-assets"
        shutil.copytree(api_docs.API_DOCS_ASSET_ROOT, escaped_assets)
        copied_assets.symlink_to(escaped_assets, target_is_directory=True)
    else:
        shutil.copytree(api_docs.API_DOCS_ASSET_ROOT, copied_assets)
        if symlink_kind == "manifest":
            escaped = tmp_path / "outside-manifest.json"
            escaped.write_bytes((copied_assets / "manifest.json").read_bytes())
            target = copied_assets / "manifest.json"
        else:
            # 目标留在资源根内，确保测试证明“拒绝 symlink 本身”，而不是仅靠
            # resolve/relative_to 的越界检查间接失败。
            escaped = copied_assets / "same-redoc-license"
            escaped.write_bytes((copied_assets / "redoc" / "LICENSE").read_bytes())
            target = copied_assets / "redoc" / "LICENSE"
        target.unlink()
        target.symlink_to(escaped)
    monkeypatch.setattr(api_docs, "API_DOCS_ASSET_ROOT", copied_assets)

    with pytest.raises(RuntimeError, match="symlink"):
        _create_injected_app(tmp_path, monkeypatch)


def test_api_docs_runtime_validates_local_files_in_online_mode(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """online 只改变传输位置，本地锁定资源损坏时仍须启动失败。"""

    copied_assets = tmp_path / "copied-assets"
    shutil.copytree(api_docs.API_DOCS_ASSET_ROOT, copied_assets)
    (copied_assets / "redoc" / "redoc.standalone.js").unlink()
    monkeypatch.setattr(api_docs, "API_DOCS_ASSET_ROOT", copied_assets)
    monkeypatch.setenv("AGENT_HARNESS_SERVICE__API_DOCS__ASSET_MODE", "online")

    with pytest.raises(RuntimeError, match="missing"):
        _create_injected_app(tmp_path, monkeypatch)


def test_api_docs_runtime_rejects_duplicate_manifest_keys(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """JSON 后值覆盖不得让歧义 manifest 越过封闭 schema。"""

    copied_assets = tmp_path / "copied-assets"
    shutil.copytree(api_docs.API_DOCS_ASSET_ROOT, copied_assets)
    manifest_path = copied_assets / "manifest.json"
    original = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        original.replace("{", '{"schema_version":"invalid",', 1),
        encoding="utf-8",
    )
    monkeypatch.setattr(api_docs, "API_DOCS_ASSET_ROOT", copied_assets)
    monkeypatch.setenv("AGENT_HARNESS_SERVICE__API_DOCS__ASSET_MODE", "online")

    with pytest.raises(RuntimeError, match="duplicate"):
        _create_injected_app(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "corruption",
    ["floating-version", "missing-provenance", "unexpected-field", "foreign-source-url"],
)
def test_api_docs_runtime_rejects_malformed_manifest_in_online_mode(
    tmp_path: Path,
    monkeypatch: Any,
    corruption: str,
) -> None:
    """online 模式也只能使用封闭 manifest 中的精确锁定版本。"""

    copied_assets = tmp_path / "copied-assets"
    shutil.copytree(api_docs.API_DOCS_ASSET_ROOT, copied_assets)
    manifest_path = copied_assets / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if corruption == "floating-version":
        manifest["redoc"]["version"] = "../redoc@latest"
    elif corruption == "missing-provenance":
        del manifest["swagger_ui"]["source_sha256"]
    elif corruption == "unexpected-field":
        manifest["swagger_ui"]["unexpected"] = True
    else:
        manifest["swagger_ui"]["source_url"] = "https://example.invalid/swagger.tgz"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(api_docs, "API_DOCS_ASSET_ROOT", copied_assets)
    monkeypatch.setenv("AGENT_HARNESS_SERVICE__API_DOCS__ASSET_MODE", "online")

    with pytest.raises(RuntimeError, match="manifest|version|source"):
        _create_injected_app(tmp_path, monkeypatch)
