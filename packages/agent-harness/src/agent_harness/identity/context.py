"""Tenant, user, session, and permission context contracts."""

from __future__ import annotations

from typing import Self

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO


class IdentityContext(HarnessDTO):
    """Identity values propagated into runs, traces, evals, and policy checks."""

    tenant_id: str = "default"
    user_id: str = "local-user"
    session_id: str
    roles: list[str] = Field(default_factory=lambda: ["admin"])
    permissions: list[str] = Field(default_factory=lambda: ["*"])
    auth_method: str = "local"

    @classmethod
    def local_default(cls, session_id: str = "local-session") -> Self:
        return cls(session_id=session_id)


class PermissionContext(HarnessDTO):
    """Policy input derived from identity without binding to auth backends."""

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
