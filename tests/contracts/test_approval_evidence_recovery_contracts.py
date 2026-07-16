"""审批确定性结果与 terminal/resolution evidence 故障恢复合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.contracts.approval_evidence_contract_helpers import fail_once_on_event
from tests.contracts.test_approval_execution_contracts import build_approval_flow

from agent_harness.approvals import ApprovalStateConflict
from agent_harness.events import CanonicalEventType
from agent_harness.runtime import RunStatus
from agent_harness.storage import AuditLogCreate
from agent_harness.storage.audit_repositories import AuditLogRepository
from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


__all__ = [
    "ApprovalStateConflict",
    "AuditLogCreate",
    "AuditLogRepository",
    "CanonicalEventType",
    "PROFILES",
    "Path",
    "ROOT",
    "RunStatus",
    "TestClient",
    "build_approval_flow",
    "create_app",
    "fail_once_on_event",
    "pytest",
]
