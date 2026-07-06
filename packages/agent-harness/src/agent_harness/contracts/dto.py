"""稳定边界 payload 使用的 DTO 基础工具。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class HarnessDTO(BaseModel):
    """API、事件、trace 和 adapter 边界共用的 DTO 基类。

    未声明字段会被拒绝，避免 vendor SDK 对象和私有实现状态悄悄进入公共
    payload 契约。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def to_payload(self) -> dict[str, Any]:
        """返回跨进程、API、事件和 trace 使用的 JSON-compatible 形态。"""

        return self.model_dump(mode="json", exclude_none=True)
