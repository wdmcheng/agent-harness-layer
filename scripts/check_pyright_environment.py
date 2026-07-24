"""防止根 Pyright 固定环境与 uv 实际环境不一致时产生假通过。"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]


def _normalize_jsonc(source: str) -> str:
    """移除字符串外的 JSONC 注释和尾逗号，同时保留行列位置。"""

    without_comments: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(source):
        char = source[index]
        if in_string:
            without_comments.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            without_comments.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(source):
            marker = source[index + 1]
            if marker == "/":
                without_comments.extend((" ", " "))
                index += 2
                while index < len(source) and source[index] not in "\r\n":
                    without_comments.append(" ")
                    index += 1
                continue
            if marker == "*":
                comment_start = index
                without_comments.extend((" ", " "))
                index += 2
                while index + 1 < len(source) and source[index : index + 2] != "*/":
                    without_comments.append(source[index] if source[index] in "\r\n" else " ")
                    index += 1
                if index + 1 >= len(source):
                    raise json.JSONDecodeError("unterminated block comment", source, comment_start)
                without_comments.extend((" ", " "))
                index += 2
                continue
        without_comments.append(char)
        index += 1

    cleaned = "".join(without_comments)
    normalized: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(cleaned):
        if in_string:
            normalized.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            normalized.append(char)
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(cleaned) and cleaned[lookahead].isspace():
                lookahead += 1
            if lookahead < len(cleaned) and cleaned[lookahead] in "}]":
                normalized.append(" ")
                continue
        normalized.append(char)
    return "".join(normalized)


def _read_config(path: Path) -> dict[str, Any]:
    """读取 Pyright JSON 或 pyproject TOML，并返回同一层级的配置表。"""

    try:
        if path.suffix == ".toml":
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            config = payload.get("tool", {}).get("pyright", {})
        else:
            config = json.loads(_normalize_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"cannot read Pyright configuration {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise SystemExit(f"Pyright configuration {path} must be an object")
    object_config = cast(dict[object, Any], config)
    if not all(isinstance(key, str) for key in object_config):
        raise SystemExit(f"Pyright configuration {path} must use string keys")
    return cast(dict[str, Any], object_config)


def _effective_environment_fields(
    path: Path,
    *,
    seen: frozenset[Path] = frozenset(),
) -> tuple[object | None, object | None, Path]:
    """按 `extends` 顺序合并环境字段，并保留 venvPath 的声明目录。"""

    resolved = path.resolve()
    if resolved in seen:
        raise SystemExit(f"cyclic Pyright configuration extends chain at {resolved}")
    config = _read_config(resolved)

    venv_path: object | None = None
    venv: object | None = None
    venv_path_base = resolved.parent
    extends = config.get("extends")
    if extends is not None:
        if not isinstance(extends, str):
            raise SystemExit(f"Pyright extends in {resolved} must be a string path")
        base_path = Path(extends)
        if not base_path.is_absolute():
            base_path = resolved.parent / base_path
        venv_path, venv, venv_path_base = _effective_environment_fields(
            base_path,
            seen=seen | {resolved},
        )

    if "venvPath" in config:
        venv_path = config["venvPath"]
        venv_path_base = resolved.parent
    if "venv" in config:
        venv = config["venv"]
    return venv_path, venv, venv_path_base


def configured_environment(config_path: Path) -> Path | None:
    """解析活动 Pyright 配置固定的环境；未固定时返回 None。"""

    venv_path, venv, venv_path_base = _effective_environment_fields(config_path)
    if venv_path is None or venv is None:
        return None
    if not isinstance(venv_path, str) or not isinstance(venv, str):
        raise SystemExit(
            f"Pyright configuration {config_path} must define string venvPath and venv values"
        )
    if not venv:
        return None

    environment_root = Path(venv_path)
    if not environment_root.is_absolute():
        environment_root = venv_path_base / environment_root
    return (environment_root / venv).resolve()


def active_config(*, project_root: Path, pyproject: Path) -> Path:
    """复现 Pyright 的发现顺序：先找当前目录或祖先 JSON，再回退项目 TOML。"""

    root = project_root.resolve()
    for directory in (root, *root.parents):
        json_config = directory / "pyrightconfig.json"
        if json_config.is_file():
            return json_config
    return pyproject.resolve()


def require_compatible_environment(
    *,
    workspace_root: Path,
    interpreter_prefix: Path,
) -> None:
    """校验活动 Pyright 配置与 uv 当前解释器使用同一个环境。

    根 `[tool.pyright]` 为 IDE 固定到共享 `.venv`。Pyright 的配置优先级
    高于 `--pythonpath`，因此只改变 uv 环境会让命令使用两套依赖视图。
    本检查只读取 `pyrightconfig.json` 及其继承链，不生成或改写使用者配置。
    """

    root = workspace_root.resolve()
    config_path = active_config(project_root=root, pyproject=root / "pyproject.toml")
    expected = configured_environment(config_path)
    # 未配置 venvPath/venv 时，Pyright CLI 使用 uv run 提供的当前解释器环境。
    if expected is None:
        return

    actual = interpreter_prefix.resolve()
    if actual == expected:
        return

    raise SystemExit(
        "root Pyright configuration expects the default workspace environment at "
        f"{expected}, but uv selected {actual}. Align UV_PROJECT_ENVIRONMENT with the active "
        "pyrightconfig.json or [tool.pyright] environment."
    )


def main() -> int:
    """按当前 uv 解释器验证根质量入口使用的 Pyright 环境。"""

    require_compatible_environment(
        workspace_root=ROOT,
        interpreter_prefix=Path(sys.prefix),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
