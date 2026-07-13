"""类型化配置加载的稳定错误与进程诊断。"""

from __future__ import annotations

from collections.abc import Sequence

from agent_harness.contracts.errors import ApiErrorEnvelope, ErrorDetail, HarnessError


class SettingsLoadError(HarnessError):
    """配置加载失败，携带可展示给 CLI/API 的诊断。"""

    def __init__(self, errors: Sequence[ErrorDetail]) -> None:
        self.errors = list(errors)
        super().__init__(self.errors)

    def to_envelope(self) -> ApiErrorEnvelope:
        return ApiErrorEnvelope(error=self.errors[0])


def settings_error_lines(error: SettingsLoadError) -> list[str]:
    """把结构化配置错误渲染为不含原始输入的稳定进程诊断。"""

    lines: list[str] = []
    for detail in error.errors:
        field = f" field={detail.field_path}" if detail.field_path else ""
        hint = f" hint={detail.hint}" if detail.hint else ""
        lines.append(f"{detail.code}:{field} {detail.message}{hint}")
    return lines


__all__ = ["SettingsLoadError", "settings_error_lines"]
