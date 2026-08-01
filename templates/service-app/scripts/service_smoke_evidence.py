"""闭合 service smoke 的传输、存储、幂等、审批与密钥证据。"""

from __future__ import annotations

from uuid import uuid4

from service_approval_smoke import run_approval_smoke
from service_http_smoke import request, submit
from service_secret_smoke import assert_configuration_secret_absent
from service_smoke_bootstrap import BootstrapEvidence
from service_smoke_reclaim import ReclaimEvidence
from service_smoke_support import postgres_terminal_evidence
from service_smoke_trace import write_service_trace
from service_sse_smoke import run_sse_smoke


def collect_evidence(
    env: dict[str, str],
    *,
    base_url: str,
    token: str,
    bootstrap: BootstrapEvidence,
    reclaim: ReclaimEvidence,
    budget_crash_windows: dict[str, object],
) -> dict[str, object]:
    """生成可归档证据，并在写入 trace 后执行最终配置密钥扫描。"""

    execution_expected = {**reclaim.expected, "message_id": reclaim.message_id}
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "postgres-sse-transport"
    sse_evidence = run_sse_smoke(
        env,
        base_url=base_url,
        token=token,
        run_id=reclaim.run_id,
    )
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "dbos-event-usage"
    try:
        postgres_evidence = postgres_terminal_evidence(
            execution_expected,
            reclaim.completed,
            workflow_id=reclaim.marker["workflow_id"],
        )
    except RuntimeError as exc:
        if str(exc).startswith("service.evidence."):
            env["SERVICE_APP_SMOKE_BOUNDARY"] = str(exc)
        raise
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "idempotency-replay"
    replay = submit(
        base_url,
        token,
        agent_id="examples.ticket_triage",
        input_payload={"text": "production outage: checkout is down"},
        idempotency_key=reclaim.expected["idempotency_key"],
        request_id=f"retry-{uuid4()}",
    )
    if replay["run_id"] != reclaim.run_id:
        raise RuntimeError("idempotent HTTP retry created another run")
    approval_evidence = run_approval_smoke(env, base_url=base_url, token=token)

    evidence: dict[str, object] = {
        "migration": "0017_model_route_chain_state",
        "secret_file": {
            "consumers": ["migration", "api", "worker"],
            "postgres_password_file": True,
            "compose_config_redacted": True,
            "redacted": True,
            "failure_cases": bootstrap.secret_failures,
        },
        "auth": {"missing": 401, "invalid": 401, "side_effects": 0},
        "api_docs": bootstrap.api_docs,
        "queue": {
            **reclaim.expected,
            "message_id": reclaim.message_id,
            "delivery_count": reclaim.worker_b_receipt["delivery_count"],
            "stale_receipt_rejected": True,
        },
        "dbos": {
            "executor_id": reclaim.marker["executor_id"],
            "owner_id": reclaim.marker["owner_id"],
            "workflow_id": reclaim.marker["workflow_id"],
            "hard_crash_exit": 23,
        },
        "run": {"run_id": reclaim.run_id, "status": "completed", "terminal_count": 1},
        "sse": sse_evidence,
        "postgresql": postgres_evidence,
        "postgresql_budget_race": bootstrap.budget_race,
        "postgresql_budget_topology": bootstrap.budget_topology,
        "shared_budget_crash_windows": budget_crash_windows,
        **approval_evidence,
    }
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "trace-export"
    write_service_trace(env, reclaim.completed)
    env["SERVICE_APP_SMOKE_BOUNDARY"] = "secret-evidence-scan"
    assert_configuration_secret_absent(
        env,
        base_url=base_url,
        evidence=evidence,
        request=request,
    )
    return evidence
