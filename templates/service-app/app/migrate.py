"""Service profile 的类型化迁移组合入口，先验证配置再产生数据库副作用。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_harness.config import SettingsLoadError, load_settings, settings_error_lines
from agent_harness.storage import run_migrations, storage_dsn_from_settings


def run(
    *,
    profile: str = "service",
    profiles_dir: Path | None = None,
    storage_dsn: str | None = None,
) -> None:
    """先完成 typed settings 门禁，再运行 migration 副作用。"""

    settings = load_settings(profile=profile, profiles_dir=profiles_dir)
    run_migrations(storage_dsn or storage_dsn_from_settings(settings))


def parse_args() -> argparse.Namespace:
    """解析迁移命令参数；显式 DSN 仅用于受控运维和测试场景。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="service")
    parser.add_argument("--profiles-dir", type=Path)
    parser.add_argument("--storage-dsn")
    return parser.parse_args()


def main() -> None:
    """运行迁移 CLI，并把配置装载错误转换为逐行可读的非零退出。"""
    args = parse_args()
    try:
        run(
            profile=args.profile,
            profiles_dir=args.profiles_dir,
            storage_dsn=args.storage_dsn,
        )
    except SettingsLoadError as exc:
        for line in settings_error_lines(exc):
            print(line, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
