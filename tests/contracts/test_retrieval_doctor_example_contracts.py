"""Retrieval doctor 降级输出与 RAG 示例 fixture 契约测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn

from agent_harness.evals import ReviewDatasetAdapter
from agent_harness.storage import run_migrations

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


def test_retrieval_extension_status_formats_optional_degradation() -> None:
    """doctor 输出要区分 optional extension 缺失和 service 必需项失败。"""

    from agent_harness.storage.diagnostics import (
        ExtensionStatus,
        format_retrieval_extension_status,
    )

    installed = ExtensionStatus(name="vector", status="installed", installed_version="0.8.1")
    missing = ExtensionStatus(name="pgroonga", status="missing")

    assert format_retrieval_extension_status(installed) == "vector: installed (0.8.1)"
    assert format_retrieval_extension_status(missing) == (
        "pgroonga: missing (optional; degraded to PostgreSQL native FTS/local BM25)"
    )


def test_doctor_cli_reports_local_retrieval_extensions_not_required(tmp_path: Path) -> None:
    """local doctor 不得把 PGroonga/pgvector 当成本地必需依赖。"""

    db_path = tmp_path / "doctor.db"
    run_migrations(sqlite_dsn(db_path))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "doctor",
            "--profile",
            "local",
            "--profiles-dir",
            str(PROFILES),
            "--storage-dsn",
            sqlite_dsn(db_path),
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "retrieval extension pgroonga: not required for local profile" in result.stdout
    assert "retrieval extension vector: not required for local profile" in result.stdout


def test_rag_assistant_example_config_and_eval_fixture_are_loadable() -> None:
    """RAG config 指向真实 approved dataset，draft 不混入评分。"""

    from agent_harness.registry import AgentRegistry

    agents_dir = ROOT / "templates" / "service-app" / "agents"
    registry = AgentRegistry.load_from_directory(agents_dir)
    descriptor = registry.get("examples.rag_assistant")
    eval_root = agents_dir / "examples" / "rag_assistant" / "evals"
    adapter = ReviewDatasetAdapter(
        drafts_dir=eval_root / "drafts",
        approved_dir=eval_root / "approved",
    )
    approved = adapter.load_approved(agent_id="examples.rag_assistant")

    assert descriptor.eval_dataset == "agents/examples/rag_assistant/evals"
    assert descriptor.tool_policy.allowed_tools == ["retrieval.query"]
    assert {case["case_id"] for case in approved} == {
        "rag-citation-hit",
        "rag-injection-boundary",
        "rag-no-source",
    }
    assert adapter.count_drafts(agent_id="examples.rag_assistant") == 1
