"""Agent `model_tool_loop` exact 配置与 descriptor 投影合同。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
import yaml
from pydantic import ValidationError
from tests.contracts.test_controlled_real_model_config_contracts import PROFILES
from tests.contracts.test_tool_intent_model_catalog_config_contracts import (
    tool_intent_override,
)

from agent_harness.config import load_settings
from agent_harness.models import (
    ToolCatalogSelection,
    ToolCatalogSourceDescriptor,
    compile_output_schema_definition,
)
from agent_harness.registry import AgentRegistry, RegistryLoadError

_SCHEMAS = """from agent_harness.contracts.dto import HarnessDTO

class Input(HarnessDTO):
    prompt: str

class Output(HarnessDTO):
    answer: str
"""

_EXECUTOR = """from agent_harness.runtime import AgentExecutionResult

class Executor:
    async def run(self, request, context):
        return AgentExecutionResult.completed({"answer": "ok"})

    async def resume(self, request, context, grant):
        return AgentExecutionResult.completed({"answer": "ok"})

executor = Executor()
"""


def _loop_config() -> dict[str, object]:
    """返回合法且显式的五项 Agent hard maxima。"""

    return {
        "max_turns": 8,
        "max_total_tokens": 2048,
        "max_total_cost_usd": 0.01,
        "max_tool_output_bytes": 8192,
        "max_duration_seconds": 120,
    }


def _agent_config(
    *,
    model_tool_loop: object = None,
    include_loop: bool = True,
) -> dict[str, object]:
    """构造指向受控 tool-intent deployment 的完整配置。"""

    config: dict[str, object] = {
        "agent_id": "examples.tool_loop",
        "version": "0.1.0",
        "name": "tool-loop",
        "description": "Tool loop registry fixture.",
        "input_schema": "agents.tool_loop.schemas.Input",
        "output_schema": "agents.tool_loop.schemas.Output",
        "executor": "executor:executor",
        "model": {
            "provider": "openai-compatible",
            "deployment_id": "real_primary",
            "allowed_models": ["fixture-text-1"],
            "default_model": "fixture-text-1",
            "fallback_models": [],
        },
        "budget": {
            "max_tokens_per_run": 4096,
            "max_cost_usd_per_run": 1.0,
        },
        "tool_allowlist": ["search"],
        "eval_dataset": None,
        "delegation_edges": [],
    }
    if include_loop:
        config["model_tool_loop"] = _loop_config() if model_tool_loop is None else model_tool_loop
    return config


def _write_agent(root: Path, config: dict[str, object], *, marker: Path | None = None) -> None:
    """写入隔离 Agent；marker 用于证明配置失败早于 executor import。"""

    package = root / "tool_loop"
    package.mkdir(parents=True)
    (package / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (package / "schemas.py").write_text(_SCHEMAS, encoding="utf-8")
    marker_statement = ""
    if marker is not None:
        marker_statement = (
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
        )
    (package / "executor.py").write_text(marker_statement + _EXECUTOR, encoding="utf-8")


def _tool_model_settings():
    """复用已验证的singleton tool-intent typed deployment。"""

    return load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=tool_intent_override(),
    ).model


def _tool_descriptors() -> tuple[ToolCatalogSourceDescriptor, ...]:
    """提供全量 Registry 加载期使用的严格只读工具事实。"""

    schema = compile_output_schema_definition(
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        schema_ref="search-input",
        version="v1",
    )
    return (
        ToolCatalogSourceDescriptor(
            name="search",
            action="search.query",
            resource="search:index",
            input_schema=schema,
            registry_ordinal=0,
        ),
    )


def test_valid_tool_loop_config_projects_read_only_descriptor_summary(tmp_path: Path) -> None:
    """五项逐值投影，公开summary不含deadline、余额、secret或本地路径。"""

    agents = tmp_path / "agents"
    _write_agent(agents, _agent_config())

    descriptor = AgentRegistry.load_from_directory(
        agents,
        model_settings=_tool_model_settings(),
        tool_catalog_descriptors=_tool_descriptors(),
    ).get("examples.tool_loop")

    assert descriptor.model_tool_loop is not None
    assert descriptor.model_tool_loop.model_dump(mode="json") == _loop_config()
    serialized = descriptor.model_dump_json()
    for forbidden in ("deadline", "remaining", "credential", str(tmp_path)):
        assert forbidden not in serialized
    with pytest.raises(ValidationError):
        descriptor.model_tool_loop.max_turns = 64


def test_tool_loop_registry_freezes_valid_catalog_before_executor_import(tmp_path: Path) -> None:
    """合法 Agent 在加载期冻结目录，运行期选择只能保序缩小该快照。"""

    agents = tmp_path / "agents"
    marker = tmp_path / "executor-imported"
    _write_agent(agents, _agent_config(), marker=marker)

    registry = AgentRegistry.load_from_directory(
        agents,
        model_settings=_tool_model_settings(),
        tool_catalog_descriptors=_tool_descriptors(),
    )

    catalog = registry.resolve_tool_catalog("examples.tool_loop", None)
    empty = registry.resolve_tool_catalog(
        "examples.tool_loop",
        ToolCatalogSelection(tool_names=()),
    )
    assert marker.exists()
    assert [(item.name, item.action, item.resource) for item in catalog.tools] == [
        ("search", "search.query", "search:index")
    ]
    assert empty.tools == ()


@pytest.mark.parametrize(
    "tool_allowlist",
    [
        ["search", "search"],
        ["unknown"],
    ],
)
def test_tool_loop_registry_rejects_invalid_allowlist_before_executor_import(
    tmp_path: Path,
    tool_allowlist: list[str],
) -> None:
    """重复或未知工具不能等到请求期才暴露，也不能产生 executor import 副作用。"""

    agents = tmp_path / "agents"
    marker = tmp_path / "executor-imported"
    config = _agent_config()
    config["tool_allowlist"] = tool_allowlist
    _write_agent(agents, config, marker=marker)

    with pytest.raises(RegistryLoadError):
        AgentRegistry.load_from_directory(
            agents,
            model_settings=_tool_model_settings(),
            tool_catalog_descriptors=_tool_descriptors(),
        )

    assert not marker.exists()


def test_tool_loop_registry_rejects_drifting_descriptor_before_executor_import(
    tmp_path: Path,
) -> None:
    """同名工具的 action/resource 漂移必须使全量加载原子失败。"""

    agents = tmp_path / "agents"
    marker = tmp_path / "executor-imported"
    _write_agent(agents, _agent_config(), marker=marker)
    stable = _tool_descriptors()[0]
    drifting = stable.model_copy(update={"action": "search.admin", "resource": "search:other"})

    with pytest.raises(RegistryLoadError):
        AgentRegistry.load_from_directory(
            agents,
            model_settings=_tool_model_settings(),
            tool_catalog_descriptors=(stable, drifting),
        )

    assert not marker.exists()


def test_tool_intent_agent_rejects_nonempty_fallback_routes_before_executor_import(
    tmp_path: Path,
) -> None:
    """tool-intent 即使只有一个候选也不得把显式 route chain 静默降级。"""

    agents = tmp_path / "agents"
    marker = tmp_path / "executor-imported"
    config = _agent_config()
    model = cast(dict[str, object], config["model"])
    model["fallback_routes"] = [{"deployment_id": "real_primary", "model_id": "fixture-text-1"}]
    _write_agent(agents, config, marker=marker)

    with pytest.raises(RegistryLoadError) as failure:
        AgentRegistry.load_from_directory(
            agents,
            model_settings=_tool_model_settings(),
            tool_catalog_descriptors=_tool_descriptors(),
        )

    assert failure.value.error_details[0].field_path == "model.fallback_routes"
    assert not marker.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        {"max_turns": True},
        {"max_turns": 0},
        {"max_turns": 65},
        {"max_total_tokens": True},
        {"max_total_tokens": 0},
        {"max_total_tokens": 4097},
        {"max_total_cost_usd": float("nan")},
        {"max_total_cost_usd": float("inf")},
        {"max_total_cost_usd": True},
        {"max_total_cost_usd": -0.01},
        {"max_total_cost_usd": 1.01},
        {"max_tool_output_bytes": True},
        {"max_tool_output_bytes": 0},
        {"max_tool_output_bytes": 1048577},
        {"max_duration_seconds": True},
        {"max_duration_seconds": 0},
        {"max_duration_seconds": 3601},
        {"extra": 1},
    ],
)
def test_invalid_loop_shape_or_root_budget_fails_before_executor_import(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    """类型、范围、额外字段与根预算扩大都使全量加载原子失败。"""

    loop = deepcopy(_loop_config())
    loop.update(mutation)
    agents = tmp_path / "agents"
    marker = tmp_path / "executor-imported"
    _write_agent(agents, _agent_config(model_tool_loop=loop), marker=marker)

    with pytest.raises(RegistryLoadError):
        AgentRegistry.load_from_directory(agents, model_settings=_tool_model_settings())

    assert not marker.exists()


def test_boolean_cost_maximum_is_rejected_as_a_field_type_before_executor_import(
    tmp_path: Path,
) -> None:
    """YAML true不得靠根预算比较碰巧失败，必须由成本字段自身拒绝。"""

    agents = tmp_path / "agents"
    marker = tmp_path / "executor-imported"
    loop = _loop_config()
    loop["max_total_cost_usd"] = True
    _write_agent(agents, _agent_config(model_tool_loop=loop), marker=marker)

    with pytest.raises(RegistryLoadError) as failure:
        AgentRegistry.load_from_directory(agents, model_settings=_tool_model_settings())

    assert failure.value.error_details[0].field_path == ("model_tool_loop.max_total_cost_usd")
    assert not marker.exists()


@pytest.mark.parametrize("missing", list(_loop_config()))
def test_all_five_loop_fields_are_required_without_defaults(tmp_path: Path, missing: str) -> None:
    """删除任一字段都不能由deployment、环境或代码常量补齐。"""

    loop = _loop_config()
    loop.pop(missing)
    agents = tmp_path / "agents"
    _write_agent(agents, _agent_config(model_tool_loop=loop))

    with pytest.raises(RegistryLoadError):
        AgentRegistry.load_from_directory(agents, model_settings=_tool_model_settings())


def test_model_tool_loop_is_required_iff_route_supports_tool_intent(tmp_path: Path) -> None:
    """缺失与错误capability上的多余对象都在executor import前关闭。"""

    missing_root = tmp_path / "missing" / "agents"
    missing_marker = tmp_path / "missing-imported"
    _write_agent(
        missing_root,
        _agent_config(include_loop=False),
        marker=missing_marker,
    )
    with pytest.raises(RegistryLoadError):
        AgentRegistry.load_from_directory(
            missing_root,
            model_settings=_tool_model_settings(),
        )
    assert not missing_marker.exists()

    fake_settings = load_settings(profile="local", profiles_dir=PROFILES).model
    extra_root = tmp_path / "extra" / "agents"
    extra_config = _agent_config()
    model = cast(dict[str, object], extra_config["model"])
    model.update(
        {
            "provider": "fake",
            "deployment_id": "fake_default",
            "allowed_models": ["fake-default"],
            "default_model": "fake-default",
        }
    )
    _write_agent(extra_root, extra_config)
    with pytest.raises(RegistryLoadError):
        AgentRegistry.load_from_directory(extra_root, model_settings=fake_settings)


def test_fake_scaffold_and_legacy_registry_keep_model_tool_loop_absent() -> None:
    """现有fake Agent不获得隐式循环配置或descriptor默认值。"""

    root = Path("templates/service-app/agents")
    registry = AgentRegistry.load_from_directory(
        root,
        model_settings=load_settings(profile="local", profiles_dir=PROFILES).model,
    )

    assert registry.list_agents()
    assert all(item.model_tool_loop is None for item in registry.list_agents())
    for config_path in root.rglob("config.yaml"):
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        assert "model_tool_loop" not in raw
