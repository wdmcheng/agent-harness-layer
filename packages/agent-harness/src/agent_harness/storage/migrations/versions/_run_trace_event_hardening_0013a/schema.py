"""识别并预检 0013a 允许的旧/最终关系库 shape。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple, NoReturn

import sqlalchemy as sa

from .check_sql import (
    EXPECTED_CHECK_SIGNATURES,
    CheckExpressionParser,
    CheckParseError,
)

_OLD_EVENT_SEQUENCE = "uq_canonical_events_run_seq"
_EVENT_SEQUENCE = "uq_canonical_events_tenant_stream_seq"
_EVENT_SCOPE = "ck_canonical_events_record_scope"
_EVENT_RUN_OWNERSHIP = "ck_canonical_events_run_ownership"
_EVENT_NON_RUN_OWNERSHIP = "ck_canonical_events_non_run_ownership"
_EVENT_RUN_OWNER = "fk_canonical_events_run_owner"
_EVENT_STREAM_INDEX = "ix_canonical_events_stream_id"
_RUN_OWNER_KEY = "uq_agent_runs_id_tenant_trace"
_AUDIT_SCOPE = "ck_audit_logs_record_scope"


class _Shape(NamedTuple):
    stream_column: bool
    stream_nullable: bool | None
    run_nullable: bool
    trace_nullable: bool
    old_event_sequence: bool
    event_sequence: bool
    event_scope: bool
    event_run_ownership: bool
    event_non_run_ownership: bool
    event_run_owner: bool
    event_stream_index: bool
    run_owner_key: bool
    audit_scope: bool


_LEGACY_SHAPE = _Shape(
    stream_column=False,
    stream_nullable=None,
    run_nullable=False,
    trace_nullable=False,
    old_event_sequence=True,
    event_sequence=False,
    event_scope=False,
    event_run_ownership=False,
    event_non_run_ownership=False,
    event_run_owner=False,
    event_stream_index=False,
    run_owner_key=False,
    audit_scope=False,
)

_FINAL_SHAPE = _Shape(
    stream_column=True,
    stream_nullable=False,
    run_nullable=True,
    trace_nullable=True,
    old_event_sequence=False,
    event_sequence=True,
    event_scope=True,
    event_run_ownership=True,
    event_non_run_ownership=True,
    event_run_owner=True,
    event_stream_index=True,
    run_owner_key=True,
    audit_scope=True,
)


def _require_supported_dialect(connection: sa.Connection) -> None:
    if connection.dialect.name not in {"sqlite", "postgresql"}:
        raise RuntimeError("0013a supports only SQLite and PostgreSQL")


def _classify_shape(connection: sa.Connection) -> str:
    inspector = sa.inspect(connection)
    required_tables = {
        "agent_runs",
        "audit_logs",
        "canonical_events",
        "run_trace_bindings",
    }
    if not required_tables.issubset(set(inspector.get_table_names())):
        _invalid_shape()

    event_columns = {column["name"]: column for column in inspector.get_columns("canonical_events")}
    audit_columns = {column["name"]: column for column in inspector.get_columns("audit_logs")}
    run_columns = {column["name"]: column for column in inspector.get_columns("agent_runs")}
    for column_name in ("run_id", "trace_id", "record_scope"):
        if column_name not in event_columns:
            _invalid_shape()
    if "record_scope" not in audit_columns or "trace_id" not in run_columns:
        _invalid_shape()
    if event_columns["record_scope"]["nullable"] or audit_columns["record_scope"]["nullable"]:
        _invalid_shape()

    event_uniques = _named(inspector.get_unique_constraints("canonical_events"))
    event_checks = _named(inspector.get_check_constraints("canonical_events"))
    event_foreign_keys = _named(inspector.get_foreign_keys("canonical_events"))
    event_indexes = _named(inspector.get_indexes("canonical_events"))
    run_uniques = _named(inspector.get_unique_constraints("agent_runs"))
    audit_checks = _named(inspector.get_check_constraints("audit_logs"))
    _validate_present_check_definitions(
        event_checks=event_checks,
        audit_checks=audit_checks,
    )
    stream_column = event_columns.get("stream_id")
    shape = _Shape(
        stream_column=stream_column is not None,
        stream_nullable=(None if stream_column is None else bool(stream_column["nullable"])),
        run_nullable=bool(event_columns["run_id"]["nullable"]),
        trace_nullable=bool(event_columns["trace_id"]["nullable"]),
        old_event_sequence=_OLD_EVENT_SEQUENCE in event_uniques,
        event_sequence=_EVENT_SEQUENCE in event_uniques,
        event_scope=_EVENT_SCOPE in event_checks,
        event_run_ownership=_EVENT_RUN_OWNERSHIP in event_checks,
        event_non_run_ownership=_EVENT_NON_RUN_OWNERSHIP in event_checks,
        event_run_owner=_EVENT_RUN_OWNER in event_foreign_keys,
        event_stream_index=_EVENT_STREAM_INDEX in event_indexes,
        run_owner_key=_RUN_OWNER_KEY in run_uniques,
        audit_scope=_AUDIT_SCOPE in audit_checks,
    )
    if shape == _LEGACY_SHAPE:
        _validate_legacy_definitions(event_uniques)
        return "legacy"
    if shape == _FINAL_SHAPE:
        _validate_final_definitions(
            event_uniques=event_uniques,
            event_foreign_keys=event_foreign_keys,
            event_indexes=event_indexes,
            run_uniques=run_uniques,
        )
        return "final"
    _invalid_shape()


def _validate_legacy_definitions(event_uniques: dict[str, Mapping[str, Any]]) -> None:
    if _columns(event_uniques[_OLD_EVENT_SEQUENCE]) != ("run_id", "seq"):
        _invalid_shape()


def _validate_present_check_definitions(
    *,
    event_checks: dict[str, Mapping[str, Any]],
    audit_checks: dict[str, Mapping[str, Any]],
) -> None:
    """同名 CHECK 必须语义精确，不能靠名称把弱约束伪装成最终 shape。"""

    reflected = {
        **{
            name: event_checks[name]
            for name in (
                _EVENT_SCOPE,
                _EVENT_RUN_OWNERSHIP,
                _EVENT_NON_RUN_OWNERSHIP,
            )
            if name in event_checks
        },
        **({_AUDIT_SCOPE: audit_checks[_AUDIT_SCOPE]} if _AUDIT_SCOPE in audit_checks else {}),
    }
    for name, item in reflected.items():
        sqltext = item.get("sqltext")
        if sqltext is None:
            _invalid_shape()
        try:
            actual = CheckExpressionParser(str(sqltext)).parse()
        except CheckParseError:
            _invalid_shape()
        if actual not in EXPECTED_CHECK_SIGNATURES[name]:
            _invalid_shape()


def _validate_final_definitions(
    *,
    event_uniques: dict[str, Mapping[str, Any]],
    event_foreign_keys: dict[str, Mapping[str, Any]],
    event_indexes: dict[str, Mapping[str, Any]],
    run_uniques: dict[str, Mapping[str, Any]],
) -> None:
    if _columns(event_uniques[_EVENT_SEQUENCE]) != ("tenant_id", "stream_id", "seq"):
        _invalid_shape()
    if _columns(run_uniques[_RUN_OWNER_KEY]) != ("id", "tenant_id", "trace_id"):
        _invalid_shape()
    if _columns(event_indexes[_EVENT_STREAM_INDEX]) != ("stream_id",):
        _invalid_shape()
    owner = event_foreign_keys[_EVENT_RUN_OWNER]
    if (
        _columns(owner) != ("run_id", "tenant_id", "trace_id")
        or owner.get("referred_table") != "agent_runs"
        or tuple(owner.get("referred_columns") or ()) != ("id", "tenant_id", "trace_id")
    ):
        _invalid_shape()


def _preflight_legacy_data(connection: sa.Connection) -> None:
    checks = (
        (
            "invalid event scope",
            "select count(*) from canonical_events "
            "where record_scope not in ('run', 'non_run') or record_scope is null",
        ),
        (
            "missing legacy event stream",
            "select count(*) from canonical_events where run_id is null",
        ),
        (
            "invalid run event ownership",
            "select count(*) from canonical_events event "
            "left join agent_runs run on run.id=event.run_id "
            "and run.tenant_id=event.tenant_id and run.trace_id=event.trace_id "
            "where event.record_scope='run' and run.id is null",
        ),
        (
            "duplicate tenant stream sequence",
            "select count(*) from (select tenant_id, run_id, seq from canonical_events "
            "group by tenant_id, run_id, seq having count(*) > 1) duplicate_stream",
        ),
        (
            "invalid audit scope",
            "select count(*) from audit_logs "
            "where record_scope not in ('run', 'non_run') or record_scope is null",
        ),
    )
    _run_preflight_checks(connection, checks)


def _preflight_final_data(connection: sa.Connection) -> None:
    checks = (
        (
            "invalid event scope",
            "select count(*) from canonical_events "
            "where record_scope not in ('run', 'non_run') or record_scope is null",
        ),
        (
            "missing event stream",
            "select count(*) from canonical_events where stream_id is null",
        ),
        (
            "invalid run event ownership",
            "select count(*) from canonical_events event "
            "left join agent_runs run on run.id=event.run_id "
            "and run.tenant_id=event.tenant_id and run.trace_id=event.trace_id "
            "where event.record_scope='run' and run.id is null",
        ),
        (
            "invalid non-run event ownership",
            "select count(*) from canonical_events "
            "where record_scope='non_run' and run_id is not null",
        ),
        (
            "duplicate tenant stream sequence",
            "select count(*) from (select tenant_id, stream_id, seq from canonical_events "
            "group by tenant_id, stream_id, seq having count(*) > 1) duplicate_stream",
        ),
        (
            "invalid audit scope",
            "select count(*) from audit_logs "
            "where record_scope not in ('run', 'non_run') or record_scope is null",
        ),
    )
    _run_preflight_checks(connection, checks)


def _run_preflight_checks(
    connection: sa.Connection,
    checks: tuple[tuple[str, str], ...],
) -> None:
    for reason, statement in checks:
        if int(connection.execute(sa.text(statement)).scalar_one()):
            raise RuntimeError(f"0013a preflight failed: {reason}")


def _named(items: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(item["name"]): item for item in items if item.get("name") is not None}


def _columns(item: Mapping[str, Any]) -> tuple[str, ...]:
    columns = item.get("column_names") or item.get("constrained_columns") or ()
    return tuple(str(column) for column in columns)


def _invalid_shape() -> NoReturn:
    raise RuntimeError("0013a preflight failed: incompatible or partial 0013 schema shape")


# 入口只经这些 revision 专属公开别名依赖协作者，底层对象名称保持原样。
OLD_EVENT_SEQUENCE = _OLD_EVENT_SEQUENCE
EVENT_SEQUENCE = _EVENT_SEQUENCE
EVENT_SCOPE = _EVENT_SCOPE
EVENT_RUN_OWNERSHIP = _EVENT_RUN_OWNERSHIP
EVENT_NON_RUN_OWNERSHIP = _EVENT_NON_RUN_OWNERSHIP
EVENT_RUN_OWNER = _EVENT_RUN_OWNER
EVENT_STREAM_INDEX = _EVENT_STREAM_INDEX
RUN_OWNER_KEY = _RUN_OWNER_KEY
AUDIT_SCOPE = _AUDIT_SCOPE
require_supported_dialect = _require_supported_dialect
classify_shape = _classify_shape
invalid_shape = _invalid_shape
preflight_final_data = _preflight_final_data
preflight_legacy_data = _preflight_legacy_data
