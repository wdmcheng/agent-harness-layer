"""Local state 显式迁移命令及其数据库协作者。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import text as sql_text

from agent_harness.cli_shared import load_settings_or_exit
from agent_harness.local_state import (
    LocalStateMigrationError,
    LocalStateMigrationResult,
    migrate_local_state,
    migrate_profile_local_state,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations, storage_dsn_from_settings


def register_local_state_commands(app: typer.Typer) -> None:
    """把 local-state 维护命令注册到根 CLI。"""

    app.command("migrate-local-state")(migrate_local_state_command)


async def _load_run_values(
    dsn: str,
    *,
    include_trace: bool,
) -> dict[str, str | None]:
    """读取迁移预检或升级后 trace 投影，并确保释放临时 storage。"""

    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        column = ", trace_id" if include_trace else ""
        async with storage.engine.connect() as connection:
            rows = await connection.execute(
                sql_text(f"select id{column} from agent_runs order by id")
            )
            return {str(row.id): (str(row.trace_id) if include_trace else None) for row in rows}
    finally:
        await storage.dispose()


def _migrate_profile_bundle(
    *,
    state_dir: Path,
    profile: str,
    profiles_dir: Path | None,
    event_paths: tuple[Path, ...],
    score_paths: tuple[Path, ...],
) -> LocalStateMigrationResult:
    """完整预检旧 schema 的文件引用，再推进数据库并重写 bundle。"""

    settings = load_settings_or_exit(profile, profiles_dir)
    dsn = storage_dsn_from_settings(settings)
    known_run_ids = set(asyncio.run(_load_run_values(dsn, include_trace=False)))

    def upgrade_database_and_load_traces() -> dict[str, str]:
        """在文件重写前完成 schema 升级并返回迁移所需的 canonical trace 映射。"""

        run_migrations(dsn)
        values = asyncio.run(_load_run_values(dsn, include_trace=True))
        return {run_id: trace_id for run_id, trace_id in values.items() if trace_id is not None}

    return migrate_profile_local_state(
        state_dir=state_dir,
        event_paths=event_paths,
        score_paths=score_paths,
        known_run_ids=known_run_ids,
        database_upgrade=upgrade_database_and_load_traces,
    )


def migrate_local_state_command(
    state_dir: Annotated[Path, typer.Option("--state-dir")],
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    file_only: Annotated[bool, typer.Option("--file-only")] = False,
    event_paths: Annotated[list[Path] | None, typer.Option("--event-path")] = None,
    score_paths: Annotated[list[Path] | None, typer.Option("--score-path")] = None,
) -> None:
    """离线迁移显式 inventory 中的 legacy event/score JSONL。"""

    if (profile is not None) == file_only or (file_only and profiles_dir is not None):
        typer.echo("local_state.mode_conflict: choose exactly one migration mode", err=True)
        raise typer.Exit(1)

    try:
        if profile is not None:
            result = _migrate_profile_bundle(
                state_dir=state_dir,
                profile=profile,
                profiles_dir=profiles_dir,
                event_paths=tuple(event_paths or ()),
                score_paths=tuple(score_paths or ()),
            )
        else:
            result = migrate_local_state(
                state_dir=state_dir,
                event_paths=event_paths or (),
                score_paths=score_paths or (),
                file_only=True,
            )
    except LocalStateMigrationError as exc:
        typer.echo(f"{exc.code}: {exc}", err=True)
        raise typer.Exit(1) from exc
    except Exception as exc:  # noqa: BLE001 - credential/DSN 不得进入 CLI 错误
        typer.echo(
            "local_state.database_migration_failed: database migration failed",
            err=True,
        )
        raise typer.Exit(1) from exc

    typer.echo(f"mode: {result.mode}")
    typer.echo(f"migrated_records: {result.migrated_records}")
    for path in result.paths:
        typer.echo(f"inventory: {path}")
