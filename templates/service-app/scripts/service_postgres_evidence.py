"""核验 service smoke 的 PostgreSQL 终态、用量与预算证据。"""

from __future__ import annotations

from typing import Any, cast


def postgres_terminal_evidence(
    expected: dict[str, str],
    completed: dict[str, Any],
    *,
    workflow_id: str,
) -> dict[str, object]:
    """核对 model usage、容量结算、执行上下文与唯一终态事件。"""

    events = cast(list[dict[str, Any]], completed["events"])
    outbox_rows = cast(list[dict[str, Any]], completed["outbox"])
    capacity = cast(dict[str, Any], completed["capacity"])
    terminals = [event for event in events if event["terminal"]]
    started = [event for event in events if event["type"] == "model.request.started"]
    usages = [event for event in events if event["type"] == "model.usage.updated"]
    terminal = terminals[0] if len(terminals) == 1 else None
    model_started = started[0] if len(started) == 1 else None
    usage = usages[0] if len(usages) == 1 else None
    started_call_id = (
        None
        if model_started is None
        else model_started.get("payload", {}).get("correlation", {}).get("usage_call_id")
    )
    usage_call_id = (
        None
        if usage is None
        else usage.get("payload", {}).get("correlation", {}).get("usage_call_id")
    )
    usage_outbox = [
        item
        for item in outbox_rows
        if item["operation_kind"] == "model_usage" and item["usage_call_id"] == usage_call_id
    ]
    raw_shared_budget = completed.get("shared_budget")
    shared_budget = (
        cast(dict[str, Any], raw_shared_budget) if isinstance(raw_shared_budget, dict) else None
    )
    raw_usage_payload = None if usage is None else usage.get("payload", {}).get("usage", {})
    usage_payload = (
        cast(dict[str, Any], raw_usage_payload) if isinstance(raw_usage_payload, dict) else None
    )
    actual_tokens = (
        None
        if usage_payload is None
        or not isinstance(usage_payload.get("input_tokens"), int)
        or not isinstance(usage_payload.get("output_tokens"), int)
        else usage_payload["input_tokens"] + usage_payload["output_tokens"]
    )
    budget_claims = (
        []
        if shared_budget is None
        else [
            item
            for item in cast(list[dict[str, Any]], shared_budget.get("claims", []))
            if item.get("operation_kind") == "direct" and item.get("usage_call_id") == usage_call_id
        ]
    )
    checks = {
        "terminal_count": terminal is not None,
        "model_started_count": model_started is not None,
        "usage_count": usage is not None,
        "usage_call_id": isinstance(usage_call_id, str) and started_call_id == usage_call_id,
        "usage_order": (
            model_started is not None
            and usage is not None
            and terminal is not None
            and model_started["seq"] < usage["seq"] < terminal["seq"]
            and not usage["terminal"]
        ),
        "usage_outbox": (
            usage is not None
            and len(usage_outbox) == 1
            and usage_outbox[0]["state"] == "published"
            and usage_outbox[0]["event_id"] == usage["event_id"]
        ),
        "capacity": (
            terminal is not None
            and capacity["highest_persisted_seq"] == terminal["seq"]
            and capacity["outstanding_reserved_event_count"] == 0
            and capacity["terminal_reservation"] == 0
        ),
        "shared_budget": (
            shared_budget is not None
            and shared_budget.get("owner_run_id") == completed.get("run_id")
            and shared_budget.get("state") == "terminal"
            and shared_budget.get("cost_enabled") is False
            and shared_budget.get("cost_impact") in {"0", "0E-8", "0.00000000"}
            and shared_budget.get("token_impact") == actual_tokens
            and len(budget_claims) == 1
            and budget_claims[0].get("state") == "settled"
            and budget_claims[0].get("side_effect_state") == "result_committed"
            and budget_claims[0].get("token_impact") == actual_tokens
        ),
        "workflow": completed["workflow_id"] == workflow_id,
        "correlation": not any(completed.get(key) != value for key, value in expected.items()),
        "terminal_shape": (
            terminal is not None
            and terminal["type"] == "run.completed"
            and terminal["visibility"] == "public"
            and terminal["request_id"] == expected["request_id"]
            and bool(terminal["event_id"])
            and terminal["trace_id"] == completed.get("trace_id")
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError("service.evidence." + ",".join(failed))
    assert terminal is not None
    assert model_started is not None
    assert usage is not None
    assert len(usage_outbox) == 1
    return {
        "execution": {key: completed[key] for key in expected},
        "terminal_event": {
            "event_id": terminal["event_id"],
            "type": terminal["type"],
            "request_id": terminal["request_id"],
            "trace_id": terminal["trace_id"],
        },
        "usage": {
            "usage_call_id": usage_call_id,
            "started_seq": model_started["seq"],
            "usage_seq": usage["seq"],
            "terminal_seq": terminal["seq"],
            "outbox_state": usage_outbox[0]["state"],
            "capacity": capacity,
        },
        "shared_budget": shared_budget,
    }


__all__ = ["postgres_terminal_evidence"]
