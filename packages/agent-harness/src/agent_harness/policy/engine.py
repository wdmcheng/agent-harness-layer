"""策略决策与输入 guardrail 的业务 seam。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import yaml
from pydantic import Field

from agent_harness.audit import AuditService
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.identity import IdentityContext

if TYPE_CHECKING:
    from agent_harness.runtime._continuation_context import RunInputProvenance
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage


class PolicyDeniedError(RuntimeError):
    """策略明确拒绝时交给 API 层转换为 403 的稳定异常。"""

    def __init__(self, message: str, *, code: str = "policy.denied") -> None:
        """保留面向调用方的简短原因和稳定错误码，不携带规则私有细节。"""

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

    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation:
        """根据稳定检查坐标返回允许、拒绝或要求审批的决策，不产生业务副作用。"""

        ...


class YamlPolicyProvider:
    """Profile YAML 映射后的内存策略来源。

    该实现适合本地 profile 和没有租户规则时的保守兜底；它只保存动作集合，不把 YAML
    文件路径或原始内容带入任何决策元数据。
    """

    def __init__(
        self,
        *,
        require_approval_actions: Iterable[str] = (),
        deny_actions: Iterable[str] = (),
    ) -> None:
        """冻结需要审批与明确拒绝的动作集合，供每次检查以常数时间匹配。"""

        self._require_approval_actions = set(require_approval_actions)
        self._deny_actions = set(deny_actions)

    @classmethod
    def default(cls) -> YamlPolicyProvider:
        """返回内置保守策略，把高影响动作转入审批而非直接放行。"""

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
        """从可选 YAML 读取动作集合；缺失、空文档或非映射根节点时回退默认集合。

        配置加载失败的兼容策略只适用于结构不匹配的可选 profile；YAML 语法或文件读取
        异常仍交给调用方处理，不能悄悄将已损坏的安全配置当作空策略。
        """

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
        """按拒绝、审批、身份权限的固定优先级返回决策。

        明确拒绝始终压过审批和通配权限，审批要求压过身份权限；只有不命中前两者时，
        才允许显式动作权限或 ``*`` 权限放行。
        """

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
    """从 policy_rules 表读取租户规则的来源，供 service profile 替换 YAML。

    租户规则只负责其显式命中的动作；未命中时仍回落到内置保守策略，避免空规则表意外
    把本应进入审批的危险动作放行。
    """

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        """保存 storage 边界；每次判断自行打开短事务读取当前租户规则。"""

        self._storage = storage

    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation:
        """匹配当前租户的首条同动作规则，否则委托内置安全兜底策略。"""

        async with self._storage.uow() as uow:
            rules = await uow.policy_rules.list_for_tenant(check.actor.tenant_id)
        for rule in rules:
            if rule.action == check.action:
                approval = None
                if rule.decision == GuardrailDecisionStatus.REQUIRE_APPROVAL.value:
                    # 审批摘要从耐久规则生成，避免调用方根据私有 payload 自行拼装。
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
        # 规则表无命中不等于默认允许；回退后仍遵循内置高影响动作保护。
        return await YamlPolicyProvider.default().evaluate(check)


class PolicyEngine:
    """统一封装策略来源、审计和未来事件发布的业务入口。"""

    def __init__(
        self,
        *,
        provider: PolicyProvider,
        audit: AuditService | None = None,
    ) -> None:
        """绑定一个只负责决策的来源，并可选地绑定独立审计服务。"""

        self._provider = provider
        self._audit = audit

    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation:
        """执行策略判断，并在配置审计时把最终决策追加为耐久审计事实。"""

        result = await self._provider.evaluate(check)
        if self._audit is not None:
            audit_record = await self._audit.record(
                actor=check.actor,
                action="policy.decision",
                resource=check.resource,
                payload=result.to_payload(),
            )
            # 不修改 provider 返回对象，复制后附加审计引用以保留其原始决策形态。
            return result.model_copy(
                update={"metadata": {**result.metadata, "audit_ref": audit_record.id}}
            )
        return result

    async def require_allowed(self, check: PolicyCheck) -> PolicyEvaluation:
        """拒绝明确 deny；审批要求保留给调用方创建 continuation 后续处理。"""

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
        """绑定策略和可选审计服务；规则命中后只通过公共策略入口决定后续动作。"""

        self._policy = policy
        self._audit = audit

    async def check(
        self,
        *,
        actor: IdentityContext,
        agent_id: str,
        input: dict[str, Any],
        provenance: RunInputProvenance | None = None,
    ) -> PolicyEvaluation:
        """检查输入中的已知高风险提示形态，并记录脱敏后的判断结果。

        未命中时构造确定的 allow 结果而不虚构策略命中；命中时则把检测项与脱敏输入
        交给 ``input.prompt_injection`` 策略，让部署配置决定拒绝还是要求人工审批。
        """

        text = str(input).lower()
        provenance_context = {"source": provenance.source} if provenance is not None else {}
        detected = [pattern for pattern in self._PATTERNS if pattern in text]
        if not detected:
            # 未命中风险词时仍返回可序列化 decision，方便 audit 和测试断言统一形状。
            result = _decision(
                PolicyCheck(
                    actor=actor,
                    resource=f"agent:{agent_id}:input",
                    action="input.guardrail",
                    context={"detected": [], **provenance_context},
                ),
                GuardrailDecisionStatus.ALLOW.value,
                "input guardrail passed",
            )
        else:
            # 原始输入可能含秘密，只将 redaction 后的内容作为策略上下文与审计事实。
            result = await self._policy.evaluate(
                PolicyCheck(
                    actor=actor,
                    resource=f"agent:{agent_id}:input",
                    action="input.prompt_injection",
                    context={
                        "agent_id": agent_id,
                        "detected": detected,
                        "input": redact_secrets(input),
                        **provenance_context,
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
    """从检查输入构造统一 DTO，并在跨边界前脱敏所有调用方元数据与上下文。"""

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
    """仅接受 YAML 列表中的字符串项；其他形态完整回退，避免部分错误配置半生效。"""

    if not isinstance(value, list):
        return list(fallback)
    items = cast(list[object], value)
    return [item for item in items if isinstance(item, str)]
