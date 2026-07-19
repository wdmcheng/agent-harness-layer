"""策略决策与输入 guardrail 的业务 seam。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol, cast

import yaml
from pydantic import Field

from agent_harness.audit import AuditService
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.identity import IdentityContext
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage


class PolicyDeniedError(RuntimeError):
    """PolicyEngine 明确 deny 时给 API 层转换成 403。"""

    def __init__(self, message: str, *, code: str = "policy.denied") -> None:
        super().__init__(message)
        self.code = code
        self.status_code = 403


class PolicyCheck(HarnessDTO):
    """一次策略检查的稳定输入，不携带执行动作本身。"""

    actor: IdentityContext
    resource: str
    action: str
    context: dict[str, Any] = Field(default_factory=dict)


class PolicyApprovalRequirement(HarnessDTO):
    """require_approval 决策返回给调用方的审批摘要。"""

    action: str
    resource: str
    reason: str


class PolicyEvaluation(HarnessDTO):
    """PolicyEngine 输出的三态决策和审计元数据。"""

    decision: str
    reason: str
    actor: IdentityContext
    action: str
    resource: str
    approval: PolicyApprovalRequirement | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyProvider(Protocol):
    """策略来源的最小协议；provider 只判断，不执行动作。"""

    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation: ...


class YamlPolicyProvider:
    """Profile YAML 映射后的内存策略 provider。"""

    def __init__(
        self,
        *,
        require_approval_actions: Iterable[str] = (),
        deny_actions: Iterable[str] = (),
    ) -> None:
        self._require_approval_actions = set(require_approval_actions)
        self._deny_actions = set(deny_actions)

    @classmethod
    def default(cls) -> YamlPolicyProvider:
        return cls(
            require_approval_actions={
                "shell.execute",
                "file.delete",
                "file.bulk_write",
                "external.network",
                "mcp.connect",
                "message.send",
                "ticket.create",
                "email.send",
                "workspace.write_outside",
                "dataset.write_approved",
                "policy.modify",
                "policy.update",
                "model.over_budget",
                "input.prompt_injection",
            }
        )

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        fallback_require_approval_actions: Iterable[str] = (),
        fallback_deny_actions: Iterable[str] = (),
    ) -> YamlPolicyProvider:
        if not path.exists():
            return cls(
                require_approval_actions=fallback_require_approval_actions,
                deny_actions=fallback_deny_actions,
            )
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            return cls(
                require_approval_actions=fallback_require_approval_actions,
                deny_actions=fallback_deny_actions,
            )
        raw = cast(dict[str, object], loaded)
        require_actions = raw.get("require_approval_actions")
        deny_actions = raw.get("deny_actions")
        return cls(
            require_approval_actions=_string_list(
                require_actions,
                fallback=fallback_require_approval_actions,
            ),
            deny_actions=_string_list(deny_actions, fallback=fallback_deny_actions),
        )

    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation:
        """按 YAML 默认规则返回 allow、deny 或 require_approval。"""

        if check.action in self._deny_actions:
            return _decision(
                check,
                GuardrailDecisionStatus.DENY.value,
                "action denied by policy",
                metadata={"matched_rules": ["default-deny-actions"]},
            )
        if check.action in self._require_approval_actions:
            return _decision(
                check,
                GuardrailDecisionStatus.REQUIRE_APPROVAL.value,
                "action requires approval",
                approval=PolicyApprovalRequirement(
                    action=check.action,
                    resource=check.resource,
                    reason="action requires approval",
                ),
                metadata={"matched_rules": ["default-dangerous-actions"]},
            )
        if "*" in check.actor.permissions or check.action in check.actor.permissions:
            return _decision(
                check,
                GuardrailDecisionStatus.ALLOW.value,
                "permission matched",
                metadata={"matched_rules": ["identity-permission"]},
            )
        return _decision(
            check,
            GuardrailDecisionStatus.DENY.value,
            "permission missing",
            metadata={"matched_rules": ["identity-permission"]},
        )


class DatabasePolicyProvider:
    """从 policy_rules 表读取规则的 provider，供 service profile 替换 YAML。"""

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        self._storage = storage

    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation:
        async with self._storage.uow() as uow:
            rules = await uow.policy_rules.list_for_tenant(check.actor.tenant_id)
        for rule in rules:
            if rule.action == check.action:
                approval = None
                if rule.decision == GuardrailDecisionStatus.REQUIRE_APPROVAL.value:
                    approval = PolicyApprovalRequirement(
                        action=check.action,
                        resource=check.resource,
                        reason=str(rule.payload.get("reason") or "action requires approval"),
                    )
                return _decision(
                    check,
                    rule.decision,
                    str(rule.payload.get("reason") or f"matched policy rule {rule.name}"),
                    approval=approval,
                    metadata={
                        "matched_rules": [rule.name],
                        "rule_id": rule.id,
                        **rule.payload,
                    },
                )
        return await YamlPolicyProvider.default().evaluate(check)


class PolicyEngine:
    """统一封装 provider、审计和未来事件发布的策略入口。"""

    def __init__(
        self,
        *,
        provider: PolicyProvider,
        audit: AuditService | None = None,
    ) -> None:
        self._provider = provider
        self._audit = audit

    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation:
        result = await self._provider.evaluate(check)
        if self._audit is not None:
            audit_record = await self._audit.record(
                actor=check.actor,
                action="policy.decision",
                resource=check.resource,
                payload=result.to_payload(),
            )
            return result.model_copy(
                update={"metadata": {**result.metadata, "audit_ref": audit_record.id}}
            )
        return result

    async def require_allowed(self, check: PolicyCheck) -> PolicyEvaluation:
        result = await self.evaluate(check)
        if result.decision == GuardrailDecisionStatus.DENY.value:
            raise PolicyDeniedError(result.reason)
        return result

    async def require_allowed_readonly(self, check: PolicyCheck) -> PolicyEvaluation:
        """只接受显式 allow，但不把纯读取写成 policy audit outcome。

        只读入口没有 approval continuation，因而 ``require_approval`` 与 ``deny``
        都必须 fail closed，不能把尚待人工批准的 internal visibility 当成授权。
        """

        result = await self._provider.evaluate(check)
        if result.decision != GuardrailDecisionStatus.ALLOW.value:
            raise PolicyDeniedError(result.reason)
        return result


class InputGuardrail:
    """run 创建前检查用户输入中明显的 prompt-injection 风险。

    当前入口只覆盖 API/CLI 用户输入；tool、MCP 和 retrieval output 的信任传播
    由对应 adapter 接入，但必须复用同一 policy/audit seam。
    """

    _PATTERNS = (
        "ignore previous instructions",
        "reveal the system prompt",
        "system prompt",
        "developer message",
    )

    def __init__(self, *, policy: PolicyEngine, audit: AuditService | None = None) -> None:
        self._policy = policy
        self._audit = audit

    async def check(
        self,
        *,
        actor: IdentityContext,
        agent_id: str,
        input: dict[str, Any],
    ) -> PolicyEvaluation:
        text = str(input).lower()
        detected = [pattern for pattern in self._PATTERNS if pattern in text]
        if not detected:
            # 未命中风险词时仍返回可序列化 decision，方便 audit 和测试断言统一形状。
            result = _decision(
                PolicyCheck(
                    actor=actor,
                    resource=f"agent:{agent_id}:input",
                    action="input.guardrail",
                    context={"detected": []},
                ),
                GuardrailDecisionStatus.ALLOW.value,
                "input guardrail passed",
            )
        else:
            result = await self._policy.evaluate(
                PolicyCheck(
                    actor=actor,
                    resource=f"agent:{agent_id}:input",
                    action="input.prompt_injection",
                    context={
                        "agent_id": agent_id,
                        "detected": detected,
                        "input": redact_secrets(input),
                    },
                )
            )
        if self._audit is not None:
            await self._audit.record(
                actor=actor,
                action="input.guardrail.checked",
                resource=f"agent:{agent_id}:input",
                payload=result.to_payload(),
            )
        return result


def _decision(
    check: PolicyCheck,
    decision: str,
    reason: str,
    *,
    approval: PolicyApprovalRequirement | None = None,
    metadata: dict[str, Any] | None = None,
) -> PolicyEvaluation:
    return PolicyEvaluation(
        decision=decision,
        reason=reason,
        actor=check.actor,
        action=check.action,
        resource=check.resource,
        approval=approval,
        metadata=redact_secrets({**(metadata or {}), "context": check.context}),
    )


def _string_list(value: object, *, fallback: Iterable[str]) -> list[str]:
    if not isinstance(value, list):
        return list(fallback)
    items = cast(list[object], value)
    return [item for item in items if isinstance(item, str)]
