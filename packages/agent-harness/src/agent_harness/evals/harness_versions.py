"""覆盖全部行为输入的不可变 harness version manifest。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath
from typing import cast

from pydantic import Field, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.evals.errors import EvalExperimentError
from agent_harness.security.redaction import redact_secrets

REQUIRED_HARNESS_INPUTS = (
    "prompt_instruction",
    "tool_descriptions",
    "agent_config",
    "retrieval_config",
    "policy_defaults",
    "model_adapter_settings",
)


class HarnessInputSource(HarnessDTO):
    """只在构建边界短暂持有原始输入；manifest 不保留 value。"""

    value: object
    diff_summary: str | None = None
    evidence_ref: str | None = None


class HarnessInputManifest(HarnessDTO):
    """单类行为输入的 checksum 与脱敏 evidence metadata。"""

    checksum_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    diff_summary: str | None = None
    evidence_ref: str | None = None

    @model_validator(mode="after")
    def validate_safe_metadata(self) -> HarnessInputManifest:
        if self.diff_summary is not None:
            _reject_secret(self.diff_summary, field_path="diff_summary")
        if self.evidence_ref is not None:
            _validate_evidence_ref(self.evidence_ref, field_path="evidence_ref")
        return self


class HarnessVersionManifest(HarnessDTO):
    """baseline/candidate 共用、内容可验证的稳定版本。"""

    version_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    inputs: dict[str, HarnessInputManifest]

    @model_validator(mode="after")
    def validate_version_id(self) -> HarnessVersionManifest:
        provided = set(self.inputs)
        required = set(REQUIRED_HARNESS_INPUTS)
        if provided != required:
            raise ValueError("harness manifest inputs must match required categories")
        expected = _manifest_version_id(self.inputs)
        if self.version_id != expected:
            raise ValueError("harness version_id does not match normalized inputs")
        return self


class HarnessVersionBuilder:
    """规范化 mapping/list 后计算 checksum，拒绝 secret 与 SDK object。"""

    def build(self, sources: Mapping[str, HarnessInputSource]) -> HarnessVersionManifest:
        provided = set(sources)
        required = set(REQUIRED_HARNESS_INPUTS)
        if provided != required:
            missing = sorted(required - provided)
            unexpected = sorted(provided - required)
            raise EvalExperimentError(
                "eval.harness.inputs_incomplete",
                "harness inputs must match the required closed categories",
                status_code=422,
                field_path="inputs",
                hint=f"missing={missing}; unexpected={unexpected}",
            )

        inputs: dict[str, HarnessInputManifest] = {}
        for category in REQUIRED_HARNESS_INPUTS:
            source = sources[category]
            normalized = _normalize_input(source.value, field_path=f"inputs.{category}.value")
            _reject_secret(normalized, field_path=f"inputs.{category}.value")
            if source.diff_summary is not None:
                _reject_secret(
                    source.diff_summary,
                    field_path=f"inputs.{category}.diff_summary",
                )
            if source.evidence_ref is not None:
                _validate_evidence_ref(
                    source.evidence_ref,
                    field_path=f"inputs.{category}.evidence_ref",
                )
            encoded = _canonical_json(normalized)
            inputs[category] = HarnessInputManifest(
                checksum_sha256=hashlib.sha256(encoded).hexdigest(),
                diff_summary=source.diff_summary,
                evidence_ref=source.evidence_ref,
            )

        version_id = _manifest_version_id(inputs)
        return HarnessVersionManifest(version_id=version_id, inputs=inputs)


def _normalize_input(value: object, *, field_path: str) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise _unserializable(field_path)
        return value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        if not all(isinstance(key, str) for key in mapping):
            raise _unserializable(field_path)
        return {
            str(key): _normalize_input(item, field_path=field_path)
            for key, item in sorted(mapping.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        sequence = cast(list[object] | tuple[object, ...], value)
        items = [_normalize_input(item, field_path=field_path) for item in sequence]
        return sorted(items, key=_canonical_json)
    raise _unserializable(field_path)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise _unserializable("inputs") from exc


def _unserializable(field_path: str) -> EvalExperimentError:
    return EvalExperimentError(
        "eval.harness.input_unserializable",
        "harness input must be provider-neutral JSON data",
        status_code=422,
        field_path=field_path,
    )


def _reject_secret(value: object, *, field_path: str) -> None:
    if _contains_redaction_marker(value) or redact_secrets(value) != value:
        raise EvalExperimentError(
            "eval.harness.input_secret",
            "harness input contains secret-shaped data",
            status_code=422,
            field_path=field_path,
        )


def _contains_redaction_marker(value: object) -> bool:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return any(_contains_redaction_marker(item) for item in mapping.values())
    if isinstance(value, list):
        items = cast(list[object], value)
        return any(_contains_redaction_marker(item) for item in items)
    return isinstance(value, str) and "[REDACTED]" in value


def _validate_evidence_ref(ref: str, *, field_path: str) -> None:
    if (
        Path(ref).is_absolute()
        or PureWindowsPath(ref).is_absolute()
        or ref.casefold().startswith("file://")
        or _contains_redaction_marker(ref)
        or redact_secrets(ref) != ref
    ):
        raise EvalExperimentError(
            "eval.harness.evidence_ref_unsafe",
            "harness evidence ref contains a secret or absolute local path",
            status_code=422,
            field_path=field_path,
        )


def _manifest_version_id(inputs: Mapping[str, HarnessInputManifest]) -> str:
    version_payload = {
        category: inputs[category].to_payload() for category in REQUIRED_HARNESS_INPUTS
    }
    return hashlib.sha256(_canonical_json(version_payload)).hexdigest()
