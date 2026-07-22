"""需求验收矩阵的结构、唯一性与终态 producer 合同。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.contracts.acceptance_matrix_test_support import (
    ROOT,
    VALIDATOR,
    run_matrix_validator,
    write_acceptance_matrix,
    write_gate_result,
    write_spec_fixture,
)


def test_validator_accepts_one_complete_mapping_per_selected_req_and_ac(tmp_path: Path) -> None:
    """矩阵选择的每个 REQ/AC 必须唯一携带实现、测试、CI 与 evidence。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        write_gate_result(tmp_path, gate)

    completed = run_matrix_validator(spec, matrix)

    assert completed.returncode == 0, completed.stderr
    assert "3/3" in completed.stdout


def test_validator_rejects_missing_or_duplicate_mapping(tmp_path: Path) -> None:
    """已选择 REQ 的 AC 遗漏和重复都会让追踪失真。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix, include_second=False)
    missing = run_matrix_validator(spec, matrix)
    assert missing.returncode == 2
    assert "AC-902" in missing.stderr

    write_acceptance_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        write_gate_result(tmp_path, gate)
    with matrix.open("a", encoding="utf-8") as stream:
        stream.write(
            "| AC-902 | pending | `src/other.py` | `test-aggregate` | "
            "`tests/test_other.py::TestOther::test_other` | "
            "`.artifacts/ci/test-aggregate/result.json` |\n"
        )
    duplicate = run_matrix_validator(spec, matrix)
    assert duplicate.returncode == 2
    assert "duplicate" in duplicate.stderr


def test_validator_rejects_acceptance_without_parent_requirement_selection(
    tmp_path: Path,
) -> None:
    """矩阵不能只列 AC 而隐式选择范围；父 REQ 必须作为持续验收边界显式出现。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
    matrix.write_text(
        matrix.read_text(encoding="utf-8").replace(
            "| REQ-001 | partial |",
            "| REQ-002 | partial |",
        ),
        encoding="utf-8",
    )
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        write_gate_result(tmp_path, gate)

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "parent REQ" in rejected.stderr


def test_validator_rejects_generic_directories_in_production_and_test_mappings(
    tmp_path: Path,
) -> None:
    """矩阵必须定位具体文件，不能用源码或测试目录冒充可追踪实现。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        write_gate_result(tmp_path, gate)
    matrix.write_text(
        matrix.read_text(encoding="utf-8").replace(
            "`src/example.py` | `ruff-lint` | `tests/unit/test_example.py::test_example`",
            "`src` | `ruff-lint` | `tests`",
        ),
        encoding="utf-8",
    )

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "specific file" in rejected.stderr


def test_validator_rejects_test_file_without_pytest_test_definition(tmp_path: Path) -> None:
    """共享 helper 即使文件存在，也不能冒充会被 pytest 收集的验收测试。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        write_gate_result(tmp_path, gate)
    (tmp_path / "tests/unit/test_example.py").write_text(
        "def build_fixture():\n    return 1\n",
        encoding="utf-8",
    )

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "exact pytest node does not exist" in rejected.stderr


def test_validator_rejects_file_only_test_mapping(tmp_path: Path) -> None:
    """测试列必须定位到精确 pytest node，文件级映射仍然过于泛化。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
    matrix.write_text(
        matrix.read_text(encoding="utf-8").replace("::test_example", ""),
        encoding="utf-8",
    )
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        write_gate_result(tmp_path, gate)

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "exact pytest node" in rejected.stderr


def test_validator_rejects_trivial_exact_pytest_node(tmp_path: Path) -> None:
    """精确节点也不能用 ``assert True`` 空壳冒充行为验收。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
    (tmp_path / "tests/unit/test_example.py").write_text(
        "def test_example():\n    assert True\n",
        encoding="utf-8",
    )
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        write_gate_result(tmp_path, gate)

    rejected = run_matrix_validator(spec, matrix)

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
        "### REQ-019: CI 门禁\n\n- [ ] AC-051: quality 与 test 分别执行\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# 需求验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-019 | partial | `src/ci.py` | `ci-contract` | "
        "`tests/contracts/test_ci.py::test_ci` | `.artifacts/ci/ci-contract/result.json` |\n"
        "| AC-051 | partial | `src/ci.py` | `ci-contract` | "
        "`tests/contracts/test_ci.py::test_ci` | `.artifacts/ci/ci-contract/result.json` |\n",
        encoding="utf-8",
    )
    write_gate_result(tmp_path, "ci-contract")

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "required CI producers" in rejected.stderr
    for producer in (
        "lock",
        "ruff-format",
        "ruff-lint",
        "pyright",
        "import-boundary",
        "quality-aggregate",
        "unit-contract",
        "integration",
        "test-aggregate",
    ):
        assert producer in rejected.stderr


