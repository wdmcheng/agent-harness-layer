"""四服务 Compose、wheel-only 镜像与隔离 smoke 的静态部署合同。"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
import yaml
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.run_trace_contract_helpers import seed_persisted_run

from agent_harness.auth import ApiKeyVerifier, hash_token
from agent_harness.storage import (
    ApiKeyCreate,
    RunCreate,
    SessionCreate,
    SQLAlchemyStorage,
    run_migrations,
)
from agent_harness.storage.evidence_repositories import EvidenceOperationKind

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "service-app"


def _script_module(module_name: str, filename: str) -> Any:
    """按脚本真实模块名加载拆分 seam，避免依赖调用进程的 ``sys.path``。"""

    path = TEMPLATE / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _smoke_support() -> Any:
    path = TEMPLATE / "scripts" / "service_smoke_support.py"
    spec = importlib.util.spec_from_file_location("service_smoke_support_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _smoke_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    support = _smoke_support()
    monkeypatch.setitem(sys.modules, "service_smoke_support", support)
    operations_path = TEMPLATE / "scripts" / "service_smoke_operations.py"
    operations_spec = importlib.util.spec_from_file_location(
        "service_smoke_operations_contract",
        operations_path,
    )
    assert operations_spec is not None and operations_spec.loader is not None
    operations_module = importlib.util.module_from_spec(operations_spec)
    operations_spec.loader.exec_module(operations_module)
    monkeypatch.setitem(sys.modules, "service_smoke_operations", operations_module)
    http_path = TEMPLATE / "scripts" / "service_http_smoke.py"
    http_spec = importlib.util.spec_from_file_location(
        "service_http_smoke_contract",
        http_path,
    )
    assert http_spec is not None and http_spec.loader is not None
    http_module = importlib.util.module_from_spec(http_spec)
    http_spec.loader.exec_module(http_module)
    monkeypatch.setitem(sys.modules, "service_http_smoke", http_module)
    sse_path = TEMPLATE / "scripts" / "service_sse_smoke.py"
    sse_spec = importlib.util.spec_from_file_location(
        "service_sse_smoke_contract",
        sse_path,
    )
    assert sse_spec is not None and sse_spec.loader is not None
    sse_module = importlib.util.module_from_spec(sse_spec)
    sse_spec.loader.exec_module(sse_module)
    monkeypatch.setitem(sys.modules, "service_sse_smoke", sse_module)
    secret_path = TEMPLATE / "scripts" / "service_secret_smoke.py"
    secret_spec = importlib.util.spec_from_file_location(
        "service_secret_smoke_contract",
        secret_path,
    )
    assert secret_spec is not None and secret_spec.loader is not None
    secret_module = importlib.util.module_from_spec(secret_spec)
    secret_spec.loader.exec_module(secret_module)
    monkeypatch.setitem(sys.modules, "service_secret_smoke", secret_module)
    approval_path = TEMPLATE / "scripts" / "service_approval_smoke.py"
    approval_spec = importlib.util.spec_from_file_location(
        "service_approval_smoke_contract",
        approval_path,
    )
    assert approval_spec is not None and approval_spec.loader is not None
    approval_module = importlib.util.module_from_spec(approval_spec)
    approval_spec.loader.exec_module(approval_module)
    monkeypatch.setitem(sys.modules, "service_approval_smoke", approval_module)
    crash_module = _script_module(
        "service_budget_crash_smoke",
        "service_budget_crash_smoke.py",
    )
    monkeypatch.setitem(sys.modules, "service_budget_crash_smoke", crash_module)
    path = TEMPLATE / "scripts" / "smoke_service.py"
    spec = importlib.util.spec_from_file_location("service_smoke_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _service_admin() -> Any:
    race_module = _script_module(
        "service_admin_budget_race",
        "service_admin_budget_race.py",
    )
    topology_module = _script_module(
        "service_admin_budget_topology",
        "service_admin_budget_topology.py",
    )
    path = TEMPLATE / "scripts" / "service_admin.py"
    spec = importlib.util.spec_from_file_location("service_admin_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            "service_admin_budget_race": race_module,
            "service_admin_budget_topology": topology_module,
        },
    ):
        spec.loader.exec_module(module)
    return module


def _root_smoke() -> Any:
    path = ROOT / "scripts" / "smoke_service.py"
    spec = importlib.util.spec_from_file_location("root_service_smoke_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compose() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.safe_load((TEMPLATE / "docker-compose.yml").read_text(encoding="utf-8")),
    )


__all__ = [
    "Any",
    "ApiKeyCreate",
    "ApiKeyVerifier",
    "EvidenceOperationKind",
    "Path",
    "ROOT",
    "RunCreate",
    "SQLAlchemyStorage",
    "SessionCreate",
    "SimpleNamespace",
    "TEMPLATE",
    "_compose",
    "_root_smoke",
    "_service_admin",
    "_smoke_service",
    "_smoke_support",
    "cast",
    "hash_token",
    "importlib",
    "isolated_database",
    "os",
    "pytest",
    "run_migrations",
    "seed_persisted_run",
    "sys",
    "yaml",
]
