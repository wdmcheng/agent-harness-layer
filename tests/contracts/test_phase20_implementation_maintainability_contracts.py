"""模型工具循环实现与复杂合同文件的可审查规模门禁。"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVIEWABLE_FILES = (
    "packages/agent-harness/src/agent_harness/tools/approved_execution.py",
    "packages/agent-harness/src/agent_harness/tools/execution_support.py",
    "tests/contracts/test_policy_gated_model_tool_loop_approved_event_atomicity_contracts.py",
    "tests/contracts/test_policy_gated_model_tool_loop_result_lifecycle_contracts.py",
    "tests/contracts/test_policy_gated_model_tool_loop_sqlite_resume_contracts.py",
    "tests/contracts/test_policy_gated_model_tool_loop_approved_resume_recovery_contracts.py",
    "tests/contracts/test_policy_gated_model_tool_loop_event_contracts.py",
    "tests/contracts/test_policy_gated_model_tool_loop_event_terminal_contracts.py",
)


def _effective_lines(path: Path) -> int:
    """排除空行、注释和docstring，保持与仓库实现审查口径一致。"""

    source = path.read_text(encoding="utf-8")
    docstring_lines: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body or not isinstance(body[0], ast.Expr):
            continue
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            end_lineno = body[0].end_lineno or body[0].lineno
            docstring_lines.update(range(body[0].lineno, end_lineno + 1))
    return sum(
        line_number not in docstring_lines
        and bool(line.strip())
        and not line.lstrip().startswith("#")
        for line_number, line in enumerate(source.splitlines(), start=1)
    )


def test_phase20_complex_files_stay_within_reviewable_effective_loc() -> None:
    """防止approved生命周期或公共恢复夹具再次聚合为超500行职责箱。"""

    counts = {name: _effective_lines(ROOT / name) for name in REVIEWABLE_FILES}
    assert all(count <= 500 for count in counts.values()), counts


def test_tool_registry_public_import_is_independent_of_pytest_collection_order() -> None:
    """用全新解释器证明公共Registry导入不依赖其他测试预先初始化runtime。"""

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agent_harness.tools import ToolRegistry; "
            "assert ToolRegistry.__name__ == 'ToolRegistry'",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
