"""Structured error contracts."""

from __future__ import annotations

from collections.abc import Sequence

from agent_harness.contracts.dto import HarnessDTO


class ErrorDetail(HarnessDTO):
    """Stable error detail for CLI/API diagnostics."""

    code: str
    message: str
    field_path: str | None = None
    hint: str | None = None


class ApiErrorEnvelope(HarnessDTO):
    """Stable API error envelope."""

    error: ErrorDetail


class HarnessError(Exception):
    """Base exception carrying one or more public error details."""

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
