"""共享 parent budget ledger 的类型化输入、身份与错误边界。"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import Field, field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.model_route_chain_state import ModelRouteChainState

BudgetState = Literal["active", "needs_review", "terminal"]
OperationState = Literal["reserved", "settled", "released", "needs_review"]
# 该状态描述外部副作用相对账本提交的持久化位置，用于恢复时决定可否重放，不能由调用方猜测。
SideEffectState = Literal["not_started", "started", "result_committed"]


class BudgetOperationConflict(RuntimeError):
    """稳定操作键已被其他不可变身份占用时抛出的 fail-closed 错误。

    重试只能复用同一份语义请求；若同一个稳定键对应不同 identity，继续执行可能把
    另一笔调用的结果或预算归属错误地带入当前请求，因此必须拒绝而非覆盖。
    """

    code = "budget.operation_conflict"

    def __init__(self) -> None:
        """固定对外错误码，避免把冲突记录的内部字段暴露给调用方。"""

        super().__init__(self.code)


class BudgetReservationRejected(RuntimeError):
    """预算预留不成立时使用的脱敏拒绝错误。

    外部只收到稳定错误码和有限原因，不能藉此推断余额、额度、单价或预算拥有者。
    """

    code = "budget.reservation_rejected"

    def __init__(self, *, reason: str = "balance_insufficient") -> None:
        """保存供内部映射的拒绝原因，同时保持异常文本为稳定公开错误码。"""

        super().__init__(self.code)
        self.reason = reason


def _canonical_bytes(value: object) -> bytes:
    """把语义输入编码为可复算且拒绝非有限数值的 UTF-8 canonical JSON。

    identity hash 依赖字节级一致性：字段顺序、空白和 Decimal 的展示形式不能随
    Python 版本或调用路径漂移。这里故意不接受 NaN/Infinity，避免同一语义请求在
    不同序列化器中得到不稳定摘要。
    """

    def jsonable(item: object) -> object:
        """递归收敛为 JSON 可表示的稳定结构，而不改变调用方传入对象。"""

        if isinstance(item, Decimal):
            # Decimal 先转成十进制文本，避免 JSON float 转换引入二进制精度差异。
            return format(item, "f")
        if isinstance(item, Mapping):
            mapping = cast(Mapping[object, object], item)
            # 键统一成文本并由外层 sort_keys 排序，保证映射的插入顺序不影响摘要。
            return {str(key): jsonable(child) for key, child in mapping.items()}
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            sequence = cast(Sequence[object], item)
            # 序列顺序本身属于请求语义，递归规范化元素但不重排。
            return [jsonable(child) for child in sequence]
        return item

    return json.dumps(
        jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _non_negative_decimal(value: Decimal | None) -> Decimal | None:
    """校验金额上界并归一化十进制表示，保留未启用成本维度时的 ``None``。"""

    if value is None:
        return None
    if not value.is_finite() or value < 0:
        raise ValueError("cost value must be finite and non-negative")
    return Decimal(format(value.normalize(), "f"))


class OperationIdentity(HarnessDTO):
    """与余额、预留记录和首次执行结果隔离的版本化操作身份。

    此对象只保存决定“是否同一语义操作”的不可变事实。账本余额、重放结果和当前
    配置等可变数据不得进入 hash，否则重试会被误判为另一笔操作。
    """

    identity_schema_version: str = "budget-operation-v1"
    ownership_kind: Literal["direct", "allocation", "delegation"]
    run_id: str
    agent_id: str
    delegation_claim_id: str | None = None
    source_agent_id: str | None = None
    target_agent_id: str | None = None
    usage_kind: Literal["model", "embedding", "delegation"]
    operation_slot: str
    request_fingerprint: str
    fingerprint_key_version: str
    tree_snapshot_id: str
    agent_sub_snapshot_id: str
    provider: str | None
    model: str | None
    price_source_ref: str | None = None
    price_source_version: str | None = None
    cache_key_digest: str | None = None
    target_route_catalog_digest: str | None = None
    cost_enabled: bool
    trusted_token_bound: int
    trusted_cost_bound: Decimal | None = None
    route_chain_digest: str | None = None
    route_candidate_count: int | None = None
    identity_hash: str

    def to_payload(self) -> dict[str, Any]:
        """导出身份载荷，并保留固定为 ``null`` 的封闭字段参与稳定比对。"""

        exclude = (
            {"route_chain_digest", "route_candidate_count"}
            if self.identity_schema_version != "budget-operation-v2"
            else None
        )
        return self.model_dump(mode="json", exclude_none=False, exclude=exclude)

    @field_validator("trusted_token_bound")
    @classmethod
    def validate_token_bound(cls, value: int) -> int:
        """拒绝布尔值、负数等会破坏预留上界比较的 token 输入。"""

        if isinstance(value, bool) or value < 0:
            raise ValueError("trusted_token_bound must be non-negative")
        return value

    @field_validator("trusted_cost_bound")
    @classmethod
    def validate_cost_bound(cls, value: Decimal | None) -> Decimal | None:
        """复用金额归一化规则，确保 identity hash 不受等价 Decimal 表示影响。"""

        return _non_negative_decimal(value)

    @field_validator("route_candidate_count")
    @classmethod
    def validate_route_candidate_count(cls, value: int | None) -> int | None:
        """显式链候选数只接受 1～8 的非 bool 整数。"""

        if value is not None and (isinstance(value, bool) or not 1 <= value <= 8):
            raise ValueError("route_candidate_count must be an integer from 1 to 8")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> OperationIdentity:
        """按所有权类型锁定字段组合，并在接受对象前验证其自带摘要。

        此校验故意发生在 DTO 构造边界：持久化层和重放路径都可依赖已经封闭的身份，
        而不必各自重写一套容易分叉的字段约束。
        """

        if self.ownership_kind == "delegation":
            # 委派不携带具体模型价格字段；它以目标路由目录作为可审计的执行依据。
            if (
                self.identity_schema_version != "budget-delegation-v1"
                or not self.delegation_claim_id
                or self.usage_kind != "delegation"
                or not self.source_agent_id
                or not self.target_agent_id
                or self.agent_id != self.source_agent_id
                or not self.target_route_catalog_digest
                or self.provider is not None
                or self.model is not None
                or self.price_source_ref is not None
                or self.price_source_version is not None
                or self.cache_key_digest is not None
                or self.route_chain_digest is not None
                or self.route_candidate_count is not None
            ):
                raise ValueError("delegation identity shape is invalid")
        else:
            # 直接调用和额度分配共享模型身份，但禁止混入委派路由字段。
            if self.identity_schema_version not in {
                "budget-operation-v1",
                "budget-operation-v2",
            }:
                raise ValueError("usage identity schema version is invalid")
            if self.usage_kind not in {"model", "embedding"}:
                raise ValueError("usage identity kind is invalid")
            if not self.provider or not self.model:
                raise ValueError("usage identity requires provider and model")
            if any(
                value is not None
                for value in (
                    self.source_agent_id,
                    self.target_agent_id,
                    self.target_route_catalog_digest,
                )
            ):
                raise ValueError("usage identity forbids delegation catalog fields")
            if self.ownership_kind == "allocation" and not self.delegation_claim_id:
                raise ValueError("allocation identity requires delegation_claim_id")
            if self.ownership_kind == "direct" and self.delegation_claim_id is not None:
                raise ValueError("direct identity forbids delegation_claim_id")
            route_fields_set = bool(
                {"route_chain_digest", "route_candidate_count"} & self.model_fields_set
            )
            if self.identity_schema_version == "budget-operation-v1":
                if (
                    route_fields_set
                    or self.route_chain_digest is not None
                    or self.route_candidate_count is not None
                ):
                    raise ValueError("budget-operation-v1 forbids route-chain fields")
            elif (
                self.usage_kind != "model"
                or not self.route_chain_digest
                or len(self.route_chain_digest) != 64
                or any(character not in "0123456789abcdef" for character in self.route_chain_digest)
                or self.route_candidate_count is None
                or not {
                    "route_chain_digest",
                    "route_candidate_count",
                }.issubset(self.model_fields_set)
            ):
                raise ValueError("budget-operation-v2 requires exact route-chain identity")
        # 成本开关与成本上界必须成对出现，避免“启用但无上界”的隐性无限预留。
        if self.cost_enabled != (self.trusted_cost_bound is not None):
            raise ValueError("cost-enabled identity requires exactly one trusted cost bound")
        expected = self._calculate_hash()
        if not hmac.compare_digest(self.identity_hash, expected):
            raise ValueError("identity_hash does not match canonical identity")
        return self

    def _hash_payload(self) -> dict[str, Any]:
        """返回唯一排除 ``identity_hash`` 的待摘要字段，防止自引用。"""

        exclude = {"identity_hash"}
        if self.identity_schema_version != "budget-operation-v2":
            exclude.update({"route_chain_digest", "route_candidate_count"})
        return self.model_dump(mode="json", exclude=exclude)

    def _calculate_hash(self) -> str:
        """按 canonical JSON 计算身份摘要，供构造和读取校验使用。"""

        return hashlib.sha256(_canonical_bytes(self._hash_payload())).hexdigest()

    def rehashed(self) -> OperationIdentity:
        """在受控字段调整后重新归一化金额并生成可再次校验的身份对象。"""

        exclude = {"identity_hash"}
        if self.identity_schema_version != "budget-operation-v2":
            exclude.update({"route_chain_digest", "route_candidate_count"})
        payload = self.model_dump(exclude=exclude)
        payload["trusted_cost_bound"] = _non_negative_decimal(self.trusted_cost_bound)
        payload["identity_hash"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
        return OperationIdentity.model_validate(payload)

    @classmethod
    def from_semantic_request(
        cls,
        *,
        tenant_id: str,
        fingerprint_key: bytes,
        fingerprint_key_version: str,
        ownership_kind: Literal["direct", "allocation"],
        run_id: str,
        agent_id: str,
        delegation_claim_id: str | None,
        usage_kind: Literal["model", "embedding"],
        operation_slot: str,
        semantic_request: object,
        tree_snapshot_id: str,
        agent_sub_snapshot_id: str,
        provider: str,
        model: str,
        price_source_ref: str | None,
        price_source_version: str | None,
        cache_key_digest: str | None,
        cost_enabled: bool,
        trusted_token_bound: int,
        trusted_cost_bound: Decimal | None,
        route_chain_digest: str | None = None,
        route_candidate_count: int | None = None,
    ) -> OperationIdentity:
        """从模型或 embedding 的语义请求构造不可变身份。

        运行时主密钥先派生 tenant 隔离密钥，再对 canonical 请求取 HMAC；数据库和
        事件记录只保存不可逆摘要，不能反推出请求正文或跨租户关联相同输入。
        """

        if not fingerprint_key:
            raise ValueError("fingerprint_key must not be empty")
        # 两层 HMAC 同时避免主密钥直接参与请求摘要，并把相同请求隔离到各租户内。
        tenant_key = hmac.new(fingerprint_key, tenant_id.encode("utf-8"), hashlib.sha256).digest()
        request_fingerprint = hmac.new(
            tenant_key,
            _canonical_bytes(semantic_request),
            hashlib.sha256,
        ).hexdigest()
        normalized_cost_bound = _non_negative_decimal(trusted_cost_bound)
        chain_mode = route_chain_digest is not None or route_candidate_count is not None
        if chain_mode and (route_chain_digest is None or route_candidate_count is None):
            raise ValueError("route-chain identity requires digest and candidate count")
        if chain_mode and usage_kind != "model":
            raise ValueError("only model usage may use budget-operation-v2")
        payload: dict[str, Any] = {
            "identity_schema_version": (
                "budget-operation-v2" if chain_mode else "budget-operation-v1"
            ),
            "ownership_kind": ownership_kind,
            "run_id": run_id,
            "agent_id": agent_id,
            "delegation_claim_id": delegation_claim_id,
            "source_agent_id": None,
            "target_agent_id": None,
            "usage_kind": usage_kind,
            "operation_slot": operation_slot,
            "request_fingerprint": request_fingerprint,
            "fingerprint_key_version": fingerprint_key_version,
            "tree_snapshot_id": tree_snapshot_id,
            "agent_sub_snapshot_id": agent_sub_snapshot_id,
            "provider": provider,
            "model": model,
            "price_source_ref": price_source_ref,
            "price_source_version": price_source_version,
            "cache_key_digest": cache_key_digest,
            "target_route_catalog_digest": None,
            "cost_enabled": cost_enabled,
            "trusted_token_bound": trusted_token_bound,
            "trusted_cost_bound": normalized_cost_bound,
        }
        if chain_mode:
            payload["route_chain_digest"] = route_chain_digest
            payload["route_candidate_count"] = route_candidate_count
        payload["identity_hash"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
        return cls.model_validate(payload)

    @classmethod
    def from_delegation_request(
        cls,
        *,
        tenant_id: str,
        fingerprint_key: bytes,
        fingerprint_key_version: str,
        canonical_request_bytes: bytes,
        parent_run_id: str,
        source_agent_id: str,
        target_agent_id: str,
        delegation_claim_id: str,
        operation_slot: str,
        tree_snapshot_id: str,
        target_sub_snapshot_id: str,
        target_route_catalog_digest: str,
        cost_enabled: bool,
        trusted_token_bound: int,
        trusted_cost_bound: Decimal | None,
    ) -> OperationIdentity:
        """从已规范化的委派请求构造独立的顶层预算身份。

        调用方提供的字节串必须来自委派请求的唯一规范化入口；此处不重新解释其结构，
        以免不同组件对同一请求生成不一致的摘要。
        """

        if not fingerprint_key:
            raise ValueError("fingerprint_key must not be empty")
        # 委派也使用 tenant 隔离摘要，但其身份字段保持模型与价格字段为空。
        tenant_key = hmac.new(fingerprint_key, tenant_id.encode("utf-8"), hashlib.sha256).digest()
        request_fingerprint = hmac.new(
            tenant_key,
            canonical_request_bytes,
            hashlib.sha256,
        ).hexdigest()
        normalized_cost_bound = _non_negative_decimal(trusted_cost_bound)
        payload: dict[str, Any] = {
            "identity_schema_version": "budget-delegation-v1",
            "ownership_kind": "delegation",
            "run_id": parent_run_id,
            "agent_id": source_agent_id,
            "delegation_claim_id": delegation_claim_id,
            "source_agent_id": source_agent_id,
            "target_agent_id": target_agent_id,
            "usage_kind": "delegation",
            "operation_slot": operation_slot,
            "request_fingerprint": request_fingerprint,
            "fingerprint_key_version": fingerprint_key_version,
            "tree_snapshot_id": tree_snapshot_id,
            "agent_sub_snapshot_id": target_sub_snapshot_id,
            "provider": None,
            "model": None,
            "price_source_ref": None,
            "price_source_version": None,
            "cache_key_digest": None,
            "target_route_catalog_digest": target_route_catalog_digest,
            "cost_enabled": cost_enabled,
            "trusted_token_bound": trusted_token_bound,
            "trusted_cost_bound": normalized_cost_bound,
        }
        payload["identity_hash"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
        return cls.model_validate(payload)


class LedgerCreate(HarnessDTO):
    """创建共享账本时一次性冻结的额度、版本和快照输入。

    账本创建后不能从当前配置回填这些字段；保留原始版本与快照可以让恢复和审计基于
    当时生效的规则，而不是被后续配置变更改写。
    """

    tenant_id: str
    budget_owner_run_id: str
    token_limit: int
    cost_limit: Decimal | None
    registry_version: str
    config_version: str
    catalog_version: str
    snapshot_id: str
    snapshot: dict[str, Any] = Field(default_factory=dict)

    @field_validator("token_limit")
    @classmethod
    def validate_token_limit(cls, value: int) -> int:
        """保证 token 总额度是非负整数，排除 Python ``bool`` 的整数子类陷阱。"""

        if isinstance(value, bool) or value < 0:
            raise ValueError("token_limit must be non-negative")
        return value

    @field_validator("cost_limit")
    @classmethod
    def validate_cost_limit(cls, value: Decimal | None) -> Decimal | None:
        """校验可选成本总额度，并统一其持久化前的十进制表现。"""

        return _non_negative_decimal(value)


class DirectBudgetClaim(HarnessDTO):
    """直属调用预留预算时写入存储层的完整请求。

    普通预留必须等于受信任身份中的上界；零影响记录只用于已有耐久结果的重放，不能
    伪装成一次无需预留的新调用。
    """

    tenant_id: str
    budget_owner_run_id: str
    usage_call_id: str
    identity: OperationIdentity
    token_reservation: int
    cost_reservation: Decimal | None
    zero_impact: bool = False
    result: dict[str, Any] | None = None
    route_chain_state: ModelRouteChainState | None = None

    @model_validator(mode="after")
    def validate_claim(self) -> DirectBudgetClaim:
        """验证直属归属、预留上界和零影响重放三项不能相互绕过的约束。"""

        if self.identity.ownership_kind != "direct":
            raise ValueError("direct claim requires direct identity")
        if self.identity.identity_schema_version == "budget-operation-v2":
            if (
                self.route_chain_state is None
                or self.route_chain_state.chain_id != self.identity.route_chain_digest
                or self.route_chain_state.usage_call_id != self.usage_call_id
                or self.token_reservation != self.route_chain_state.current_reservation.token_bound
                or self.cost_reservation
                != (
                    None
                    if self.route_chain_state.current_reservation.cost_bound is None
                    else Decimal(str(self.route_chain_state.current_reservation.cost_bound))
                )
            ):
                raise ValueError("direct route-chain claim state is invalid")
        elif self.route_chain_state is not None:
            raise ValueError("legacy direct claim forbids route-chain state")
        elif not self.zero_impact and self.token_reservation != self.identity.trusted_token_bound:
            raise ValueError("token reservation must equal trusted bound")
        elif not self.zero_impact and self.cost_reservation != self.identity.trusted_cost_bound:
            raise ValueError("cost reservation must equal trusted bound")
        if self.zero_impact and (
            self.token_reservation != 0
            or self.cost_reservation not in {None, Decimal("0")}
            or self.result is None
        ):
            # 没有耐久结果的零影响请求会跳过记账后仍触发外部调用，必须拒绝。
            raise ValueError("zero-impact direct claim requires zero bounds and durable result")
        return self


class AllocationBudgetClaim(HarnessDTO):
    """委派子调用从已获配额度中取用时写入存储层的完整请求。

    子调用仍需绑定到同一 delegation id 和可信上界；这避免错误使用另一委派的额度，
    或借零影响路径逃避耐久结果要求。
    """

    tenant_id: str
    budget_owner_run_id: str
    delegation_id: str
    usage_call_id: str
    identity: OperationIdentity
    token_reservation: int
    cost_reservation: Decimal | None
    zero_impact: bool = False
    result: dict[str, Any] | None = None
    route_chain_state: ModelRouteChainState | None = None

    @model_validator(mode="after")
    def validate_claim(self) -> AllocationBudgetClaim:
        """验证额度分配归属、委派绑定与零影响重放的封闭条件。"""

        if self.identity.ownership_kind != "allocation":
            raise ValueError("allocation requires allocation identity")
        if self.identity.delegation_claim_id != self.delegation_id:
            raise ValueError("allocation identity must bind delegation_id")
        if self.identity.identity_schema_version == "budget-operation-v2":
            if (
                self.route_chain_state is None
                or self.route_chain_state.chain_id != self.identity.route_chain_digest
                or self.route_chain_state.usage_call_id != self.usage_call_id
                or self.token_reservation != self.route_chain_state.current_reservation.token_bound
                or self.cost_reservation
                != (
                    None
                    if self.route_chain_state.current_reservation.cost_bound is None
                    else Decimal(str(self.route_chain_state.current_reservation.cost_bound))
                )
            ):
                raise ValueError("allocation route-chain claim state is invalid")
        elif self.route_chain_state is not None:
            raise ValueError("legacy allocation forbids route-chain state")
        elif not self.zero_impact and self.token_reservation != self.identity.trusted_token_bound:
            raise ValueError("token reservation must equal trusted bound")
        elif not self.zero_impact and self.cost_reservation != self.identity.trusted_cost_bound:
            raise ValueError("cost reservation must equal trusted bound")
        if self.zero_impact and (
            self.token_reservation != 0
            or self.cost_reservation not in {None, Decimal("0")}
            or self.result is None
        ):
            # 子调用同样只能在已有结果可重放时跳过额度影响。
            raise ValueError("zero-impact allocation requires zero bounds and durable result")
        return self


class LedgerRecord(HarnessDTO):
    """共享账本的只读快照，供调用方判断额度影响与乐观并发版本。"""

    tenant_id: str
    budget_owner_run_id: str
    token_limit: int
    cost_limit: Decimal | None
    token_impact: int
    cost_impact: Decimal
    state: BudgetState
    version: int
    snapshot_id: str


class ClaimRecord(HarnessDTO):
    """直属或顶层委派预留的持久化结果，包括副作用恢复位置和重放标记。"""

    id: str
    tenant_id: str
    budget_owner_run_id: str
    operation_kind: Literal["direct", "delegation"]
    usage_call_id: str | None
    delegation_id: str | None
    state: OperationState
    side_effect_state: SideEffectState
    token_impact: int
    cost_impact: Decimal
    result: dict[str, Any] | None
    route_chain_state: ModelRouteChainState | None = None
    replayed: bool = False


class AllocationRecord(HarnessDTO):
    """委派额度分配的持久化结果，包括子调用副作用恢复位置和重放标记。"""

    id: str
    tenant_id: str
    budget_owner_run_id: str
    delegation_id: str
    usage_call_id: str
    state: OperationState
    side_effect_state: SideEffectState
    token_impact: int
    cost_impact: Decimal
    result: dict[str, Any] | None
    route_chain_state: ModelRouteChainState | None = None
    replayed: bool = False


class BudgetOperationOwnership(HarnessDTO):
    """描述 usage 操作归属的最小稳定坐标，供回放种子脱离当前账本使用。"""

    kind: Literal["direct", "allocation"]
    budget_owner_run_id: str
    delegation_id: str | None = None


class BudgetOperationReplaySeed(HarnessDTO):
    """不依赖当前账本或快照的稳定 usage 回放种子。

    读取路径以此识别已有操作，不能根据已经变化的额度、配置或快照重新推导身份。
    """

    operation_kind: Literal["direct", "allocation"]
    ownership: BudgetOperationOwnership
    identity: OperationIdentity
    state: OperationState
    side_effect_state: SideEffectState
    result: dict[str, Any] | None
    route_chain_state: ModelRouteChainState | None = None


def validate_actual_usage(
    *,
    actual_tokens: int | None,
    actual_cost: Decimal | None,
    cost_status: str,
) -> None:
    """校验实际用量与成本状态的组合，避免结算路径接受含糊或不可比较的数据。

    即使某一额度维度未启用，已上报的 usage 仍必须合法；否则恢复、审计和后续配置
    变化会面对无法解释的耐久记录。
    """

    raw_tokens: Any = actual_tokens
    raw_cost: Any = actual_cost
    # ``bool`` 是 ``int`` 的子类，必须显式排除，避免 True 被当成一个 token。
    if raw_tokens is not None and (
        isinstance(raw_tokens, bool) or not isinstance(raw_tokens, int) or raw_tokens < 0
    ):
        raise ValueError("actual_tokens must be a non-negative integer")
    if raw_cost is not None:
        if not isinstance(raw_cost, Decimal):
            raise ValueError("actual_cost must be a Decimal or null")
        _non_negative_decimal(raw_cost)
    if cost_status not in {"reported", "estimated", "unavailable"}:
        raise ValueError("unsupported cost_status")
    # 缺失成本只能用 unavailable 表示，反之 unavailable 不得携带伪造数值。
    if (cost_status == "unavailable") != (actual_cost is None):
        raise ValueError("cost_usd/cost_status combination is invalid")
    if actual_cost is not None and not math.isfinite(float(actual_cost)):
        raise ValueError("actual_cost must be finite")


__all__ = [
    "AllocationBudgetClaim",
    "AllocationRecord",
    "BudgetOperationConflict",
    "BudgetOperationOwnership",
    "BudgetReservationRejected",
    "ClaimRecord",
    "DirectBudgetClaim",
    "LedgerCreate",
    "LedgerRecord",
    "OperationIdentity",
    "validate_actual_usage",
]
