"""AgentRegistry schema reference 的整体预校验合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.registry import AgentRegistry, RegistryLoadError


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
