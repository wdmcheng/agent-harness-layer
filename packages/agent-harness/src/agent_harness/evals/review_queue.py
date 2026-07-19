"""文件型草稿与已批准数据集适配器，明确隔离自动发现和人工批准边界。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from agent_harness.storage import EvalCaseRecord


class ReviewDatasetAdapter:
    """把 review queue 暴露为 `eval-cases/drafts` 和 `approved` 两个目录。"""

    def __init__(self, *, drafts_dir: Path, approved_dir: Path) -> None:
        """保存两条物理队列路径；调用方负责提供同一项目内受控目录。"""
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
        """返回草稿 case 的稳定文件路径，不在查询操作中创建目录。"""
        return self.drafts_dir / f"{case_id}.json"

    def approved_path(self, case_id: str) -> Path:
        """返回已批准 case 的稳定文件路径，供服务与 CLI 使用同一定位规则。"""
        return self.approved_dir / f"{case_id}.json"

    def remove_draft(self, case_id: str) -> None:
        """删除已完成审核的草稿文件；文件已缺失时保持幂等以支持恢复重试。"""
        self.draft_path(case_id).unlink(missing_ok=True)

    def remove_approved(self, case_id: str) -> None:
        """回滚已写入但未提交的批准文件，避免数据库与文件队列出现半提交。"""
        self.approved_path(case_id).unlink(missing_ok=True)

    def load_approved(self, *, agent_id: str | None = None) -> list[dict[str, object]]:
        """读取 approved case 文件，忽略 draft，供 `make eval` 和 CLI 使用。"""

        return _load_cases(self.approved_dir, agent_id=agent_id)

    def count_drafts(self, *, agent_id: str | None = None) -> int:
        """统计仍被排除在评分之外的草稿数量，供 CLI 明确报告人工待办。"""
        return len(_load_cases(self.drafts_dir, agent_id=agent_id))


def _load_cases(directory: Path, *, agent_id: str | None) -> list[dict[str, object]]:
    """读取目录中的对象型 JSON case，可选按 Agent 过滤并忽略损坏形状。

    草稿或批准文件可能由人工调整；非对象内容不进入评测，以防文件队列把无效
    载荷误当为可执行样本。解析错误仍向上传递，提示维护者修复真正损坏的 JSON。
    """
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
