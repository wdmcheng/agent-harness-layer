"""FastAPI route 共用的认证、策略和 approval dependencies。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agent_harness.approvals import ApprovalService
from agent_harness.auth import AuthError, TokenVerifier
from agent_harness.identity import IdentityContext
from agent_harness.policy import InputGuardrail, PolicyEngine

bearer_scheme = HTTPBearer(auto_error=False)


def get_auth_verifier() -> TokenVerifier | None:
    return None


def get_policy_engine() -> PolicyEngine | None:
    return None


def get_input_guardrail() -> InputGuardrail | None:
    return None


def get_approval_service() -> ApprovalService:
    raise RuntimeError("ApprovalService dependency is not configured")


def get_optional_approval_service() -> ApprovalService | None:
    return None


async def current_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    verifier: Annotated[TokenVerifier | None, Depends(get_auth_verifier)],
) -> IdentityContext:
    if verifier is None:
        return IdentityContext.local_default()
    if credentials is None:
        raise AuthError("missing bearer token", code="auth.missing_credentials")
    identity = await verifier.verify(credentials.credentials)
    if identity is None:
        raise AuthError("invalid bearer token")
    return identity
