"""AgentRegistry schema reference 的整体预校验合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_harness.models import compile_output_schema
from agent_harness.registry import AgentRegistry, RegistryLoadError
from agents.examples.dev_assistant.schemas import DevAssistantOutput
from agents.examples.rag_assistant.schemas import RagOutput


def _write_agent(
    root: Path,
    name: str,
    *,
    input_schema: str,
    output_schema: str | None = None,
    schema_source: str,
    import_marker: Path | None = None,
) -> None:
    """写入最小 agent 包及可控 schema/executor 源码。

    用于验证 registry 先校验 schema 再导入执行器。
    """

    package = root / name
    package.mkdir(parents=True)
    marker_statement = (
        ""
        if import_marker is None
        else f"from pathlib import Path\nPath({str(import_marker)!r}).write_text('imported')\n"
    )
    (package / "config.yaml").write_text(
        f"""agent_id: examples.{name}
version: 0.1.0
name: {name}
description: Schema validation fixture.
input_schema: {input_schema}
output_schema: {output_schema or f"agents.{name}.schemas.Output"}
executor: executor:executor
model:
  provider: fake
  default_model: fake
  fallback_models: []
budget:
  max_tokens_per_run: 128
  max_cost_usd_per_run: null
tool_allowlist: []
delegation_edges: []
""",
        encoding="utf-8",
    )
    (package / "schemas.py").write_text(schema_source, encoding="utf-8")
    (package / "executor.py").write_text(
        marker_statement
        + """from agent_harness.runtime import AgentExecutionResult

class Executor:
    async def run(self, request, context):
        return AgentExecutionResult.completed({"ok": True})
    async def resume(self, request, context, grant):
        return AgentExecutionResult.completed({"ok": True})

executor = Executor()
""",
        encoding="utf-8",
    )


VALID_SCHEMAS = """from agent_harness.contracts.dto import HarnessDTO

class Input(HarnessDTO):
    value: str = ""

class Output(HarnessDTO):
    ok: bool = True
"""


@pytest.mark.parametrize(
    ("field_path", "schema_ref", "schema_source"),
    [
        ("input_schema", "agents.bad.missing.Input", VALID_SCHEMAS),
        ("input_schema", "agents.bad.schemas.DoesNotExist", VALID_SCHEMAS),
        (
            "input_schema",
            "agents.bad.schemas.NOT_A_SCHEMA",
            VALID_SCHEMAS + "\nNOT_A_SCHEMA = object()\n",
        ),
        ("output_schema", "agents.bad.schemas.DoesNotExist", VALID_SCHEMAS),
    ],
)
def test_registry_rejects_invalid_schema_before_importing_any_executor(
    tmp_path: Path,
    field_path: str,
    schema_ref: str,
    schema_source: str,
) -> None:
    """任一 agent schema 引用无效时 registry 必须整体失败。

    失败前不得 import 其他 executor 产生副作用。
    """

    marker = tmp_path / "executor-imported.txt"
    agents_root = tmp_path / "agents"
    _write_agent(
        agents_root,
        "good",
        input_schema="agents.good.schemas.Input",
        schema_source=VALID_SCHEMAS,
        import_marker=marker,
    )
    _write_agent(
        agents_root,
        "bad",
        input_schema=(schema_ref if field_path == "input_schema" else "agents.bad.schemas.Input"),
        output_schema=(
            schema_ref if field_path == "output_schema" else "agents.bad.schemas.Output"
        ),
        schema_source=schema_source,
    )

    with pytest.raises(RegistryLoadError) as exc_info:
        AgentRegistry.load_from_directory(agents_root)

    assert exc_info.value.error_details[0].field_path == field_path, exc_info.value.error_details
    assert not marker.exists()


def test_non_identifier_roots_with_same_basename_do_not_share_schema_cache(
    tmp_path: Path,
) -> None:
    """不同物理根即使 basename 相同也不能复用 schema cache，防止错误包借用先前合法解析结果。"""

    first_root = tmp_path / "first" / "custom-root"
    second_root = tmp_path / "second" / "custom-root"
    schema_ref = "custom-root.sample.schemas"
    _write_agent(
        first_root,
        "sample",
        input_schema=f"{schema_ref}.Input",
        output_schema=f"{schema_ref}.Output",
        schema_source=VALID_SCHEMAS,
    )
    _write_agent(
        second_root,
        "sample",
        input_schema=f"{schema_ref}.Input",
        output_schema=f"{schema_ref}.Output",
        schema_source="Input = object\nOutput = object\n",
    )

    AgentRegistry.load_from_directory(first_root)
    with pytest.raises(RegistryLoadError, match="Pydantic BaseModel"):
        AgentRegistry.load_from_directory(second_root)


def test_output_catalog_compile_failure_is_atomic_before_any_executor_import(
    tmp_path: Path,
) -> None:
    """任一 sibling 输出含关闭关键字时，definition catalog 与 executor 必须整体回滚。"""

    marker = tmp_path / "executor-imported.txt"
    agents_root = tmp_path / "agents"
    _write_agent(
        agents_root,
        "good",
        input_schema="agents.good.schemas.Input",
        schema_source=VALID_SCHEMAS,
        import_marker=marker,
    )
    _write_agent(
        agents_root,
        "bad_format",
        input_schema="agents.bad_format.schemas.Input",
        schema_source="""from datetime import datetime
