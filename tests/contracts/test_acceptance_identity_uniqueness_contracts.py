"""验收 identity 全局唯一性、语义映射与迁移追溯合同。"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.contracts.acceptance_matrix_test_support import (
    ROOT,
    run_matrix_validator,
    write_acceptance_matrix,
    write_gate_result,
    write_spec_fixture,
)


def test_validator_rejects_duplicate_acceptance_identity_in_same_requirement(
    tmp_path: Path,
) -> None:
    """同一 REQ 的重复 identity 不能被 ``set`` 折叠后伪装成单项。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    spec.write_text(
        spec.read_text(encoding="utf-8").replace("AC-902: second", "AC-901: second"),
        encoding="utf-8",
    )
    write_acceptance_matrix(matrix, include_second=False)
    for gate in ("ruff-lint", "unit-contract"):
        write_gate_result(tmp_path, gate)

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "duplicate Product-Spec acceptance identities: AC-901" in rejected.stderr


def test_validator_rejects_duplicate_acceptance_identity_across_requirements(
    tmp_path: Path,
) -> None:
    """未被矩阵选择的 REQ 也不能复用其他 REQ 的 live identity。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    spec.write_text(
        spec.read_text(encoding="utf-8").replace("AC-003: ignored", "AC-901: ignored"),
        encoding="utf-8",
    )
    write_acceptance_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        write_gate_result(tmp_path, gate)

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "duplicate Product-Spec acceptance identities: AC-901" in rejected.stderr


def _write_api_docs_fixture(root: Path, *, exact_tests: bool, gate: str) -> tuple[Path, Path]:
    """构造 AC-089 的最小隔离仓库，分别验证 producer 与精确节点。"""

    spec = root / "Product-Spec.md"
    matrix = root / "matrix.md"
    (root / "src").mkdir()
    (root / "tests/contracts").mkdir(parents=True)
    (root / "src/api_docs.py").write_text("ENABLED = True\n", encoding="utf-8")
    (root / "tests/contracts/test_api_docs.py").write_text(
        "def test_api_docs():\n    enabled = True\n    assert enabled is True\n",
        encoding="utf-8",
    )
    tests = ["tests/contracts/test_api_docs.py::test_api_docs"]
    if exact_tests:
        profile = root / "tests/contracts/test_typed_config_profiles_secret_files_contracts.py"
        profile.write_text(
            "def test_local_and_service_profiles_load_typed_settings():\n"
            "    enabled = True\n"
            "    assert enabled is True\n",
            encoding="utf-8",
        )
        surface = root / "templates/service-app/tests/test_app_surface.py"
        surface.parent.mkdir(parents=True)
        surface.write_text(
            "def test_api_docs_can_be_disabled_without_reading_assets():\n"
            "    status = 404\n"
            "    assert status == 404\n",
            encoding="utf-8",
        )
        tests = [
            "tests/contracts/test_typed_config_profiles_secret_files_contracts.py::"
            "test_local_and_service_profiles_load_typed_settings",
            "templates/service-app/tests/test_app_surface.py::"
            "test_api_docs_can_be_disabled_without_reading_assets",
        ]
    test_cell = "<br>".join(f"`{node}`" for node in tests)
    spec.write_text(
        "### REQ-003: API 文档\n\n- [ ] AC-089: 关闭 API docs 后公开面全部不可用\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# 需求验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-003 | partial | `src/api_docs.py` | `ci-contract` | "
        "`tests/contracts/test_api_docs.py::test_api_docs` | "
        "`.artifacts/ci/ci-contract/result.json` |\n"
        f"| AC-089 | partial | `src/api_docs.py` | `{gate}` | {test_cell} | "
        f"`.artifacts/ci/{gate}/result.json` |\n",
        encoding="utf-8",
    )
    write_gate_result(root, "ci-contract")
    if gate != "ci-contract":
        write_gate_result(root, gate)
    return spec, matrix


def test_validator_rejects_non_test_producer_for_api_docs_ac089(tmp_path: Path) -> None:
    """AC-089 必须由 test-aggregate 证明，普通合同 gate 不能替代。"""

    spec, matrix = _write_api_docs_fixture(tmp_path, exact_tests=True, gate="ci-contract")

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "required CI producers" in rejected.stderr
    assert "test-aggregate" in rejected.stderr


def test_validator_rejects_generic_test_mapping_for_api_docs_ac089(tmp_path: Path) -> None:
    """AC-089 必须同时绑定 typed profile 与关闭全部公开面的行为节点。"""

    spec, matrix = _write_api_docs_fixture(
        tmp_path,
        exact_tests=False,
        gate="test-aggregate",
    )

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "required test mappings" in rejected.stderr


def _policy_mapping(name: str) -> dict[str, set[str]]:
    """静态解析 policy 常量，避免执行门禁模块产生环境副作用。"""

    tree = ast.parse((ROOT / "scripts/acceptance_matrix_policy.py").read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    )
    assert isinstance(assignment.value, ast.Dict)
    mapping: dict[str, set[str]] = {}
    for key_node, value_node in zip(assignment.value.keys, assignment.value.values, strict=True):
        assert key_node is not None
        key = ast.literal_eval(key_node)
        assert isinstance(key, str)
        assert isinstance(value_node, ast.Call) and value_node.args
        mapping[key] = set(ast.literal_eval(value_node.args[0]))
    return mapping


def test_live_dependency_and_api_docs_identities_keep_distinct_semantics() -> None:
    """live 规格、矩阵、policy 与 changelog 必须共同完成可追溯迁移。"""

    product = (ROOT / "Product-Spec.md").read_text(encoding="utf-8")
    identities = re.findall(r"(?m)^- \[[ xX]\] (AC-\d+[A-Z]*):", product)
    assert identities.count("AC-070") == 1
    assert identities.count("AC-089") == 1
    ac070_spec = next(line for line in product.splitlines() if "AC-070:" in line)
    ac089_spec = next(line for line in product.splitlines() if "AC-089:" in line)
    assert "uv.lock" in ac070_spec
    assert "service.api_docs.enabled" in ac089_spec

    rows = (ROOT / "docs/acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
    ac070 = next(line for line in rows if line.startswith("| AC-070 |"))
    ac089 = next(line for line in rows if line.startswith("| AC-089 |"))
    for gate in ("lock", "install", "license", "release-dry-run", "test-aggregate"):
        assert f"`{gate}`" in ac070
    assert "test_lock_package_identities_match_reviewed_baseline" in ac070
    assert "`test-aggregate`" in ac089
    assert "test_local_and_service_profiles_load_typed_settings" in ac089
    assert "test_api_docs_can_be_disabled_without_reading_assets" in ac089

    producers = _policy_mapping("REQUIRED_PRODUCER_GATES")
    tests = _policy_mapping("REQUIRED_TEST_MAPPINGS")
    assert producers["AC-070"] == {
        "lock",
        "install",
        "license",
        "release-dry-run",
        "test-aggregate",
    }
    assert producers["AC-089"] == {"test-aggregate"}
    assert tests["AC-070"] == {
        "tests/contracts/test_dependency_version_policy_contracts.py::"
        "test_uv_range_and_conflicting_groups_keep_ci_environment_concrete",
        "tests/contracts/test_dependency_version_policy_contracts.py::"
        "test_lock_package_identities_match_reviewed_baseline",
    }
    assert tests["AC-089"] == {
        "tests/contracts/test_typed_config_profiles_secret_files_contracts.py::"
        "test_local_and_service_profiles_load_typed_settings",
        "templates/service-app/tests/test_app_surface.py::"
        "test_api_docs_can_be_disabled_without_reading_assets",
    }

    changelog = (ROOT / "Product-Spec-CHANGELOG.md").read_text(encoding="utf-8")
    migration = next(
        line
        for line in changelog.splitlines()
        if "AC-070" in line and "AC-089" in line and "API docs" in line
    )
    assert "dependency" in migration
