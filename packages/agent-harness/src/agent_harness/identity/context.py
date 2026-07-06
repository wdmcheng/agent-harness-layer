"""租户、用户、会话和权限上下文契约。"""

from __future__ import annotations

from typing import Self

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO


class IdentityContext(HarnessDTO):
    """会传播到 run、trace、eval 和 policy check 的身份值。"""

    tenant_id: str = "default"
    user_id: str = "local-user"
    session_id: str
    roles: list[str] = Field(default_factory=lambda: ["admin"])
    permissions: list[str] = Field(default_factory=lambda: ["*"])
    auth_method: str = "local"

    @classmethod
    def local_default(cls, session_id: str = "local-session") -> Self:
        """返回显式单用户 local identity；这不是认证后端的结果。"""

        return cls(session_id=session_id)


class PermissionContext(HarnessDTO):
    """从 identity 派生的 policy 输入，不绑定具体认证实现。"""

    tenant_id: str
    user_id: str
    session_id: str
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    auth_method: str
    agent_id: str | None = None
    resource: str
    action: str

    @classmethod
    def from_identity(
        cls,
        identity: IdentityContext,
        *,
        resource: str,
        action: str,
        agent_id: str | None = None,
    ) -> Self:
        """从身份上下文复制 actor 字段，并补入本次资源和动作。"""

        return cls(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            session_id=identity.session_id,
            roles=list(identity.roles),
            permissions=list(identity.permissions),
            auth_method=identity.auth_method,
            agent_id=agent_id,
            resource=resource,
            action=action,
        )
