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
    """应用工厂注入认证 verifier；None 表示 local/dev 默认身份。"""

    return None


def get_policy_engine() -> PolicyEngine | None:
    """应用工厂注入 PolicyEngine；测试可 override 该 dependency。"""

    return None


def get_input_guardrail() -> InputGuardrail | None:
    """应用工厂注入 run create 前的输入 guardrail。"""

    return None


def get_approval_service() -> ApprovalService:
    """返回必需的 ApprovalService，未注入时让错误显式暴露。"""

    raise RuntimeError("ApprovalService dependency is not configured")


def get_optional_approval_service() -> ApprovalService | None:
    """run create 可选接入 approval；缺失时只走 checkpoint seam。"""

    return None


async def current_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    verifier: Annotated[TokenVerifier | None, Depends(get_auth_verifier)],
) -> IdentityContext:
    """把 HTTP Bearer 凭据转换成稳定 IdentityContext。"""

    if verifier is None:
        return IdentityContext.local_default()
    if credentials is None:
        raise AuthError("missing bearer token", code="auth.missing_credentials")
    identity = await verifier.verify(credentials.credentials)
    if identity is None:
        raise AuthError("invalid bearer token")
    return identity
