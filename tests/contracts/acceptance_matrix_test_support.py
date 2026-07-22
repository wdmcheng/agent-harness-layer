"""需求验收矩阵合同测试共享的 CLI 与隔离仓库夹具。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts" / "acceptance_matrix.py"
EVIDENCE = ROOT / "scripts" / "ci_evidence.py"


def run_matrix_validator(spec: Path, matrix: Path) -> subprocess.CompletedProcess[str]:
    """通过公开 CLI 执行矩阵校验，保留完整退出状态与诊断。"""

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


def write_spec_fixture(path: Path) -> None:
    """写入一个被矩阵选择的 REQ 和一个未选择 REQ，验证范围由矩阵声明。"""

    path.write_text(
        "### REQ-001: 示例门禁\n\n"
        "- [ ] AC-901: first\n"
        "- [x] AC-902: second\n\n"
        "### REQ-002: 未选择\n\n"
        "- [ ] AC-003: ignored\n",
        encoding="utf-8",
    )


def write_acceptance_matrix(path: Path, *, include_second: bool = True) -> None:
    """写入引用真实文件与 pytest node 的最小验收矩阵。"""

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
    path.write_text("# 需求验收矩阵\n\n" + "\n".join(rows) + "\n", encoding="utf-8")


def write_gate_result(
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
