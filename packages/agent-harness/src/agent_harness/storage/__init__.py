"""持久化、migration 和 Unit of Work 的公开入口。"""

from __future__ import annotations

from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage as SQLAlchemyStorage
from agent_harness.storage.eval_acceptance_repositories import (
    HarnessAcceptanceCreate as HarnessAcceptanceCreate,
)
from agent_harness.storage.eval_acceptance_repositories import (
    HarnessAcceptanceRecord as HarnessAcceptanceRecord,
)
from agent_harness.storage.eval_dataset_split_repositories import (
    EvalDatasetSplitCreate as EvalDatasetSplitCreate,
)
from agent_harness.storage.eval_dataset_split_repositories import (
    EvalDatasetSplitRecord as EvalDatasetSplitRecord,
)
from agent_harness.storage.eval_experiment_repositories import (
    EvalExperimentCreate as EvalExperimentCreate,
)
from agent_harness.storage.eval_experiment_repositories import (
    EvalExperimentRecord as EvalExperimentRecord,
)
from agent_harness.storage.eval_experiment_repositories import (
    ExperimentStorageConcurrentConflict as ExperimentStorageConcurrentConflict,
)
from agent_harness.storage.eval_experiment_repositories import (
    ExperimentStorageConflict as ExperimentStorageConflict,
)
from agent_harness.storage.eval_experiment_repositories import (
    ExperimentStorageNotFound as ExperimentStorageNotFound,
)
from agent_harness.storage.eval_repositories import EvalCaseCreate as EvalCaseCreate
from agent_harness.storage.eval_repositories import EvalCaseRecord as EvalCaseRecord
from agent_harness.storage.eval_repositories import EvalRunCreate as EvalRunCreate
from agent_harness.storage.eval_repositories import EvalRunRecord as EvalRunRecord
from agent_harness.storage.eval_repositories import EvalScoreCreate as EvalScoreCreate
from agent_harness.storage.eval_repositories import EvalScoreRecord as EvalScoreRecord
from agent_harness.storage.migrations.runner import get_current_revision as get_current_revision
from agent_harness.storage.migrations.runner import run_migrations as run_migrations
from agent_harness.storage.repositories import (
    ApiKeyCreate as ApiKeyCreate,
)
from agent_harness.storage.repositories import (
    ApiKeyRecord as ApiKeyRecord,
)
from agent_harness.storage.repositories import (
    ApprovalCreate as ApprovalCreate,
)
from agent_harness.storage.repositories import (
    ApprovalRecord as ApprovalRecord,
)
from agent_harness.storage.repositories import (
    AuditLogCreate as AuditLogCreate,
)
from agent_harness.storage.repositories import (
    AuditLogRecord as AuditLogRecord,
)
from agent_harness.storage.repositories import (
    CheckpointCreate as CheckpointCreate,
)
from agent_harness.storage.repositories import (
    CheckpointRecord as CheckpointRecord,
)
from agent_harness.storage.repositories import (
    PolicyRuleCreate as PolicyRuleCreate,
)
from agent_harness.storage.repositories import (
    PolicyRuleRecord as PolicyRuleRecord,
)
from agent_harness.storage.repositories import (
    RunCreate as RunCreate,
)
from agent_harness.storage.repositories import (
    RunRecord as RunRecord,
)
from agent_harness.storage.repositories import (
    SessionCreate as SessionCreate,
)
from agent_harness.storage.repositories import (
    SessionRecord as SessionRecord,
)
from agent_harness.storage.repositories import (
    TenantRecord as TenantRecord,
)
from agent_harness.storage.retrieval_repositories import (
    RetrievalChunkCreate as RetrievalChunkCreate,
)
from agent_harness.storage.retrieval_repositories import (
    RetrievalChunkRecord as RetrievalChunkRecord,
)
from agent_harness.storage.retrieval_repositories import (
    RetrievalDocumentCreate as RetrievalDocumentCreate,
)
from agent_harness.storage.retrieval_repositories import (
    RetrievalDocumentRecord as RetrievalDocumentRecord,
)
from agent_harness.storage.settings import storage_dsn_from_settings as storage_dsn_from_settings
from agent_harness.storage.tool_repositories import (
    ToolInvocationCreate as ToolInvocationCreate,
)
from agent_harness.storage.tool_repositories import (
    ToolInvocationRecord as ToolInvocationRecord,
)
from agent_harness.storage.tool_repositories import WorkspaceCreate as WorkspaceCreate
from agent_harness.storage.tool_repositories import WorkspaceRecord as WorkspaceRecord

_REPOSITORY_DTO_EXPORTS = [
    "ApiKeyCreate",
    "ApiKeyRecord",
    "ApprovalCreate",
    "ApprovalRecord",
    "AuditLogCreate",
    "AuditLogRecord",
    "CheckpointCreate",
    "CheckpointRecord",
    "PolicyRuleCreate",
    "PolicyRuleRecord",
    "RunCreate",
    "RunRecord",
    "SessionCreate",
    "SessionRecord",
    "TenantRecord",
    "EvalCaseCreate",
    "EvalCaseRecord",
    "EvalRunCreate",
    "EvalRunRecord",
    "EvalScoreCreate",
    "EvalScoreRecord",
    "EvalDatasetSplitCreate",
    "EvalDatasetSplitRecord",
    "EvalExperimentCreate",
    "EvalExperimentRecord",
    "ExperimentStorageConflict",
    "ExperimentStorageConcurrentConflict",
    "ExperimentStorageNotFound",
    "HarnessAcceptanceCreate",
    "HarnessAcceptanceRecord",
    "RetrievalChunkCreate",
    "RetrievalChunkRecord",
    "RetrievalDocumentCreate",
    "RetrievalDocumentRecord",
    "ToolInvocationCreate",
    "ToolInvocationRecord",
    "WorkspaceCreate",
    "WorkspaceRecord",
]

_STORAGE_ADAPTER_EXPORTS = [
    "SQLAlchemyStorage",
]

_MIGRATION_EXPORTS = [
    "get_current_revision",
    "run_migrations",
]

_SETTINGS_EXPORTS = [
    "storage_dsn_from_settings",
]

__all__ = [  # pyright: ignore[reportUnsupportedDunderAll]
    *_REPOSITORY_DTO_EXPORTS,
    *_STORAGE_ADAPTER_EXPORTS,
    *_MIGRATION_EXPORTS,
    *_SETTINGS_EXPORTS,
]