from agent_harness.contracts.dto import HarnessDTO

class Input(HarnessDTO):
    value: str = ""

class Output(HarnessDTO):
    timestamp: datetime
""",
    )

    with pytest.raises(RegistryLoadError) as failure:
        AgentRegistry.load_from_directory(agents_root)

    assert failure.value.error_details[0].field_path == "output_schema"
    assert not marker.exists()


def test_output_catalog_meta_schema_failure_uses_stable_registry_error(
    tmp_path: Path,
) -> None:
    """Draft 元schema拒绝也必须归一化，且不能导入任一 sibling executor。"""

    marker = tmp_path / "executor-imported.txt"
    agents_root = tmp_path / "agents"
    _write_agent(
        agents_root,
        "good",
        input_schema="agents.good.schemas.Input",
        schema_source=VALID_SCHEMAS,
        import_marker=marker,
    )
    _write_agent(
        agents_root,
        "bad_meta_schema",
        input_schema="agents.bad_meta_schema.schemas.Input",
        schema_source="""from agent_harness.contracts.dto import HarnessDTO

class Input(HarnessDTO):
    value: str = ""

class Output(HarnessDTO):
    value: str

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema = handler(core_schema)
        schema["properties"]["value"]["minLength"] = "invalid"
        return schema
""",
    )

    with pytest.raises(RegistryLoadError) as failure:
        AgentRegistry.load_from_directory(agents_root)

    assert len(failure.value.error_details) == 1
    detail = failure.value.error_details[0]
    assert detail.code == "registry.invalid_schema"
    assert detail.field_path == "output_schema"
    assert not marker.exists()


def test_output_catalog_pydantic_schema_failure_uses_stable_registry_error(
    tmp_path: Path,
) -> None:
    """Pydantic无法生成JSON Schema时也必须在Registry公共边界归一化。"""

    marker = tmp_path / "executor-imported.txt"
    agents_root = tmp_path / "agents"
    _write_agent(
        agents_root,
        "good",
        input_schema="agents.good.schemas.Input",
        schema_source=VALID_SCHEMAS,
        import_marker=marker,
    )
    _write_agent(
        agents_root,
        "bad_pydantic_schema",
        input_schema="agents.bad_pydantic_schema.schemas.Input",
        schema_source="""from collections.abc import Callable
from agent_harness.contracts.dto import HarnessDTO

class Input(HarnessDTO):
    value: str = ""

class Output(HarnessDTO):
    callback: Callable[[int], int]
