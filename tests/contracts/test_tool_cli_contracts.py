"""Tools CLI 合同测试。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from agent_harness.storage import run_migrations

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


def _profile_with_agent_allowlist(
    tmp_path: Path,
    tool_names: list[str],
    *,
    storage_root: Path | None = None,
    db_path: Path | None = None,
    extra: str = "",
) -> Path:
    """复制本地 profile 并按用例注入工具白名单与隔离状态路径。

    测试通过真实 CLI 配置装配能力，而不是绕过配置层直接构造运行时，借此
    固定空白名单默认拒绝和 MCP 白名单透传的对外语义。
    """
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile_text = (PROFILES / "local.yaml").read_text(encoding="utf-8")
    if storage_root is not None:
        profile_text = profile_text.replace(
            "root: .agent-harness/local",
            f"root: {storage_root}",
        )
    if db_path is not None:
        profile_text = profile_text.replace(
            "dsn: sqlite+aiosqlite:///.agent-harness/local/agent_harness.db",
            f"dsn: sqlite+aiosqlite:///{db_path}",
        )
    profile_text += "\nagent:\n  tool_allowlist:\n"
    for tool_name in tool_names:
        profile_text += f"    - {tool_name}\n"
    profile_text += extra
    (profiles_dir / "local.yaml").write_text(profile_text, encoding="utf-8")
    return profiles_dir


def test_tools_cli_group_is_available(tmp_path: Path) -> None:
    """CLI seam 是工具能力的用户可见入口，必须至少能列出工具。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "agent_harness.db"
    profiles_dir = _profile_with_agent_allowlist(
        tmp_path,
        ["file.read_file", "shell.execute"],
        storage_root=tmp_path / "state",
        db_path=db_path,
    )
    run_migrations(f"sqlite+aiosqlite:///{db_path}")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "tools",
            "list",
            "--profile",
            "local",
            "--profiles-dir",
            str(profiles_dir),
            "--workspace",
            str(workspace),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "file.read_file" in result.stdout
    assert "shell.execute" in result.stdout
    first_line = json.loads(result.stdout.splitlines()[0])
    assert first_line["name"]
    assert "handler" not in first_line
    assert first_line["policy"]["decision"] in {"allow", "require_approval"}


def test_tools_cli_list_includes_configured_mcp_allowlist(tmp_path: Path) -> None:
    """MCP server 配置必须进入 CLI list seam，即使本地 discovery server 未启动。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "agent_harness.db"
    profiles_dir = _profile_with_agent_allowlist(
        tmp_path,
        ["mcp.demo.unsafe"],
        storage_root=tmp_path / "state",
        db_path=db_path,
        extra="""
tools:
  mcp_servers:
    - name: demo
      transport: stdio
      command: null
      allowlist:
        - unsafe
""",
    )
    run_migrations(f"sqlite+aiosqlite:///{db_path}")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "tools",
            "list",
            "--profile",
            "local",
            "--profiles-dir",
            str(profiles_dir),
            "--workspace",
            str(workspace),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "mcp.demo.unsafe" in result.stdout


def test_tools_cli_call_records_workspace_and_invocation(tmp_path: Path) -> None:
    """CLI call 必须留下 workspace/tool_invocation 证据，不只是在 stdout 打结果。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.txt").write_text("visible", encoding="utf-8")
    db_path = tmp_path / "agent_harness.db"
    state_root = tmp_path / "state"
    profiles_dir = _profile_with_agent_allowlist(
        tmp_path,
        ["file.read_file"],
        storage_root=state_root,
        db_path=db_path,
    )
    run_migrations(f"sqlite+aiosqlite:///{db_path}")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "tools",
            "call",
            "file.read_file",
            "--profile",
            "local",
            "--profiles-dir",
            str(profiles_dir),
            "--workspace",
            str(workspace),
            "--arguments",
            '{"path":"sample.txt"}',
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "completed"
    assert payload["policy"]["decision"] == "allow"
    assert payload["trust_level"] == "untrusted"
    assert payload["request_id"].startswith("req_")
    assert payload["trace_id"].startswith("trace_")

    with sqlite3.connect(db_path) as connection:
        workspace_count = connection.execute("select count(*) from workspaces").fetchone()
        invocation = connection.execute(
            "select tool_name,status,args_ref,result_ref,request_id,trace_id from tool_invocations"
        ).fetchone()

    assert workspace_count == (1,)
    assert invocation[0:2] == ("file.read_file", "completed")
    assert invocation[2].startswith("artifact://")
    assert invocation[3].startswith("artifact://")
    assert invocation[4].startswith("req_")
    assert invocation[5].startswith("trace_")


def test_tools_cli_call_denies_default_empty_agent_allowlist(tmp_path: Path) -> None:
    """默认空 agent.tool_allowlist 不能被 CLI runtime 扩权成全部工具可用。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.txt").write_text("visible", encoding="utf-8")
    db_path = tmp_path / "agent_harness.db"
    state_root = tmp_path / "state"
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile_text = (PROFILES / "local.yaml").read_text(encoding="utf-8")
    profile_text = profile_text.replace(
        "root: .agent-harness/local",
        f"root: {state_root}",
    )
    profile_text = profile_text.replace(
        "dsn: sqlite+aiosqlite:///.agent-harness/local/agent_harness.db",
        f"dsn: sqlite+aiosqlite:///{db_path}",
    )
    (profiles_dir / "local.yaml").write_text(profile_text, encoding="utf-8")
    run_migrations(f"sqlite+aiosqlite:///{db_path}")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "tools",
            "call",
            "file.read_file",
            "--profile",
            "local",
            "--profiles-dir",
            str(profiles_dir),
            "--workspace",
            str(workspace),
            "--arguments",
            '{"path":"sample.txt"}',
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "denied"
    assert payload["error"]["code"] == "tool.policy_denied"
