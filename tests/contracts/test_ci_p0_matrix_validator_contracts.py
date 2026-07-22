"""P0 acceptance matrix validator 的公开 CLI 合同。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts" / "ci_p0_matrix.py"
EVIDENCE = ROOT / "scripts" / "ci_evidence.py"


def _run(spec: Path, matrix: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--spec",
            str(spec),
            "--matrix",
            str(matrix),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_spec(path: Path) -> None:
    path.write_text(
        "### REQ-001: 示例门禁\n\n"
        "**优先级：** P0\n\n"
        "- [ ] AC-901: first\n"
        "- [x] AC-902: second\n\n"
        "### REQ-002: 非 P0\n\n"
        "**优先级：** P1\n\n"
        "- [ ] AC-003: ignored\n",
        encoding="utf-8",
    )


def _write_matrix(path: Path, *, include_second: bool = True) -> None:
    (path.parent / "src").mkdir(exist_ok=True)
    (path.parent / "tests/unit").mkdir(parents=True, exist_ok=True)
    (path.parent / "tests/contracts").mkdir(parents=True, exist_ok=True)
    (path.parent / "src/example.py").write_text("VALUE = 1\n", encoding="utf-8")
    (path.parent / "src/other.py").write_text("VALUE = 2\n", encoding="utf-8")
    (path.parent / "tests/unit/test_example.py").write_text(
        "def test_example():\n    value = 1\n    assert value == 1\n", encoding="utf-8"
    )
    (path.parent / "tests/contracts/test_example.py").write_text(
        "async def test_contract_example():\n    value = 2\n    assert value == 2\n",
        encoding="utf-8",
    )
    (path.parent / "tests/test_other.py").write_text(
        "class TestOther:\n"
        "    def test_other(self):\n"
        "        value = 3\n"
        "        assert value == 3\n",
        encoding="utf-8",
    )
    rows = [
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |",
        "|---|---|---|---|---|---|",
        "| REQ-001 | partial | `src/example.py` | `ruff-lint` | "
        "`tests/unit/test_example.py::test_example` | `.artifacts/ci/ruff-lint/result.json` |",
        "| AC-901 | pending | `src/example.py` | `unit-contract` | "
        "`tests/unit/test_example.py::test_example` | `.artifacts/ci/unit-contract/result.json` |",
    ]
    if include_second:
        rows.append(
            "| AC-902 | hosted-unverified | `src/example.py` | `release-dry-run` | "
            "`tests/contracts/test_example.py::test_contract_example` | "
            "`.artifacts/ci/release-dry-run/result.json` |"
        )
    path.write_text("# P0 验收矩阵\n\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _write_result(
    root: Path,
    gate: str,
    *,
    status: str = "pass",
    identity: dict[str, str] | None = None,
) -> None:
    """写入最小但完整的 ci-result/v1，供矩阵合同测试消费。"""

    targets = {"ci-contract": "ci-contract-check"}
    target = targets.get(gate, gate)
    gate_dir = root / ".artifacts" / "ci" / gate
    gate_dir.mkdir(parents=True, exist_ok=True)
    log = gate_dir / "command.log"
    log.write_text(f"make {gate}\n", encoding="utf-8")
    result = {
        "schema_version": "ci-result/v1",
        "gate": gate,
        "status": status,
        "command": ["make", "--no-print-directory", target],
        "exit_code": 0 if status == "pass" else 1,
        "input_identity": identity
        or {
            "commit_sha": "a" * 40,
            "dirty_diff_sha256": "b" * 64,
        },
        "artifacts": [
            {
                "path": log.relative_to(root).as_posix(),
                "kind": "log",
                "sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                "size": log.stat().st_size,
                "producer_gate": gate,
            }
        ],
    }
    (gate_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def test_validator_accepts_one_complete_mapping_per_p0_req_and_ac(tmp_path: Path) -> None:
    """每个 P0 ID 必须唯一携带 production/CI/test/evidence 五类事实。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        _write_result(tmp_path, gate)

    completed = _run(spec, matrix)

    assert completed.returncode == 0, completed.stderr
    assert "3/3" in completed.stdout


