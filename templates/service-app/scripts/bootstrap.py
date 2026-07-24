"""为仓库内或独立复制的 service-app 选择可追溯的核心包来源。"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import cast

APP_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = APP_ROOT / "pyproject.toml"
WORKSPACE_CORE = APP_ROOT.parent.parent / "packages" / "agent-harness" / "pyproject.toml"
ENV_EXAMPLE = APP_ROOT / ".env.example"
ENV_FILE = APP_ROOT / ".env"

_WORKSPACE_PYRIGHT_ENVIRONMENT = {"venvPath": "../..", "venv": ".venv"}
_WORKSPACE_CORE_SOURCE = {"workspace": True}


def _has_explicit_source() -> bool:
    """判断复制项目是否已由一次 bootstrap 写入本地可信 source。"""

    payload = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    sources = payload.get("tool", {}).get("uv", {}).get("sources", {})
    source = sources.get("agent-harness")
    return isinstance(source, dict) and any(key in source for key in ("path", "url", "git"))


def _resolve_source(source: str) -> Path:
    """在任何项目配置写入前解析并验证本地核心包来源。"""

    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"agent-harness source does not exist: {path}")
    return path


def _configure_source(*, uv: str, source: Path) -> None:
    """让 uv 以已验证的本地 wheel/sdist/project 覆盖标准版本依赖。"""

    subprocess.run(
        [uv, "add", "--no-sync", str(source)],
        cwd=APP_ROOT,
        check=True,
    )


def _remove_copied_workspace_source() -> None:
    """从独立复制项目移除只在源 workspace 内成立的核心包来源。

    只处理模板发布的精确 ``{ workspace = true }``；path、url、git 等使用者
    覆盖保持原样。空的 sources 表一并删除，避免复制项目长期携带无意义配置。
    """

    text = PYPROJECT.read_text(encoding="utf-8")
    payload = tomllib.loads(text)
    raw_sources = payload.get("tool", {}).get("uv", {}).get("sources", {})
    if not isinstance(raw_sources, dict):
        return
    sources = cast(dict[str, object], raw_sources)
    if sources.get("agent-harness") != _WORKSPACE_CORE_SOURCE:
        return

    header = re.search(r"(?m)^\[tool\.uv\.sources\]\s*$", text)
    if header is None:
        raise SystemExit("template workspace source configuration is missing [tool.uv.sources]")
    next_section = re.search(r"(?m)^\[", text[header.end() :])
    section_end = len(text) if next_section is None else header.end() + next_section.start()
    section = text[header.start() : section_end]
    updated_section, replacements = re.subn(
        r"(?m)^\s*agent-harness\s*=\s*\{\s*workspace\s*=\s*true\s*\}\s*(?:#.*)?\n?",
        "",
        section,
        count=1,
    )
    if replacements != 1:
        raise SystemExit("template agent-harness workspace source could not be removed safely")

    remaining_body = updated_section.removeprefix("[tool.uv.sources]")
    meaningful_lines = [
        line
        for line in remaining_body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not meaningful_lines:
        updated_section = ""
    PYPROJECT.write_text(
        text[: header.start()] + updated_section + text[section_end:],
        encoding="utf-8",
    )


def _normalize_copied_pyright_environment() -> None:
    """把模板成员的 workspace 路径改为复制项目自己的默认 `.venv`。

    只识别随模板发布的精确配置；使用者已修改 TOML 时保持原值。显式
    `pyrightconfig.json` 由 Pyright 按自身优先级处理，本脚本既不读取也不改写。
    """

    text = PYPROJECT.read_text(encoding="utf-8")
    payload = tomllib.loads(text)
    raw_pyright = payload.get("tool", {}).get("pyright", {})
    if not isinstance(raw_pyright, dict):
        return
    pyright = cast(dict[str, object], raw_pyright)
    if any(pyright.get(key) != value for key, value in _WORKSPACE_PYRIGHT_ENVIRONMENT.items()):
        return

    section_start = text.find("[tool.pyright]")
    if section_start < 0:
        raise SystemExit("template Pyright configuration is missing [tool.pyright]")
    next_section = re.search(r"(?m)^\[", text[section_start + 1 :])
    section_end = len(text) if next_section is None else section_start + 1 + next_section.start()
    section = text[section_start:section_end]
    updated_section, replacements = re.subn(
        r'(?m)^(\s*venvPath\s*=\s*)(["\'])\.\./\.\.\2(\s*(?:#.*)?)$',
        lambda match: f"{match.group(1)}{match.group(2)}.{match.group(2)}{match.group(3)}",
        section,
        count=1,
    )
    if replacements != 1:
        raise SystemExit("template Pyright venvPath could not be normalized safely")
    PYPROJECT.write_text(
        text[:section_start] + updated_section + text[section_end:],
        encoding="utf-8",
    )


def _print_env_hint() -> None:
    """缺少本机覆盖文件时给出可执行提示，同时保留 local 安全默认值。"""

    if ENV_EXAMPLE.exists() and not ENV_FILE.exists():
        print("bootstrap: .env is missing; copy the local override template with:")
        print("  cp .env.example .env")
        print(
            "bootstrap: local profile will continue with safe defaults; "
            "service and secret overrides are not assumed"
        )


def parse_args() -> argparse.Namespace:
    """解析 bootstrap 所用 uv 可执行文件，便于受控环境注入固定工具路径。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv", help="uv executable used for add/sync")
    return parser.parse_args()


def main() -> int:
    """拒绝含糊的公共同名包解析，然后同步当前项目依赖。"""

    args = parse_args()
    _print_env_hint()
    source = os.environ.get("AGENT_HARNESS_SOURCE", "").strip()
    allow_index = os.environ.get("AGENT_HARNESS_ALLOW_INDEX", "0") == "1"
    copied_project = not WORKSPACE_CORE.exists()
    if copied_project and not source and not _has_explicit_source() and not allow_index:
        raise SystemExit(
            "copied template requires AGENT_HARNESS_SOURCE=/path/to/agent_harness-VERSION.whl "
            "or AGENT_HARNESS_ALLOW_INDEX=1 with a trusted configured index"
        )

    source_path = _resolve_source(source) if source else None
    original_pyproject = PYPROJECT.read_text(encoding="utf-8") if copied_project else None
    try:
        if copied_project:
            _remove_copied_workspace_source()
        if source_path is not None:
            _configure_source(uv=args.uv, source=source_path)

        if copied_project:
            _normalize_copied_pyright_environment()

        subprocess.run([args.uv, "sync"], cwd=APP_ROOT, check=True)
    except (OSError, subprocess.SubprocessError):
        # 独立复制态会先移除仅在源 workspace 成立的配置；外部工具拒绝来源或
        # 同步失败时恢复原文，避免留下无法判断是否完成 bootstrap 的半成品。
        if original_pyproject is not None:
            PYPROJECT.write_text(original_pyproject, encoding="utf-8")
        raise
    print("bootstrap: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
