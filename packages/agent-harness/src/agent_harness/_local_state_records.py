"""Local-state JSONL 记录预检与 run scope 识别。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from agent_harness._local_state_common import LocalStateKind, LocalStateMigrationError


def preflight_file(
    path: Path,
    *,
    kind: LocalStateKind,
    file_only: bool,
    trace_by_run_id: Mapping[str, str] | None,
) -> list[dict[str, Any]]:
    """解析并规范化一个 inventory 文件，不在预检阶段写盘。"""

    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise LocalStateMigrationError(
            "local_state.path_unreadable", "inventory path is unreadable"
        ) from exc
    for line in lines:
        if not line:
            continue
        try:
            raw = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise LocalStateMigrationError(
                "local_state.record_invalid", "JSONL record is invalid"
            ) from exc
        if not isinstance(raw, dict):
            raise LocalStateMigrationError("local_state.record_invalid", "JSONL record is invalid")
        record = cast(dict[str, Any], raw)
        record_scope, run_id = _record_scope_and_run_id(record, kind=kind)
        if record_scope == "run":
            assert run_id is not None
            if file_only:
                raise LocalStateMigrationError(
                    "local_state.run_scope_requires_profile",
                    "run-scoped evidence requires profile mode",
                )
            assert trace_by_run_id is not None
            trace_id = trace_by_run_id.get(run_id)
            if trace_id is None:
                raise LocalStateMigrationError(
                    "local_state.orphan_run",
                    "run-scoped evidence references an unknown run",
                )
            record["trace_id"] = trace_id
            record["record_scope"] = "run"
            _rewrite_telemetry_context(record, trace_id)
        else:
            record["record_scope"] = "non_run"
        records.append(record)
    return records


def _record_scope_and_run_id(
    record: Mapping[str, Any],
    *,
    kind: LocalStateKind,
) -> tuple[Literal["run", "non_run"], str | None]:
    """先解释显式 discriminator；只有旧记录才进入结构推断。"""

    if "record_scope" in record:
        record_scope = record.get("record_scope")
        if record_scope == "non_run":
            return "non_run", None
        if record_scope != "run":
            raise LocalStateMigrationError(
                "local_state.record_scope_invalid",
                "record scope is invalid",
            )
        run_id = _top_level_run_id(record)
        if run_id is None:
            raise LocalStateMigrationError(
                "local_state.run_owner_invalid",
                "run-scoped evidence requires a real run owner",
            )
        return "run", run_id

    run_id = _legacy_real_run_id(record, kind=kind)
    return ("run", run_id) if run_id is not None else ("non_run", None)


def _legacy_real_run_id(record: Mapping[str, Any], *, kind: LocalStateKind) -> str | None:
    """仅为缺少 discriminator 的旧记录恢复真实 run owner。"""

    if kind == "events":
        payload = record.get("payload")
        if isinstance(payload, dict):
            telemetry = cast(dict[str, Any], payload).get("telemetry")
            if isinstance(telemetry, dict):
                context = cast(dict[str, Any], telemetry).get("context")
                if isinstance(context, dict):
                    nested = cast(dict[str, Any], context).get("run_id")
                    return nested if isinstance(nested, str) and nested else None
    return _top_level_run_id(record)


def _top_level_run_id(record: Mapping[str, Any]) -> str | None:
    raw = record.get("run_id")
    return raw if isinstance(raw, str) and raw else None


def _rewrite_telemetry_context(record: dict[str, Any], trace_id: str) -> None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return
    telemetry = cast(dict[str, Any], payload).get("telemetry")
    if not isinstance(telemetry, dict):
        return
    context = cast(dict[str, Any], telemetry).get("context")
    if isinstance(context, dict):
        cast(dict[str, Any], context)["trace_id"] = trace_id
