"""公共 facade 的导出名单契约。"""

from __future__ import annotations

import importlib
from types import ModuleType

PUBLIC_FACADE_MODULES = [
    "agent_harness",
    "agent_harness.artifacts",
    "agent_harness.config",
    "agent_harness.contracts",
    "agent_harness.events",
    "agent_harness.identity",
    "agent_harness.observability",
    "agent_harness.runtime",
    "agent_harness.security",
    "agent_harness.storage",
    "app",
    "app.api.routes",
]


def _missing_exports(module: ModuleType) -> list[str]:
    return [export for export in module.__all__ if not hasattr(module, export)]


def test_public_facade_exports_are_resolvable_and_unique() -> None:
    for module_name in PUBLIC_FACADE_MODULES:
        module = importlib.import_module(module_name)
        assert len(module.__all__) == len(set(module.__all__)), module_name
        assert _missing_exports(module) == [], module_name
