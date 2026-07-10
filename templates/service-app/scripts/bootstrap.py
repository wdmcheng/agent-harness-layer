"""为仓库内或独立复制的 service-app 选择可信核心包来源。"""

from __future__ import annotations

import argparse
import os
import subprocess
import tomllib
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = APP_ROOT / "pyproject.toml"
WORKSPACE_CORE = APP_ROOT.parent.parent / "packages" / "agent-harness" / "pyproject.toml"
ENV_EXAMPLE = APP_ROOT / ".env.example"
ENV_FILE = APP_ROOT / ".env"


def _has_explicit_source() -> bool:
    """判断复制项目是否已由一次 bootstrap 写入本地可信 source。"""

    payload = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    sources = payload.get("tool", {}).get("uv", {}).get("sources", {})
    source = sources.get("agent-harness")
    return isinstance(source, dict) and any(key in source for key in ("path", "url", "git"))


def _configure_source(*, uv: str, source: str) -> None:
    """让 uv 以本地 wheel/sdist/project 覆盖标准版本依赖。"""

    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"agent-harness source does not exist: {path}")
    subprocess.run(
        [uv, "add", "--no-sync", str(path)],
        cwd=APP_ROOT,
        check=True,
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", default="uv", help="uv executable used for add/sync")
    return parser.parse_args()


def main() -> int:
    """拒绝含糊的公共同名包解析，然后同步当前项目依赖。"""

    args = parse_args()
    _print_env_hint()
    source = os.environ.get("AGENT_HARNESS_SOURCE", "").strip()
    allow_index = os.environ.get("AGENT_HARNESS_ALLOW_INDEX", "0") == "1"
    if source:
        _configure_source(uv=args.uv, source=source)
    elif not WORKSPACE_CORE.exists() and not _has_explicit_source() and not allow_index:
        raise SystemExit(
            "copied template requires AGENT_HARNESS_SOURCE=/path/to/agent_harness-0.1.0.whl "
            "or AGENT_HARNESS_ALLOW_INDEX=1 with a trusted configured index"
        )

    subprocess.run([args.uv, "sync"], cwd=APP_ROOT, check=True)
    print("bootstrap: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
