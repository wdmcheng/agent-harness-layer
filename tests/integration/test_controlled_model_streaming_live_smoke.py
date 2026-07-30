"""真实 provider 增量 smoke 的显式双授权入口；默认环境不触网。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from scripts.smoke_live_model_stream import run

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AGENT_HARNESS_LIVE_MODEL_AUTHORIZED") != "1"
    or os.environ.get("AGENT_HARNESS_LIVE_MODEL_STREAM_OPT_IN") != "1",
    reason="真实 provider stream smoke 需要当前会话双重授权",
)
async def test_opt_in_real_text_stream() -> None:
    """四项前置齐全后才允许一次受控 streaming 调用。"""

    payload, exit_code = await run(
        profile="service",
        profiles_dir=ROOT / "templates" / "service-app" / "configs" / "profiles",
    )
    assert exit_code == 0
    assert payload["status"] == "passed"
    assert payload["provider_called"] is True
