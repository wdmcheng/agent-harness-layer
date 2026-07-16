"""0013a 已发布 shape 的 revision 与 CHECK parser 合同。"""

from __future__ import annotations

import importlib

import pytest
from alembic.script import ScriptDirectory
from tests.contracts.run_trace_revision_hardening_helpers import (
    REVISION_0013,
    REVISION_0013A,
)

from agent_harness.storage.migrations.runner import alembic_config, get_head_revision


def test_trace_event_hardening_is_the_unique_linear_head() -> None:
    """已发布 0013/0014 不改写；后续迁移只能追加线性后继。"""

    script = ScriptDirectory.from_config(alembic_config("sqlite+aiosqlite:///:memory:"))
    assert script.get_heads() == ["0015_agent_delegation"]
    hardening = script.get_revision(REVISION_0013A)
    delegation = script.get_revision("0015_agent_delegation")
    assert hardening is not None
    assert hardening.down_revision == REVISION_0013
    assert delegation is not None
    assert delegation.down_revision == "0014_run_evidence_outbox"
    assert get_head_revision() == "0015_agent_delegation"


@pytest.mark.parametrize(
    ("name", "sqltext"),
    (
        (
            "ck_canonical_events_record_scope",
            "record_scope IN ('run', 'non_run')",
        ),
        (
            "ck_canonical_events_run_ownership",
            "CHECK (((trace_id IS NOT NULL AND run_id IS NOT NULL) OR "
            "((record_scope)::text <> 'run'::text)))",
        ),
        (
            "ck_canonical_events_non_run_ownership",
            "run_id IS NULL OR record_scope != 'non_run'",
        ),
        (
            "ck_audit_logs_record_scope",
            "CHECK ((record_scope)::text = ANY "
            "((ARRAY['run'::character varying, 'non_run'::character varying])::text[]))",
        ),
    ),
)
def test_check_signature_parser_accepts_controlled_sqlite_and_postgresql_equivalents(
    name: str,
    sqltext: str,
) -> None:
    """受控 AST 容忍方言 cast/括号/可交换顺序，但仍保留完整逻辑结构。"""

    migration = importlib.import_module(
        "agent_harness.storage.migrations.versions.0013a_run_trace_event_hardening"
    )
    actual = migration._CheckExpressionParser(sqltext).parse()

    assert actual in migration._EXPECTED_CHECK_SIGNATURES[name]


@pytest.mark.parametrize(
    "sqltext",
    (
        "record_scope::bpchar IN ('run'::bpchar, 'non_run'::bpchar)",
        "record_scope::char IN ('run'::char, 'non_run'::char)",
        "record_scope::name IN ('run'::name, 'non_run'::name)",
        "record_scope::citext IN ('run'::citext, 'non_run'::citext)",
        "record_scope::scope_domain IN ('run'::scope_domain, 'non_run'::scope_domain)",
        "record_scope::text::text IN ('run'::text, 'non_run'::text)",
        "record_scope::text IN ('run', 'non_run')",
        "record_scope IN ('run'::text, 'non_run'::text)",
        "record_scope::text = ANY (ARRAY['run'::bpchar, 'non_run'::bpchar]::text[])",
        "record_scope COLLATE \"C\" IN ('run', 'non_run')",
    ),
)
def test_check_signature_parser_rejects_cast_and_collation_semantic_changes(
    sqltext: str,
) -> None:
    """非合同类型、多重 cast、单边 cast 与 collation 都不能伪装成 scope 合同。"""

    migration = importlib.import_module(
        "agent_harness.storage.migrations.versions.0013a_run_trace_event_hardening"
    )
    try:
        actual = migration._CheckExpressionParser(sqltext).parse()
    except migration._CheckParseError:
        return

    assert actual not in migration._EXPECTED_CHECK_SIGNATURES["ck_canonical_events_record_scope"]


@pytest.mark.parametrize(
    "sqltext",
    (
        "(" * 65 + "record_scope IN ('run', 'non_run')" + ")" * 65,
        " " * 4097,
        " OR ".join(["record_scope IS NULL"] * 130),
    ),
)
def test_check_signature_parser_rejects_expression_complexity_over_limits(
    sqltext: str,
) -> None:
    """长度、token 或嵌套越过受控边界时统一返回稳定解析错误。"""

    migration = importlib.import_module(
        "agent_harness.storage.migrations.versions.0013a_run_trace_event_hardening"
    )

    with pytest.raises(migration._CheckParseError):
        migration._CheckExpressionParser(sqltext).parse()
