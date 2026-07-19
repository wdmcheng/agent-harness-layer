"""Provider-neutral canonical run trace 格式与结构化错误。"""

from __future__ import annotations

import re
from uuid import uuid4

TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RunTraceError(RuntimeError):
    """API、CLI 与内部入口共用的结构化 trace 错误。"""

    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        """保存跨 API、CLI 与运行时可一致映射的错误码和 HTTP 语义。"""

        super().__init__(message)
        self.code = code
        self.status_code = status_code


class RunTraceValidationError(RunTraceError):
    """调用方提供的 trace 标识不符合受限字符与长度规则。"""

    def __init__(self) -> None:
        """构造不回显非法 trace 原文的固定校验错误。"""

        super().__init__("validation_error", "trace id is invalid", status_code=422)


class RunTraceConflict(RunTraceError):
    """新根运行试图占用已经归属的 canonical trace。"""

    def __init__(self) -> None:
        """构造稳定冲突响应，不暴露现有运行或租户身份。"""

        super().__init__("trace.conflict", "trace id is already bound", status_code=409)


class RunTraceIdempotencyConflict(RunTraceError):
    """同一幂等运行被请求绑定到不同 trace 时的身份冲突。"""

    def __init__(self) -> None:
        """构造可供调用方修正 trace 的固定 409 错误。"""

        super().__init__(
            "trace.idempotency_conflict",
            "idempotent run is already bound to another trace",
            status_code=409,
        )


class PreparedRunTrace(str):
    """保留 canonical 值及原始 caller 是否显式提供 trace 的 provenance。"""

    caller_trace_id: str | None
    replays_existing: bool

    def __new__(
        cls,
        canonical_trace_id: str,
        *,
        caller_trace_id: str | None,
        replays_existing: bool,
    ) -> PreparedRunTrace:
        """创建字符串子类并附带调用方来源与既有运行重放标记。"""

        instance = super().__new__(cls, canonical_trace_id)
        instance.caller_trace_id = caller_trace_id
        instance.replays_existing = replays_existing
        return instance


def normalize_trace_id(value: str | None) -> str:
    """保留合法 caller 值；缺失时生成 lowercase RFC 4122 UUID。"""

    if value is None:
        return str(uuid4())
    if TRACE_ID_PATTERN.fullmatch(value) is None:
        raise RunTraceValidationError
    return value
