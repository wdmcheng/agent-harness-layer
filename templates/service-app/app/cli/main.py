"""service-app 专用 Typer 入口，只负责 FastAPI 进程装配。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from agent_harness.config import SettingsLoadError, settings_error_lines
from app.main import create_app

cli = typer.Typer(no_args_is_help=True)


@cli.callback()
def cli_root() -> None:
    """运行 service-app；核心管理命令继续使用 `agent-harness`。"""


@cli.command()
def serve(
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
    events_path: Annotated[Path | None, typer.Option("--events-path")] = None,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
) -> None:
    """从类型化 profile 创建唯一 FastAPI app，并交给 Uvicorn 运行。"""

    try:
        application = create_app(
            profile=profile,
            profiles_dir=profiles_dir,
            storage_dsn=storage_dsn,
            events_path=events_path,
        )
    except SettingsLoadError as exc:
        for line in settings_error_lines(exc):
            typer.echo(line, err=True)
        raise typer.Exit(1) from exc
    uvicorn.run(application, host=host, port=port)


def main() -> None:
    """console script 入口。"""

    cli()


if __name__ == "__main__":
    main()
