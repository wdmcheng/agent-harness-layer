"""Local state 迁移合同共享的路径、JSONL 与 SQLite 只读夹具。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates/service-app/configs/profiles"
AGENTS = ROOT / "templates/service-app/agents"


async def trace_a_resolver(*, tenant_id: str, run_id: str) -> str:
    """为 local sink 合同提供固定且可核对的 run-trace binding。"""

    assert tenant_id == "tenant-a"
    assert run_id == "run-a"
    return "trace-a"


def write_jsonl(path: Path, records: list[dict[str, object]]) -> bytes:
    """按迁移入口消费的逐行 JSON 格式写入合同 fixture。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()
    path.write_bytes(payload)
    return payload


def table_count(database: Path, table: str) -> int:
    """只读统计隔离测试库中的业务记录数。"""

    with sqlite3.connect(database) as connection:
        row = connection.execute(f"select count(*) from {table}").fetchone()
    assert row is not None
    return int(row[0])
