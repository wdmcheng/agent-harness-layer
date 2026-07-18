"""真实 service worker 的 shared-budget 三个 crash window 探针。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import cast
from uuid import uuid4

from service_http_smoke import (
    submit as _submit,
)
from service_http_smoke import (
    wait_for as _wait_for,
)
from service_http_smoke import (
    wait_run_status as _wait_run_status,
)
from service_smoke_operations import (
    inspect_run,
)
from service_smoke_support import (
    compose,
    reclaim_receipts_match,
    run,
)

APP_ROOT = Path(__file__).resolve().parents[1]
STREAM = "agent-harness:service:runs:stream"
GROUP = "agent-harness-workers"


def shared_budget_crash_smoke(
    env: dict[str, str],
    *,
    base_url: str,
    token: str,
) -> dict[str, object]:
    """在真实 Redis reclaim 中逐个钉住 shared claim 三个 durable window。"""

    evidence: dict[str, object] = {}
    for phase, exit_code in (
        ("not_started", 25),
        ("started", 26),
        ("result_committed", 27),
    ):
        env["SERVICE_APP_SMOKE_BOUNDARY"] = f"shared-budget-crash-windows-{phase}"
        submitted = _submit(
            base_url,
            token,
            agent_id="examples.rag_assistant",
            input_payload={
                "query": f"shared budget crash window {phase}",
                "collection": f"shared-budget-{phase}",
                "documents": [
                    {
                        "document_id": f"shared-budget-{phase}",
                        "content": (
                            f"Shared budget crash window {phase} recovery must preserve citations."
                        ),
                        "source_ref": f"smoke://shared-budget/{phase}",
                        "citation": f"Shared Budget {phase}",
                    }
                ],
            },
            idempotency_key=f"shared-budget-{phase}-{uuid4()}",
            request_id=f"shared-budget-request-{phase}-{uuid4()}",
        )
        run_id = cast(str, submitted["run_id"])
        crash_name = f"{env['SERVICE_APP_COMPOSE_PROJECT']}-budget-{phase}-crash"
        recover_name = f"{env['SERVICE_APP_COMPOSE_PROJECT']}-budget-{phase}-recover"
        crash_marker = f"/smoke/budget-{phase}-crash.json"
        crash_receipt = f"/smoke/budget-{phase}-receipt-a.json"
        recover_receipt = f"/smoke/budget-{phase}-receipt-b.json"
        recover_marker = f"/smoke/budget-{phase}-recovery.json"
        try:
            compose(
                env,
                "run",
                "-d",
                "--name",
                crash_name,
                "--no-deps",
                "-e",
                f"SERVICE_APP_SMOKE_SHARED_BUDGET_CRASH={phase}",
                "-e",
                f"SERVICE_APP_SMOKE_SHARED_BUDGET_MARKER={crash_marker}",
                "-e",
                f"SERVICE_APP_SMOKE_RECEIPT_MARKER={crash_receipt}",
                "-e",
                "SERVICE_APP_READY_FILE=",
                "-e",
                "SERVICE_APP_SMOKE_RECLAIM_RELEASE=",
                "worker",
            )

            crash_state = {"value": ""}

            def crashed(
                crash_name: str = crash_name,
                crash_state: dict[str, str] = crash_state,
            ) -> bool:
                inspected = run(
                    [
                        "docker",
                        "inspect",
                        "-f",
                        "{{.State.Status}}|{{.State.ExitCode}}",
                        crash_name,
                    ],
                    env=env,
                    check=False,
                )
                crash_state["value"] = inspected.stdout.strip()
                return crash_state["value"].startswith("exited|")

            env["SERVICE_APP_SMOKE_BOUNDARY"] = f"shared-budget-crash-windows-{phase}-crash-exit"
            _wait_for(f"shared budget {phase} hard crash", crashed)
            expected_crash_state = f"exited|{exit_code}"
            if crash_state["value"] != expected_crash_state:
                observed = crash_state["value"].replace("|", "-") or "missing"
                env["SERVICE_APP_SMOKE_BOUNDARY"] = (
                    f"shared-budget-crash-windows-{phase}-unexpected-{observed}"
                )
                raise RuntimeError(
                    f"shared budget {phase} crash exit mismatch: {crash_state['value']}"
                )
            env["SERVICE_APP_SMOKE_BOUNDARY"] = f"shared-budget-crash-windows-{phase}-crash-marker"
            marker_path = Path(env["SERVICE_APP_SMOKE_DIR"]) / f"budget-{phase}-crash.json"
            receipt_a_path = Path(env["SERVICE_APP_SMOKE_DIR"]) / f"budget-{phase}-receipt-a.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            receipt_a = json.loads(receipt_a_path.read_text(encoding="utf-8"))
            if marker != {"phase": phase, "run_id": run_id}:
                raise RuntimeError(f"shared budget {phase} crash marker mismatch: {marker}")
            env["SERVICE_APP_SMOKE_BOUNDARY"] = f"shared-budget-crash-windows-{phase}-reclaim-start"
            time.sleep(float(env["SERVICE_APP_RECLAIM_IDLE_SECONDS"]) + 0.25)
            compose(
                env,
                "run",
                "-d",
                "--name",
                recover_name,
                "--no-deps",
                "-e",
                f"SERVICE_APP_SMOKE_RECEIPT_MARKER={recover_receipt}",
                "-e",
                f"SERVICE_APP_SMOKE_RECOVERY_MARKER={recover_marker}",
                "-e",
                "SERVICE_APP_READY_FILE=",
                "-e",
                "SERVICE_APP_SMOKE_RECLAIM_RELEASE=",
                "worker",
            )
            receipt_b_path = Path(env["SERVICE_APP_SMOKE_DIR"]) / f"budget-{phase}-receipt-b.json"
            env["SERVICE_APP_SMOKE_BOUNDARY"] = (
                f"shared-budget-crash-windows-{phase}-reclaim-receipt"
            )
            _wait_for(f"shared budget {phase} reclaim receipt", receipt_b_path.exists)
            receipt_b = json.loads(receipt_b_path.read_text(encoding="utf-8"))
            if not reclaim_receipts_match(receipt_a["message_id"], receipt_a, receipt_b):
                raise RuntimeError(f"shared budget {phase} reclaim mismatch: {receipt_b}")

            recover_state = {"value": ""}
            recovered_run_state: dict[str, object] = {}

            def recovered_or_worker_exited(
                recover_name: str = recover_name,
                recover_state: dict[str, str] = recover_state,
                recovered_run_state: dict[str, object] = recovered_run_state,
                run_id: str = run_id,
                phase: str = phase,
            ) -> bool:
                inspected = run(
                    [
                        "docker",
                        "inspect",
                        "-f",
                        "{{.State.Status}}|{{.State.ExitCode}}",
                        recover_name,
                    ],
                    env=env,
                    check=False,
                )
                recover_state["value"] = inspected.stdout.strip()
                if recover_state["value"].startswith("exited|"):
                    return True
                state = inspect_run(env, run_id)
                recovered_run_state.clear()
                recovered_run_state.update(state)
                if phase in {"not_started", "result_committed"}:
                    return state.get("status") == "completed"
                shared = cast(dict[str, object], state.get("shared_budget") or {})
                claims = cast(list[dict[str, object]], shared.get("claims", []))
                return any(item.get("state") == "needs_review" for item in claims)

            env["SERVICE_APP_SMOKE_BOUNDARY"] = f"shared-budget-crash-windows-{phase}-recover-state"
            _wait_for(
                f"shared budget {phase} recovery state",
                recovered_or_worker_exited,
            )
            if recover_state["value"].startswith("exited|"):
                observed = recover_state["value"].replace("|", "-") or "missing"
                logs = run(
                    ["docker", "logs", "--tail", "20", recover_name],
                    env=env,
                    check=False,
                )
                last_line = next(
                    (
                        line.strip()
                        for line in reversed((logs.stdout + logs.stderr).splitlines())
                        if line.strip()
                    ),
                    "unknown",
                )
                error_type = "".join(
                    character if character.isalnum() or character in "._-" else "-"
                    for character in last_line
                )[:120]
                recovery_marker_path = (
                    Path(env["SERVICE_APP_SMOKE_DIR"]) / f"budget-{phase}-recovery.json"
                )
                recovery_error = "missing"
                if recovery_marker_path.exists():
                    recovery_payload = json.loads(recovery_marker_path.read_text(encoding="utf-8"))
                    recovery_error = "".join(
                        character if character.isalnum() or character in "._-" else "-"
                        for character in str(recovery_payload.get("error") or "missing")
                    )[:120]
                failed_state = inspect_run(env, run_id)
                shared = cast(dict[str, object], failed_state.get("shared_budget") or {})
                failed_claims = cast(list[dict[str, object]], shared.get("claims", []))
                failed_outbox = cast(list[dict[str, object]], failed_state.get("outbox", []))
                claim_summary = "_".join(
                    f"{item.get('state')}-{item.get('side_effect_state')}" for item in failed_claims
                )
                outbox_summary = "_".join(
                    f"{item.get('operation_kind')}-{item.get('state')}" for item in failed_outbox
                )
                env["SERVICE_APP_SMOKE_BOUNDARY"] = (
                    f"shared-budget-crash-windows-{phase}-recover-{observed}-{error_type}"
                    f"-executor-{recovery_error}"
                    f"-run-{failed_state.get('status')}-ledger-{shared.get('state')}"
                    f"-claims-{claim_summary}-outbox-{outbox_summary}"
                )
                raise RuntimeError(
                    f"shared budget {phase} recovery worker failed: {recover_state['value']}"
                )
            env["SERVICE_APP_SMOKE_BOUNDARY"] = (
                f"shared-budget-crash-windows-{phase}-recovered-state"
            )
            if phase in {"not_started", "result_committed"}:
                _wait_run_status(base_url, token, run_id, "completed")
            else:

                def needs_review(run_id: str = run_id) -> bool:
                    state = inspect_run(env, run_id)
                    shared = cast(dict[str, object], state.get("shared_budget") or {})
                    claims = cast(list[dict[str, object]], shared.get("claims", []))
                    return any(item.get("state") == "needs_review" for item in claims)

                _wait_for("shared budget started window needs_review", needs_review)
            env["SERVICE_APP_SMOKE_BOUNDARY"] = f"shared-budget-crash-windows-{phase}-claim-count"
            state = inspect_run(env, run_id)
            shared = cast(dict[str, object], state.get("shared_budget") or {})
            claims = cast(list[dict[str, object]], shared.get("claims", []))
            if len(claims) != 2:
                raise RuntimeError(f"shared budget {phase} duplicated claim: {claims}")
            claim_states = sorted(str(item["state"]) for item in claims)
            expected_states = (
                ["needs_review", "settled"] if phase == "started" else ["settled", "settled"]
            )
            if claim_states != expected_states:
                raise RuntimeError(
                    f"shared budget {phase} model/embedding states mismatch: {claims}"
                )
            evidence[phase] = {
                "delivery_count": receipt_b["delivery_count"],
                "claim_states": claim_states,
                "side_effect_states": sorted(str(item["side_effect_state"]) for item in claims),
                "model_and_embedding": True,
                "run_status": state["status"],
            }
        finally:
            run(["docker", "rm", "-f", crash_name, recover_name], env=env, check=False)
    return evidence


__all__ = ["shared_budget_crash_smoke"]