def test_current_repository_matrix_is_complete_and_valid() -> None:
    """当前矩阵只在各 CI producer 已落盘时执行真实 evidence 闭环。"""

    first_evidence = ROOT / ".artifacts/ci/test-aggregate/result.json"
    if not first_evidence.is_file():
        pytest.skip("需要先运行矩阵引用的 CI evidence producer")

    completed = run_matrix_validator(ROOT / "Product-Spec.md", ROOT / "docs/acceptance-matrix.md")

    if "commit/diff identity does not match current input" in completed.stderr:
        pytest.skip("需要先用当前输入重新生成冻结 CI evidence")
    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout


def test_ac050_maps_to_independent_terminal_acceptance_validator() -> None:
    """AC-050 必须由独立终态 acceptance-validate 证明，不能映射回测试聚合。"""

    matrix = (ROOT / "docs/acceptance-matrix.md").read_text(encoding="utf-8")
    row = next(line for line in matrix.splitlines() if line.startswith("| AC-050 |"))

    assert "`acceptance-validate`" in row
    assert ".artifacts/ci/acceptance-validate/result.json" in row
    assert "`test-aggregate`" not in row


def test_validator_rejects_non_terminal_producer_for_ac050(tmp_path: Path) -> None:
    """即使测试聚合为绿，AC-050 缺少独立 acceptance-validate producer 仍须失败。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests/contracts").mkdir(parents=True)
    (tmp_path / "src/acceptance.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/contracts/test_acceptance.py").write_text(
        "def test_acceptance():\n    value = 1\n    assert value == 1\n", encoding="utf-8"
    )
    spec.write_text(
        "### REQ-019: CI 门禁\n\n- [ ] AC-050: 每项验收映射到独立终态 需求验收 validator\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# 需求验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-019 | partial | `src/acceptance.py` | `test-aggregate` | "
        "`tests/contracts/test_acceptance.py::test_acceptance` | "
        "`.artifacts/ci/test-aggregate/result.json` |\n"
        "| AC-050 | partial | `src/acceptance.py` | `test-aggregate` | "
        "`tests/contracts/test_acceptance.py::test_acceptance` | "
        "`.artifacts/ci/test-aggregate/result.json` |\n",
        encoding="utf-8",
    )
    write_gate_result(tmp_path, "test-aggregate")

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "acceptance-validate" in rejected.stderr


def test_active_acceptance_producer_bootstraps_only_its_own_terminal_result(tmp_path: Path) -> None:
    """producer 运行中可等待自身 result 落盘，但仍须校验 AC-050 的终态映射。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests/contracts").mkdir(parents=True)
    (tmp_path / "src/acceptance.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/contracts/test_acceptance.py").write_text(
        "def test_acceptance():\n    value = 1\n    assert value == 1\n", encoding="utf-8"
    )
    spec.write_text(
        "### REQ-019: CI 门禁\n\n- [ ] AC-050: 每项验收映射到独立终态 需求验收 validator\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# 需求验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-019 | partial | `src/acceptance.py` | `test-aggregate` | "
        "`tests/contracts/test_acceptance.py::test_acceptance` | "
        "`.artifacts/ci/test-aggregate/result.json` |\n"
        "| AC-050 | partial | `src/acceptance.py` | `acceptance-validate` | "
        "`tests/contracts/test_acceptance.py::test_acceptance` | "
        "`.artifacts/ci/acceptance-validate/result.json` |\n",
        encoding="utf-8",
    )
    write_gate_result(tmp_path, "test-aggregate")
    env = os.environ.copy()
    env["CI_EVIDENCE_ACTIVE_GATE"] = "acceptance-validate"

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
