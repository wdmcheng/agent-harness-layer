"""真实 provider smoke 的显式授权入口；默认 hosted 环境不执行网络。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from scripts.smoke_live_model import run

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AGENT_HARNESS_LIVE_MODEL_AUTHORIZED") != "1",
    reason="真实 provider smoke 需要当前会话另行授权",
)
async def test_opt_in_real_text_completion() -> None:
    """四项前置均满足后才允许一次受控非流式文本调用。"""

    payload, exit_code = await run(
        profile="service",
        profiles_dir=ROOT / "templates" / "service-app" / "configs" / "profiles",
    )
    assert exit_code == 0
    assert payload["status"] == "pass"
    assert payload["provider_called"] is True
