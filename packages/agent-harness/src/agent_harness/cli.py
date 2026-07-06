"""Agent Harness 的命令行入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agent_harness.config import SettingsLoadError, load_settings

app = typer.Typer(no_args_is_help=True)


@app.callback()
def cli_root() -> None:
    """开发者和维护者共用的本地命令集合。"""


@app.command()
def doctor(
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
) -> None:
    """校验 profile 配置并输出本地诊断，不打开外部连接。"""

    try:
        settings = load_settings(profile=profile, profiles_dir=profiles_dir)
    except SettingsLoadError as exc:
        for error in exc.errors:
            field = f" field={error.field_path}" if error.field_path else ""
            hint = f" hint={error.hint}" if error.hint else ""
            typer.echo(f"{error.code}:{field} {error.message}{hint}", err=True)
        raise typer.Exit(1) from exc

    # doctor 只报告配置边界；数据库、队列、模型和观测连接留给后续 service smoke。
    key_status = "api key required" if settings.model.requires_api_key else "api key not required"
    typer.echo(f"profile: {settings.profile}")
    typer.echo(f"storage: {settings.storage.kind}")
    typer.echo(f"queue: {settings.queue.kind}")
    typer.echo(f"observability: {settings.observability.kind}")
    typer.echo(f"policy: {settings.policy.provider}")
    typer.echo(
        f"identity: {settings.identity.default.tenant_id}/{settings.identity.default.user_id}"
    )
    typer.echo(f"model: {settings.model.provider} ({key_status})")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
