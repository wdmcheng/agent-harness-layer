"""根 workspace 的 Pyright 环境选择合同。"""

from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_pyright_environment.py"
TEMPLATE_SCRIPT = ROOT / "templates" / "service-app" / "scripts" / "check_pyright_environment.py"


def _load_module() -> ModuleType:
    """直接加载维护脚本，避免把 scripts 目录变成运行时 package。"""

    spec = importlib.util.spec_from_file_location("check_pyright_environment", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_template_module() -> ModuleType:
    """加载模板副本，用于锁定必须保持一致的公共解析逻辑。"""

    spec = importlib.util.spec_from_file_location(
        "template_check_pyright_environment",
        TEMPLATE_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_pyproject(path: Path) -> Path:
    """写入根 workspace 约定的最小 Pyright 配置。"""

    pyproject = path / "pyproject.toml"
    pyproject.write_text(
        '[tool.pyright]\nvenvPath = "."\nvenv = ".venv"\n',
        encoding="utf-8",
    )
    return pyproject


def test_default_workspace_environment_is_accepted(tmp_path: Path) -> None:
    """没有显式覆盖时，根配置与 uv 默认 `.venv` 必须一致。"""

    module = _load_module()
    _write_pyproject(tmp_path)

    module.require_compatible_environment(
        workspace_root=tmp_path,
        interpreter_prefix=tmp_path / ".venv",
    )


def test_custom_workspace_environment_is_rejected_without_explicit_config(
    tmp_path: Path,
) -> None:
    """仅改 uv 环境不能让固定的根 Pyright 配置产生假通过。"""

    module = _load_module()
    _write_pyproject(tmp_path)

    with pytest.raises(SystemExit, match="UV_PROJECT_ENVIRONMENT"):
        module.require_compatible_environment(
            workspace_root=tmp_path,
            interpreter_prefix=tmp_path / "custom env",
        )


def test_user_pyrightconfig_explicitly_overrides_default_environment(tmp_path: Path) -> None:
    """JSON 与 uv 明确指向同一自定义环境时允许质量检查继续。"""

    module = _load_module()
    (tmp_path / "pyrightconfig.json").write_text(
        json.dumps({"venvPath": str(tmp_path), "venv": "custom env"}),
        encoding="utf-8",
    )

    module.require_compatible_environment(
        workspace_root=tmp_path,
        interpreter_prefix=tmp_path / "custom env",
    )


def test_user_pyrightconfig_extends_toml_environment(tmp_path: Path) -> None:
    """JSON 未覆盖环境字段时必须继承 TOML，不能仅凭文件存在绕过根约定。"""

    module = _load_module()
    _write_pyproject(tmp_path)
    (tmp_path / "pyrightconfig.json").write_text(
        json.dumps({"extends": "./pyproject.toml"}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="uv selected"):
        module.require_compatible_environment(
            workspace_root=tmp_path,
            interpreter_prefix=tmp_path / "custom env",
        )


def test_user_pyrightconfig_cannot_point_away_from_uv_environment(tmp_path: Path) -> None:
    """JSON 与 uv 指向不同环境时必须失败，不能因覆盖文件存在就无条件放行。"""

    module = _load_module()
    (tmp_path / "pyrightconfig.json").write_text(
        json.dumps(
            {
                "venvPath": str(tmp_path / "python envs"),
                "venv": "pyright-env",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="uv selected"):
        module.require_compatible_environment(
            workspace_root=tmp_path,
            interpreter_prefix=tmp_path / "uv-env",
        )


def test_user_pyrightconfig_accepts_pyright_jsonc_syntax(tmp_path: Path) -> None:
    """检查入口必须与 Pyright 一样接受注释、尾逗号及字符串内的注释符号。"""

    module = _load_module()
    (tmp_path / "pyrightconfig.json").write_text(
        f"""{{
  // 本机环境覆盖；尾逗号也是 Pyright 接受的 JSONC 语法。
  "venvPath": {json.dumps(str(tmp_path))},
  /* 字符串中的 // 和块注释标记不能被解析器误删。 */
  "venv": "custom env",
  "exclude": ["https://example.test//types", "/* literal */"],
}}
""",
        encoding="utf-8",
    )

    module.require_compatible_environment(
        workspace_root=tmp_path,
        interpreter_prefix=tmp_path / "custom env",
    )


def test_workspace_honors_matching_parent_pyrightconfig(tmp_path: Path) -> None:
    """根入口也必须遵循 Pyright 的祖先 JSON 优先发现规则。"""

    module = _load_module()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _write_pyproject(workspace_root)
    custom_environment = tmp_path / "python envs" / "custom"
    (tmp_path / "pyrightconfig.json").write_text(
        json.dumps(
            {
                "venvPath": str(custom_environment.parent),
                "venv": custom_environment.name,
            }
        ),
        encoding="utf-8",
    )

    module.require_compatible_environment(
        workspace_root=workspace_root,
        interpreter_prefix=custom_environment,
    )


def test_workspace_rejects_parent_pyrightconfig_mismatch(tmp_path: Path) -> None:
    """祖先 JSON 与 uv 错配时，根入口不能按本地 TOML 假通过。"""

    module = _load_module()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _write_pyproject(workspace_root)
    custom_environment = tmp_path / "python envs" / "custom"
    (tmp_path / "pyrightconfig.json").write_text(
        json.dumps(
            {
                "venvPath": str(custom_environment.parent),
                "venv": custom_environment.name,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="uv selected"):
        module.require_compatible_environment(
            workspace_root=workspace_root,
            interpreter_prefix=workspace_root / ".venv",
        )


@pytest.mark.parametrize(
    "config",
    (
        {"venvPath": "/absolute/python-envs"},
        {"venv": "custom"},
        {"venvPath": "/absolute/python-envs", "venv": ""},
    ),
)
def test_single_environment_field_falls_back_to_uv_interpreter(
    tmp_path: Path,
    config: dict[str, str],
) -> None:
    """Pyright 仅在两个字段同时有效时固定环境，单字段配置应使用 uv 解释器。"""

    module = _load_module()
    (tmp_path / "pyrightconfig.json").write_text(json.dumps(config), encoding="utf-8")

    module.require_compatible_environment(
        workspace_root=tmp_path,
        interpreter_prefix=tmp_path / "custom env",
    )


def test_root_and_template_keep_common_pyright_parsing_logic_identical() -> None:
    """复制产物必须自包含，但公共解析函数不能在根与模板之间继续漂移。"""

    root_module = _load_module()
    template_module = _load_template_module()
    for name in (
        "_normalize_jsonc",
        "_read_config",
        "_effective_environment_fields",
        "configured_environment",
        "active_config",
    ):
        assert inspect.getsource(getattr(root_module, name)) == inspect.getsource(
            getattr(template_module, name)
        )
