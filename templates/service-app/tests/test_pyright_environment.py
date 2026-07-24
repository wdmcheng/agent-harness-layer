"""service-app 在 workspace 与复制项目中的 Pyright 环境合同。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

TEMPLATE = Path(__file__).resolve().parents[1]
SCRIPT = TEMPLATE / "scripts" / "check_pyright_environment.py"


def _load_module() -> ModuleType:
    """直接加载维护脚本，避免把复制项目 scripts 变成运行时 package。"""

    spec = importlib.util.spec_from_file_location("service_app_pyright_environment", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_pyproject(path: Path, *, venv_path: str) -> Path:
    """写入只包含本测试所需 Pyright 表的最小项目配置。"""

    pyproject = path / "pyproject.toml"
    pyproject.write_text(
        f'[tool.pyright]\nvenvPath = "{venv_path}"\nvenv = ".venv"\n',
        encoding="utf-8",
    )
    return pyproject


def test_source_workspace_member_resolves_root_environment(tmp_path: Path) -> None:
    """成员配置的 `../..` 必须解析为 workspace 根 `.venv`。"""

    module = _load_module()
    app_root = tmp_path / "templates" / "service-app"
    app_root.mkdir(parents=True)
    pyproject = _write_pyproject(app_root, venv_path="../..")

    module.require_compatible_environment(
        app_root=app_root,
        pyproject=pyproject,
        interpreter_prefix=tmp_path / ".venv",
    )


def test_copied_project_resolves_local_environment(tmp_path: Path) -> None:
    """bootstrap 归一化后的 `.` 必须解析为复制项目自己的 `.venv`。"""

    module = _load_module()
    pyproject = _write_pyproject(tmp_path, venv_path=".")

    module.require_compatible_environment(
        app_root=tmp_path,
        pyproject=pyproject,
        interpreter_prefix=tmp_path / ".venv",
    )


def test_custom_uv_environment_is_rejected_without_explicit_config(tmp_path: Path) -> None:
    """仅改 uv 环境时必须失败，不能让 Pyright 继续读取默认 `.venv` 假通过。"""

    module = _load_module()
    pyproject = _write_pyproject(tmp_path, venv_path=".")

    with pytest.raises(SystemExit, match="UV_PROJECT_ENVIRONMENT"):
        module.require_compatible_environment(
            app_root=tmp_path,
            pyproject=pyproject,
            interpreter_prefix=tmp_path / "custom env",
        )


def test_user_pyrightconfig_overrides_default_environment(tmp_path: Path) -> None:
    """JSON 与 uv 明确指向同一自定义环境时允许质量检查继续。"""

    module = _load_module()
    pyproject = _write_pyproject(tmp_path, venv_path=".")
    (tmp_path / "pyrightconfig.json").write_text(
        json.dumps({"venvPath": str(tmp_path), "venv": "custom env"}),
        encoding="utf-8",
    )

    module.require_compatible_environment(
        app_root=tmp_path,
        pyproject=pyproject,
        interpreter_prefix=tmp_path / "custom env",
    )


def test_user_pyrightconfig_extends_toml_environment(tmp_path: Path) -> None:
    """JSON 未覆盖环境字段时必须继承 TOML，不能仅凭文件存在绕过默认环境。"""

    module = _load_module()
    pyproject = _write_pyproject(tmp_path, venv_path=".")
    (tmp_path / "pyrightconfig.json").write_text(
        json.dumps({"extends": "./pyproject.toml"}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="uv selected"):
        module.require_compatible_environment(
            app_root=tmp_path,
            pyproject=pyproject,
            interpreter_prefix=tmp_path / "custom env",
        )


def test_user_pyrightconfig_cannot_point_away_from_uv_environment(tmp_path: Path) -> None:
    """JSON 与 uv 指向不同环境时必须失败，不能因覆盖文件存在就无条件放行。"""

    module = _load_module()
    pyproject = _write_pyproject(tmp_path, venv_path=".")
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
            app_root=tmp_path,
            pyproject=pyproject,
            interpreter_prefix=tmp_path / "uv-env",
        )


def test_user_pyrightconfig_accepts_pyright_jsonc_syntax(tmp_path: Path) -> None:
    """检查入口必须与 Pyright 一样接受注释、尾逗号及字符串内的注释符号。"""

    module = _load_module()
    pyproject = _write_pyproject(tmp_path, venv_path=".")
    (tmp_path / "pyrightconfig.json").write_text(
        f"""{{
  // 本机环境覆盖；尾逗号也是 Pyright 接受的 JSONC 语法。
  "extends": "./pyproject.toml",
  "venvPath": {json.dumps(str(tmp_path))},
  /* 字符串中的 // 和块注释标记不能被解析器误删。 */
  "venv": "custom env",
  "exclude": ["https://example.test//types", "/* literal */"],
}}
""",
        encoding="utf-8",
    )

    module.require_compatible_environment(
        app_root=tmp_path,
        pyproject=pyproject,
        interpreter_prefix=tmp_path / "custom env",
    )


def test_source_workspace_honors_matching_parent_pyrightconfig(tmp_path: Path) -> None:
    """源码模板必须像 Pyright 一样优先使用祖先 workspace 的 JSON 覆盖。"""

    module = _load_module()
    app_root = tmp_path / "templates" / "service-app"
    app_root.mkdir(parents=True)
    pyproject = _write_pyproject(app_root, venv_path="../..")
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
        app_root=app_root,
        pyproject=pyproject,
        interpreter_prefix=custom_environment,
    )


def test_source_workspace_rejects_parent_pyrightconfig_mismatch(tmp_path: Path) -> None:
    """祖先 JSON 与 uv 错配时不能按模板 TOML 假通过。"""

    module = _load_module()
    app_root = tmp_path / "templates" / "service-app"
    app_root.mkdir(parents=True)
    pyproject = _write_pyproject(app_root, venv_path="../..")
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
            app_root=app_root,
            pyproject=pyproject,
            interpreter_prefix=tmp_path / ".venv",
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
    pyproject = _write_pyproject(tmp_path, venv_path=".")
    (tmp_path / "pyrightconfig.json").write_text(json.dumps(config), encoding="utf-8")

    module.require_compatible_environment(
        app_root=tmp_path,
        pyproject=pyproject,
        interpreter_prefix=tmp_path / "custom env",
    )
