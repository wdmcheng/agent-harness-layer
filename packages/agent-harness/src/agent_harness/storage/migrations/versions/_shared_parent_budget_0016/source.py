"""0016 independent backfill source 与 versioned history 校验。"""

# checkpoint JSON 在逐字段运行时校验后才使用；SQLAlchemy stubs 无法把动态
# mapping 收窄成静态泛型，禁止 unknown 报告即可。
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import sqlalchemy as sa

from agent_harness.storage.migrations.versions._shared_parent_budget_0016.values import (
    _canonical_hash,
)


def _load_source_baseline(
    connection: sa.Connection,
    *,
    tenant_id: object,
    run_id: object,
    bundle_record: Mapping[str, object],
    bundle: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object], str]:
    """返回独立 history 与 delegation fingerprint provenance 基线。"""

    source_ref = bundle.get("source_ref")
    if not isinstance(source_ref, Mapping):
        raise RuntimeError("0016 backfill independent source is missing")
    source_id = source_ref.get("checkpoint_id")
    history_id = source_ref.get("history_checkpoint_id")
    content_hash = source_ref.get("content_hash")
    fingerprint_proofs_hash = source_ref.get("delegation_fingerprint_proofs_hash")
    if (
        not isinstance(source_id, str)
        or not source_id
        or not isinstance(history_id, str)
        or not history_id
        or source_id == bundle_record["id"]
        or history_id in {source_id, bundle_record["id"]}
        or not isinstance(content_hash, str)
        or not isinstance(fingerprint_proofs_hash, str)
        or source_ref.get("source_version") != "shared-budget-source-v1"
        or source_ref.get("history_version") != "shared-budget-history-v1"
    ):
        raise RuntimeError("0016 backfill independent source is invalid")
    records = list(
        connection.execute(
            sa.text(
                "select id,sequence,state_json from checkpoints "
                "where tenant_id=:tenant_id and run_id=:run_id"
            ).columns(state_json=sa.JSON()),
            {"tenant_id": tenant_id, "run_id": run_id},
        ).mappings()
    )
    source_records = [
        record
        for record in records
        if isinstance(record["state_json"], Mapping)
        and isinstance(record["state_json"].get("shared_budget_source_v1"), Mapping)
    ]
    history_records = [
        record
        for record in records
        if isinstance(record["state_json"], Mapping)
        and isinstance(record["state_json"].get("shared_budget_history_v1"), Mapping)
    ]
    if len(source_records) != 1 or len(history_records) != 1:
        raise RuntimeError("0016 backfill independent source is ambiguous")
    source_record = source_records[0]
    history_record = history_records[0]
    if (
        source_record["id"] != source_id
        or history_record["id"] != history_id
        or not isinstance(source_record["sequence"], int)
        or not isinstance(history_record["sequence"], int)
        or not isinstance(bundle_record["sequence"], int)
        or not history_record["sequence"] < source_record["sequence"] < bundle_record["sequence"]
    ):
        raise RuntimeError("0016 backfill independent source history is invalid")
    source_state = cast(Mapping[str, object], source_record["state_json"])
    source = cast(Mapping[str, object], source_state["shared_budget_source_v1"])
    source_bundle = source.get("backfill")
    fingerprint_proofs = source.get("delegation_fingerprint_proofs")
    bundle_core = {
        "ledger": bundle.get("ledger"),
        "claims": bundle.get("claims"),
        "allocations": bundle.get("allocations"),
    }
    if (
        source.get("source_version") != "shared-budget-source-v1"
        or source.get("history_checkpoint_id") != history_id
        or source.get("content_hash") != content_hash
        or source.get("delegation_fingerprint_proofs_hash") != fingerprint_proofs_hash
        or not isinstance(fingerprint_proofs, Mapping)
        or _canonical_hash(fingerprint_proofs) != fingerprint_proofs_hash
        or not isinstance(source_bundle, Mapping)
        or dict(source_bundle) != bundle_core
        or _canonical_hash(bundle_core) != content_hash
    ):
        raise RuntimeError("0016 backfill independent source conflicts with bundle")
    history_state = cast(Mapping[str, object], history_record["state_json"])
    history = cast(Mapping[str, object], history_state["shared_budget_history_v1"])
    return history, cast(Mapping[str, object], fingerprint_proofs), fingerprint_proofs_hash


def _versioned_history_matches(
    history: Mapping[str, object],
    *,
    ledger: Mapping[str, object],
    snapshot: Mapping[str, object],
    delegation_fingerprint_proofs_hash: str,
) -> bool:
    """验证版本化 history 是否与冻结账本、代理目录及 delegation 证明完全一致。

    迁移恢复不能仅相信单个 hash：此处逐项重建 descriptor/catalog 投影，确保
    checkpoint 所声称的历史确实来自同一份 durable snapshot。
    """

    raw_agents = snapshot.get("agents")
    if not isinstance(raw_agents, Mapping):
        return False
    agents = cast(Mapping[str, object], raw_agents)
    descriptor_versions: dict[str, object] = {}
    catalog: dict[str, object] = {}
    for agent_id, raw_agent in agents.items():
        if not isinstance(raw_agent, Mapping):
            return False
        agent = cast(Mapping[str, object], raw_agent)
        descriptor_versions[str(agent_id)] = agent.get("descriptor_version")
        catalog[str(agent_id)] = {
            "agent_id": agent.get("agent_id"),
            "model_policy": agent.get("model_policy"),
            "target_budget": agent.get("target_budget"),
            "routes": agent.get("routes"),
        }
    return bool(
        history.get("history_version") == "shared-budget-history-v1"
        and history.get("registry_version") == ledger.get("registry_version")
        and history.get("config_version") == ledger.get("config_version")
        and history.get("catalog_version") == ledger.get("catalog_version")
        and history.get("descriptor_versions") == descriptor_versions
        and history.get("catalog_hash") == _canonical_hash(catalog)
        and history.get("delegation_fingerprint_proofs_hash") == delegation_fingerprint_proofs_hash
    )


__all__ = ["_load_source_baseline", "_versioned_history_matches"]
