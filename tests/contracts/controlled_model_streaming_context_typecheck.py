"""受控模型文本流 Pydantic context 的静态兼容性夹具。

该文件由仓库级 Pyright 门禁分析，不作为运行时 pytest 用例。正例同时覆盖
adapter 内的 TYPE_CHECKING 夹具覆盖真实 SDK 返回类型；本文件覆盖本地 double，
且负例必须保持不兼容，防止 protocol 被放宽。
"""

# pyright: reportUnnecessaryTypeIgnoreComment=true

from __future__ import annotations

from collections.abc import AsyncIterator
from types import TracebackType

from agent_harness.adapters.models._pydantic_ai_streaming import StreamEventContext


def _accept_stream_context(context: StreamEventContext) -> None:
    """把赋值兼容性集中到公开的窄 protocol 边界。"""


class _CompatibleContextDouble:
    """本地 double 只实现 adapter 真正消费的 async context manager 表面。"""

    async def __aenter__(self) -> AsyncIterator[object]:
        async def events() -> AsyncIterator[object]:
            if False:
                yield object()

        return events()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> bool | None:
        return None


_accept_stream_context(_CompatibleContextDouble())


class _IncompatibleContextDouble:
    """错误 double 返回字符串迭代器，不得跨过 stream event protocol。"""

    async def __aenter__(self) -> str:
        return "not-an-event-iterator"

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> bool | None:
        return None


_accept_stream_context(
    _IncompatibleContextDouble()  # pyright: ignore[reportArgumentType]
)
