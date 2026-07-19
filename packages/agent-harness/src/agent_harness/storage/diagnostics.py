"""doctor 命令使用的 storage 与 service 依赖诊断。"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agent_harness.config.schemas import HarnessSettings, ObservabilityProviderSettings
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage.migrations.runner import get_current_revision
from agent_harness.storage.settings import storage_dsn_from_settings

RETRIEVAL_OPTIONAL_EXTENSIONS = ("pgroonga", "vector")


@dataclass(frozen=True)
class ExtensionStatus:
    """PostgreSQL extension 的 doctor 诊断摘要。"""

    name: str
    status: str
    default_version: str | None = None
    installed_version: str | None = None
    error: str | None = None


def eval_directory_for_profiles(profiles_dir: Path | None) -> Path:
    """根据 profile 目录或当前工作目录推导评测用例目录，保持 CLI 默认可预测。"""

    if profiles_dir is not None:
        return profiles_dir.parent.parent / "eval-cases"
    cwd_candidate = Path.cwd() / "templates" / "service-app" / "eval-cases"
    if cwd_candidate.exists():
        return cwd_candidate
    return Path.cwd() / "eval-cases"


def migration_revision(settings: HarnessSettings, storage_dsn: str | None = None) -> str | None:
    """读取配置或显式 DSN 所指数据库的当前迁移版本，不产生任何写入。"""

    dsn = storage_dsn or storage_dsn_from_settings(settings)
    return get_current_revision(dsn)


def _directory_writable(path: Path) -> tuple[bool, str]:
    """通过创建并删除短生命周期探针文件验证目录的实际写入能力。"""

    if not path.exists():
        return False, "missing"
    if not path.is_dir():
        return False, "not a directory"

    probe = path / ".agent-harness-doctor.tmp"
    try:
        # doctor 用一个短生命周期 probe 文件检查真实写权限。只看 os.access
        # 容易被 ACL、容器挂载或只读 volume 欺骗。
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"not writable ({exc})"
    return True, "ok"


def eval_directory_status(profiles_dir: Path | None) -> tuple[bool, str, Path]:
    """返回评测目录的可写状态、诊断信息和最终解析路径。"""

    directory = eval_directory_for_profiles(profiles_dir)
    ok, message = _directory_writable(directory)
    return ok, message, directory


def observability_status(settings: HarnessSettings) -> tuple[bool, str]:
    """验证本地 JSONL 观测落点可写；非本地后端仅报告其已配置状态。"""

    if settings.observability.kind != "local-jsonl":
        return True, f"{settings.observability.kind} configured"

    sink_path = Path(settings.observability.path or ".agent-harness/traces.jsonl")
    parent = sink_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    ok, message = _directory_writable(parent)
    if not ok:
        return False, f"local-jsonl path not writable: {message}"
    return True, f"local-jsonl writable ({sink_path})"


def observability_provider_statuses(settings: HarnessSettings) -> list[str]:
    """返回外部 observability provider 的 doctor 摘要。

    provider 是 optional fan-out；缺失 token 或未启用不应影响 local/jsonl evidence。
    """

    if not settings.observability.providers:
        return ["none configured"]
    return [
        _format_observability_provider(provider) for provider in settings.observability.providers
    ]


def _format_observability_provider(provider: ObservabilityProviderSettings) -> str:
    """生成不泄露 token 值的外部观测 provider 摘要，并说明本地回退仍可用。"""

    if not provider.enabled:
        return f"{provider.kind}: disabled (optional; local-jsonl remains active)"
    if provider.token_env is not None:
        return (
            f"{provider.kind}: enabled "
            f"(token env {redact_secrets(provider.token_env)}, local-jsonl fallback active)"
        )
    if provider.endpoint is not None:
        return f"{provider.kind}: enabled ({provider.endpoint}, local-jsonl fallback active)"
    return f"{provider.kind}: enabled (local-jsonl fallback active)"


def redis_status(settings: HarnessSettings, timeout_seconds: float = 1.0) -> tuple[bool, str]:
    """对已启用 Redis 队列执行有界 PING 探测，避免 doctor 长时间阻塞。"""

    if settings.queue.kind != "redis":
        return True, "not required"
    if not settings.queue.dsn:
        return False, "missing dsn"

    parsed = urlparse(settings.queue.dsn)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    try:
        # 直接发送最小 RESP PING，避免为诊断命令引入额外 Redis 客户端状态。
        with socket.create_connection((host, port), timeout=timeout_seconds) as connection:
            connection.sendall(b"*1\r\n$4\r\nPING\r\n")
            response = connection.recv(16)
    except OSError as exc:
        return False, f"unreachable ({exc})"
    if response.startswith(b"+PONG"):
        return True, "ok"
    return False, "bad response"


def retrieval_extension_statuses(
    settings: HarnessSettings,
    storage_dsn: str | None = None,
) -> list[ExtensionStatus]:
    """返回 retrieval optional extensions 的诊断状态，不把缺失当作必需失败。"""

    if settings.storage.kind != "postgresql":
        return [
            ExtensionStatus(name=name, status="not_required")
            for name in RETRIEVAL_OPTIONAL_EXTENSIONS
        ]
    try:
        dsn = storage_dsn or storage_dsn_from_settings(settings)
    except ValueError as exc:
        return [
            ExtensionStatus(name=name, status="unknown", error=str(exc))
            for name in RETRIEVAL_OPTIONAL_EXTENSIONS
        ]
    return _run_extension_probe(dsn, RETRIEVAL_OPTIONAL_EXTENSIONS)


def format_retrieval_extension_status(status: ExtensionStatus) -> str:
    """格式化 doctor 输出，明确 optional extension 的降级语义。"""

    if status.status == "not_required":
        return f"{status.name}: not required for local profile"
    if status.status == "installed":
        version = f" ({status.installed_version})" if status.installed_version else ""
        return f"{status.name}: installed{version}"
    if status.status == "available":
        version = f" ({status.default_version})" if status.default_version else ""
        return (
            f"{status.name}: available{version} "
            "(optional; not installed, degraded to PostgreSQL native FTS/local BM25)"
        )
    if status.status == "missing":
        return f"{status.name}: missing (optional; degraded to PostgreSQL native FTS/local BM25)"
    detail = f" ({status.error})" if status.error else ""
    return f"{status.name}: unknown{detail}"


def _run_extension_probe(dsn: str, names: tuple[str, ...]) -> list[ExtensionStatus]:
    """在同步 doctor 命令中运行异步扩展探测，保持调用方无需管理事件循环。"""

    import asyncio

    return asyncio.run(_extension_probe(dsn, names))


async def _extension_probe(dsn: str, names: tuple[str, ...]) -> list[ExtensionStatus]:
    """查询 PostgreSQL 可选扩展可用性；任何连接异常降级为可展示诊断结果。"""

    engine = create_async_engine(dsn)
    statuses: list[ExtensionStatus] = []
    try:
        # 每个扩展独立记录状态，缺失可选能力不会让整个 doctor 命令失败。
        async with engine.connect() as connection:
            for name in names:
                result = await connection.execute(
                    text(
                        """
                        select name, default_version, installed_version
                        from pg_available_extensions
                        where name = :name
                        """
                    ),
                    {"name": name},
                )
                row = result.mappings().first()
                if row is None:
                    statuses.append(ExtensionStatus(name=name, status="missing"))
                elif row["installed_version"]:
                    statuses.append(
                        ExtensionStatus(
                            name=name,
                            status="installed",
                            default_version=row["default_version"],
                            installed_version=row["installed_version"],
                        )
                    )
                else:
                    statuses.append(
                        ExtensionStatus(
                            name=name,
                            status="available",
                            default_version=row["default_version"],
                        )
                    )
    except Exception as exc:  # noqa: BLE001 - doctor must report diagnostics, not traceback
        statuses = [ExtensionStatus(name=name, status="unknown", error=str(exc)) for name in names]
    finally:
        await engine.dispose()
    return statuses
