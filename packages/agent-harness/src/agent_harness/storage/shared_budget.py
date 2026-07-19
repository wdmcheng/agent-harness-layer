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

BudgetState = Literal["active", "needs_review", "terminal"]
OperationState = Literal["reserved", "settled", "released", "needs_review"]
SideEffectState = Literal["not_started", "started", "result_committed"]


class BudgetOperationConflict(RuntimeError):
    """Stable key 已存在，但 immutable identity 不同。"""

    code = "budget.operation_conflict"

    def __init__(self) -> None:
        super().__init__(self.code)


class BudgetReservationRejected(RuntimeError):
    """对外不泄露余额、limit、price 或 owner 的 direct 拒绝。"""

    code = "budget.reservation_rejected"

    def __init__(self, *, reason: str = "balance_insufficient") -> None:
        super().__init__(self.code)
        self.reason = reason


def _canonical_bytes(value: object) -> bytes:
    """生成拒绝非有限数值的稳定 UTF-8 canonical JSON。"""

    def jsonable(item: object) -> object:
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, Mapping):
            mapping = cast(Mapping[object, object], item)
            return {str(key): jsonable(child) for key, child in mapping.items()}
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            sequence = cast(Sequence[object], item)
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
    if value is None:
        return None
    if not value.is_finite() or value < 0:
        raise ValueError("cost value must be finite and non-negative")
    return Decimal(format(value.normalize(), "f"))


class OperationIdentity(HarnessDTO):
    """与余额及首次执行结果无关的版本化 operation identity。"""

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
    identity_hash: str

    def to_payload(self) -> dict[str, Any]:
        """Identity canonical JSON 必须显式保留固定为 null 的封闭字段。"""

        return self.model_dump(mode="json", exclude_none=False)

    @field_validator("trusted_token_bound")
    @classmethod
    def validate_token_bound(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("trusted_token_bound must be non-negative")
        return value

    @field_validator("trusted_cost_bound")
    @classmethod
    def validate_cost_bound(cls, value: Decimal | None) -> Decimal | None:
        return _non_negative_decimal(value)

    @model_validator(mode="after")
    def validate_shape(self) -> OperationIdentity:
        if self.ownership_kind == "delegation":
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
            ):
                raise ValueError("delegation identity shape is invalid")
        else:
            if self.identity_schema_version != "budget-operation-v1":
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
        if self.cost_enabled != (self.trusted_cost_bound is not None):
            raise ValueError("cost-enabled identity requires exactly one trusted cost bound")
        expected = self._calculate_hash()
        if not hmac.compare_digest(self.identity_hash, expected):
            raise ValueError("identity_hash does not match canonical identity")
        return self

    def _hash_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"identity_hash"})

    def _calculate_hash(self) -> str:
        return hashlib.sha256(_canonical_bytes(self._hash_payload())).hexdigest()

    def rehashed(self) -> OperationIdentity:
        payload = self.model_dump(exclude={"identity_hash"})
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
    ) -> OperationIdentity:
        """用运行时注入的 key 派生 tenant key，数据库只看到 opaque digest。"""

        if not fingerprint_key:
            raise ValueError("fingerprint_key must not be empty")
        tenant_key = hmac.new(fingerprint_key, tenant_id.encode("utf-8"), hashlib.sha256).digest()
        request_fingerprint = hmac.new(
            tenant_key,
            _canonical_bytes(semantic_request),
            hashlib.sha256,
        ).hexdigest()
        normalized_cost_bound = _non_negative_decimal(trusted_cost_bound)
        payload: dict[str, Any] = {
            "identity_schema_version": "budget-operation-v1",
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
        """以 0015 同一 canonical request bytes 生成独立 top-level budget identity。"""

        if not fingerprint_key:
            raise ValueError("fingerprint_key must not be empty")
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
        if isinstance(value, bool) or value < 0:
            raise ValueError("token_limit must be non-negative")
        return value

    @field_validator("cost_limit")
    @classmethod
    def validate_cost_limit(cls, value: Decimal | None) -> Decimal | None:
        return _non_negative_decimal(value)


class DirectBudgetClaim(HarnessDTO):
    tenant_id: str
    budget_owner_run_id: str
    usage_call_id: str
    identity: OperationIdentity
    token_reservation: int
    cost_reservation: Decimal | None
    zero_impact: bool = False
    result: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_claim(self) -> DirectBudgetClaim:
        if self.identity.ownership_kind != "direct":
            raise ValueError("direct claim requires direct identity")
        if not self.zero_impact and self.token_reservation != self.identity.trusted_token_bound:
            raise ValueError("token reservation must equal trusted bound")
        if not self.zero_impact and self.cost_reservation != self.identity.trusted_cost_bound:
            raise ValueError("cost reservation must equal trusted bound")
        if self.zero_impact and (
            self.token_reservation != 0
            or self.cost_reservation not in {None, Decimal("0")}
            or self.result is None
        ):
            raise ValueError("zero-impact direct claim requires zero bounds and durable result")
        return self


class AllocationBudgetClaim(HarnessDTO):
    tenant_id: str
    budget_owner_run_id: str
    delegation_id: str
    usage_call_id: str
    identity: OperationIdentity
    token_reservation: int
    cost_reservation: Decimal | None
    zero_impact: bool = False
    result: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_claim(self) -> AllocationBudgetClaim:
        if self.identity.ownership_kind != "allocation":
            raise ValueError("allocation requires allocation identity")
        if self.identity.delegation_claim_id != self.delegation_id:
            raise ValueError("allocation identity must bind delegation_id")
        if not self.zero_impact and self.token_reservation != self.identity.trusted_token_bound:
            raise ValueError("token reservation must equal trusted bound")
        if not self.zero_impact and self.cost_reservation != self.identity.trusted_cost_bound:
            raise ValueError("cost reservation must equal trusted bound")
        if self.zero_impact and (
            self.token_reservation != 0
            or self.cost_reservation not in {None, Decimal("0")}
            or self.result is None
        ):
            raise ValueError("zero-impact allocation requires zero bounds and durable result")
        return self


class LedgerRecord(HarnessDTO):
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
    replayed: bool = False


class AllocationRecord(HarnessDTO):
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
    replayed: bool = False


class BudgetOperationOwnership(HarnessDTO):
    kind: Literal["direct", "allocation"]
    budget_owner_run_id: str
    delegation_id: str | None = None


class BudgetOperationReplaySeed(HarnessDTO):
    """不依赖当前 ledger/snapshot 的稳定 usage replay 身份。"""

    operation_kind: Literal["direct", "allocation"]
    ownership: BudgetOperationOwnership
    identity: OperationIdentity
    state: OperationState
    side_effect_state: SideEffectState
    result: dict[str, Any] | None


def validate_actual_usage(
    *,
    actual_tokens: int | None,
    actual_cost: Decimal | None,
    cost_status: str,
) -> None:
    """维度关闭也不绕过 usage 数值与 cost/status 组合校验。"""

    raw_tokens: Any = actual_tokens
    raw_cost: Any = actual_cost
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
