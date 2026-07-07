"""service profile 的 runtime worker 外壳。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from app.runtime import build_runtime_components


async def run_once(
    *,
    profile: str = "local",
    profiles_dir: Path | None = None,
    storage_dsn: str | None = None,
    events_path: Path | None = None,
    idempotency_key: str | None = None,
) -> str:
    """执行一次内置 fake run，证明 worker shell 共用 RunOrchestrator seam。"""

    components = build_runtime_components(
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
        events_path=events_path,
    )
    try:
        # 这里暂不拉真实队列；worker 先穿过同一 runtime seam，证明后续取任务
        # 逻辑可以复用 API/CLI 的 storage、event 和 idempotency 边界。
        result = await components.orchestrator.start_run(
            agent_id="fake-agent",
            input={"source": "worker"},
            idempotency_key=idempotency_key or f"worker-{uuid4()}",
        )
        return result.run_id
    finally:
        await components.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Run one fake worker task and exit.")
    parser.add_argument("--profile", default="local")
    parser.add_argument("--profiles-dir", type=Path)
    parser.add_argument("--storage-dsn")
    parser.add_argument("--events-path", type=Path)
    parser.add_argument("--idempotency-key")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.once:
        run_id = asyncio.run(
            run_once(
                profile=args.profile,
                profiles_dir=args.profiles_dir,
                storage_dsn=args.storage_dsn,
                events_path=args.events_path,
                idempotency_key=args.idempotency_key,
            )
        )
        print(f"runtime-worker: run_id={run_id}")
        return
    print("runtime-worker: ready")


if __name__ == "__main__":
    main()