def test_validator_rejects_missing_or_duplicate_mapping(tmp_path: Path) -> None:
    """遗漏和重复都会让追踪失真，不能靠任一行的绿色状态掩盖。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix, include_second=False)
    missing = _run(spec, matrix)
    assert missing.returncode == 2
    assert "AC-902" in missing.stderr

    _write_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        _write_result(tmp_path, gate)
    with matrix.open("a", encoding="utf-8") as stream:
        stream.write(
            "| AC-902 | pending | `src/other.py` | `test-aggregate` | "
            "`tests/test_other.py::TestOther::test_other` | "
            "`.artifacts/ci/test-aggregate/result.json` |\n"
        )
    duplicate = _run(spec, matrix)
    assert duplicate.returncode == 2
    assert "duplicate" in duplicate.stderr


def test_validator_rejects_generic_directories_in_production_and_test_mappings(
    tmp_path: Path,
) -> None:
    """矩阵必须定位具体文件，不能用源码或测试目录冒充可追踪实现。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        _write_result(tmp_path, gate)
    matrix.write_text(
        matrix.read_text(encoding="utf-8").replace(
            "`src/example.py` | `ruff-lint` | `tests/unit/test_example.py::test_example`",
            "`src` | `ruff-lint` | `tests`",
        ),
        encoding="utf-8",
    )

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "specific file" in rejected.stderr


def test_validator_rejects_test_file_without_pytest_test_definition(tmp_path: Path) -> None:
    """共享 helper 即使文件存在，也不能冒充会被 pytest 收集的验收测试。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        _write_result(tmp_path, gate)
    (tmp_path / "tests/unit/test_example.py").write_text(
        "def build_fixture():\n    return 1\n",
        encoding="utf-8",
    )

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "exact pytest node does not exist" in rejected.stderr


def test_validator_rejects_file_only_test_mapping(tmp_path: Path) -> None:
    """测试列必须定位到精确 pytest node，文件级映射仍然过于泛化。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix)
    matrix.write_text(
        matrix.read_text(encoding="utf-8").replace("::test_example", ""),
        encoding="utf-8",
    )
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        _write_result(tmp_path, gate)

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "exact pytest node" in rejected.stderr


def test_validator_rejects_trivial_exact_pytest_node(tmp_path: Path) -> None:
    """精确节点也不能用 ``assert True`` 空壳冒充行为验收。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix)
    (tmp_path / "tests/unit/test_example.py").write_text(
        "def test_example():\n    assert True\n",
        encoding="utf-8",
    )
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        _write_result(tmp_path, gate)

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "trivial pytest node" in rejected.stderr


def test_validator_rejects_incomplete_producers_for_compound_acceptance(
    tmp_path: Path,
) -> None:
    """复合 AC 必须列出每个实际行为 producer，不能由无关单 gate 冒充。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests/contracts").mkdir(parents=True)
    (tmp_path / "src/ci.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/contracts/test_ci.py").write_text(
        "def test_ci():\n    value = 1\n    assert value == 1\n", encoding="utf-8"
    )
    spec.write_text(
        "### REQ-019: CI 门禁\n\n**优先级：** P0\n\n- [ ] AC-051: quality 与 test 分别执行\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# P0 验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-019 | partial | `src/ci.py` | `ci-contract` | "
        "`tests/contracts/test_ci.py::test_ci` | `.artifacts/ci/ci-contract/result.json` |\n"
        "| AC-051 | partial | `src/ci.py` | `ci-contract` | "
        "`tests/contracts/test_ci.py::test_ci` | `.artifacts/ci/ci-contract/result.json` |\n",
        encoding="utf-8",
    )
    _write_result(tmp_path, "ci-contract")

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "required CI producers" in rejected.stderr
    assert "quality-aggregate" in rejected.stderr
    assert "test-aggregate" in rejected.stderr


