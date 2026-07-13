"""本地 CLI 子命令共用的配置、事件路径和策略构造 helper。"""

from __future__ import annotations

from pathlib import Path

import typer

from agent_harness.audit import AuditService
from agent_harness.config import (
    HarnessSettings,
    SettingsLoadError,
    load_settings,
    settings_error_lines,
)
from agent_harness.policy import DatabasePolicyProvider, PolicyEngine, YamlPolicyProvider
from agent_harness.storage import SQLAlchemyStorage


def load_settings_or_exit(profile: str, profiles_dir: Path | None) -> HarnessSettings:
    """加载 settings；失败时按 CLI 可读格式输出结构化诊断。"""

    try:
        return load_settings(profile=profile, profiles_dir=profiles_dir)
    except SettingsLoadError as exc:
        for line in settings_error_lines(exc):
            typer.echo(line, err=True)
        raise typer.Exit(1) from exc


def event_path(settings: HarnessSettings, events_path: Path | None) -> Path:
    """解析 local/jsonl event sink 路径。"""

    if events_path is not None:
        return events_path
    return Path(settings.observability.path or ".agent-harness/traces.jsonl")


def policy_engine(
    settings: HarnessSettings,
    storage: SQLAlchemyStorage,
    audit: AuditService,
    profiles_dir: Path | None = None,
) -> PolicyEngine:
    """按 profile 构造 CLI 使用的 PolicyEngine。"""

    if settings.policy.provider == "db":
        provider = DatabasePolicyProvider(storage=storage)
    else:
        policy_path = resolve_policy_path(settings, profiles_dir)
        provider = (
            YamlPolicyProvider.from_path(
                policy_path,
                fallback_require_approval_actions=settings.policy.require_approval_actions,
                fallback_deny_actions=settings.policy.deny_actions,
            )
            if policy_path is not None
            else YamlPolicyProvider(
                require_approval_actions=settings.policy.require_approval_actions,
                deny_actions=settings.policy.deny_actions,
            )
        )
    return PolicyEngine(provider=provider, audit=audit)


def resolve_policy_path(settings: HarnessSettings, profiles_dir: Path | None) -> Path | None:
    """解析相对 policy YAML 路径，兼容模板目录和当前工作目录。"""

    if settings.policy.path is None:
        return None
    configured_path = Path(settings.policy.path)
    if configured_path.is_absolute():
        return configured_path
    service_root = profiles_dir.parent.parent if profiles_dir is not None else Path.cwd()
    candidate_service_root = service_root / configured_path
    if candidate_service_root.exists():
        return candidate_service_root
    repo_template_root = Path.cwd() / "templates" / "service-app"
    return repo_template_root / configured_path
