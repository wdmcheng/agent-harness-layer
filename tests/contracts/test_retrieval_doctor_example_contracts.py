"""Retrieval doctor 降级输出与 RAG 示例 fixture 契约测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn

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
    """RAG 示例基础数据不声明完整示例产品流已完成。"""

    from agent_harness.registry import AgentRegistry

    agents_dir = ROOT / "templates" / "service-app" / "agents"
    registry = AgentRegistry.load_from_directory(agents_dir)
    descriptor = registry.get("examples.rag_assistant")
    eval_path = agents_dir / "examples" / "rag_assistant" / "evals" / "approved.yaml"
    eval_data = yaml.safe_load(eval_path.read_text(encoding="utf-8"))

    assert descriptor.eval_dataset == "agents/examples/rag_assistant/evals/approved.yaml"
    assert descriptor.tool_policy.allowed_tools == ["retrieval.query"]
    assert {case["id"] for case in eval_data["cases"]} == {
        "rag-citation-hit",
        "rag-no-source",
    }
    assert eval_data["cases"][0]["expected"]["must_include_citation"] is True
    assert eval_data["cases"][1]["expected"]["no_source_behavior"] == "state_no_source"
