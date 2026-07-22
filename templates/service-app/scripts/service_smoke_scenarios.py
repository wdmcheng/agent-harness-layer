"""按因果顺序编排 service smoke 场景，不在入口重复各阶段实现。"""

from __future__ import annotations

from service_budget_crash_smoke import shared_budget_crash_smoke
from service_smoke_bootstrap import prepare_service, verify_authentication
from service_smoke_evidence import collect_evidence
from service_smoke_reclaim import run_reclaim_scenario


def run_service_smoke(env: dict[str, str], token: str, tenant_id: str) -> dict[str, object]:
    """依次执行启动认证、预算崩溃、worker 重领和证据闭合阶段。"""

    base_url = f"http://127.0.0.1:{env['SERVICE_APP_API_PORT']}"
    bootstrap = prepare_service(env, token)
    verify_authentication(env, base_url)

    env["SERVICE_APP_SMOKE_BOUNDARY"] = "shared-budget-crash-windows"
    budget_crash_windows = shared_budget_crash_smoke(env, base_url=base_url, token=token)
    reclaim = run_reclaim_scenario(
        env,
        base_url=base_url,
        token=token,
        tenant_id=tenant_id,
    )
    return collect_evidence(
        env,
        base_url=base_url,
        token=token,
        bootstrap=bootstrap,
        reclaim=reclaim,
        budget_crash_windows=budget_crash_windows,
    )
