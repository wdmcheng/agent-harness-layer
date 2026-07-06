"""结构化错误契约。"""

from __future__ import annotations

from collections.abc import Sequence

from agent_harness.contracts.dto import HarnessDTO


class ErrorDetail(HarnessDTO):
    """CLI/API 可展示的稳定错误详情。"""

    code: str
    message: str
    request_id: str | None = None
    field_path: str | None = None
    hint: str | None = None


class ApiErrorEnvelope(HarnessDTO):
    """API 层统一错误封套。"""

    error: ErrorDetail


class HarnessError(Exception):
    """携带一个或多个公共错误详情的基础异常。"""

    def __init__(self, errors: Sequence[ErrorDetail]) -> None:
        self.error_details = list(errors)
        message = "; ".join(error.message for error in self.error_details)
        super().__init__(message)

    def to_envelope(self) -> ApiErrorEnvelope:
        first = (
            self.error_details[0]
            if self.error_details
            else ErrorDetail(
                code="unknown",
                message="Unknown error",
            )
        )
        return ApiErrorEnvelope(error=first)
