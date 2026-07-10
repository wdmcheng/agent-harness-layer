"""Eval experiment 公共错误类型。"""

from __future__ import annotations


class EvalExperimentError(RuntimeError):
    """可由 API/CLI 映射为稳定错误 envelope 的业务错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        field_path: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.field_path = field_path
        self.hint = hint


class DatasetSplitError(EvalExperimentError):
    """Behavior tag 或 dataset split 不满足安全/完整性门禁。"""