@pytest.mark.parametrize(
    ("identifier", "required_gate"),
    [
        ("AC-001", "install"),
        ("AC-002", "build"),
        ("AC-003", "integration"),
        ("AC-011", "smoke-service"),
        ("AC-012", "smoke-service"),
        ("AC-068", "smoke-service"),
    ],
)
def test_validator_rejects_non_executing_producer_for_semantic_acceptance(
    tmp_path: Path,
    identifier: str,
    required_gate: str,
) -> None:
    """命令、PostgreSQL 与并发 AC 必须由真正执行对应行为的 producer 证明。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests/contracts").mkdir(parents=True)
    (tmp_path / "src/packaging.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/contracts/test_packaging.py").write_text(
        "def test_packaging():\n    value = 1\n    assert value == 1\n", encoding="utf-8"
    )
    test_references = ["tests/contracts/test_packaging.py::test_packaging"]
    if identifier == "AC-003":
        test_path = Path("tests/integration/test_template_local_dev_example_smoke.py")
        (tmp_path / "tests/integration").mkdir(parents=True)
        (tmp_path / test_path).write_text(
            "def test_copied_template_runs_local_dev_and_generated_example():\n"
            "    value = 1\n"
            "    assert value == 1\n",
            encoding="utf-8",
        )
        test_references = [
            f"{test_path.as_posix()}::test_copied_template_runs_local_dev_and_generated_example"
        ]
    elif identifier in {"AC-011", "AC-012", "AC-068"}:
        service_path = Path("tests/contracts/test_service_deployment_packaging_smoke_contracts.py")
        (tmp_path / service_path).write_text(
            "def test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios():\n"
            "    value = 1\n"
            "    assert value == 1\n",
            encoding="utf-8",
        )
        test_references = [
            f"{service_path.as_posix()}::"
            "test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios"
        ]
        if identifier == "AC-012":
            storage_path = Path("tests/contracts/test_storage_migration_uow_contracts.py")
            (tmp_path / storage_path).write_text(
                "def test_repository_contract_uses_uow_and_rolls_back():\n"
                "    value = 1\n"
                "    assert value == 1\n",
                encoding="utf-8",
            )
            test_references.insert(
                0,
                f"{storage_path.as_posix()}::test_repository_contract_uses_uow_and_rolls_back",
            )
        elif identifier == "AC-068":
            sqlite_path = Path(
                "tests/contracts/test_shared_parent_budget_repository_competition_contracts.py"
            )
            (tmp_path / sqlite_path).write_text(
                "def test_sqlite_true_concurrency_commits_only_safe_direct_combination():\n"
                "    value = 1\n"
                "    assert value == 1\n",
                encoding="utf-8",
            )
            test_references.insert(
                0,
                f"{sqlite_path.as_posix()}::"
                "test_sqlite_true_concurrency_commits_only_safe_direct_combination",
            )
    test_cell = "<br>".join(f"`{reference}`" for reference in test_references)
    spec.write_text(
        f"### REQ-001: 包安装与构建\n\n**优先级：** P0\n\n- [ ] {identifier}: 执行真实命令\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# P0 验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-001 | partial | `src/packaging.py` | `ci-contract` | "
        "`tests/contracts/test_packaging.py::test_packaging` | "
        "`.artifacts/ci/ci-contract/result.json` |\n"
        f"| {identifier} | pass | `src/packaging.py` | `test-aggregate` | "
        f"{test_cell} | `.artifacts/ci/test-aggregate/result.json` |\n",
        encoding="utf-8",
    )
    _write_result(tmp_path, "ci-contract")
    _write_result(tmp_path, "test-aggregate")

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "required CI producers" in rejected.stderr
    assert required_gate in rejected.stderr


def test_packaging_acceptance_rows_map_to_command_producers() -> None:
    """仓库矩阵不得用 pytest 聚合冒充真实安装或构建命令。"""

    rows = (ROOT / "docs/p0-acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
    ac001 = next(line for line in rows if line.startswith("| AC-001 |"))
    ac002 = next(line for line in rows if line.startswith("| AC-002 |"))

    assert "| `install` |" in ac001
    assert ".artifacts/ci/install/result.json" in ac001
    assert "| `build` |" in ac002
    assert ".artifacts/ci/build/result.json" in ac002


def test_ac003_maps_to_external_wheel_install_integration() -> None:
    """AC-003 必须指向 workspace 外安装 wheel 并启动模板的真实集成测试。"""

    rows = (ROOT / "docs/p0-acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
    ac003 = next(line for line in rows if line.startswith("| AC-003 |"))

    assert "tests/integration/test_template_local_dev_example_smoke.py" in ac003
    assert "`integration`" in ac003
    assert ".artifacts/ci/integration/result.json" in ac003


def test_validator_rejects_non_external_wheel_test_for_ac003(tmp_path: Path) -> None:
    """AC-003 的测试路径必须实际执行 workspace 外 wheel 安装，不能只读 Dockerfile。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests/contracts").mkdir(parents=True)
    (tmp_path / "tests/integration").mkdir(parents=True)
    (tmp_path / "src/packaging.py").write_text("VALUE = 1\n", encoding="utf-8")
    for relative in (
        "tests/contracts/test_packaging.py",
        "tests/integration/test_template_local_dev_example_smoke.py",
    ):
        (tmp_path / relative).write_text(
            "def test_packaging():\n    value = 1\n    assert value == 1\n", encoding="utf-8"
        )
    spec.write_text(
        "### REQ-001: 包安装与构建\n\n**优先级：** P0\n\n"
        "- [ ] AC-003: workspace 外安装 wheel 并运行模板\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# P0 验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-001 | partial | `src/packaging.py` | `ci-contract` | "
        "`tests/contracts/test_packaging.py::test_packaging` | "
        "`.artifacts/ci/ci-contract/result.json` |\n"
        "| AC-003 | pass | `src/packaging.py` | `integration` | "
        "`tests/contracts/test_packaging.py::test_packaging` | "
        "`.artifacts/ci/integration/result.json` |\n",
        encoding="utf-8",
    )
    _write_result(tmp_path, "ci-contract")
    _write_result(tmp_path, "integration")

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "required test mappings" in rejected.stderr


