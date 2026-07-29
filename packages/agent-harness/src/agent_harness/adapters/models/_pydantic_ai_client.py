"""受控 OpenAI transport 与 client lease/factory。

该私有模块只管理 credential-bearing HTTP/SDK 资源和可信响应分类；provider adapter
继续只消费这里的封闭 lease，不向核心层泄漏 vendor 类型。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI, OpenAIError
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from agent_harness.config.model_endpoints import normalize_model_endpoint
from agent_harness.config.schemas import ModelSettings
from agent_harness.models.providers import ModelAttemptEvidence
from agent_harness.models.router import ModelRoutePlan


class _PydanticAgent(Protocol):
    """client lease 只依赖 async Agent.run 的最小表面。"""

    async def run(self, prompt: str, *, model_settings: object) -> Any: ...


AgentFactory = Callable[[ModelRoutePlan], Any]
TransportFactory = Callable[[], httpx.AsyncBaseTransport]


class ModelProviderError(RuntimeError):
    """adapter 向核心暴露的安全失败，不保存 raw response/header/exception。"""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        retry_after_ms: int | None = None,
        completion_observed: bool | None = None,
        side_effect_state: str = "unknown",
        attempts: tuple[ModelAttemptEvidence, ...] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after_ms = retry_after_ms
        self.completion_observed = completion_observed
        self.side_effect_state = side_effect_state
        self.attempts = attempts


class _ControlledTransportSignal(ModelProviderError, OpenAIError):
    """让安全分类穿过锁定 OpenAI/Pydantic AI 链而不暴露 raw response。

    OpenAI SDK 会原样传播 `OpenAIError`，Pydantic AI 只重写其公开的
    APIStatus/APIConnection 子类。私有多继承 signal 因此仍能回到本 adapter 的
    `ModelProviderError` 控制器，同时不会把 vendor 类型或原始 header 送进核心层。
    """


class ControlledOpenAITransport(httpx.AsyncBaseTransport):
    """socket send 前复核 origin/path，并重建封闭的 credential header 集合。"""

    def __init__(
        self,
        *,
        inner: httpx.AsyncBaseTransport,
        canonical_base_url: str,
        api_key: str,
        completion_classifier_ref: str | None = None,
        completion_classifier_version: str | None = None,
    ) -> None:
        self._inner = inner
        self._base = urlsplit(canonical_base_url)
        self._origin = f"{self._base.scheme}://{self._base.hostname}" + (
            f":{self._base.port}" if self._base.port is not None else ""
        )
        self._base_path = self._base.path.rstrip("/")
        self._api_key = api_key
        self._completion_classifier_ref = completion_classifier_ref
        self._completion_classifier_version = completion_classifier_version
        self._closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """只转发冻结 origin/base path 内的 JSON POST，并丢弃 SDK/ambient headers。"""

        request_origin = f"{request.url.scheme}://{request.url.host}" + (
            f":{request.url.port}" if request.url.port is not None else ""
        )
        if (
            request.method != "POST"
            or request_origin != self._origin
            or not request.url.path.startswith(f"{self._base_path}/")
        ):
            raise httpx.RequestError("controlled endpoint identity mismatch", request=request)
        content = await request.aread()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # 当前 non-stream attempt seam 尚未产生稳定幂等身份，因此不能转发 SDK
        # 请求中的同名头；该值可能来自 OPENAI_CUSTOM_HEADERS，而非冻结 route。
        controlled = httpx.Request(
            method=request.method,
            url=request.url,
            headers=headers,
            content=content,
        )
        try:
            response = await self._inner.handle_async_request(controlled)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise _ControlledTransportSignal(
                "model.provider_failed",
                completion_observed=False,
                side_effect_state="not_started",
            ) from None
        except httpx.RequestError:
            # read/write/protocol/pool 等异常都无法证明请求未离开进程。把安全的
            # OpenAIError 子类直接送回 adapter，避免 SDK/Pydantic AI 改写后丢失
            # unknown 语义，并禁止上层自动 retry/fallback 或释放预算。
            raise _ControlledTransportSignal(
                "model.provider_side_effect_unknown",
                completion_observed=None,
                side_effect_state="unknown",
            ) from None
        if response.status_code < 300:
            return response
        body = await response.aread()
        completion_observed = self._completion_observed(response=response, body=body)
        retry_after_ms = self._retry_after_ms(response)
        await response.aclose()
        raise _ControlledTransportSignal(
            "model.provider_failed",
            status_code=response.status_code,
            retry_after_ms=retry_after_ms,
            completion_observed=completion_observed,
            # 收到 HTTP response 已证明请求离开本进程；可信 false 只授权 retry，
            # 不能把已经发生的 attempt 改写成零副作用。
            side_effect_state="started",
        )

    def _completion_observed(self, *, response: httpx.Response, body: bytes) -> bool | None:
        """仅 exact 版本化原始 header 可证明未开始；业务响应证据具有否决权。"""

        if (
            self._completion_classifier_ref != "trusted_response_header_not_started"
            or self._completion_classifier_version != "v1"
        ):
            return None
        values = [
            value
            for name, value in response.headers.raw
            if name.lower() == b"x-agent-harness-completion-state"
        ]
        if len(values) != 1 or values[0].strip(b" \t") != b"not-started":
            return None
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict) and any(
            key in payload and payload[key] not in (None, [], {})
            for key in ("id", "usage", "choices", "output", "data")
        ):
            return None
        return False

    @staticmethod
    def _retry_after_ms(response: httpx.Response) -> int | None:
        """解析单值 Retry-After；重复、负数或非法日期不进入安全证据。"""

        values = [value for name, value in response.headers.raw if name.lower() == b"retry-after"]
        if len(values) != 1:
            return None
        try:
            raw = values[0].decode("ascii").strip()
        except UnicodeDecodeError:
            return None
        if raw.isdecimal():
            return int(raw) * 1000
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0, int((when - datetime.now(UTC)).total_seconds() * 1000))

    async def aclose(self) -> None:
        """幂等关闭底层 transport，不依赖垃圾回收释放连接。"""

        if self._closed:
            return
        self._closed = True
        self._api_key = ""
        await self._inner.aclose()


@dataclass
class ControlledOpenAIClientLease:
    """adapter 私有 SDK 资源集合；vendor 对象不得越过本模块。"""

    agent: _PydanticAgent
    openai_client: AsyncOpenAI
    http_client: httpx.AsyncClient
    _closed: bool = False

    async def aclose(self) -> None:
        """按 client owner 关系幂等关闭 SDK 与 HTTP 资源。"""

        if self._closed:
            return
        self._closed = True
        await self.openai_client.close()


async def _close_partial_client_resources(
    *,
    transport: ControlledOpenAITransport,
    http_client: httpx.AsyncClient | None,
    openai_client: AsyncOpenAI | None,
) -> None:
    """按构造进度关闭唯一 owner，避免失败路径泄漏或重复关闭同一 transport。

    ``AsyncOpenAI`` 构造成功后拥有传入的 ``http_client``；此前则由 factory
    临时持有 HTTP/transport。只有完整 lease 登记成功后，所有权才交给 lease。
    """

    if openai_client is not None:
        await openai_client.close()
    elif http_client is not None:
        await http_client.aclose()
    else:
        await transport.aclose()


class ControlledOpenAIClientFactory:
    """按冻结 route identity 延迟构造并缓存受控 OpenAI-compatible client lease。"""

    def __init__(
        self,
        *,
        model_settings: ModelSettings,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self._model_settings = model_settings
        self._transport_factory = transport_factory or httpx.AsyncHTTPTransport
        self._leases: dict[str, ControlledOpenAIClientLease] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self.client_construction_count = 0

    async def acquire(self, plan: ModelRoutePlan) -> ControlledOpenAIClientLease:
        """合法 plan 才能取得 lease；构造 client 本身不执行 DNS/HTTP。"""

        if self._closed:
            raise RuntimeError("controlled client factory is closed")
        if plan.provider != "openai-compatible" or plan.provider_kind != plan.provider:
            raise ValueError("controlled client requires openai-compatible route identity")
        cache_key = ":".join(
            [
                plan.deployment_id,
                plan.endpoint_policy_digest or "",
                plan.model_catalog_digest or "",
            ]
        )
        async with self._lock:
            if self._closed:
                raise RuntimeError("controlled model client factory is closed")
            existing = self._leases.get(cache_key)
            if existing is not None:
                return existing
            if (
                plan.canonical_base_url is None
                or plan.endpoint_origin is None
                or plan.credential_ref is None
            ):
                raise ValueError("route and typed client blueprint identity mismatch")
            canonical_base_url, endpoint_origin = normalize_model_endpoint(
                plan.canonical_base_url,
                allow_local_http=plan.canonical_base_url.startswith("http://"),
            )
            if (
                canonical_base_url != plan.canonical_base_url
                or endpoint_origin != plan.endpoint_origin
            ):
                raise ValueError("frozen route endpoint identity mismatch")
            try:
                credential = self._model_settings.credentials[plan.credential_ref]
            except KeyError:
                raise ValueError("frozen credential ref is no longer available") from None
            allowed_origins = {
                normalize_model_endpoint(
                    item,
                    allow_local_http=item.startswith("http://"),
                )[1]
                for item in credential.allowed_origins
            }
            if plan.endpoint_origin not in allowed_origins:
                raise ValueError("frozen credential cannot be forwarded to route origin")
            api_key = credential.value.get_secret_value()
            transport = ControlledOpenAITransport(
                inner=self._transport_factory(),
                canonical_base_url=plan.canonical_base_url,
                api_key=api_key,
                completion_classifier_ref=plan.completion_classifier_ref,
                completion_classifier_version=plan.completion_classifier_version,
            )
            http_client: httpx.AsyncClient | None = None
            openai_client: AsyncOpenAI | None = None
            try:
                http_client = httpx.AsyncClient(
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(
                        timeout=plan.total_timeout_ms / 1000,
                        connect=plan.connect_timeout_ms / 1000,
                        read=plan.read_timeout_ms / 1000,
                    ),
                )
                openai_client = AsyncOpenAI(
                    api_key=api_key,
                    admin_api_key="",
                    organization="",
                    project="",
                    webhook_secret="",
                    base_url=plan.canonical_base_url,
                    default_headers={},
                    max_retries=0,
                    timeout=plan.total_timeout_ms / 1000,
                    http_client=http_client,
                )
                provider = OpenAIProvider(openai_client=openai_client)
                model = OpenAIChatModel(cast(Any, plan.model), provider=provider)
                agent = cast(
                    _PydanticAgent,
                    Agent(model, retries=0, tools=(), instructions=None),
                )
            except BaseException:
                await _close_partial_client_resources(
                    transport=transport,
                    http_client=http_client,
                    openai_client=openai_client,
                )
                raise
            lease = ControlledOpenAIClientLease(
                agent=agent,
                openai_client=openai_client,
                http_client=http_client,
            )
            self._leases[cache_key] = lease
            self.client_construction_count += 1
            return lease

    async def aclose(self) -> None:
        """关闭全部已构造 lease；未构造时保持无副作用。"""

        if self._closed:
            return
        self._closed = True
        leases = list(self._leases.values())
        self._leases.clear()
        for lease in leases:
            await lease.aclose()
