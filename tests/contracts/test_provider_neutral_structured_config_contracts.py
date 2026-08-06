"""结构化配置、生产职责与稳定交付边界合同。"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)
from tests.contracts.test_provider_neutral_structured_transport_contracts import (
    ControlledStructuredProvider,
    build_structured_bound,
    structured_schema,
)

from agent_harness.config import load_settings
from agent_harness.config.schemas import ModelDeploymentSettings
from agent_harness.models import ModelProviderInvocationError, ModelRequest
from agent_harness.registry import AgentModelPolicy

ROOT = Path(__file__).resolve().parents[2]


def _deployment(**overrides: object) -> dict[str, object]:
    """返回最小合法 deployment，并允许单字段负路径覆写。"""

    payload: dict[str, object] = {
        "provider_kind": "fake",
        "allowed_models": ["fake-basic"],
        "default_model": "fake-basic",
        "capabilities": ["text_completion", "structured_output"],
        "max_structured_repair_attempts": 2,
    }
    payload.update(overrides)
    return payload


def test_structured_capability_and_repair_limit_are_typed_and_round_trip() -> None:
    """合法配置保留显式 capability 顺序与非 bool repair 上限。"""

    deployment = ModelDeploymentSettings.model_validate(_deployment())

    assert deployment.capabilities == ["text_completion", "structured_output"]
    assert deployment.max_structured_repair_attempts == 2
    assert ModelDeploymentSettings.model_validate(deployment.to_payload()) == deployment


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capabilities", []),
        ("capabilities", ["structured_output", "structured_output"]),
        ("capabilities", ["provider_native_structured"]),
        ("max_structured_repair_attempts", True),
        ("max_structured_repair_attempts", -1),
        ("max_structured_repair_attempts", 3),
    ],
)
def test_invalid_structured_config_fails_closed(field: str, value: object) -> None:
    """空/重复/未知 capability 与越界或 bool repair 不得进入冻结设置。"""

    with pytest.raises(ValidationError):
        ModelDeploymentSettings.model_validate(_deployment(**{field: value}))


@pytest.mark.asyncio
async def test_controlled_deployment_without_structured_capability_uses_stable_error(
    tmp_path: Path,
) -> None:
    """Deployment缺少structured capability时必须在公开seam零副作用拒绝。"""

    schema = structured_schema()
    provider = ControlledStructuredProvider(schema)
    provider.provider_id = "openai-compatible"
    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=real_model_override(),
    )
    policy = AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
    )
    service, storage, bound, run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
        model_settings=settings.model,
        agent_policy_resolver=lambda _agent_id: policy,
        provider_key="openai-compatible",
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                ModelRequest(
                    deployment_id="real_primary",
                    provider="openai-compatible",
                    model="fixture-text-1",
                    prompt="return an answer",
                    max_output_tokens=8,
                ),
                operation_key="unsupported-deployment",
            )
        assert failure.value.code == "model.structured_capability_unsupported"
        assert failure.value.provider_called is False
        assert failure.value.attempt_count == 0
        assert (provider.prepares, provider.sends, provider.closes) == (0, [], 0)
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
        assert rows == []
    finally:
        await service.aclose()
        await storage.dispose()


def test_structured_acceptance_rows_reference_existing_exact_pytest_nodes() -> None:
    """REQ-028与AC-096～103的维护映射不得引用改名前或不存在的测试节点。"""

    rows = (ROOT / "docs/acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
    identifiers = {"REQ-028", *(f"AC-{number:03d}" for number in range(96, 104))}
    selected = {
        identifier: next(line for line in rows if line.startswith(f"| {identifier} |"))
        for identifier in identifiers
    }
    for identifier, row in selected.items():
        references = re.findall(r"`(tests/contracts/[^`]+\.py::test_[^`]+)`", row)
        assert references, f"{identifier}缺少精确pytest node"
        for reference in references:
            relative_path, test_name = reference.split("::", maxsplit=1)
            tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
            names = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            assert test_name in names, f"{identifier}引用不存在节点：{reference}"


def test_structured_acceptance_policy_references_existing_exact_pytest_nodes() -> None:
    """结构化验收强制映射须跟随测试拆分，避免矩阵合法但策略引用失效。"""

    policy_path = ROOT / "scripts/acceptance_matrix_policy.py"
    tree = ast.parse(policy_path.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "REQUIRED_TEST_MAPPINGS"
            for target in node.targets
        )
    )
    assert isinstance(assignment.value, ast.Dict)
    phase19_identifiers = {f"AC-{number:03d}" for number in range(96, 104)}
    matrix_rows = (ROOT / "docs/acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
    selected_rows = {
        identifier: next(line for line in matrix_rows if line.startswith(f"| {identifier} |"))
        for identifier in phase19_identifiers
    }
    for key_node, value_node in zip(assignment.value.keys, assignment.value.values, strict=True):
        assert key_node is not None
        identifier = ast.literal_eval(key_node)
        if identifier not in phase19_identifiers:
            continue
        assert isinstance(value_node, ast.Call) and value_node.args
        for reference in ast.literal_eval(value_node.args[0]):
            assert f"`{reference}`" in selected_rows[identifier], (
                f"{identifier}矩阵缺少策略强制节点：{reference}"
            )
            relative_path, test_name = reference.split("::", maxsplit=1)
            test_tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
            names = {
                node.name
                for node in test_tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            assert test_name in names, f"{identifier}策略引用不存在节点：{reference}"


def test_structured_acceptance_rows_cover_split_production_owners() -> None:
    """结果、replay seed与两个证据脚本必须出现在精确REQ/AC生产映射。"""

    rows = (ROOT / "docs/acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
    selected = {
        identifier: next(line for line in rows if line.startswith(f"| {identifier} |"))
        for identifier in ("REQ-028", "AC-096", "AC-097", "AC-100", "AC-101")
    }
    expected = {
        "REQ-028": ("scripts/acceptance_matrix_policy.py",),
        "AC-096": ("scripts/live_model_schema_identity.py",),
        "AC-097": ("models/_invocation_structured_result.py",),
        "AC-100": (
            "models/_invocation_structured_execution.py",
            "models/_invocation_structured_result.py",
        ),
        "AC-101": (
            "models/_invocation_structured_result.py",
            "storage/_structured_usage_evidence_repository.py",
        ),
    }
    for identifier, producers in expected.items():
        assert all(producer in selected[identifier] for producer in producers), identifier


def test_living_plan_decision_identifiers_are_unique() -> None:
    """长期决策编号必须全局唯一，后续引用不能依赖上下文猜测。"""

    plan = (ROOT / "docs/plans/architecture-evolution-plan.md").read_text(encoding="utf-8")
    identifiers = re.findall(r"^\| (D-\d+) \|", plan, re.MULTILINE)

    assert len(identifiers) == len(set(identifiers))


def test_structured_execution_and_compatibility_owners_are_consistent() -> None:
    """执行循环与三个共享兼容owner必须在契约真相源中逐项一致。"""

    change = ROOT / "openspec/changes/archive/2026-08-03-provider-neutral-structured-output"
    sources = {
        "proposal": (change / "proposal.md").read_text(encoding="utf-8"),
        "design": (change / "design.md").read_text(encoding="utf-8"),
        "tasks": (change / "tasks.md").read_text(encoding="utf-8"),
        "dev": (ROOT / "DEV-PLAN.md").read_text(encoding="utf-8"),
        "matrix": (ROOT / "docs/plans/architecture-evolution-change-matrix.md").read_text(
            encoding="utf-8"
        ),
    }
    execution_owner = "_invocation_structured_execution.py"
    compatibility_owners = (
        "_invocation_chain_base.py",
        "_invocation_streaming.py",
        "_router_snapshot_chain.py",
    )
    split_production_owners = (
        "_pydantic_ai_structured.py",
        "_structured_settlement_evidence_models.py",
    )

    assert all(execution_owner in text for text in sources.values())
    assert "在`models/_invocation_structured_execution.py`实现" in sources["tasks"]
    for owner in compatibility_owners:
        assert all(owner in text for text in sources.values()), owner
    for owner in split_production_owners:
        assert all(owner in text for text in sources.values()), owner

    evidence_spec = (change / "specs/model-usage-evidence/spec.md").read_text(encoding="utf-8")
    assert "只能由`models/_invocation_structured_execution.py`" in evidence_spec


def test_structured_modules_stay_within_reviewable_effective_loc() -> None:
    """交付manifest内全部Python生产职责都必须满足统一500有效行门禁。"""

    plan = (ROOT / "docs/plans/architecture-evolution-plan.md").read_text(encoding="utf-8")
    manifest = re.search(r"生产\d+路径：\n\n```text\n(?P<paths>.*?)\n```", plan, re.DOTALL)
    assert manifest is not None, "living plan必须提供可机械解析的生产changed-file manifest"
    modules = [name for name in manifest.group("paths").splitlines() if name.endswith(".py")]
    assert modules, "生产manifest必须至少包含一个Python文件"
    counts: dict[str, int] = {}
    for name in modules:
        source = (ROOT / name).read_text(encoding="utf-8")
        docstring_lines: set[int] = set()
        for node in ast.walk(ast.parse(source)):
            body = getattr(node, "body", None)
            if not isinstance(body, list) or not body or not isinstance(body[0], ast.Expr):
                continue
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                end_lineno = body[0].end_lineno or body[0].lineno
                docstring_lines.update(range(body[0].lineno, end_lineno + 1))
        counts[name] = sum(
            line_number not in docstring_lines
            and bool(line.strip())
            and not line.lstrip().startswith("#")
            for line_number, line in enumerate(source.splitlines(), start=1)
        )

    assert all(count <= 500 for count in counts.values()), counts


def test_structured_collaborators_use_narrow_trusted_annotations() -> None:
    """可信预算、Policy、prepared、settlement与prompt协作者不得退化为Any。"""

    expected_class_annotations = {
        "packages/agent-harness/src/agent_harness/models/_invocation_structured.py": {
            "_shared_budget": "IdentityRuntime | None",
            "_agent_policy_resolver": "Callable[[str], AgentModelPolicy] | None",
        },
        "packages/agent-harness/src/agent_harness/models/_invocation_structured_support.py": {
            "_shared_budget": "IdentityRuntime | None",
            "_agent_policy_resolver": "Callable[[str], AgentModelPolicy] | None",
        },
    }
    for relative_path, expected in expected_class_annotations.items():
        tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
        actual = {
            node.target.id: ast.unparse(node.annotation)
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        assert {name: actual.get(name) for name in expected} == expected

    execution_path = (
        ROOT / "packages/agent-harness/src/agent_harness/models/_invocation_structured_execution.py"
    )
    execution_tree = ast.parse(execution_path.read_text(encoding="utf-8"))
    execute = next(
        node
        for node in ast.walk(execution_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_execute_structured"
    )
    annotations = {
        argument.arg: ast.unparse(argument.annotation) if argument.annotation else None
        for argument in (*execute.args.args, *execute.args.kwonlyargs)
    }
    assert annotations["settlement"] == "SettlementStart"
    assert annotations["prompt_builder"] == "StructuredPromptBuilder"

    support_path = (
        ROOT / "packages/agent-harness/src/agent_harness/models/_invocation_structured_support.py"
    )
    support_tree = ast.parse(support_path.read_text(encoding="utf-8"))
    close = next(
        node
        for node in ast.walk(support_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_close_structured_prepared"
    )
    prepared = next(argument for argument in close.args.args if argument.arg == "prepared")
    assert prepared.annotation is not None
    assert ast.unparse(prepared.annotation) == "PreparedStructuredModelCall"

    schema_path = ROOT / "packages/agent-harness/src/agent_harness/models/structured_schema.py"
    schema_source = schema_path.read_text(encoding="utf-8")
    assert not any(line.startswith("# pyright:") for line in schema_source.splitlines())
    schema_tree = ast.parse(schema_source)
    validator = next(
        node
        for node in ast.walk(schema_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "validate_structured_candidate"
    )
    candidate = next(argument for argument in validator.args.args if argument.arg == "candidate")
    assert candidate.annotation is not None
    assert ast.unparse(candidate.annotation) == "StructuredProviderCandidate"


def test_api_contract_keeps_delivery_process_out_of_stable_mod005_status() -> None:
    """稳定API合同不得固化Reviewer票据、任务计数或ready-to-archive过程状态。"""

    api_contract = (ROOT / "API-Contract.md").read_text(encoding="utf-8")
    section = api_contract.split("### MOD-005 Provider-neutral Structured Output", 1)[1]
    status_row = next(line for line in section.splitlines() if line.startswith("| 状态 |"))
    forbidden = (
        "Reviewer",
        "findings",
        "ready-to-archive",
        "active change",
        "43/44",
        "3ec3716a",
        "5f13353e",
    )
    assert not any(token in status_row for token in forbidden), status_row
