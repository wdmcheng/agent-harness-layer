"""Local JSONL eval score sink adapter。"""

from __future__ import annotations

from pathlib import Path

from agent_harness.evals.score_sink import ScoreSink


class LocalJsonlScoreSink(ScoreSink):
    """命名 adapter，供扩展者按 `adapters.evals.local_jsonl` 路径发现本地 sink。"""

    def __init__(self, path: Path) -> None:
        """以显式 JSONL 文件构造本地 score sink，保持 adapter 发现入口稳定。"""

        super().__init__(local_path=path)
