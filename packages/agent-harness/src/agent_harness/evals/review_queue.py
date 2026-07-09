"""文件型 draft/approved dataset adapter。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from agent_harness.storage import EvalCaseRecord


class ReviewDatasetAdapter:
    """把 review queue 暴露为 `eval-cases/drafts` 和 `approved` 两个目录。"""

    def __init__(self, *, drafts_dir: Path, approved_dir: Path) -> None:
        self.drafts_dir = drafts_dir
        self.approved_dir = approved_dir

    def write_draft(self, case: EvalCaseRecord) -> Path:
        """写 draft JSON；自动 detector 只能调用这个入口。"""

        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        path = self.draft_path(case.case_id)
        content = json.dumps(case.to_payload(), ensure_ascii=False, indent=2)
        path.write_text(content, encoding="utf-8")
        return path

    def write_approved(self, case: EvalCaseRecord) -> Path:
        """写 approved JSON；调用方必须已经完成人工审核和 audit。"""

        self.approved_dir.mkdir(parents=True, exist_ok=True)
        path = self.approved_path(case.case_id)
        content = json.dumps(case.to_payload(), ensure_ascii=False, indent=2)
        path.write_text(content, encoding="utf-8")
        return path

    def draft_path(self, case_id: str) -> Path:
        return self.drafts_dir / f"{case_id}.json"

    def approved_path(self, case_id: str) -> Path:
        return self.approved_dir / f"{case_id}.json"

    def remove_draft(self, case_id: str) -> None:
        self.draft_path(case_id).unlink(missing_ok=True)

    def remove_approved(self, case_id: str) -> None:
        self.approved_path(case_id).unlink(missing_ok=True)

    def load_approved(self, *, agent_id: str | None = None) -> list[dict[str, object]]:
        """读取 approved case 文件，忽略 draft，供 `make eval` 和 CLI 使用。"""

        return _load_cases(self.approved_dir, agent_id=agent_id)

    def count_drafts(self, *, agent_id: str | None = None) -> int:
        return len(_load_cases(self.drafts_dir, agent_id=agent_id))


def _load_cases(directory: Path, *, agent_id: str | None) -> list[dict[str, object]]:
    if not directory.exists():
        return []
    cases: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json")):
        loaded = json.loads(path.read_text(encoding="utf-8"))
        payload = cast(dict[str, object], loaded) if isinstance(loaded, dict) else None
        if not isinstance(payload, dict):
            continue
        if agent_id is not None and payload.get("agent_id") != agent_id:
            continue
        cases.append(payload)
    return cases
