"""全仓测试共享的非生产 secret 引用。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def budget_fingerprint_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """0016 identity 必须有稳定 key；测试只注入不可用于部署的固定值。"""

    monkeypatch.setenv(
        "BUDGET_LEDGER_FINGERPRINT_KEY",
        "test-only-shared-budget-fingerprint-key",
    )
