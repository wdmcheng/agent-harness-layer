"""Delegation UoW repository 的兼容 facade。"""

from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._delegation_claim_repository import DelegationClaimRepositoryMixin
from agent_harness.storage._delegation_read_repository import DelegationReadRepositoryMixin
from agent_harness.storage._delegation_records import (
    DelegatedChildRunRecord,
    DelegationAggregateRecord,
    DelegationBudgetExceeded,
    DelegationBudgetReservationRecord,
    DelegationClaimCreate,
    DelegationClaimResult,
    DelegationRecord,
    DelegationRecoveryCandidate,
    DelegationReplayIdentitySeed,
    DelegationStorageConflict,
    DelegationStorageError,
    DelegationSummaryProjectionRecord,
    DelegationUsageEvidenceRecord,
)
from agent_harness.storage._delegation_settlement_repository import (
    DelegationSettlementRepositoryMixin,
)

# records 与错误继续属于公开 repository facade，私有职责模块不进入稳定身份。
for _public_record in (
    DelegatedChildRunRecord,
    DelegationAggregateRecord,
    DelegationBudgetExceeded,
    DelegationBudgetReservationRecord,
    DelegationClaimCreate,
    DelegationClaimResult,
    DelegationRecord,
    DelegationRecoveryCandidate,
    DelegationReplayIdentitySeed,
    DelegationStorageConflict,
    DelegationStorageError,
    DelegationSummaryProjectionRecord,
    DelegationUsageEvidenceRecord,
):
    _public_record.__module__ = __name__
del _public_record


class DelegationRepository(
    DelegationClaimRepositoryMixin,
    DelegationReadRepositoryMixin,
    DelegationSettlementRepositoryMixin,
):
    """调用方在 parent lock 内使用；PostgreSQL 额外锁 parent row。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session


__all__ = [
    "DelegatedChildRunRecord",
    "DelegationAggregateRecord",
    "DelegationBudgetExceeded",
    "DelegationBudgetReservationRecord",
    "DelegationClaimCreate",
    "DelegationClaimResult",
    "DelegationRecord",
    "DelegationRecoveryCandidate",
    "DelegationReplayIdentitySeed",
    "DelegationRepository",
    "DelegationStorageConflict",
    "DelegationStorageError",
    "DelegationSummaryProjectionRecord",
    "DelegationUsageEvidenceRecord",
]
