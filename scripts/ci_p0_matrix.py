"""校验 P0 REQ/AC 到 production、CI、测试和 evidence 的唯一映射。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import cast

from ci_evidence import GATE_TARGETS
from ci_identity import InputIdentityError, input_identity

REQ_HEADING = re.compile(r"(?m)^### (REQ-\d+):")
AC_ID = re.compile(r"(?m)^- \[[ xX]\] (AC-\d+[A-Z]*):")
ALLOWED_STATUS = {"pass", "partial", "pending", "blocked", "hosted-unverified"}
REQUIRED_HEADERS = ["ID", "状态", "生产路径", "CI job", "测试", "Evidence"]
PLACEHOLDERS = {"", "-", "n/a", "none", "todo", "待定", "缺失"}
EVIDENCE_GATE_PATH = re.compile(r"^\.artifacts/ci/([a-z0-9][a-z0-9-]*)/result\.json$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")
CI_RESULT_SCHEMA = "ci-result/v1"
CI_RESULT_STATUSES = {"pass", "fail", "skipped"}
KNOWN_EVIDENCE_GATES = set(GATE_TARGETS)
MULTI_VALUE_SEPARATOR = re.compile(r"\s*<br\s*/?>\s*", re.IGNORECASE)

# 自然语言 AC 中的复合行为不能退化为任意一个绿色 gate。这里只记录跨 gate
# 的特殊约束；普通能力仍由矩阵中的具体 pytest 测试和 producer 命令共同追踪。
REQUIRED_PRODUCER_GATES = {
    "AC-001": frozenset({"install"}),
    "AC-002": frozenset({"build"}),
    "AC-003": frozenset({"integration"}),
    "AC-006": frozenset({"integration"}),
    "AC-007": frozenset({"test-aggregate", "smoke-service"}),
    "AC-011": frozenset({"test-aggregate", "smoke-service"}),
    "AC-012": frozenset({"test-aggregate", "smoke-service"}),
    "AC-029": frozenset({"test-aggregate", "eval"}),
    "AC-050": frozenset({"p0-validate"}),
    "AC-051": frozenset({"quality-aggregate", "test-aggregate"}),
    "AC-052": frozenset({"eval"}),
    "AC-053": frozenset({"quality-aggregate", "eval", "smoke-local", "smoke-service"}),
    "AC-054": frozenset({"quality-aggregate", "eval", "smoke-local", "smoke-service"}),
    "AC-060": frozenset({"test-aggregate", "smoke-service"}),
    "AC-065": frozenset({"smoke-local"}),
    "AC-068": frozenset({"test-aggregate", "smoke-service"}),
}
REQUIRED_TEST_MAPPINGS = {
    "AC-003": frozenset(
        {
            "tests/integration/test_template_local_dev_example_smoke.py::"
            "test_copied_template_runs_local_dev_and_generated_example"
        }
    ),
    "AC-004": frozenset(
        {
            "tests/contracts/test_vendor_boundary_doctor_contracts.py::"
            "test_example_agents_have_no_direct_vendor_sdk_imports"
        }
    ),
    "AC-005": frozenset(
        {
            "tests/contracts/test_agent_registry_router_model_contracts.py::"
            "test_model_router_uses_fake_provider_and_reports_budget_fallback"
        }
    ),
    "AC-006": frozenset(
        {
            "tests/integration/test_template_local_dev_example_smoke.py::"
            "test_copied_template_runs_local_dev_and_generated_example"
        }
    ),
    "AC-007": frozenset(
        {
            "tests/contracts/test_service_deployment_packaging_smoke_contracts.py::"
            "test_service_smoke_uses_http_auth_crash_reclaim_checkpoint_and_scoped_cleanup"
        }
    ),
    "AC-011": frozenset(
        {
            "tests/contracts/test_service_deployment_packaging_smoke_contracts.py::"
            "test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios"
        }
    ),
    "AC-012": frozenset(
        {
            "tests/contracts/test_storage_migration_uow_contracts.py::"
            "test_repository_contract_uses_uow_and_rolls_back",
            "tests/contracts/test_service_deployment_packaging_smoke_contracts.py::"
            "test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios",
        }
    ),
    "AC-019": frozenset(
        {
            "tests/contracts/test_runtime_checkpoint_orchestrator_contracts.py::"
            "test_default_identity_propagates_to_run_session_trace_and_eval"
        }
    ),
    "AC-023": frozenset(
        {
            "tests/contracts/test_dev_approval_flows_contracts.py::"
            "test_dev_deny_and_known_tool_failure_keep_approval_semantics"
        }
    ),
    "AC-026": frozenset(
        {
            "tests/contracts/test_tool_registry_authorization_contracts.py::"
            "test_tool_registry_preflight_errors_are_not_masked_by_approval"
        }
    ),
    "AC-029": frozenset(
        {
            "tests/contracts/test_example_eval_migration_contracts.py::"
            "test_example_eval_uses_fake_model_without_real_provider_keys"
        }
    ),
    "AC-052": frozenset(
        {
            "tests/contracts/test_example_eval_migration_contracts.py::"
            "test_example_eval_uses_fake_model_without_real_provider_keys"
        }
    ),
    "AC-060": frozenset(
        {
            "tests/contracts/test_service_deployment_packaging_smoke_contracts.py::"
            "test_service_smoke_uses_http_auth_crash_reclaim_checkpoint_and_scoped_cleanup"
        }
    ),
    "AC-061": frozenset(
        {
            "tests/contracts/test_vendor_boundary_doctor_contracts.py::"
            "test_business_agents_have_no_vendor_or_orm_session_imports"
        }
    ),
    "AC-062": frozenset(
        {
            "tests/contracts/test_runtime_checkpoint_template_contracts.py::"
            "test_template_api_helper_uses_runtime_seam",
            "tests/contracts/test_service_worker_shared_identity_contracts.py::"
            "test_service_submit_and_worker_execute_share_run_and_identity",
            "tests/contracts/test_tool_registry_public_seam_contracts.py::"
            "test_tool_registry_public_seam_enforces_errors_policy_and_output_metadata",
            "tests/contracts/test_model_usage_runtime_composition_contracts.py::"
            "test_rag_runtime_composition_emits_correlated_model_and_embedding_usage",
            "tests/contracts/test_sse_http_openapi_contracts.py::"
            "test_run_003_and_run_006_expose_the_same_public_envelopes",
        }
    ),
    "AC-065": frozenset(
        {
            "tests/contracts/test_model_usage_smoke_contracts.py::"
            "test_public_local_fake_run_completes_under_fixed_threshold"
        }
    ),
    "AC-068": frozenset(
        {
            "tests/contracts/test_shared_parent_budget_repository_competition_contracts.py::"
            "test_sqlite_true_concurrency_commits_only_safe_direct_combination",
            "tests/contracts/test_service_deployment_packaging_smoke_contracts.py::"
            "test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios",
        }
    ),
}


class MatrixError(RuntimeError):
    """表示矩阵缺项、重复或用占位内容冒充证据。"""


def _git(repo: Path, *args: str) -> str | None:
    """读取仓库身份；临时合同夹具不在 Git 仓库时返回 None。"""

    try:
        completed = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _repository_root(spec_path: Path, matrix_path: Path) -> Path:
    """以 Git 根为证据基准，非 Git 临时夹具退回 spec 所在目录。"""

    for candidate in (spec_path.resolve().parent, matrix_path.resolve().parent):
        root = _git(candidate, "rev-parse", "--show-toplevel")
        if root:
            return Path(root).resolve()
    return spec_path.resolve().parent


def _current_identity(repo: Path) -> dict[str, str] | None:
    """复用 evidence runner 的完整输入身份；非 Git 夹具跳过当前态比对。"""

    try:
        return input_identity(repo)
    except InputIdentityError:
        return None


def _relative_artifact_path(root: Path, raw: object, *, field: str) -> Path:
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise MatrixError(f"evidence artifact {field} must be a relative path")
    candidate = Path(raw)
    if ".." in candidate.parts:
        raise MatrixError(f"evidence artifact {field} has unsafe path: {raw}")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MatrixError(f"evidence artifact {field} escapes repository: {raw}") from exc
    return resolved


def _load_evidence(
    root: Path,
    row: dict[str, str],
    evidence_path: Path,
    expected_identity: dict[str, str] | None,
    ci_job: str,
) -> None:
    """验证 result、gate、artifact checksum 及当前输入身份，全部失败即拒绝。"""

    identifier = row["ID"]
    if not evidence_path.is_file() or evidence_path.is_symlink():
        raise MatrixError(f"{identifier} evidence file is missing or not regular: {evidence_path}")
    try:
        raw_payload: object = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MatrixError(f"{identifier} evidence is not valid JSON: {evidence_path}") from exc
    if not isinstance(raw_payload, dict):
        raise MatrixError(f"{identifier} evidence result must be a JSON object")
    payload = cast(dict[str, object], raw_payload)
    if payload.get("schema_version") != CI_RESULT_SCHEMA:
        raise MatrixError(f"{identifier} evidence schema_version must be {CI_RESULT_SCHEMA}")

    match = EVIDENCE_GATE_PATH.fullmatch(evidence_path.relative_to(root).as_posix())
    if match is None:
        raise MatrixError(f"{identifier} evidence path is not a CI result path")
    path_gate = match.group(1)
    gate = payload.get("gate")
    if gate != path_gate:
        raise MatrixError(f"{identifier} evidence gate does not match path: {gate!r}")
    expected_gate = ci_job if ci_job in KNOWN_EVIDENCE_GATES else ci_job.removeprefix("ci-")
    if gate != expected_gate:
        raise MatrixError(f"{identifier} evidence gate {gate!r} does not match CI job {ci_job!r}")

    status = payload.get("status")
    if status not in CI_RESULT_STATUSES:
        raise MatrixError(f"{identifier} evidence has invalid status: {status!r}")
    raw_command = payload.get("command")
    if (
        not isinstance(raw_command, list)
        or not raw_command
        or not all(isinstance(item, str) and item for item in cast(list[object], raw_command))
    ):
        raise MatrixError(f"{identifier} evidence command is missing or invalid")
    expected_command = ["make", "--no-print-directory", GATE_TARGETS[path_gate]]
    if raw_command != expected_command:
        raise MatrixError(
            f"{identifier} evidence command does not execute producer {path_gate!r}: "
            f"expected {expected_command!r}"
        )
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise MatrixError(f"{identifier} evidence exit_code is invalid")
    if status == "pass" and exit_code != 0:
        raise MatrixError(f"{identifier} passing evidence has non-zero exit_code")
    if status == "fail" and exit_code == 0:
        raise MatrixError(f"{identifier} failing evidence has zero exit_code")
    if row["状态"] == "pass" and status != "pass":
        raise MatrixError(f"{identifier} matrix pass is not backed by passing evidence")

    raw_identity = payload.get("input_identity")
    if not isinstance(raw_identity, dict):
        raise MatrixError(f"{identifier} evidence input_identity is missing")
    identity = cast(dict[str, object], raw_identity)
    commit_sha = identity.get("commit_sha")
    dirty_diff_sha256 = identity.get("dirty_diff_sha256")
    if not isinstance(commit_sha, str) or GIT_OBJECT_ID.fullmatch(commit_sha) is None:
        raise MatrixError(f"{identifier} evidence commit_sha is invalid")
    if not isinstance(dirty_diff_sha256, str) or SHA256.fullmatch(dirty_diff_sha256) is None:
        raise MatrixError(f"{identifier} evidence dirty_diff_sha256 is invalid")
    if expected_identity is not None and identity != expected_identity:
        raise MatrixError(
            f"{identifier} evidence commit/diff identity does not match current input"
        )

    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise MatrixError(f"{identifier} evidence artifacts are missing")
    artifacts = cast(list[object], raw_artifacts)
    required_artifact_fields = {"path", "kind", "sha256", "size", "producer_gate"}
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise MatrixError(f"{identifier} evidence artifact {index} is incomplete")
        artifact_record = cast(dict[str, object], artifact)
        if not required_artifact_fields <= artifact_record.keys():
            raise MatrixError(f"{identifier} evidence artifact {index} is incomplete")
        artifact_path = _relative_artifact_path(root, artifact_record.get("path"), field="path")
        if not artifact_path.is_file() or artifact_path.is_symlink():
            raise MatrixError(
                f"{identifier} evidence artifact is missing: {artifact_record.get('path')}"
            )
        checksum = artifact_record.get("sha256")
        if not isinstance(checksum, str) or SHA256.fullmatch(checksum) is None:
            raise MatrixError(f"{identifier} evidence artifact checksum is invalid")
        actual_checksum = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if checksum != actual_checksum:
            raise MatrixError(
                f"{identifier} evidence artifact checksum drift: {artifact_record.get('path')}"
            )
        size = artifact_record.get("size")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size != artifact_path.stat().st_size
        ):
            raise MatrixError(
                f"{identifier} evidence artifact size drift: {artifact_record.get('path')}"
            )
        if artifact_record.get("producer_gate") != gate:
            raise MatrixError(f"{identifier} evidence artifact producer gate mismatch")


def p0_ids(spec_path: Path) -> set[str]:
    if not spec_path.is_file():
        raise MatrixError(f"spec file is missing: {spec_path}")
    text = spec_path.read_text(encoding="utf-8")
    matches = list(REQ_HEADING.finditer(text))
    result: set[str] = set()
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start() : end]
        if re.search(r"\*\*优先级：\*\*\s*P0\b", section) is None:
            continue
        result.add(match.group(1))
        result.update(AC_ID.findall(section))
    if not result:
        raise MatrixError("Product-Spec contains no P0 REQ/AC identifiers")
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
    expected = p0_ids(spec_path)
    rows = matrix_rows(matrix_path)
    root = _repository_root(spec_path, matrix_path)
    current_identity = _current_identity(root)
    active_gate = os.environ.get("CI_EVIDENCE_ACTIVE_GATE", "")
    counts = Counter(row["ID"] for row in rows)
    duplicates = sorted(identifier for identifier, count in counts.items() if count > 1)
    if duplicates:
        raise MatrixError(f"duplicate matrix mapping: {', '.join(duplicates)}")
    actual = set(counts)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise MatrixError(f"missing P0 mapping: {', '.join(missing)}")
    if extra:
        raise MatrixError(f"matrix contains non-P0 or unknown identifiers: {', '.join(extra)}")
    for row in rows:
        identifier = row["ID"]
        if row["状态"] not in ALLOWED_STATUS:
            raise MatrixError(f"{identifier} has invalid status: {row['状态']}")
        production = row["生产路径"].strip()
        if production.lower() in PLACEHOLDERS:
            raise MatrixError(f"{identifier} has placeholder 生产路径")
        _specific_repository_file(root, identifier, "生产路径", production)

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
            # 映射到 p0-validate，但本次 result 只能在 validator 成功退出后原子写入。
            if (
                identifier == "AC-050"
                and gate == "p0-validate"
                and active_gate == "p0-validate"
                and not evidence_path.exists()
            ):
                continue
            _load_evidence(root, row, evidence_path, current_identity, ci_job)
    return len(actual), len(expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=Path("Product-Spec.md"))
    parser.add_argument("--matrix", type=Path, default=Path("docs/p0-acceptance-matrix.md"))
    args = parser.parse_args()
    try:
        covered, total = validate(args.spec, args.matrix)
    except MatrixError as exc:
        print(f"ci-p0-matrix: {exc}", file=sys.stderr)
        return 2
    print(f"ci-p0-matrix: ok {covered}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
