"""认证凭据与数据库策略规则仓储，隔离 ORM 模型和明文凭据边界。"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from pydantic import Field
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.models import ApiKeyModel, PolicyRuleModel


class ApiKeyCreate(HarnessDTO):
    """创建 API key 记录时的脱敏输入。"""

    tenant_id: str
    user_id: str
    name: str
    token_hash: str
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    disabled: bool = False


class ApiKeyRecord(ApiKeyCreate):
    """已持久化的 API key 记录，只包含 token hash。"""

    id: str


class PolicyRuleCreate(HarnessDTO):
    """DB policy provider 的规则写入 DTO。"""

    tenant_id: str
    name: str
    action: str
    decision: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PolicyRuleRecord(PolicyRuleCreate):
    """已持久化的策略规则。"""

    id: str


def _api_key_record(model: ApiKeyModel) -> ApiKeyRecord:
    """将 API key ORM 模型投影为仅含 hash 的 DTO，永不恢复或暴露明文 token。"""
    return ApiKeyRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        name=model.name,
        token_hash=model.token_hash,
        roles=model.roles_json,
        permissions=model.permissions_json,
        disabled=model.disabled,
    )


def _policy_rule_record(model: PolicyRuleModel) -> PolicyRuleRecord:
    """将策略规则 ORM 模型投影为 DTO，供 Provider 在 session 外安全读取。"""
    return PolicyRuleRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        name=model.name,
        action=model.action,
        decision=model.decision,
        payload=model.payload_json,
    )


class ApiKeyRepository:
    """API key verifier 使用的 token hash 查询 repository。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前工作单元的 session；创建和删除的提交时机由上层统一控制。"""
        self._session = session

    async def create(self, data: ApiKeyCreate) -> ApiKeyRecord:
        """写入已 hash 的 API key；repository 不接收明文 token。"""

        model = ApiKeyModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            user_id=data.user_id,
            name=data.name,
            token_hash=data.token_hash,
            roles_json=data.roles,
            permissions_json=data.permissions,
            disabled=data.disabled,
        )
        self._session.add(model)
        await self._session.flush()
        return _api_key_record(model)

    async def get_by_hash(self, token_hash: str) -> ApiKeyRecord | None:
        """按精确 hash 查询凭据，不接受明文 token 或模糊匹配，避免认证旁路。"""
        result = await self._session.scalars(
            select(ApiKeyModel).where(ApiKeyModel.token_hash == token_hash)
        )
        model = result.first()
        return None if model is None else _api_key_record(model)

    async def delete_by_hash(self, token_hash: str) -> bool:
        """删除精确匹配的临时 credential，不接收或记录明文 token。"""

        result = cast(
            CursorResult[Any],
            await self._session.execute(
                delete(ApiKeyModel).where(ApiKeyModel.token_hash == token_hash)
            ),
        )
        return result.rowcount == 1


class PolicyRuleRepository:
    """DB-backed PolicyProvider 使用的规则 repository。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前工作单元 session，使策略写入能与其他配置更新原子提交。"""
        self._session = session

    async def create(self, data: PolicyRuleCreate) -> PolicyRuleRecord:
        """写入 DB policy provider 可读取的单条规则。"""

        model = PolicyRuleModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            name=data.name,
            action=data.action,
            decision=data.decision,
            payload_json=data.payload,
        )
        self._session.add(model)
        await self._session.flush()
        return _policy_rule_record(model)

    async def list_for_tenant(self, tenant_id: str) -> list[PolicyRuleRecord]:
        """列出租户内全部策略规则；不跨租户聚合，排序语义由 Provider 明确定义。"""
        result = await self._session.scalars(
            select(PolicyRuleModel).where(PolicyRuleModel.tenant_id == tenant_id)
        )
        return [_policy_rule_record(model) for model in result.all()]


__all__ = [
    "ApiKeyCreate",
    "ApiKeyRecord",
    "ApiKeyRepository",
    "PolicyRuleCreate",
    "PolicyRuleRecord",
    "PolicyRuleRepository",
]
