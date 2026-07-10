"""通过 ExampleEvalAdapter 运行四个 approved fake-model dataset。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    # 脚本应能从任意 cwd 运行；这里只暴露复制项目自身，不猜 monorepo 相对路径。
    sys.path.insert(0, str(APP_ROOT))

from agent_harness.evals import EvalRunner, ScoreSink  # noqa: E402
from agent_harness.identity import IdentityContext  # noqa: E402
from agent_harness.observability import TelemetryFacade  # noqa: E402
from app.evals import ExampleEvalAdapter  # noqa: E402
from app.runtime import build_runtime_components  # noqa: E402

AGENTS = (
    "examples.rag_assistant",
    "examples.ticket_triage",
    "examples.repo_analyst",
    "examples.dev_assistant",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=AGENTS, action="append")
    parser.add_argument("--state-dir", type=Path, default=APP_ROOT / ".agent-harness" / "eval")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    """共用同一 runtime composition，并把 score/trace 先写 local evidence。"""

    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    components = build_runtime_components(
        profile="local",
        profiles_dir=APP_ROOT / "configs" / "profiles",
        storage_dsn=f"sqlite+aiosqlite:///{state_dir / 'eval.db'}",
        events_path=state_dir / "traces.jsonl",
        artifact_root=state_dir / "artifacts",
    )
    identity = IdentityContext.local_default(session_id="example-eval")
    adapter = ExampleEvalAdapter(
        orchestrator=components.orchestrator,
        approval_service=components.approval_service,
        storage=components.storage,
        identity=identity,
    )
    runner = EvalRunner(
        score_sink=ScoreSink(
            local_path=state_dir / "scores.jsonl",
            telemetry=TelemetryFacade(local_sink=components.event_sink),
        )
    )
    failures = 0
    try:
        for agent_id in args.agent or AGENTS:
            dataset_dir = APP_ROOT / "agents" / Path(*agent_id.split(".")) / "evals"
            result = await runner.run_file_dataset(
                dataset_dir=dataset_dir,
                agent_id=agent_id,
                dataset=f"{agent_id}.approved",
                case_executor=adapter,
            )
            passed = int(result.score_summary.get("passed", 0))
            failed = int(result.score_summary.get("failed", 0))
            failures += failed
            print(
                f"example-eval: agent={agent_id} status={result.status} "
                f"cases={result.case_count} passed={passed} failed={failed} "
                f"drafts_skipped={result.skipped_drafts} refs={len(result.local_refs)}"
            )
    finally:
        await components.close()
    print(f"example-eval: status={'ok' if failures == 0 else 'failed'} failures={failures}")
    return 0 if failures == 0 else 1


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
