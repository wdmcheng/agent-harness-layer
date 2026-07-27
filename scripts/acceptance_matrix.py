"""校验需求验收矩阵到 production、CI、测试和 evidence 的唯一映射。"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from collections import Counter
from pathlib import Path

from acceptance_evidence_contract import (
    MatrixError,
    current_identity,
    load_evidence,
    repository_root,
)
from acceptance_matrix_policy import (
    AC_ID,
    ALLOWED_STATUS,
    EVIDENCE_GATE_PATH,
    KNOWN_EVIDENCE_GATES,
    MULTI_VALUE_SEPARATOR,
    PLACEHOLDERS,
    REQ_HEADING,
    REQUIRED_HEADERS,
    REQUIRED_PRODUCER_GATES,
    REQUIRED_TEST_MAPPINGS,
)


def requirement_groups(spec_path: Path) -> dict[str, set[str]]:
    """按 requirement 分组读取 Product Spec 中全部 REQ/AC 标识。

    分组会把 AC 收入集合，因此必须先在完整 Product Spec 上验证 live AC identity
    全局唯一，避免同一 REQ 或跨 REQ 的重号在集合折叠后失去证据。矩阵仍通过
    显式列出 REQ 选择持续验收范围，并要求所选 REQ 的全部 AC 同时进入矩阵。
    """

    if not spec_path.is_file():
        raise MatrixError(f"spec file is missing: {spec_path}")
    text = spec_path.read_text(encoding="utf-8")
    acceptance_counts = Counter(AC_ID.findall(text))
    duplicate_acceptance = sorted(
        identifier for identifier, count in acceptance_counts.items() if count > 1
    )
    if duplicate_acceptance:
        raise MatrixError(
            "duplicate Product-Spec acceptance identities: " + ", ".join(duplicate_acceptance)
        )
    matches = list(REQ_HEADING.finditer(text))
    result: dict[str, set[str]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start() : end]
        result[match.group(1)] = set(AC_ID.findall(section))
    if not result:
        raise MatrixError("Product-Spec contains no REQ identifiers")
    return result


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _field_values(identifier: str, field: str, value: str) -> list[str]:
    """解析单值或 ``<br>`` 分隔的矩阵值，并拒绝空项。"""

    values = [item.strip().strip("`").strip() for item in MULTI_VALUE_SEPARATOR.split(value)]
    if not values or any(not item or item.lower() in PLACEHOLDERS for item in values):
        raise MatrixError(f"{identifier} has placeholder {field}")
    return values


def _producer_gate(ci_job: str) -> str:
    """把 workflow job 名归一到 evidence gate 名。"""

    return ci_job if ci_job in KNOWN_EVIDENCE_GATES else ci_job.removeprefix("ci-")


def matrix_rows(matrix_path: Path) -> list[dict[str, str]]:
    """读取验收矩阵的唯一目标表格，拒绝表头或单元格数量漂移。"""

    if not matrix_path.is_file():
        raise MatrixError(f"matrix file is missing: {matrix_path}")
    lines = matrix_path.read_text(encoding="utf-8").splitlines()
    header_index = -1
    for index, line in enumerate(lines):
        if line.lstrip().startswith("|") and _cells(line) == REQUIRED_HEADERS:
            header_index = index
            break
    if header_index < 0:
        raise MatrixError(f"matrix table headers must be: {', '.join(REQUIRED_HEADERS)}")
    rows: list[dict[str, str]] = []
    for line in lines[header_index + 2 :]:
        if not line.lstrip().startswith("|"):
            if rows:
                break
            continue
        values = _cells(line)
        if len(values) != len(REQUIRED_HEADERS):
            raise MatrixError(f"matrix row has {len(values)} cells instead of 6: {line}")
        rows.append(dict(zip(REQUIRED_HEADERS, values, strict=True)))
    return rows


def _specific_repository_file(root: Path, identifier: str, field: str, value: str) -> Path:
    """要求矩阵字段指向仓库内已存在的具体文件，拒绝目录和逃逸路径。"""

    normalized = value.strip().strip("`")
    candidate = Path(normalized)
    if not normalized or candidate.is_absolute() or ".." in candidate.parts:
        raise MatrixError(f"{identifier} {field} must be a safe repository-relative specific file")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise MatrixError(
            f"{identifier} {field} must be a repository-relative specific file"
        ) from exc
    if not resolved.is_file():
        raise MatrixError(
            f"{identifier} {field} must reference an existing specific file: {normalized}"
        )
    return resolved


def _is_trivial_pytest_node(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """拒绝无行为的空壳节点；语义匹配仍由矩阵中特定节点契约和审查共同保证。"""

    body = list(node.body)
    if body and isinstance(body[0], ast.Expr):
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            body = body[1:]
    if not body or all(isinstance(statement, ast.Pass) for statement in body):
        return True
    return (
        len(body) == 1
        and isinstance(body[0], ast.Assert)
        and isinstance(body[0].test, ast.Constant)
        and body[0].test.value is True
    )


def _pytest_test_node(root: Path, identifier: str, reference: str) -> str:
    """解析并校验 ``path.py::test_name`` 或 ``path.py::TestClass::test_name``。"""

    parts = reference.split("::")
    if len(parts) not in {2, 3} or any(not part for part in parts):
        raise MatrixError(
            f"{identifier} 测试 must reference an exact pytest node with path.py::test_name"
        )
    relative_path, *selector = parts
    path = _specific_repository_file(root, identifier, "测试", relative_path)
    if path.suffix != ".py":
        raise MatrixError(f"{identifier} 测试 must reference a Python pytest test file")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise MatrixError(f"{identifier} 测试 is not a valid pytest test module: {path}") from exc
    selected: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    if len(selector) == 1:
        selected = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == selector[0]
                and node.name.startswith("test_")
            ),
            None,
        )
    else:
        class_name, method_name = selector
        test_class = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef)
                and node.name == class_name
                and node.name.startswith("Test")
            ),
            None,
        )
        if test_class is not None:
            selected = next(
                (
                    member
                    for member in test_class.body
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and member.name == method_name
                    and member.name.startswith("test_")
                ),
                None,
            )
    if selected is None:
        raise MatrixError(f"{identifier} 测试 exact pytest node does not exist: {reference}")
    if _is_trivial_pytest_node(selected):
        raise MatrixError(f"{identifier} 测试 maps to a trivial pytest node: {reference}")
    return reference


def validate(spec_path: Path, matrix_path: Path) -> tuple[int, int]:
    """闭合矩阵显式选择的 REQ/AC 到实现、测试、CI producer 与真实 evidence。"""

    groups = requirement_groups(spec_path)
    rows = matrix_rows(matrix_path)
    root = repository_root(spec_path, matrix_path)
    input_identity_state = current_identity(root)
    active_gate = os.environ.get("CI_EVIDENCE_ACTIVE_GATE", "")
    counts = Counter(row["ID"] for row in rows)
    duplicates = sorted(identifier for identifier, count in counts.items() if count > 1)
    if duplicates:
        raise MatrixError(f"duplicate matrix mapping: {', '.join(duplicates)}")
    actual = set(counts)
    selected_requirements = actual & groups.keys()
    if not selected_requirements:
        raise MatrixError("matrix must explicitly select at least one Product-Spec REQ")
    expected = {
        identifier
        for requirement in selected_requirements
        for identifier in {requirement, *groups[requirement]}
    }
    known_acceptance = {identifier for values in groups.values() for identifier in values}
    orphan_acceptance = sorted((actual & known_acceptance) - expected)
    if orphan_acceptance:
        raise MatrixError(
            "matrix AC requires its parent REQ selection: " + ", ".join(orphan_acceptance)
        )
    missing = sorted(expected - actual)
    extra = sorted(actual - groups.keys() - known_acceptance)
    if missing:
        raise MatrixError(f"selected requirement mapping is incomplete: {', '.join(missing)}")
    if extra:
        raise MatrixError(f"matrix contains unknown Product-Spec identifiers: {', '.join(extra)}")
    for row in rows:
        identifier = row["ID"]
        if row["状态"] not in ALLOWED_STATUS:
            raise MatrixError(f"{identifier} has invalid status: {row['状态']}")
        production_references = _field_values(identifier, "生产路径", row["生产路径"])
        actual_production = {
            _specific_repository_file(root, identifier, "生产路径", reference)
            for reference in production_references
        }
        if len(actual_production) != len(production_references):
            raise MatrixError(f"{identifier} has duplicate specific production path mapping")

        test_references = _field_values(identifier, "测试", row["测试"])
        actual_tests = {
            _pytest_test_node(root, identifier, reference) for reference in test_references
        }
        if len(actual_tests) != len(test_references):
            raise MatrixError(f"{identifier} has duplicate exact pytest node mapping")
        required_tests = REQUIRED_TEST_MAPPINGS.get(identifier, frozenset())
        missing_tests = sorted(required_tests - actual_tests)
        if missing_tests:
            raise MatrixError(
                f"{identifier} required test mappings are missing: {', '.join(missing_tests)}"
            )

        ci_jobs = _field_values(identifier, "CI job", row["CI job"])
        evidence_values = _field_values(identifier, "Evidence", row["Evidence"])
        if len(ci_jobs) != len(evidence_values):
            raise MatrixError(f"{identifier} CI job and Evidence producer counts must match")
        producer_gates = {_producer_gate(ci_job) for ci_job in ci_jobs}
        unknown = sorted(producer_gates - KNOWN_EVIDENCE_GATES)
        if unknown:
            raise MatrixError(f"{identifier} has unknown evidence producer: {', '.join(unknown)}")
        required = REQUIRED_PRODUCER_GATES.get(identifier, frozenset())
        missing_producers = sorted(required - producer_gates)
        if missing_producers:
            raise MatrixError(
                f"{identifier} required CI producers are missing: {', '.join(missing_producers)}"
            )

        seen_gates: set[str] = set()
        for ci_job, normalized in zip(ci_jobs, evidence_values, strict=True):
            gate = _producer_gate(ci_job)
            if gate in seen_gates:
                raise MatrixError(f"{identifier} has duplicate CI producer: {gate}")
            seen_gates.add(gate)
            if Path(normalized).is_absolute() or ".." in Path(normalized).parts:
                raise MatrixError(f"{identifier} has unsafe Evidence path: {normalized}")
            match = EVIDENCE_GATE_PATH.fullmatch(normalized)
            if not match:
                raise MatrixError(
                    f"{identifier} evidence path must be "
                    f".artifacts/ci/<gate>/result.json: {normalized}"
                )
            if match.group(1) not in KNOWN_EVIDENCE_GATES:
                raise MatrixError(f"{identifier} has unknown evidence producer: {normalized}")
            evidence_path = (root / normalized).resolve()
            try:
                evidence_path.relative_to(root)
            except ValueError as exc:
                raise MatrixError(f"{identifier} has unsafe Evidence path: {normalized}") from exc
            # evidence runner 会在执行 producer 前撤下旧 result。AC-050 仍须先验证自己
            # 映射到 acceptance-validate，但本次 result 只能在 validator 成功退出后原子写入。
            if (
                identifier == "AC-050"
                and gate == "acceptance-validate"
                and active_gate == "acceptance-validate"
                and not evidence_path.exists()
            ):
                continue
            load_evidence(root, row, evidence_path, input_identity_state, ci_job)
    return len(actual), len(expected)


def main() -> int:
    """运行需求验收矩阵 validator，并输出稳定的覆盖计数或失败诊断。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=Path("Product-Spec.md"))
    parser.add_argument("--matrix", type=Path, default=Path("docs/acceptance-matrix.md"))
    args = parser.parse_args()
    try:
        covered, total = validate(args.spec, args.matrix)
    except MatrixError as exc:
        print(f"acceptance-matrix: {exc}", file=sys.stderr)
        return 2
    print(f"acceptance-matrix: ok {covered}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