""",
    )

    with pytest.raises(RegistryLoadError) as failure:
        AgentRegistry.load_from_directory(agents_root)

    assert len(failure.value.error_details) == 1
    detail = failure.value.error_details[0]
    assert detail.code == "registry.invalid_schema"
    assert detail.field_path == "output_schema"
    assert not marker.exists()


def test_output_catalog_schema_hook_failure_uses_stable_registry_error(
    tmp_path: Path,
) -> None:
    """受信schema扩展钩子的生成失败也不能把第三方异常泄漏给Registry调用方。"""

    marker = tmp_path / "executor-imported.txt"
    agents_root = tmp_path / "agents"
    _write_agent(
        agents_root,
        "good",
        input_schema="agents.good.schemas.Input",
        schema_source=VALID_SCHEMAS,
        import_marker=marker,
    )
    _write_agent(
        agents_root,
        "broken_schema_hook",
        input_schema="agents.broken_schema_hook.schemas.Input",
        schema_source="""from agent_harness.contracts.dto import HarnessDTO

class Input(HarnessDTO):
    value: str = ""

class Output(HarnessDTO):
    value: str

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        raise RuntimeError("raw schema hook failure")
""",
    )

    with pytest.raises(RegistryLoadError) as failure:
        AgentRegistry.load_from_directory(agents_root)

    assert len(failure.value.error_details) == 1
    detail = failure.value.error_details[0]
    assert detail.code == "registry.invalid_schema"
    assert detail.field_path == "output_schema"
    assert not marker.exists()


def test_registry_exposes_compiled_definition_matching_public_descriptor_identity(
    tmp_path: Path,
) -> None:
    """Registry 保留可解析 definition，descriptor 只公开同一版本化 identity。"""

    agents_root = tmp_path / "agents"
    _write_agent(
        agents_root,
        "sample",
        input_schema="agents.sample.schemas.Input",
        schema_source=VALID_SCHEMAS,
    )

    registry = AgentRegistry.load_from_directory(agents_root)
    descriptor = registry.get("examples.sample")
    definition = registry.resolve_output_schema("examples.sample")

    assert descriptor.output_schema_ref == "agents.sample.schemas.Output"
    assert descriptor.output_schema_identity == definition.identity
    assert definition.identity.version == descriptor.version
    assert definition.schema_definition["additionalProperties"] is False


def test_migrated_example_output_schemas_are_closed_and_reject_mixed_variants() -> None:
    """Dev 任意字段与 RAG 空/六字段混搭不得迫使核心 compiler 放宽。"""

    dev_schema = compile_output_schema(
        DevAssistantOutput,
        schema_ref="agents.examples.dev_assistant.schemas.DevAssistantOutput",
        version="1.0.0",
    )
    rag_schema = compile_output_schema(
        RagOutput,
        schema_ref="agents.examples.rag_assistant.schemas.RagOutput",
        version="1.0.0",
    )
    assert dev_schema.schema_definition["additionalProperties"] is False
    assert rag_schema.schema_definition["additionalProperties"] is False

    with pytest.raises(ValidationError):
        DevAssistantOutput.model_validate(
            {
                "status": "completed",
                "tool_name": "filesystem.read",
                "result": {"unexpected": "provider-native"},
                "source_ref": "tool-call:1",
                "trace_ref": "trace:1",
            }
        )
    with pytest.raises(ValidationError):
        RagOutput.model_validate(
            {
                "status": "completed",
                "answer": "mixed",
                "citations": ["citation:1"],
                "source_refs": ["source:1"],
                "retrieval_provider": "fake",
                "assembly_id": "assembly:1",
                "assembly_truncation": {"input_count": 1},
                "model_provider": "fake",
                "trace_ref": "trace:1",
            }
        )
    with pytest.raises(ValidationError):
        RagOutput.model_validate(
            {
                "status": "no_source",
                "answer": "",
                "retrieval_provider": "fake",
                "assembly_truncation": {
                    "input_count": 0,
                    "retained_count": 0,
                    "truncated_count": 0,
                    "dropped_count": 0,
                    "used_tokens": 0,
                    "fragment_count": 0,
                },
                "trace_ref": "trace:1",
            }
        )
