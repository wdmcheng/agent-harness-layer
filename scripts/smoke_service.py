"""Run service profile smoke against Docker Compose PostgreSQL and Redis.

这个脚本是 storage/runtime service profile 的证据入口，不是单纯的“能起容器”：
它必须证明 PostgreSQL migration revision、Redis reachability，以及 repository
adapter 真能穿过 PostgreSQL 写入 run。SQLite smoke 不能替代这个脚本的证据。

脚本只管理本项目 `agent-harness-layer` compose project 和 `agent-harness-*`
容器。它不会读取或修改 wiki-brain；若本机已有可复用镜像，只通过 compose env
覆盖镜像名，不改 Dockerfile 或外部项目。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from agent_harness.config import load_settings
from agent_harness.context import ContextAssembler, ContextFragment
from agent_harness.embeddings import EmbeddingRequest, LocalEmbeddingProvider
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.diagnostics import migration_revision, redis_status
from agent_harness.storage.repositories import RunCreate, SessionCreate

ROOT = Path(__file__).resolve().parents[1]
SERVICE_APP = ROOT / "templates" / "service-app"
COMPOSE_FILE = SERVICE_APP / "docker-compose.yml"
PROFILES = SERVICE_APP / "configs" / "profiles"
PROJECT_NAME = "agent-harness-layer"


def image_exists(image: str) -> bool:
    """检查本机镜像是否存在，用于实现“优先复用本地已有镜像”的运行策略。"""

    result = subprocess.run(
        ["docker", "image", "inspect", image],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def compose_env() -> dict[str, str]:
    """构造 compose 环境变量。

    compose 文件默认保持计划里的 Redis 7.2.4；但用户要求优先复用本地镜像，
    所以 smoke 运行时如果没有 `redis:7.2.4` 但已有 `redis:8`，只在本次
    smoke 的环境变量里覆盖。这样既保留默认合规线，又让本地验证不因为缺少
    非核心镜像而阻塞。
    """

    env = os.environ.copy()
    # 镜像选择只属于本次运行环境。compose 文件保留计划默认值；脚本可以把
    # Docker Compose 指到本机已有镜像，从而满足“优先复用本地镜像”，同时不碰
    # PostgreSQL packaging 文件。
    if "AGENT_HARNESS_POSTGRES_IMAGE" not in env and image_exists("postgres:18"):
        env["AGENT_HARNESS_POSTGRES_IMAGE"] = "postgres:18"
    if (
        "AGENT_HARNESS_REDIS_IMAGE" not in env
        and not image_exists("redis:7.2.4")
        and image_exists("redis:8")
    ):
        env["AGENT_HARNESS_REDIS_IMAGE"] = "redis:8"
    return env


def run_compose_up(env: dict[str, str]) -> None:
    """启动 service profile 依赖，并等待 healthcheck。

    `--wait` 让失败停在容器健康状态，而不是后续 migration 抛一个更模糊的
    connection error。PostgreSQL 18 的 volume 布局已在 compose 中按官方镜像
    要求挂载到 `/var/lib/postgresql`。
    """

    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "-p",
            PROJECT_NAME,
            "--profile",
            "service",
            "up",
            "-d",
            "--wait",
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )


async def repository_probe(dsn: str) -> str:
    """穿过 PostgreSQL repository/UoW 写入一条 run，返回 run id 作为证据。

    这一步避免 service smoke 只证明“migration 能跑”。如果 repository adapter、
    asyncpg driver、事务提交或 idempotency unique constraint 有问题，这里会失败。
    """

    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            # probe 使用和 runtime 一样的 tenant -> session -> run 路径。
            # 手写 INSERT 会绕过 repository mapping、idempotency 查询和显式事务语义。
            tenant = await uow.tenants.ensure("default")
            session = await uow.sessions.create(
                SessionCreate(
                    tenant_id=tenant.id,
                    user_id="service-smoke",
                    agent_id="fake-agent",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id=tenant.id,
                    session_id=session.id,
                    agent_id="fake-agent",
                    idempotency_key="service-smoke",
                    input={"smoke": "service"},
                )
            )
            await uow.commit()
        return run.id
    finally:
        await storage.dispose()


async def context_embedding_probe(dsn: str) -> tuple[str, str]:
    """穿过 PostgreSQL repository 写 context assembly 和 embedding cache 证据。"""

    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            assembly = await ContextAssembler(uow.context_assemblies).assemble(
                tenant_id="default",
                run_id=None,
                fragments=[
                    ContextFragment(
                        source_ref="smoke:history",
                        trust_level="trusted",
                        content="service smoke history",
                        token_estimate=3,
                    )
                ],
                token_budget=32,
                output_ref="context://service-smoke",
            )
            provider = LocalEmbeddingProvider(cache=uow.embedding_cache)
            first = await provider.embed(EmbeddingRequest(input="service smoke embedding"))
            second = await provider.embed(EmbeddingRequest(input="service smoke embedding"))
            await uow.commit()
        cache_status = (
            "hit" if second.cache.hit and first.vector_ref == second.vector_ref else "miss"
        )
        return assembly.id, cache_status
    finally:
        await storage.dispose()


def run_worker_probe(dsn: str) -> str:
    """运行一次 service worker shell，返回 worker 创建的 run id。"""

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(SERVICE_APP)
        if not existing_pythonpath
        else f"{SERVICE_APP}{os.pathsep}{existing_pythonpath}"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.workers.runtime_worker",
            "--once",
            "--profile",
            "service",
            "--profiles-dir",
            str(PROFILES),
            "--storage-dsn",
            dsn,
            "--events-path",
            str(ROOT / ".agent-harness" / "service-worker-events.jsonl"),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    # worker 输出保持一行，方便 CI 日志和 reviewer 直接定位 worker seam 证据。
    return result.stdout.strip().removeprefix("runtime-worker: run_id=")


def parse_args() -> argparse.Namespace:
    """保留 migrate-only 模式，供只想验证 schema 或 CI 拆步时使用。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migrate-only",
        action="store_true",
        help="Start service dependencies and run PostgreSQL migration without repository probe.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings(profile="service", profiles_dir=PROFILES)
    env = compose_env()
    # 记录实际使用的镜像名，最终输出要能让 reviewer 区分默认镜像和本机复用镜像。
    postgres_image = env.get("AGENT_HARNESS_POSTGRES_IMAGE", "postgres:18")
    redis_image = env.get("AGENT_HARNESS_REDIS_IMAGE", "redis:7.2.4")

    run_compose_up(env)
    assert settings.storage.dsn is not None
    # migration、Redis、repository 检查分别输出证据行。这样失败点可诊断，
    # 也避免把容器 healthcheck 绿灯误当成完整 service smoke 通过。
    # service profile 的 migration 必须连真实 PostgreSQL；不能用 SQLite revision 代替。
    run_migrations(settings.storage.dsn)
    revision = migration_revision(settings)
    redis_ok, redis_message = redis_status(settings, timeout_seconds=2.0)
    if revision is None:
        print("smoke-service: PostgreSQL migration revision missing", file=sys.stderr)
        return 1
    if not redis_ok:
        print(f"smoke-service: Redis check failed: {redis_message}", file=sys.stderr)
        return 1

    run_id = "(migrate-only)"
    worker_run_id = "(migrate-only)"
    context_assembly_id = "(migrate-only)"
    embedding_cache = "(migrate-only)"
    if not args.migrate_only:
        # repository probe 是 PostgreSQL storage adapter 的最小行为证明。
        run_id = asyncio.run(repository_probe(settings.storage.dsn))
        context_assembly_id, embedding_cache = asyncio.run(
            context_embedding_probe(settings.storage.dsn)
        )
        # worker probe 证明 runtime worker shell 不是孤立占位，而是共用 runtime seam。
        worker_run_id = run_worker_probe(settings.storage.dsn)

    print(f"smoke-service: postgres image={postgres_image} container=agent-harness-postgres")
    print(f"smoke-service: redis image={redis_image} container=agent-harness-redis")
    print(f"smoke-service: migration={revision}")
    print(f"smoke-service: redis={redis_message}")
    print(f"smoke-service: repository_run={run_id}")
    print(f"smoke-service: context_assembly={context_assembly_id}")
    print(f"smoke-service: embedding_cache={embedding_cache}")
    print(f"smoke-service: worker_run={worker_run_id}")
    print("smoke-service: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
