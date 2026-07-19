"""Model/embedding provider-neutral usage evidence 公开合同测试。"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest
from pydantic import ValidationError


def usage_payload(**overrides: object) -> dict[str, object]:
    """构造最小合法 usage，单项用例只覆盖一个合同边界。"""

    payload: dict[str, object] = {
        "usage_kind": "model",
        "tenant_id": "tenant-a",
        "provider": "fake",
        "model": "fake-basic",
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0,
        "cost_status": "reported",
        "latency_ms": 0,
        "decision": {"route": "default", "provider_called": True},
        "run_id": "run-a",
        "agent_id": "examples.basic",
        "request_id": "request-a",
        "trace_id": "trace-a",
    }
    payload.update(overrides)
    return payload


def test_model_usage_evidence_has_exact_public_shape() -> None:
    """公开 payload 只能包含协议允许字段，稳定调用关联键不得穿透到业务与审计表面。"""

    from agent_harness.models import ModelUsageEvidence

    evidence = ModelUsageEvidence.model_validate(usage_payload())

    assert evidence.to_payload() == usage_payload()
    with pytest.raises(ValidationError):
        ModelUsageEvidence.model_validate(usage_payload(usage_call_id="private-correlation"))


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "latency_ms"])
@pytest.mark.parametrize("value", [True, -1, 1.5, "1"])
def test_model_usage_evidence_rejects_invalid_integer_metrics(
    field: str,
    value: object,
) -> None:
    """token 与时延必须是非负整数，防止布尔、浮点或字符串被宽松转换后污染计量。"""

    from agent_harness.models import ModelUsageEvidence

    with pytest.raises(ValidationError):
        ModelUsageEvidence.model_validate(usage_payload(**{field: value}))


@pytest.mark.parametrize("value", [True, -1, math.nan, math.inf, -math.inf, "0"])
def test_model_usage_evidence_rejects_invalid_cost(value: object) -> None:
    """成本必须为有限的非负数，拒绝 NaN、无穷和隐式类型转换以保持账务语义可靠。"""

    from agent_harness.models import ModelUsageEvidence

    with pytest.raises(ValidationError):
        ModelUsageEvidence.model_validate(usage_payload(cost_usd=value))


@pytest.mark.parametrize(
    ("cost_status", "cost_usd"),
    [
        ("reported", None),
        ("estimated", None),
        ("unavailable", 0),
    ],
)
def test_model_usage_evidence_rejects_inconsistent_cost_state(
    cost_status: str,
    cost_usd: object,
) -> None:
    """成本可用性状态与数值必须一致，调用方不能用相互矛盾的数据伪造完整用量。"""

    from agent_harness.models import ModelUsageEvidence

    with pytest.raises(ValidationError):
        ModelUsageEvidence.model_validate(usage_payload(cost_status=cost_status, cost_usd=cost_usd))


def test_estimated_cost_requires_safe_price_source() -> None:
    """估算成本必须附带可追溯且安全的价格来源，避免无依据的金额进入审计证据。"""

    from agent_harness.models import ModelUsageEvidence

    with pytest.raises(ValidationError):
        ModelUsageEvidence.model_validate(usage_payload(cost_status="estimated", cost_usd=0.01))
    evidence = ModelUsageEvidence.model_validate(
        usage_payload(
            cost_status="estimated",
            cost_usd=0.01,
            decision={
                "route": "default",
                "provider_called": True,
                "price_source_ref": "pricing://fake/basic",
                "price_source_version": "2026-07-14",
            },
        )
    )

    assert evidence.cost_usd == 0.01
    assert evidence.cost_status == "estimated"


def test_unavailable_metrics_preserve_null_in_public_payload() -> None:
    """供应商或缓存无法确认计量时公开载荷保留 null，不能把未知压缩成零。"""

    from agent_harness.models import ModelUsageEvidence

    evidence = ModelUsageEvidence.model_validate(
        usage_payload(
            usage_kind="embedding",
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            cost_status="unavailable",
            decision={"cache_status": "hit", "provider_called": False},
        )
    )

    assert evidence.model_dump(mode="json") == usage_payload(
        usage_kind="embedding",
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        cost_status="unavailable",
        decision={"cache_status": "hit", "provider_called": False},
    )


def test_model_and_embedding_adapters_share_evidence_shape() -> None:
    """模型与 embedding 适配器须投影到同一公开证据形状，方便统一存储和消费。"""

    from agent_harness.models import (
        UsageEvidenceContext,
        embedding_usage_evidence,
        model_usage_evidence,
    )

    context = UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id="run-a",
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    model = model_usage_evidence(
        provider="fake",
        model="fake-basic",
        token_usage={"input_tokens": 3, "output_tokens": 2},
        latency_ms=4,
        decision={"route": "default", "provider_called": True},
        context=context,
    )
    embedding = embedding_usage_evidence(
        provider="local",
        model="mock-small",
        cache_hit=True,
        latency_ms=2,
        context=context,
    )

    assert set(model.to_payload()) == set(embedding.to_payload())
    assert embedding.decision == {"cache_status": "hit", "provider_called": False}
    assert embedding.cost_usd is None


def test_business_agents_and_api_routes_do_not_parse_provider_usage() -> None:
    """业务表面只能调用受控 seam，不得重新引入 raw usage 旁路。"""

    root = Path(__file__).parents[2]
    surfaces = [
        *sorted((root / "templates/service-app/agents").rglob("*.py")),
        *sorted((root / "templates/service-app/app/api/routes").glob("*.py")),
    ]
    forbidden_modules = {"pydantic_ai", "openai"}
    for path in surfaces:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    name.name.split(".")[0] not in forbidden_modules for name in node.names
                ), path
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                assert node.module.split(".")[0] not in forbidden_modules, path
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id != "ModelUsageEvidence", path
                if isinstance(node.func, ast.Attribute) and node.func.attr == "require_service":
                    service_names = {
                        argument.value
                        for argument in node.args
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                    }
                    assert "model_provider" not in service_names, path
            elif isinstance(node, ast.Attribute):
                assert node.attr != "token_usage", path
