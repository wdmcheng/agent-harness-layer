"""Eval gate CLI 与 make eval 执行合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_gate_storage_cli_contracts import (
    ROOT as ROOT,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    Path as Path,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    json as json,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    sqlite3 as sqlite3,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    subprocess as subprocess,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    sys as sys,
)


def test_eval_cli_draft_and_approve_use_storage_and_audit(tmp_path: Path) -> None:
    """CLI approve 也必须走 repository/audit，并把 draft 移出 review queue。"""

    db_path = tmp_path / "eval-cli.db"
    dsn = sqlite_dsn(db_path)
    dataset_dir = tmp_path / "eval-cases"
    scores_path = tmp_path / "scores.jsonl"
    run_migrations(dsn)

    draft = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "eval",
            "draft",
            "examples.basic",
            "--dataset-dir",
            str(dataset_dir),
            "--storage-dsn",
            dsn,
            "--scores-path",
            str(scores_path),
            "--prompt",
            "hello",
            "--score",
            "exact_match=0.2",
            "--score-threshold",
            "0.8",
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert draft.returncode == 0, draft.stderr
    case_id = next(
        line.removeprefix("case_id: ").strip()
        for line in draft.stdout.splitlines()
        if line.startswith("case_id: ")
    )
    assert (dataset_dir / "drafts" / f"{case_id}.json").exists()
    draft_payload = json.loads(
        (dataset_dir / "drafts" / f"{case_id}.json").read_text(encoding="utf-8")
    )
    assert draft_payload["trigger"] == "low_score"
    assert draft_payload["metadata"]["score_signal"]["low_scores"] == {"exact_match": 0.2}

    approved = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "eval",
            "approve",
            case_id,
            "--dataset-dir",
            str(dataset_dir),
            "--storage-dsn",
            dsn,
            "--scores-path",
            str(scores_path),
            "--reviewer",
            "cli-reviewer",
            "--reason",
            "covered by CLI review",
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert approved.returncode == 0, approved.stderr
    assert "status: approved" in approved.stdout
    assert not (dataset_dir / "drafts" / f"{case_id}.json").exists()
    assert (dataset_dir / "approved" / f"{case_id}.json").exists()
    with sqlite3.connect(db_path) as connection:
        case_status = connection.execute(
            "select status, approved_by from eval_cases where id = ?",
            (case_id,),
        ).fetchone()
        audit_action = connection.execute("select action from audit_logs").fetchone()
    assert case_status == ("approved", "cli-reviewer")
    assert audit_action == ("eval.case.approved",)


def test_make_eval_runs_cli_against_approved_cases_only(tmp_path: Path) -> None:
    """`make eval` 的真实命令路径必须只消费 approved dataset。"""

    service_root = tmp_path / "service-app"
    drafts = service_root / "eval-cases" / "drafts"
    approved = service_root / "eval-cases" / "approved"
    drafts.mkdir(parents=True)
    approved.mkdir(parents=True)
    (drafts / "draft.json").write_text(
        json.dumps(
            {
                "case_id": "draft-case",
                "tenant_id": "default",
                "agent_id": "examples.basic",
                "status": "draft",
                "input": {"prompt": "draft should not run"},
                "expected": {"answer": "draft"},
            }
        ),
        encoding="utf-8",
    )
    (approved / "approved.json").write_text(
        json.dumps(
            {
                "case_id": "approved-case",
                "tenant_id": "default",
                "agent_id": "examples.basic",
                "status": "approved",
                "input": {"prompt": "hello"},
                "expected": {"answer": "hello"},
                "output": {"answer": "hello"},
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "eval",
            "run",
            "--dataset-dir",
            str(service_root / "eval-cases"),
            "--scores-path",
            str(tmp_path / "scores.jsonl"),
            "--agent-id",
            "examples.basic",
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "case_count: 1" in result.stdout
    assert "skipped_drafts: 1" in result.stdout
    assert "approved-case" in (tmp_path / "scores.jsonl").read_text(encoding="utf-8")
    assert "draft-case" not in (tmp_path / "scores.jsonl").read_text(encoding="utf-8")
