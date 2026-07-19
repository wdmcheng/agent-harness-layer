"""0016 fresh upgrade、legacy 分类与 evidence-aware downgrade 合同。"""

# ruff: noqa: F401

from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from tests.contracts.run_trace_migration_test_helpers import migration_config, seed_identity

from agent_harness.storage import get_current_revision, run_migrations
from agent_harness.storage.shared_budget import OperationIdentity


def sqlite_dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def seed_backfill_records(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    run_id: str,
    bundle: dict[str, Any],
    delegation_fingerprint_proofs: dict[str, Any] | None = None,
    prefix: str = "checkpoint",
) -> None:
    """把 history、immutable source、backfill bundle 写成三个独立 checkpoint。"""

    core = deepcopy(bundle)
    ledger = cast(dict[str, Any], core["ledger"])
    snapshot = cast(dict[str, Any], ledger["snapshot"])
    agents = cast(dict[str, dict[str, Any]], snapshot["agents"])
    history_id = f"{prefix}-history"
    source_id = f"{prefix}-source"
    bundle_id = f"{prefix}-bundle"
    catalog = {
        agent_id: {
            "agent_id": agent["agent_id"],
            "model_policy": agent["model_policy"],
            "target_budget": agent["target_budget"],
            "routes": agent["routes"],
        }
        for agent_id, agent in agents.items()
    }
    fingerprint_proofs = deepcopy(delegation_fingerprint_proofs or {})
    fingerprint_proofs_hash = canonical_hash(fingerprint_proofs)
    history = {
        "history_version": "shared-budget-history-v1",
        "registry_version": ledger["registry_version"],
        "config_version": ledger["config_version"],
        "catalog_version": ledger["catalog_version"],
        "descriptor_versions": {
            agent_id: agent["descriptor_version"] for agent_id, agent in agents.items()
        },
        "catalog_hash": canonical_hash(catalog),
        "delegation_fingerprint_proofs_hash": fingerprint_proofs_hash,
    }
    content_hash = canonical_hash(core)
    source = {
        "source_version": "shared-budget-source-v1",
        "history_checkpoint_id": history_id,
        "content_hash": content_hash,
        "delegation_fingerprint_proofs_hash": fingerprint_proofs_hash,
        "delegation_fingerprint_proofs": fingerprint_proofs,
        "backfill": core,
    }
    referenced_bundle = deepcopy(core)
    referenced_bundle["source_ref"] = {
        "checkpoint_id": source_id,
        "history_checkpoint_id": history_id,
        "source_version": "shared-budget-source-v1",
        "history_version": "shared-budget-history-v1",
        "content_hash": content_hash,
        "delegation_fingerprint_proofs_hash": fingerprint_proofs_hash,
    }
    connection.executemany(
        "insert into checkpoints(id,tenant_id,run_id,sequence,resume_token,state_json) "
        "values (?,?,?,?,?,?)",
        [
            (
                history_id,
                tenant_id,
                run_id,
                1,
                f"{prefix}-history-resume",
                json.dumps({"shared_budget_history_v1": history}),
            ),
            (
                source_id,
                tenant_id,
                run_id,
                2,
                f"{prefix}-source-resume",
                json.dumps({"shared_budget_source_v1": source}),
            ),
            (
                bundle_id,
                tenant_id,
                run_id,
                3,
                f"{prefix}-bundle-resume",
                json.dumps({"shared_budget_backfill_v1": referenced_bundle}),
            ),
        ],
    )


def delegation_fingerprint_proofs(
    identity: dict[str, Any],
    *,
    delegation_id: str,
    request_hash: str,
) -> dict[str, Any]:
    """模拟旧 writer 在 canonical request 边界独立持久化的非敏感 provenance。"""

    return {
        delegation_id: {
            "proof_version": "delegation-fingerprint-proof-v1",
            "canonical_request_hash": request_hash,
            "request_fingerprint": identity["request_fingerprint"],
            "fingerprint_key_version": identity["fingerprint_key_version"],
            "identity_hash": identity["identity_hash"],
        }
    }


__all__ = [
    "Any",
    "Decimal",
    "OperationIdentity",
    "Path",
    "canonical_hash",
    "cast",
    "command",
    "get_current_revision",
    "hashlib",
    "json",
    "migration_config",
    "pytest",
    "run_migrations",
    "delegation_fingerprint_proofs",
    "seed_backfill_records",
    "seed_identity",
    "sqlite3",
    "sqlite_dsn",
]
