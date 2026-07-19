"""0013 canonical run trace 迁移专属的预检与回填协作者。"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, NoReturn, cast
from uuid import UUID, uuid5

import sqlalchemy as sa
from sqlalchemy.engine import RowMapping

_TRACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TRACE_NAMESPACE = UUID("e29d69b4-88a8-4b97-bac5-3f1aa29c0ac2")
# 此清单与 0012a schema 的全部 run_id 列一一对应。nullable=True 只允许
# 非 run-scoped 记录使用 NULL；一旦声明 run_id，仍必须验证 run 存在且 tenant 一致。
_RUN_RELATIONS = (
    ("approvals", False, "trace_id"),
    ("artifacts", True, None),
    ("checkpoints", False, None),
    ("context_assemblies", True, None),
    ("eval_cases", True, "trace_id"),
    ("eval_runs", True, None),
    ("eval_scores", True, "trace_id"),
    ("tool_invocations", True, "trace_id"),
    ("trace_refs", True, None),
    ("workspaces", True, None),
)


def build_backfill_plan(connection: sa.Connection) -> dict[str, Any]:
    """在不写库的前提下汇总谱系 trace，并验证所有可回填证据互不矛盾。

    一个根 run 谱系只能拥有一个 trace；旧数据缺失 trace 时才以稳定 UUID5 补齐。
    任何孤儿关系、跨租户边、循环或冲突候选都会在此阶段失败，确保后续迁移不会
    留下半完成的关联关系。
    """

    run_rows = connection.execute(
        sa.text(
            "select id, tenant_id, parent_run_id, execution_context_json "
            "from agent_runs order by id"
        )
    ).mappings()
    runs: dict[str, dict[str, Any]] = {
        str(row["id"]): {
            "tenant_id": str(row["tenant_id"]),
            "parent_run_id": (
                str(row["parent_run_id"]) if row["parent_run_id"] is not None else None
            ),
            "execution_context": _json_object(row["execution_context_json"], allow_none=True),
        }
        for row in run_rows
    }
    roots: dict[str, str] = {}
    for run_id in runs:
        roots[run_id] = _resolve_root(run_id, runs)
    relation_rows = _preflight_run_relations(connection, runs)
    canonical_events = _preflight_canonical_events(connection, runs)

    candidates: dict[str, set[str]] = defaultdict(set)
    for run_id, row in runs.items():
        execution_context = cast(dict[str, Any], row["execution_context"])
        _add_candidate(candidates[roots[run_id]], execution_context.get("trace_id"))
    for table, _nullable, trace_column in _RUN_RELATIONS:
        if trace_column is None:
            continue
        for row, run_key in relation_rows[table]:
            if run_key is None:
                continue
            _add_candidate(candidates[roots[run_key]], row[trace_column])
    for item in canonical_events:
        row = item["row"]
        run_key = item["run_key"]
        if run_key is None:
            continue
        _add_candidate(candidates[roots[run_key]], row["trace_id"])
        envelope = item["envelope"]
        _add_candidate(candidates[roots[run_key]], envelope.get("trace_id"))
        nested_context = item["telemetry_context"]
        if nested_context is not None:
            _add_candidate(candidates[roots[run_key]], nested_context.get("trace_id"))
    for row in connection.execute(
        sa.text("select id, tenant_id, payload_json from audit_logs")
    ).mappings():
        payload = _json_object(row["payload_json"])
        if payload.get("run_id") is None:
            continue
        run_key = str(payload["run_id"])
        if run_key not in runs:
            _invalid_backfill("orphan run-scoped audit")
        if str(row["tenant_id"]) != runs[run_key]["tenant_id"]:
            _invalid_backfill("cross-tenant run-scoped audit")
        _add_candidate(candidates[roots[run_key]], payload.get("trace_id"))
    for row in connection.execute(
        sa.text("select id, tenant_id, run_id, state_json from checkpoints")
    ).mappings():
        run_id = _validated_run_key(row, runs, nullable=False)
        assert run_id is not None
        state = _json_object(row["state_json"])
        _add_candidate(candidates[roots[run_id]], state.get("trace_id"))
    trace_by_root: dict[str, str] = {}
    used: set[str] = set()
    for root_id in sorted({*roots.values()}):
        values = candidates[root_id]
        if len(values) > 1:
            _invalid_backfill("conflicting lineage traces")
        trace_id = next(iter(values), str(uuid5(_TRACE_NAMESPACE, root_id)))
        if trace_id in used:
            _invalid_backfill("trace reused by multiple root lineages")
        used.add(trace_id)
        trace_by_root[root_id] = trace_id
    return {
        "runs": runs,
        "roots": roots,
        "trace_by_root": trace_by_root,
        "canonical_events": canonical_events,
    }


def _preflight_run_relations(
    connection: sa.Connection,
    runs: dict[str, dict[str, Any]],
) -> dict[str, list[tuple[RowMapping, str | None]]]:
    """统一读取并验证 0012a schema 的全部显式 run_id 关系。"""

    rows_by_table: dict[str, list[tuple[RowMapping, str | None]]] = {}
    for table, nullable, trace_column in _RUN_RELATIONS:
        columns = "id, tenant_id, run_id"
        if trace_column is not None:
            columns = f"{columns}, {trace_column}"
        rows = connection.execute(sa.text(f"select {columns} from {table}")).mappings()
        rows_by_table[table] = [
            (
                row,
                _validated_run_key(
                    row,
                    runs,
                    nullable=nullable,
                ),
            )
            for row in rows
        ]
    return rows_by_table


def _preflight_canonical_events(
    connection: sa.Connection,
    runs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """用 ordinary telemetry nested context 区分真实 run ownership 与合成 stream。"""

    planned: list[dict[str, Any]] = []
    rows = connection.execute(
        sa.text(
            "select id, tenant_id, run_id, agent_id, event_type, seq, terminal, "
            "visibility, payload_json, payload_ref, request_id, trace_id, created_at, "
            "envelope_json "
            "from canonical_events order by id"
        )
    ).mappings()
    for row in rows:
        payload = _json_object(row["payload_json"], allow_none=True)
        envelope = _json_object(row["envelope_json"], allow_none=True)
        envelope_payload = envelope.get("payload")
        if not payload and isinstance(envelope_payload, dict):
            payload = cast(dict[str, Any], envelope_payload)
        telemetry = payload.get("telemetry")
        telemetry_context: dict[str, Any] | None = None
        if isinstance(telemetry, dict) and "context" in telemetry:
            telemetry_payload = cast(dict[str, Any], telemetry)
            telemetry_context = _json_object(telemetry_payload.get("context"))
            context_tenant = telemetry_context.get("tenant_id")
            if context_tenant is not None and str(context_tenant) != str(row["tenant_id"]):
                _invalid_backfill("cross-tenant ordinary telemetry context")
            nested_run_id = telemetry_context.get("run_id")
            if nested_run_id is None:
                run_key = None
            else:
                if not isinstance(nested_run_id, str) or not nested_run_id:
                    _invalid_backfill("invalid ordinary telemetry run ownership")
                run_key = _validated_run_key(
                    {"run_id": nested_run_id, "tenant_id": row["tenant_id"]},
                    runs,
                    nullable=False,
                )
        else:
            run_key = _validated_run_key(row, runs, nullable=False)
        envelope_run_id = envelope.get("run_id")
        if envelope_run_id is None:
            envelope_run_id = row["run_id"]
        if not isinstance(envelope_run_id, str) or not envelope_run_id:
            _invalid_backfill("invalid canonical event stream")
        planned.append(
            {
                "row": row,
                "run_key": run_key,
                "stream_id": envelope_run_id,
                "envelope": envelope,
                "payload": (
                    payload
                    if row["payload_json"] is not None or isinstance(envelope_payload, dict)
                    else None
                ),
                "telemetry_context": telemetry_context,
            }
        )
    return planned


def apply_backfill(connection: sa.Connection, plan: dict[str, Any]) -> None:
    """将已通过预检的 trace 计划写入所有 run-scoped 表及嵌套事件载荷。

    调用方必须先使用同一连接生成 ``plan``；本函数假定其输入已验证，因此只做
    确定性更新。canonical event 的普通 telemetry context 也随所属 run 更新，
    而非 run-scoped 的合成 stream 则保留原有 trace。
    """

    runs = plan["runs"]
    roots = plan["roots"]
    trace_by_root = plan["trace_by_root"]
    json_parameter = sa.bindparam("payload", type_=sa.JSON())
    for run_id, row in runs.items():
        trace_id = trace_by_root[roots[run_id]]
        execution_context = dict(row["execution_context"])
        execution_context["trace_id"] = trace_id
        connection.execute(
            sa.text(
                "update agent_runs set trace_id=:trace_id, "
                "execution_context_json=:payload where id=:id"
            ).bindparams(json_parameter),
            {"trace_id": trace_id, "payload": execution_context, "id": run_id},
        )
    for root_id, trace_id in trace_by_root.items():
        connection.execute(
            sa.text(
                "insert into run_trace_bindings(trace_id, tenant_id, root_run_id) "
                "values (:trace_id, :tenant_id, :root_run_id)"
            ),
            {
                "trace_id": trace_id,
                "tenant_id": runs[root_id]["tenant_id"],
                "root_run_id": root_id,
            },
        )
    for table in ("approvals", "tool_invocations", "eval_cases", "eval_scores"):
        for run_id, root_id in roots.items():
            connection.execute(
                sa.text(f"update {table} set trace_id=:trace_id where run_id=:run_id"),
                {"trace_id": trace_by_root[root_id], "run_id": run_id},
            )
    for item in plan["canonical_events"]:
        row = item["row"]
        run_key = item["run_key"]
        envelope = _complete_canonical_event_envelope(
            row,
            dict(item["envelope"]),
            stream_id=item["stream_id"],
            payload=item["payload"],
        )
        record_scope = "run" if run_key is not None else "non_run"
        envelope["record_scope"] = record_scope
        trace_id = row["trace_id"]
        if run_key is not None:
            trace_id = trace_by_root[roots[run_key]]
            envelope["trace_id"] = trace_id
            telemetry_context = item["telemetry_context"]
            if telemetry_context is not None:
                payload = _json_object(envelope.get("payload"), allow_none=True)
                telemetry = payload.get("telemetry")
                if isinstance(telemetry, dict):
                    telemetry_payload = cast(dict[str, Any], telemetry)
                    context_payload = dict(telemetry_context)
                    context_payload["trace_id"] = trace_id
                    telemetry_payload["context"] = context_payload
                    payload["telemetry"] = telemetry_payload
                    envelope["payload"] = payload
        connection.execute(
            sa.text(
                "update canonical_events set run_id=:run_id, stream_id=:stream_id, "
                "trace_id=:trace_id, record_scope=:record_scope, envelope_json=:payload "
                "where id=:id"
            ).bindparams(json_parameter),
            {
                "run_id": run_key,
                "stream_id": item["stream_id"],
                "trace_id": trace_id,
                "record_scope": record_scope,
                "payload": envelope,
                "id": row["id"],
            },
        )
    for row in connection.execute(
        sa.text("select id, run_id, state_json from checkpoints")
    ).mappings():
        state = _json_object(row["state_json"])
        state["trace_id"] = trace_by_root[roots[str(row["run_id"])]]
        connection.execute(
            sa.text("update checkpoints set state_json=:payload where id=:id").bindparams(
                json_parameter
            ),
            {"payload": state, "id": row["id"]},
        )
    for table in ("trace_refs", "eval_runs"):
        for run_id, root_id in roots.items():
            connection.execute(
                sa.text(f"update {table} set trace_id=:trace_id where run_id=:run_id"),
                {"trace_id": trace_by_root[root_id], "run_id": run_id},
            )
    for row in connection.execute(sa.text("select id, payload_json from audit_logs")).mappings():
        payload = _json_object(row["payload_json"])
        run_id = payload.get("run_id")
        if run_id is None:
            continue
        run_key = str(run_id)
        if run_key not in roots:
            _invalid_backfill("orphan run-scoped audit")
        payload["trace_id"] = trace_by_root[roots[run_key]]
        connection.execute(
            sa.text(
                "update audit_logs set payload_json=:payload, record_scope='run' where id=:id"
            ).bindparams(json_parameter),
            {"payload": payload, "id": row["id"]},
        )


def _complete_canonical_event_envelope(
    row: RowMapping,
    envelope: dict[str, Any],
    *,
    stream_id: str,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """用 0011 持久化列补齐旧事件；已有 envelope 扩展字段保持不变。"""

    timestamp = row["created_at"]
    if hasattr(timestamp, "isoformat"):
        timestamp = timestamp.isoformat()
    defaults = {
        "event_id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "run_id": stream_id,
        "agent_id": row["agent_id"],
        "event_type": str(row["event_type"]),
        "seq": int(row["seq"]),
        "timestamp": str(timestamp),
        "payload": payload,
        "payload_ref": row["payload_ref"],
        "terminal": bool(row["terminal"]),
        "visibility": str(row["visibility"]),
        "request_id": row["request_id"],
        "trace_id": row["trace_id"],
    }
    for key, value in defaults.items():
        envelope.setdefault(key, value)
    return envelope


def _resolve_root(run_id: str, runs: dict[str, dict[str, Any]]) -> str:
    """沿 parent_run_id 向上定位谱系根，同时拒绝循环、孤儿和跨租户父边。"""

    current = run_id
    seen: set[str] = set()
    while True:
        if current in seen:
            _invalid_backfill("run lineage cycle")
        seen.add(current)
        row = runs[current]
        parent_id = row["parent_run_id"]
        if parent_id is None:
            return current
        if parent_id not in runs:
            _invalid_backfill("orphan parent run")
        parent = runs[parent_id]
        if parent["tenant_id"] != row["tenant_id"]:
            _invalid_backfill("cross-tenant parent edge")
        current = parent_id


def _validated_run_key(
    row: RowMapping | dict[str, Any],
    runs: dict[str, dict[str, Any]],
    *,
    nullable: bool,
) -> str | None:
    """在任何 migration mutation 前验证 evidence 声明的 run/tenant 关系。"""

    run_id = row["run_id"]
    if run_id is None:
        if nullable:
            return None
        _invalid_backfill("missing run ownership")
    run_key = str(run_id)
    if run_key not in runs:
        _invalid_backfill("orphan run-scoped record")
    if str(row["tenant_id"]) != runs[run_key]["tenant_id"]:
        _invalid_backfill("cross-tenant run-scoped record")
    return run_key


def _add_candidate(values: set[str], raw: object) -> None:
    """接受格式合法的非空 trace 候选；非法值立即终止回填预检。"""

    if raw is None:
        return
    if not isinstance(raw, str) or _TRACE_PATTERN.fullmatch(raw) is None:
        _invalid_backfill("invalid canonical trace")
    values.add(raw)


def _json_object(raw: object, *, allow_none: bool = False) -> dict[str, Any]:
    """解析持久化 JSON 对象，禁止数组、标量和损坏文本混入迁移计划。"""

    if raw is None and allow_none:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            _invalid_backfill("invalid json evidence")
    if not isinstance(raw, dict):
        _invalid_backfill("invalid json evidence")
    return cast(dict[str, Any], raw)


def _invalid_backfill(reason: str) -> NoReturn:
    """隐藏底层载荷细节后终止预检，避免迁移错误日志泄露历史证据内容。"""

    del reason
    raise RuntimeError("0013 canonical trace backfill preflight failed")
