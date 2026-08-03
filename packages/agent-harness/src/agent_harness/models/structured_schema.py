"""Strict JSON Schema 编译与 provider-neutral 候选验证。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel

from agent_harness.models.structured import (
    OutputSchemaDefinition,
    OutputSchemaIdentity,
    StructuredValidationResult,
    assert_structured_json_value,
    canonical_structured_json,
)

if TYPE_CHECKING:
    from agent_harness.models.providers import StructuredProviderCandidate


class _ValidationErrorView(Protocol):
    """封闭jsonschema动态错误对象，只暴露稳定投影所需字段。"""

    @property
    def absolute_path(self) -> Sequence[object]: ...

    @property
    def validator(self) -> object: ...

    @property
    def instance(self) -> object: ...

    @property
    def validator_value(self) -> object: ...

    @property
    def schema(self) -> Mapping[str, object]: ...


class _ValidatorView(Protocol):
    """把第三方validator的Unknown返回值收窄到本模块可验证的错误视图。"""

    def iter_errors(self, instance: object) -> Iterable[_ValidationErrorView]: ...


_SCHEMA_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "type",
        "properties",
        "patternProperties",
        "additionalProperties",
        "required",
        "items",
        "prefixItems",
        "enum",
        "const",
        "minimum",
        "exclusiveMinimum",
        "maximum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "dependentRequired",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "title",
        "description",
        "default",
        "examples",
    }
)
_SCHEMA_MAPPING_CHILDREN = ("$defs", "properties", "patternProperties")
_SCHEMA_SEQUENCE_CHILDREN = ("prefixItems", "anyOf", "oneOf", "allOf")


def _json_pointer(parts: Sequence[object]) -> str | None:
    """把 jsonschema absolute path 转成稳定 RFC 6901 pointer。"""

    if not parts:
        return ""
    encoded = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    pointer = "/" + "/".join(encoded)
    return pointer if len(pointer.encode("utf-8")) <= 1024 else None


def _resolve_local_ref(root: dict[str, object], reference: str) -> dict[str, object]:
    """只解析本地非根 JSON pointer；远程和boolean schema一律拒绝。"""

    if not reference.startswith("#/"):
        raise ValueError("only local non-root schema references are supported")
    current: object = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping):
            raise ValueError("schema reference cannot be resolved")
        current_mapping = cast(Mapping[str, object], current)
        if part not in current_mapping:
            raise ValueError("schema reference cannot be resolved")
        current = current_mapping[part]
    if not isinstance(current, dict):
        raise ValueError("schema reference must resolve to an object schema")
    return cast(dict[str, object], current)


def _compile_schema_node(
    node: object,
    *,
    root: dict[str, object],
    ref_stack: tuple[str, ...],
) -> dict[str, object]:
    """内联有限本地ref，并按冻结遍历规则递归关闭object层。"""

    if not isinstance(node, dict):
        raise ValueError("boolean or non-object schema nodes are not supported")
    node_mapping = cast(dict[str, object], node)
    unknown = set(node_mapping) - _SCHEMA_KEYWORDS
    if unknown:
        raise ValueError(f"unsupported schema keywords: {sorted(unknown)!r}")
    if "$ref" in node_mapping:
        if set(node_mapping) != {"$ref"}:
            raise ValueError("schema reference siblings are not supported")
        reference = node_mapping["$ref"]
        if not isinstance(reference, str) or reference in ref_stack:
            raise ValueError("recursive or invalid schema reference")
        return _compile_schema_node(
            _resolve_local_ref(root, reference),
            root=root,
            ref_stack=(*ref_stack, reference),
        )

    compiled: dict[str, object] = {}
    for key, value in node_mapping.items():
        if key in _SCHEMA_MAPPING_CHILDREN:
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be a schema mapping")
            children: dict[str, object] = {}
            for name, child in cast(dict[object, object], value).items():
                if not isinstance(name, str):
                    raise ValueError(f"non-string JSON object key at schema.{key}")
                children[name] = _compile_schema_node(child, root=root, ref_stack=ref_stack)
            compiled[key] = children
            continue
        if key in _SCHEMA_SEQUENCE_CHILDREN:
            if not isinstance(value, list):
                raise ValueError(f"{key} must be a schema array")
            compiled[key] = [
                _compile_schema_node(child, root=root, ref_stack=ref_stack)
                for child in cast(list[object], value)
            ]
            continue
        if key in {"items", "not"}:
            compiled[key] = _compile_schema_node(value, root=root, ref_stack=ref_stack)
            continue
        if key == "additionalProperties":
            if value is not False:
                raise ValueError("additionalProperties must be false")
            compiled[key] = False
            continue
        assert_structured_json_value(value, path=f"schema.{key}")
        compiled[key] = value

    node_type = compiled.get("type")
    object_shape = (
        node_type == "object"
        or (isinstance(node_type, list) and "object" in node_type)
        or any(key in compiled for key in ("properties", "patternProperties", "required"))
    )
    if object_shape:
        compiled["additionalProperties"] = False
    return compiled


def compile_output_schema(
    model: type[BaseModel], *, schema_ref: str, version: str
) -> OutputSchemaDefinition:
    """把受信Pydantic schema class编译为严格、可版本化JSON Schema。"""

    try:
        schema_definition = model.model_json_schema(mode="validation")
    # Pydantic允许schema类通过hook执行扩展代码；该插件边界的普通失败必须统一
    # 投影为核心编译错误，避免Registry调用方依赖第三方异常层次。
    except Exception as exc:
        raise ValueError("output schema cannot be generated as JSON Schema") from exc
    return compile_output_schema_definition(
        schema_definition,
        schema_ref=schema_ref,
        version=version,
    )


def compile_output_schema_definition(
    schema_definition: Mapping[str, object], *, schema_ref: str, version: str
) -> OutputSchemaDefinition:
    """编译显式JSON Schema，供无Python schema class的可信组合夹具使用。"""

    raw: dict[str, object] = dict(schema_definition)
    strict = _compile_schema_node(raw, root=raw, ref_stack=())
    if strict.get("type") != "object":
        raise ValueError("output schema root must be an object")
    try:
        Draft202012Validator.check_schema(cast(dict[str, Any], strict))
    except SchemaError as exc:
        raise ValueError("output schema is invalid under Draft 2020-12") from exc
    canonical = canonical_structured_json(strict)
    return OutputSchemaDefinition(
        identity=OutputSchemaIdentity(
            schema_ref=schema_ref,
            version=version,
            digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        ),
        schema=strict,
        canonical_schema=canonical,
    )


def _validation_issues(
    value: dict[str, object], *, schema: OutputSchemaDefinition
) -> tuple[list[dict[str, str]], bool]:
    """只投影直接validator errors，不消费不稳定message或递归context。"""

    issues: set[tuple[str, str]] = set()
    has_extra_field = False
    overflow = False

    def add_issue(code: str, parts: Sequence[object]) -> None:
        nonlocal overflow
        path = _json_pointer(parts)
        if path is None:
            overflow = True
        else:
            issues.add((path, code))

    validator = cast(_ValidatorView, Draft202012Validator(schema.schema_definition))
    for error in validator.iter_errors(value):
        base_path = tuple(error.absolute_path)
        keyword = str(error.validator)
        instance_value = error.instance
        if keyword == "required" and isinstance(instance_value, dict):
            required = error.validator_value
            if isinstance(required, list):
                required_names = set(cast(list[str], required))
                instance = cast(dict[str, object], instance_value)
                for name in sorted(required_names - set(instance)):
                    add_issue("missing_required", (*base_path, name))
            continue
        if keyword == "additionalProperties" and isinstance(instance_value, dict):
            instance = cast(dict[str, object], instance_value)
            properties = error.schema.get("properties", {})
            pattern_properties = error.schema.get("patternProperties", {})
            declared = set(
                cast(dict[str, object], properties) if isinstance(properties, dict) else {}
            )
            patterns = tuple(
                re.compile(pattern)
                for pattern in (
                    cast(dict[str, object], pattern_properties)
                    if isinstance(pattern_properties, dict)
                    else {}
                )
            )
            for name in sorted(
                key
                for key in instance
                if key not in declared and not any(pattern.search(key) for pattern in patterns)
            ):
                has_extra_field = True
                add_issue("extra_field", (*base_path, name))
            continue
        code = {
            "type": "type_mismatch",
            "enum": "value_not_allowed",
            "const": "value_not_allowed",
        }.get(keyword, "constraint_violation")
        add_issue(code, base_path)
    ordered = sorted(issues)
    if len(ordered) > 64 or overflow:
        return ([{"code": "validation_issue_overflow", "path": ""}], has_extra_field)
    return ([{"code": code, "path": path} for path, code in ordered], has_extra_field)


def validate_structured_candidate(
    candidate: StructuredProviderCandidate, *, schema: OutputSchemaDefinition
) -> StructuredValidationResult:
    """在provider candidate DTO之后执行唯一核心schema oracle。"""

    if candidate.schema_identity != schema.identity:
        return StructuredValidationResult(
            status="invalid", issues=[{"code": "schema_invalid", "path": ""}]
        )
    raw = candidate.candidate
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return StructuredValidationResult(
                status="invalid", issues=[{"code": "json_invalid", "path": ""}]
            )
    else:
        parsed = raw
    if not isinstance(parsed, dict):
        return StructuredValidationResult(
            status="invalid", issues=[{"code": "json_invalid", "path": ""}]
        )
    try:
        assert_structured_json_value(cast(dict[object, object], parsed))
    except ValueError:
        return StructuredValidationResult(
            status="invalid", issues=[{"code": "json_invalid", "path": ""}]
        )
    value = cast(dict[str, object], parsed)
    issues, has_extra_field = _validation_issues(value, schema=schema)
    if not issues:
        canonical_value = cast(dict[str, Any], json.loads(canonical_structured_json(value)))
        return StructuredValidationResult(status="valid", value=canonical_value)
    status: Literal["invalid", "extra_fields"] = "extra_fields" if has_extra_field else "invalid"
    return StructuredValidationResult(status=status, issues=issues)


__all__ = [
    "compile_output_schema",
    "compile_output_schema_definition",
    "validate_structured_candidate",
]
