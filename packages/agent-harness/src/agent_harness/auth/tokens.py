"""API key / bearer token 到 IdentityContext 的认证 seam。"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Protocol

from agent_harness.identity import IdentityContext
from agent_harness.storage import SQLAlchemyStorage


class AuthError(RuntimeError):
    """认证失败时给 API handler 使用的结构化错误。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "auth.invalid_token",
        status_code: int = 401,
    ) -> None:
        """保存稳定错误码和 HTTP 状态，供 API 层生成一致的认证错误信封。"""
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class TokenVerifier(Protocol):
    """认证 provider 只暴露 token -> identity 的最小契约。"""

    async def verify(self, token: str) -> IdentityContext | None:
        """验证凭据并返回其绑定身份；未知、禁用或无效 token 一律返回空值。"""
        ...


def hash_token(token: str) -> str:
    """API key 持久化只存 hash，避免明文 token 进入数据库。"""

    return sha256(token.encode()).hexdigest()


class StaticTokenVerifier:
    """测试和本地 profile 使用的确定性 bearer token verifier。"""

    def __init__(self, identities: Mapping[str, IdentityContext]) -> None:
        """复制本地 token 映射，避免调用方后续改动配置影响已装配 verifier。"""
        self._identities = dict(identities)

    async def verify(self, token: str) -> IdentityContext | None:
        """按精确 bearer token 查找本地测试身份，不做前缀或大小写宽松匹配。"""
        return self._identities.get(token)


class ApiKeyVerifier:
    """DB-backed API key verifier；service profile 后续可由管理 CLI 写入 key。"""

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        """绑定认证凭据仓储；明文 token 不会保存到对象状态或传给数据库。"""
        self._storage = storage

    async def verify(self, token: str) -> IdentityContext | None:
        """查询 token 哈希并构造身份；禁用记录与不存在记录统一不泄露原因。

        API key 记录 ID 复用为 session ID，以保持会话外键形状稳定；认证方法标记
        仍区分凭据来源，供审计和策略层按身份类型判断。
        """
        async with self._storage.uow() as uow:
            record = await uow.api_keys.get_by_hash(hash_token(token))
        if record is None or record.disabled:
            return None
        return IdentityContext(
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            # ApiKey record id 本身已是稳定 UUID；直接复用可保持 session FK 的
            # VARCHAR(36) 合同，auth_method 已足以标识身份来源。
            session_id=record.id,
            roles=record.roles,
            permissions=record.permissions,
            auth_method="api-key",
        )