@pytest.mark.parametrize("identifier", ["AC-011", "AC-012", "AC-068"])
def test_postgresql_acceptance_rows_map_to_real_service_smoke(identifier: str) -> None:
    """需要真实 PostgreSQL 的验收不能映射到 clean test job 中会跳过的用例。"""

    rows = (ROOT / "docs/p0-acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
    row = next(line for line in rows if line.startswith(f"| {identifier} |"))

    assert "`smoke-service`" in row
    assert ".artifacts/ci/smoke-service/result.json" in row
    assert "test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios" in row
    if identifier in {"AC-012", "AC-068"}:
        assert "`test-aggregate`" in row
        assert ".artifacts/ci/test-aggregate/result.json" in row


def test_ac012_and_ac068_map_sqlite_and_postgresql_behavior_separately() -> None:
    """复合后端验收必须同时保留 SQLite 精确节点与真实 PostgreSQL producer。"""

    rows = (ROOT / "docs/p0-acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
    ac012 = next(line for line in rows if line.startswith("| AC-012 |"))
    ac068 = next(line for line in rows if line.startswith("| AC-068 |"))

    assert "test_repository_contract_uses_uow_and_rolls_back" in ac012
    assert "test_sqlite_true_concurrency_commits_only_safe_direct_combination" in ac068
    for row in (ac012, ac068):
        assert "`test-aggregate`<br>`smoke-service`" in row
        assert ".artifacts/ci/test-aggregate/result.json" in row
        assert ".artifacts/ci/smoke-service/result.json" in row


def test_current_repository_matrix_is_complete_and_valid() -> None:
    """当前矩阵只在各 CI producer 已落盘时执行真实 evidence 闭环。"""

    first_evidence = ROOT / ".artifacts/ci/test-aggregate/result.json"
    if not first_evidence.is_file():
        pytest.skip("需要先运行矩阵引用的 CI evidence producer")

    completed = _run(ROOT / "Product-Spec.md", ROOT / "docs/p0-acceptance-matrix.md")

    if "commit/diff identity does not match current input" in completed.stderr:
        pytest.skip("需要先用当前输入重新生成冻结 CI evidence")
    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout


def test_ac006_maps_to_real_copied_template_dev_and_example_smoke() -> None:
    """AC-006 不能继续映射只 mock server 或静态查看 Makefile 的测试。"""

    matrix = (ROOT / "docs/p0-acceptance-matrix.md").read_text(encoding="utf-8")
    row = next(line for line in matrix.splitlines() if line.startswith("| AC-006 |"))

    assert "tests/integration/test_template_local_dev_example_smoke.py" in row
    assert "tests/contracts/test_service_app_template_maintenance_contracts.py" not in row


def test_ac065_maps_to_full_local_smoke_latency_producer() -> None:
    """AC-065 的整轮耗时必须由 local smoke producer 证明，不能映射 SSE 首帧测试。"""

    matrix = (ROOT / "docs/p0-acceptance-matrix.md").read_text(encoding="utf-8")
    row = next(line for line in matrix.splitlines() if line.startswith("| AC-065 |"))

    assert "scripts/smoke_local.py" in row
    assert (
        "tests/contracts/test_model_usage_smoke_contracts.py::"
        "test_public_local_fake_run_completes_under_fixed_threshold" in row
    )
    assert "`smoke-local`" in row
    assert ".artifacts/ci/smoke-local/result.json" in row
    assert "test_sse_first_frame_performance.py" not in row


def test_reviewed_semantic_acceptance_rows_use_behavioral_nodes() -> None:
    """审查点名的 AC 必须映射到实际执行该行为的测试，不能只满足 AST 形状。"""

    rows = {
        line.split("|", 2)[1].strip(): line
        for line in (ROOT / "docs/p0-acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| AC-")
    }
    required_nodes = {
        "AC-004": ["test_example_agents_have_no_direct_vendor_sdk_imports"],
        "AC-005": ["test_model_router_uses_fake_provider_and_reports_budget_fallback"],
        "AC-019": ["test_default_identity_propagates_to_run_session_trace_and_eval"],
        "AC-023": ["test_dev_deny_and_known_tool_failure_keep_approval_semantics"],
        "AC-026": ["test_tool_registry_preflight_errors_are_not_masked_by_approval"],
        "AC-029": ["test_example_eval_uses_fake_model_without_real_provider_keys"],
        "AC-052": ["test_example_eval_uses_fake_model_without_real_provider_keys"],
        "AC-061": ["test_business_agents_have_no_vendor_or_orm_session_imports"],
        "AC-062": [
            "test_template_api_helper_uses_runtime_seam",
            "test_service_submit_and_worker_execute_share_run_and_identity",
            "test_tool_registry_public_seam_enforces_errors_policy_and_output_metadata",
            "test_rag_runtime_composition_emits_correlated_model_and_embedding_usage",
            "test_run_003_and_run_006_expose_the_same_public_envelopes",
        ],
    }
    rejected_nodes = {
        "test_boundary_contract_lists_banned_vendors_and_adapter_allowlist",
        "test_orchestrator_uses_typed_executor_and_has_no_fake_fallback",
        "test_env_example_local_profile_uses_default_identity_without_authorization",
        "test_policy_engine_guardrail_approval_and_audit_flow",
        "test_tool_registry_enforces_agent_tool_allowlist",
        "test_file_eval_optional_executor_preserves_drafts_and_scores_real_output",
        "test_identity_and_permission_context_keep_tenant_session_fields",
    }

    for identifier, nodes in required_nodes.items():
        row = rows[identifier]
        assert all(node in row for node in nodes), identifier
        assert not any(node in row for node in rejected_nodes), identifier


def test_ac050_maps_to_independent_terminal_p0_validator() -> None:
    """AC-050 必须由独立终态 p0-validate 证明，不能映射回测试聚合。"""

    matrix = (ROOT / "docs/p0-acceptance-matrix.md").read_text(encoding="utf-8")
    row = next(line for line in matrix.splitlines() if line.startswith("| AC-050 |"))

    assert "`p0-validate`" in row
    assert ".artifacts/ci/p0-validate/result.json" in row
    assert "`test-aggregate`" not in row


def test_validator_rejects_non_terminal_producer_for_ac050(tmp_path: Path) -> None:
    """即使测试聚合为绿，AC-050 缺少独立 p0-validate producer 仍须失败。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests/contracts").mkdir(parents=True)
    (tmp_path / "src/p0.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/contracts/test_p0.py").write_text(
        "def test_p0():\n    value = 1\n    assert value == 1\n", encoding="utf-8"
    )
    spec.write_text(
        "### REQ-019: CI 门禁\n\n**优先级：** P0\n\n"
        "- [ ] AC-050: 每项验收映射到独立终态 P0 validator\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# P0 验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-019 | partial | `src/p0.py` | `test-aggregate` | "
        "`tests/contracts/test_p0.py::test_p0` | `.artifacts/ci/test-aggregate/result.json` |\n"
        "| AC-050 | partial | `src/p0.py` | `test-aggregate` | "
        "`tests/contracts/test_p0.py::test_p0` | `.artifacts/ci/test-aggregate/result.json` |\n",
        encoding="utf-8",
    )
    _write_result(tmp_path, "test-aggregate")

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "p0-validate" in rejected.stderr


def test_active_p0_producer_bootstraps_only_its_own_terminal_result(tmp_path: Path) -> None:
    """producer 运行中可等待自身 result 落盘，但仍须校验 AC-050 的终态映射。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests/contracts").mkdir(parents=True)
    (tmp_path / "src/p0.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/contracts/test_p0.py").write_text(
        "def test_p0():\n    value = 1\n    assert value == 1\n", encoding="utf-8"
    )
    spec.write_text(
        "### REQ-019: CI 门禁\n\n**优先级：** P0\n\n"
        "- [ ] AC-050: 每项验收映射到独立终态 P0 validator\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# P0 验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-019 | partial | `src/p0.py` | `test-aggregate` | "
        "`tests/contracts/test_p0.py::test_p0` | `.artifacts/ci/test-aggregate/result.json` |\n"
        "| AC-050 | partial | `src/p0.py` | `p0-validate` | "
        "`tests/contracts/test_p0.py::test_p0` | `.artifacts/ci/p0-validate/result.json` |\n",
        encoding="utf-8",
    )
    _write_result(tmp_path, "test-aggregate")
    env = os.environ.copy()
    env["CI_EVIDENCE_ACTIVE_GATE"] = "p0-validate"

    completed = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--spec",
            str(spec),
            "--matrix",
            str(matrix),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "2/2" in completed.stdout


def test_validator_rejects_non_smoke_producer_for_ac065(tmp_path: Path) -> None:
    """即使文件与 evidence 都存在，AC-065 缺少 smoke-local producer 也必须失败。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests/contracts").mkdir(parents=True)
    (tmp_path / "src/smoke.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/contracts/test_model_usage_smoke_contracts.py").write_text(
        "def test_public_local_fake_run_completes_under_fixed_threshold():\n"
        "    value = 1\n"
        "    assert value == 1\n",
        encoding="utf-8",
    )
    spec.write_text(
        "### REQ-022: 性能\n\n**优先级：** P0\n\n"
        "- [ ] AC-065: local fake provider 单 agent 完整执行小于 5 秒\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# P0 验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-022 | partial | `src/smoke.py` | `test-aggregate` | "
        "`tests/contracts/test_model_usage_smoke_contracts.py::"
        "test_public_local_fake_run_completes_under_fixed_threshold` | "
        "`.artifacts/ci/test-aggregate/result.json` |\n"
        "| AC-065 | partial | `src/smoke.py` | `test-aggregate` | "
        "`tests/contracts/test_model_usage_smoke_contracts.py::"
        "test_public_local_fake_run_completes_under_fixed_threshold` | "
        "`.artifacts/ci/test-aggregate/result.json` |\n",
        encoding="utf-8",
    )
    _write_result(tmp_path, "test-aggregate")

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "smoke-local" in rejected.stderr


def test_validator_rejects_unknown_ci_evidence_producer(tmp_path: Path) -> None:
    """Evidence 必须来自受控 CI producer，不能用任意伪造 artifact 路径。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        _write_result(tmp_path, gate)
    text = matrix.read_text(encoding="utf-8").replace(
        ".artifacts/ci/ruff-lint/result.json",
        ".artifacts/ci/forged/result.json",
    )
    matrix.write_text(text, encoding="utf-8")

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "unknown evidence producer" in rejected.stderr


def test_validator_rejects_malformed_evidence_result(tmp_path: Path) -> None:
    """路径存在但不是 ci-result/v1 时必须失败，不能只看文件名。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        _write_result(tmp_path, gate)
    result_path = tmp_path / ".artifacts" / "ci" / "ruff-lint" / "result.json"
    result_path.write_text('{"schema_version":"wrong/v1"}\n', encoding="utf-8")

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "schema_version" in rejected.stderr


def test_validator_rejects_evidence_checksum_drift(tmp_path: Path) -> None:
    """result 中记录的 artifact 摘要漂移时必须失败。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        _write_result(tmp_path, gate)
    (tmp_path / ".artifacts" / "ci" / "ruff-lint" / "command.log").write_text(
        "tampered\n", encoding="utf-8"
    )

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "checksum drift" in rejected.stderr


def test_validator_rejects_result_gate_mismatch(tmp_path: Path) -> None:
    """Evidence 文件目录、result gate 与矩阵 CI job 必须三方一致。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        _write_result(tmp_path, gate)
    result_path = tmp_path / ".artifacts" / "ci" / "ruff-lint" / "result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["gate"] = "unit-contract"
    result_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "gate does not match path" in rejected.stderr


def test_validator_rejects_stale_commit_diff_identity(tmp_path: Path) -> None:
    """Git 工作区存在时，旧 commit/diff 证据不得继续支撑矩阵。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Contract Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "tag.gpgSign", "false"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "Product-Spec.md", "matrix.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    identity = {"commit_sha": commit, "dirty_diff_sha256": hashlib.sha256(b"").hexdigest()}
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        _write_result(tmp_path, gate, identity=identity)
    result_path = tmp_path / ".artifacts" / "ci" / "ruff-lint" / "result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["input_identity"]["commit_sha"] = "c" * 40
    result_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    rejected = _run(spec, matrix)

    assert rejected.returncode == 2
    assert "commit/diff identity" in rejected.stderr


def test_validator_accepts_evidence_identity_with_untracked_source(tmp_path: Path) -> None:
    """未跟踪源码属于冻结输入，producer 与 validator 必须通过公开 CLI 得出同一摘要。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix)
    (tmp_path / ".gitignore").write_text(".artifacts/\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text(
        "ruff-lint unit-contract release-dry-run:\n\t@true\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Contract Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "tag.gpgSign", "false"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    (tmp_path / "new_source.py").write_text("VALUE = 1\n", encoding="utf-8")

    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        produced = subprocess.run(
            [sys.executable, str(EVIDENCE), "--repo", str(tmp_path), "--gate", gate],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert produced.returncode == 0, produced.stderr

    completed = _run(spec, matrix)

    assert completed.returncode == 0, completed.stderr
    assert "3/3" in completed.stdout


def test_validator_keeps_known_gate_name_with_ci_prefix(tmp_path: Path) -> None:
    """`ci-contract` 本身是受控 gate，不能把名称前缀误当 Make wrapper 剥掉。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    _write_spec(spec)
    _write_matrix(matrix)
    matrix.write_text(
        matrix.read_text(encoding="utf-8").replace(
            "`ruff-lint` | `tests/unit/test_example.py::test_example` | "
            "`.artifacts/ci/ruff-lint/result.json`",
            "`ci-contract` | `tests/unit/test_example.py::test_example` | "
            "`.artifacts/ci/ci-contract/result.json`",
        ),
        encoding="utf-8",
    )
    for gate in ("ci-contract", "unit-contract", "release-dry-run"):
        _write_result(tmp_path, gate)

    completed = _run(spec, matrix)

    assert completed.returncode == 0, completed.stderr
    assert "3/3" in completed.stdout
