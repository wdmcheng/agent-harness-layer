"""复制模板 bootstrap 的供应链约束与本机配置提示测试。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

TEMPLATE = Path(__file__).resolve().parents[1]


def _load_bootstrap_module() -> Any:
    """从脚本路径加载模块，保持复制项目不需要把 scripts 变成业务 package。"""

    script = TEMPLATE / "scripts" / "bootstrap.py"
    spec = importlib.util.spec_from_file_location("service_app_bootstrap", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare_module(module: Any, tmp_path: Path) -> None:
    """把脚本文件常量隔离到临时复制目录。"""

    module.APP_ROOT = tmp_path
    module.PYPROJECT = tmp_path / "pyproject.toml"
    module.WORKSPACE_CORE = tmp_path / "workspace-core" / "pyproject.toml"
    module.ENV_EXAMPLE = tmp_path / ".env.example"
    module.ENV_FILE = tmp_path / ".env"
    module.PYPROJECT.write_text(
        '[project]\nname = "copied-service-app"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    module.ENV_EXAMPLE.write_text("AGENT_HARNESS_PROFILE=local\n", encoding="utf-8")


def test_bootstrap_missing_env_prints_actionable_hint_and_keeps_local_defaults(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """缺少 `.env` 不阻塞 local，但必须明确提示复制和 service/secret 边界。"""

    module = _load_bootstrap_module()
    _prepare_module(module, tmp_path)
    module.WORKSPACE_CORE.parent.mkdir(parents=True)
    module.WORKSPACE_CORE.write_text("[project]\nname='agent-harness'\n", encoding="utf-8")
    calls: list[tuple[list[str], Path]] = []

    def fake_run(command: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        """记录外部 uv 调用而不执行进程，验证 bootstrap 仅同步可信来源。"""
        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--uv", "trusted-uv"])
    monkeypatch.delenv("AGENT_HARNESS_SOURCE", raising=False)
    monkeypatch.delenv("AGENT_HARNESS_ALLOW_INDEX", raising=False)

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "cp .env.example .env" in output
    assert "local profile will continue with safe defaults" in output
    assert "service and secret overrides are not assumed" in output
    assert calls == [(["trusted-uv", "sync"], tmp_path)]


def test_copied_bootstrap_rejects_unknown_public_source(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """复制项目没有可信 artifact 或显式 index 授权时必须 fail closed。"""

    module = _load_bootstrap_module()
    _prepare_module(module, tmp_path)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    monkeypatch.delenv("AGENT_HARNESS_SOURCE", raising=False)
    monkeypatch.delenv("AGENT_HARNESS_ALLOW_INDEX", raising=False)

    with pytest.raises(SystemExit, match="copied template requires AGENT_HARNESS_SOURCE"):
        module.main()
