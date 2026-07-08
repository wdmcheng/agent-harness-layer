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
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class TokenVerifier(Protocol):
    """认证 provider 只暴露 token -> identity 的最小契约。"""

    async def verify(self, token: str) -> IdentityContext | None: ...


def hash_token(token: str) -> str:
    """API key 持久化只存 hash，避免明文 token 进入数据库。"""

    return sha256(token.encode()).hexdigest()


class StaticTokenVerifier:
    """测试和本地 profile 使用的确定性 bearer token verifier。"""

    def __init__(self, identities: Mapping[str, IdentityContext]) -> None:
        self._identities = dict(identities)

    async def verify(self, token: str) -> IdentityContext | None:
        return self._identities.get(token)


class ApiKeyVerifier:
    """DB-backed API key verifier；service profile 后续可由管理 CLI 写入 key。"""

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        self._storage = storage

    async def verify(self, token: str) -> IdentityContext | None:
        async with self._storage.uow() as uow:
            record = await uow.api_keys.get_by_hash(hash_token(token))
        if record is None or record.disabled:
            return None
        return IdentityContext(
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            session_id=f"api-key-{record.id}",
            roles=record.roles,
            permissions=record.permissions,
            auth_method="api-key",
        )
