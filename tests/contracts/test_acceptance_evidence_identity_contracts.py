"""需求验收矩阵引用的 CI evidence 身份、状态与摘要合同。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from tests.contracts.acceptance_matrix_test_support import (
    EVIDENCE,
    ROOT,
    run_matrix_validator,
    write_acceptance_matrix,
    write_gate_result,
    write_spec_fixture,
)


def test_validator_rejects_unknown_ci_evidence_producer(tmp_path: Path) -> None:
    """Evidence 必须来自受控 CI producer，不能用任意伪造 artifact 路径。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        write_gate_result(tmp_path, gate)
    text = matrix.read_text(encoding="utf-8").replace(
        ".artifacts/ci/ruff-lint/result.json",
        ".artifacts/ci/forged/result.json",
    )
    matrix.write_text(text, encoding="utf-8")

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "unknown evidence producer" in rejected.stderr


def test_validator_rejects_malformed_evidence_result(tmp_path: Path) -> None:
    """路径存在但不是 ci-result/v1 时必须失败，不能只看文件名。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        write_gate_result(tmp_path, gate)
    result_path = tmp_path / ".artifacts" / "ci" / "ruff-lint" / "result.json"
    result_path.write_text('{"schema_version":"wrong/v1"}\n', encoding="utf-8")

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "schema_version" in rejected.stderr


def test_validator_rejects_evidence_checksum_drift(tmp_path: Path) -> None:
    """result 中记录的 artifact 摘要漂移时必须失败。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        write_gate_result(tmp_path, gate)
    (tmp_path / ".artifacts" / "ci" / "ruff-lint" / "command.log").write_text(
        "tampered\n", encoding="utf-8"
    )

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "checksum drift" in rejected.stderr


def test_validator_rejects_result_gate_mismatch(tmp_path: Path) -> None:
    """Evidence 文件目录、result gate 与矩阵 CI job 必须三方一致。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
    for gate in ("ruff-lint", "unit-contract", "release-dry-run"):
        write_gate_result(tmp_path, gate)
    result_path = tmp_path / ".artifacts" / "ci" / "ruff-lint" / "result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["gate"] = "unit-contract"
    result_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "gate does not match path" in rejected.stderr


def test_validator_rejects_stale_commit_diff_identity(tmp_path: Path) -> None:
    """Git 工作区存在时，旧 commit/diff 证据不得继续支撑矩阵。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
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
        write_gate_result(tmp_path, gate, identity=identity)
    result_path = tmp_path / ".artifacts" / "ci" / "ruff-lint" / "result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["input_identity"]["commit_sha"] = "c" * 40
    result_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "commit/diff identity" in rejected.stderr


def test_validator_accepts_evidence_identity_with_untracked_source(tmp_path: Path) -> None:
    """未跟踪源码属于冻结输入，producer 与 validator 必须通过公开 CLI 得出同一摘要。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
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

    completed = run_matrix_validator(spec, matrix)

    assert completed.returncode == 0, completed.stderr
    assert "3/3" in completed.stdout


def test_validator_keeps_known_gate_name_with_ci_prefix(tmp_path: Path) -> None:
    """`ci-contract` 本身是受控 gate，不能把名称前缀误当 Make wrapper 剥掉。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    write_spec_fixture(spec)
    write_acceptance_matrix(matrix)
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
        write_gate_result(tmp_path, gate)

    completed = run_matrix_validator(spec, matrix)

    assert completed.returncode == 0, completed.stderr
    assert "3/3" in completed.stdout
