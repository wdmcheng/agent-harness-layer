"""普通文本流 typed config、固定容量与版本化 identity 合同。"""

from __future__ import annotations

from typing import Any, cast

import pytest
from tests.contracts.test_controlled_real_model_config_contracts import PROFILES

from agent_harness.config import SettingsLoadError, load_settings
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
    operation_event_capacity,
)
from agent_harness.storage.stream_evidence_repositories import (
    stream_completed_event_id,
    stream_delta_event_id,
    stream_group_id,
    stream_usage_event_id,
)


def test_stream_config_defaults_and_legal_bounds_do_not_change_capacity() -> None:
    """目标分片与候选窗口可缩放，stream 事件预约始终固定为 65。"""

    defaults = load_settings(profile="local", profiles_dir=PROFILES)
    configured = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides={
            "model": {
                "model_stream_chunk_utf8_bytes": 256,
                "model_stream_sensitive_candidate_utf8_bytes": 128,
            }
        },
    )

    assert defaults.model.model_stream_chunk_utf8_bytes == 1024
    assert defaults.model.model_stream_sensitive_candidate_utf8_bytes == 512
    assert configured.model.model_stream_chunk_utf8_bytes == 256
    assert configured.model.model_stream_sensitive_candidate_utf8_bytes == 128
    assert operation_event_capacity(EvidenceOperationKind.MODEL_STREAM) == 65


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_stream_chunk_utf8_bytes", 0),
        ("model_stream_chunk_utf8_bytes", 4097),
        ("model_stream_sensitive_candidate_utf8_bytes", 127),
        ("model_stream_sensitive_candidate_utf8_bytes", 4097),
        ("model_stream_chunk_utf8_bytes", True),
        ("model_stream_sensitive_candidate_utf8_bytes", "512"),
    ],
)
def test_stream_config_rejects_invalid_or_coerced_bounds(field: str, value: object) -> None:
    """非法配置在 composition/provider 之前关闭失败，不能依赖 Pydantic coercion。"""

    with pytest.raises(SettingsLoadError):
        load_settings(
            profile="local",
            profiles_dir=PROFILES,
            overrides={"model": {field: value}},
        )


@pytest.mark.parametrize("field", ["model_stream_max_deltas", "model_stream_event_capacity"])
def test_stream_capacity_is_not_environment_or_configurable_input(field: str) -> None:
    """业务配置不能自报 delta 数或总容量，未知字段必须直接拒绝。"""

    override = {"model": {field: 1}}
    with pytest.raises(SettingsLoadError):
        load_settings(
            profile="local",
            profiles_dir=PROFILES,
            overrides=cast(dict[str, Any], override),
        )


def test_stream_identities_are_bounded_and_reject_noncanonical_call_ids() -> None:
    """五种 identity 只使用 64 位调用根，最大 tenant 不再进入数据库键。"""

    usage_call_id = "a" * 64

    assert stream_group_id(usage_call_id) == f"model-stream:{usage_call_id}"
    assert len(stream_group_id(usage_call_id)) == 77
    assert len(stream_delta_event_id(usage_call_id, 64)) == 82
    assert len(stream_completed_event_id(usage_call_id)) == 79
    assert len(stream_usage_event_id(usage_call_id, "started")) == 79
    assert len(stream_usage_event_id(usage_call_id, "final")) == 79
    with pytest.raises(ValueError):
        stream_delta_event_id("not-a-sha", 1)
    with pytest.raises(ValueError):
        stream_delta_event_id(usage_call_id, 0)
    with pytest.raises(ValueError):
        stream_delta_event_id(usage_call_id, 65)
