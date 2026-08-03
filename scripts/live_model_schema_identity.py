"""受控 live smoke 直接 descriptor 使用的严格文本输出身份。"""

from __future__ import annotations

from agent_harness.models import OutputSchemaIdentity, compile_output_schema_definition

_LIVE_TEXT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"output_text": {"type": "string"}},
    "required": ["output_text"],
    "additionalProperties": False,
}


def live_text_output_schema_identity(
    *,
    schema_ref: str,
    version: str,
) -> OutputSchemaIdentity:
    """从 live smoke 的显式严格输出形状生成可复算 identity。"""

    return compile_output_schema_definition(
        _LIVE_TEXT_OUTPUT_SCHEMA,
        schema_ref=schema_ref,
        version=version,
    ).identity
