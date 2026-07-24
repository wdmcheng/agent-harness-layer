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
        '[project]\nname = "copied-service-app"\nversion = "0.1.0"\n'
        'dependencies = ["agent-harness"]\n'
        "\n[tool.uv.sources]\n# source-workspace only\n"
        "agent-harness = { workspace = true }\n"
        '\n[tool.pyright]\nvenvPath = "../.."\nvenv = ".venv"\n',
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
    original = module.PYPROJECT.read_text(encoding="utf-8")

    with pytest.raises(SystemExit, match="copied template requires AGENT_HARNESS_SOURCE"):
        module.main()
    assert module.PYPROJECT.read_text(encoding="utf-8") == original


def test_copied_bootstrap_rejects_missing_artifact_without_mutating_pyproject(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """无效 artifact 必须在移除 workspace source 之前失败。"""

    module = _load_bootstrap_module()
    _prepare_module(module, tmp_path)
    missing_artifact = tmp_path / "agent_harness-missing.whl"
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    monkeypatch.setenv("AGENT_HARNESS_SOURCE", str(missing_artifact))
    monkeypatch.delenv("AGENT_HARNESS_ALLOW_INDEX", raising=False)
    original = module.PYPROJECT.read_text(encoding="utf-8")

    with pytest.raises(SystemExit, match="agent-harness source does not exist"):
        module.main()
    assert module.PYPROJECT.read_text(encoding="utf-8") == original


def test_copied_bootstrap_restores_pyproject_when_uv_rejects_source(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """来源即使存在，uv 拒绝时也不能留下半完成的 source 替换。"""

    module = _load_bootstrap_module()
    _prepare_module(module, tmp_path)
    invalid_artifact = tmp_path / "agent_harness-invalid.whl"
    invalid_artifact.write_bytes(b"not-a-wheel")

    def reject_source(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        """模拟 uv 完成参数校验后拒绝损坏或非 Python 来源。"""

        raise subprocess.CalledProcessError(2, command)

    monkeypatch.setattr(module.subprocess, "run", reject_source)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    monkeypatch.setenv("AGENT_HARNESS_SOURCE", str(invalid_artifact))
    monkeypatch.delenv("AGENT_HARNESS_ALLOW_INDEX", raising=False)
    original = module.PYPROJECT.read_text(encoding="utf-8")

    with pytest.raises(subprocess.CalledProcessError):
        module.main()
    assert module.PYPROJECT.read_text(encoding="utf-8") == original


def test_copied_bootstrap_restores_pyproject_when_sync_fails_after_source_add(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """uv add 已改写来源但后续同步失败时，bootstrap 仍须回滚整个 TOML。"""

    module = _load_bootstrap_module()
    _prepare_module(module, tmp_path)
    artifact = tmp_path / "agent_harness-valid-shape.whl"
    artifact.write_bytes(b"subprocess-boundary-only")

    def fail_sync(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        """模拟 uv add 写入 path source 后，uv sync 在解析依赖时失败。"""

        if command[1] == "add":
            current = module.PYPROJECT.read_text(encoding="utf-8")
            replacement = (
                f'[tool.uv.sources]\nagent-harness = {{ path = "{artifact}" }}\n\n[tool.pyright]'
            )
            module.PYPROJECT.write_text(
                current.replace("[tool.pyright]", replacement),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0)
        raise subprocess.CalledProcessError(2, command)

    monkeypatch.setattr(module.subprocess, "run", fail_sync)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    monkeypatch.setenv("AGENT_HARNESS_SOURCE", str(artifact))
    monkeypatch.delenv("AGENT_HARNESS_ALLOW_INDEX", raising=False)
    original = module.PYPROJECT.read_text(encoding="utf-8")

    with pytest.raises(subprocess.CalledProcessError):
        module.main()
    assert module.PYPROJECT.read_text(encoding="utf-8") == original


def test_copied_bootstrap_restores_pyproject_when_uv_cannot_spawn(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """uv 无法启动属于外部工具失败，同样不能留下半完成配置。"""

    module = _load_bootstrap_module()
    _prepare_module(module, tmp_path)
    artifact = tmp_path / "agent_harness-local.whl"
    artifact.write_bytes(b"subprocess-boundary-only")

    def missing_uv(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        """模拟系统找不到用户指定的 uv 可执行文件。"""

        raise FileNotFoundError(command[0])

    monkeypatch.setattr(module.subprocess, "run", missing_uv)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--uv", "missing-uv"])
    monkeypatch.setenv("AGENT_HARNESS_SOURCE", str(artifact))
    monkeypatch.delenv("AGENT_HARNESS_ALLOW_INDEX", raising=False)
    original = module.PYPROJECT.read_text(encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        module.main()
    assert module.PYPROJECT.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "source_line",
    [
        'agent-harness = { path = "../trusted-core", editable = true }',
        'agent-harness = { url = "https://packages.example.invalid/agent-harness.whl" }',
        'agent-harness = { git = "https://example.invalid/agent-harness.git", rev = "abc123" }',
    ],
)
def test_copied_bootstrap_preserves_existing_explicit_source(
    tmp_path: Path,
    monkeypatch: Any,
    source_line: str,
) -> None:
    """复制项目已有 path、url 或 git 覆盖时，完整 bootstrap 不得重写该行。"""

    module = _load_bootstrap_module()
    _prepare_module(module, tmp_path)
    module.PYPROJECT.write_text(
        module.PYPROJECT.read_text(encoding="utf-8").replace(
            "agent-harness = { workspace = true }",
            source_line,
        ),
        encoding="utf-8",
    )

    def successful_sync(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        """隔离外部同步，只验证 bootstrap 对现有 source 的配置边界。"""

        assert command == ["trusted-uv", "sync"]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", successful_sync)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--uv", "trusted-uv"])
    monkeypatch.delenv("AGENT_HARNESS_SOURCE", raising=False)
    monkeypatch.delenv("AGENT_HARNESS_ALLOW_INDEX", raising=False)

    assert module.main() == 0
    assert source_line in module.PYPROJECT.read_text(encoding="utf-8")


def test_copied_bootstrap_removes_workspace_source_for_explicit_index(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """显式授权 index 后，独立项目必须先解除已失效的 workspace source。"""

    module = _load_bootstrap_module()
    _prepare_module(module, tmp_path)
    calls: list[tuple[list[str], Path]] = []

    def fake_run(command: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        """记录同步调用，避免合同测试访问真实 index 或修改解释器环境。"""

        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--uv", "trusted-uv"])
    monkeypatch.delenv("AGENT_HARNESS_SOURCE", raising=False)
    monkeypatch.setenv("AGENT_HARNESS_ALLOW_INDEX", "1")

    assert module.main() == 0
    payload = module.tomllib.loads(module.PYPROJECT.read_text(encoding="utf-8"))
    assert "sources" not in payload.get("tool", {}).get("uv", {})
    assert calls == [(["trusted-uv", "sync"], tmp_path)]


def test_copied_bootstrap_removes_workspace_source_before_adding_artifact(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """可信 artifact 注入不能依赖 uv 容忍独立项目中的失效 workspace 引用。"""

    module = _load_bootstrap_module()
    _prepare_module(module, tmp_path)
    artifact = tmp_path / "agent_harness-test.whl"
    artifact.write_bytes(b"contract-only-wheel")
    calls: list[tuple[list[str], Path]] = []

    def fake_run(command: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        """记录 source 与 sync 顺序；真实 wheel 替换由 copy-out 集成测试证明。"""

        calls.append((command, cwd))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--uv", "trusted-uv"])
    monkeypatch.setenv("AGENT_HARNESS_SOURCE", str(artifact))
    monkeypatch.delenv("AGENT_HARNESS_ALLOW_INDEX", raising=False)

    assert module.main() == 0
    payload = module.tomllib.loads(module.PYPROJECT.read_text(encoding="utf-8"))
    assert "sources" not in payload.get("tool", {}).get("uv", {})
    assert calls == [
        (["trusted-uv", "add", "--no-sync", str(artifact)], tmp_path),
        (["trusted-uv", "sync"], tmp_path),
    ]


def test_copied_bootstrap_normalizes_workspace_pyright_environment(tmp_path: Path) -> None:
    """复制项目应把模板成员的根 workspace 路径改回项目内默认环境。"""

    module = _load_bootstrap_module()
    _prepare_module(module, tmp_path)

    module._normalize_copied_pyright_environment()

    payload = module.tomllib.loads(module.PYPROJECT.read_text(encoding="utf-8"))
    assert payload["tool"]["pyright"] == {"venvPath": ".", "venv": ".venv"}


def test_copied_bootstrap_preserves_explicit_toml_override(tmp_path: Path) -> None:
    """使用者已修改 TOML 时，bootstrap 不得把本机选择改回默认值。"""

    module = _load_bootstrap_module()
    _prepare_module(module, tmp_path)
    module.PYPROJECT.write_text(
        module.PYPROJECT.read_text(encoding="utf-8").replace(
            'venvPath = "../.."',
            'venvPath = "/opt/python-envs"',
        ),
        encoding="utf-8",
    )
    module._normalize_copied_pyright_environment()

    assert 'venvPath = "/opt/python-envs"' in module.PYPROJECT.read_text(encoding="utf-8")


def test_copied_bootstrap_does_not_rewrite_pyrightconfig(tmp_path: Path) -> None:
    """默认 TOML 仍可归一化，但使用者的高优先级 JSON 必须逐字保留。"""

    module = _load_bootstrap_module()
    _prepare_module(module, tmp_path)
    pyrightconfig = tmp_path / "pyrightconfig.json"
    explicit_config = '{"venvPath":"/opt/python-envs","venv":"custom"}\n'
    pyrightconfig.write_text(explicit_config, encoding="utf-8")

    module._normalize_copied_pyright_environment()

    payload = module.tomllib.loads(module.PYPROJECT.read_text(encoding="utf-8"))
    assert payload["tool"]["pyright"] == {"venvPath": ".", "venv": ".venv"}
    assert pyrightconfig.read_text(encoding="utf-8") == explicit_config
