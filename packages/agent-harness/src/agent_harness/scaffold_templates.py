"""Agent scaffold 的离线安全模板与 staged package 渲染。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

PACKAGE_INIT = '"""由 Agent Harness scaffold 维护的 Python package。"""\n'


def render_staged_package(
    staged_agents: Path,
    staged_target: Path,
    agent_id: str,
    parts: Sequence[str],
) -> None:
    """在隔离 staging 根内渲染完整 package，供发布前校验。"""

    staged_target.mkdir(parents=True)
    (staged_target / "evals" / "drafts").mkdir(parents=True)
    (staged_target / "evals" / "approved").mkdir(parents=True)
    _write_text(staged_agents / "__init__.py", PACKAGE_INIT)
    current = staged_agents
    for part in parts[:-1]:
        current /= part
        _write_text(current / "__init__.py", PACKAGE_INIT)
    rendered = _render_agent_files(agent_id, parts, namespace=staged_agents.name)
    for relative_path, content in rendered.items():
        _write_text(staged_target / relative_path, content)


def _render_agent_files(
    agent_id: str,
    parts: Sequence[str],
    *,
    namespace: str,
) -> dict[Path, str]:
    """生成单个 agent package 的静态文件内容，所有引用都指向 staging 命名空间。

    该函数只返回内容而不写盘，使调用方能先在隔离目录完成校验；生成的默认工具、
    评测样例和配置均采用最小权限，业务项目需要显式审核后再扩展。
    """

    package_ref = ".".join((namespace, *parts))
    title = " ".join(part.replace("_", " ").title() for part in parts)
    path_ref = "/".join((namespace, *parts, "evals", "approved"))
    return {
        Path("__init__.py"): PACKAGE_INIT,
        Path("agent.py"): _agent_source(agent_id),
        Path("tools.py"): (
            '"""默认不授予工具权限；审核后再显式扩展 config allowlist。"""\n\n'
            "TOOL_ALLOWLIST: tuple[str, ...] = ()\n"
        ),
        Path("schemas.py"): _schemas_source(),
        Path("config.yaml"): (
            "# agent-harness-scaffold: executor-v1\n"
            f"agent_id: {agent_id}\n"
            "version: 0.1.0\n"
            f"name: {title} Agent\n"
            "description: 离线 scaffold agent，等待补充领域实现。\n"
            f"input_schema: {package_ref}.schemas.ScaffoldInput\n"
            f"output_schema: {package_ref}.schemas.ScaffoldOutput\n"
            "executor: agent:executor\n"
            "model:\n"
            "  provider: fake\n"
            "  default_model: fake-scaffold\n"
            "  fallback_models: []\n"
            "budget:\n"
            "  max_tokens_per_run: 1024\n"
            "  max_cost_usd_per_run: null\n"
            "tool_allowlist: []\n"
            f"eval_dataset: {path_ref}\n"
            "delegation_edges: []\n"
        ),
        Path("evals/drafts/example.yaml"): (
            "# Draft 只供人工审核；scaffold 不会把它写入 approved。\n"
            f"case_id: {agent_id.replace('.', '-')}-draft-example\n"
            f"agent_id: {agent_id}\n"
            "payload:\n"
            "  input:\n"
            "    prompt: 验证 scaffold runtime。\n"
            "  expected:\n"
            f"    agent_id: {agent_id}\n"
            "    result: scaffold-ready\n"
            "    model_provider: fake\n"
        ),
    }


def _agent_source(agent_id: str) -> str:
    """生成离线 fake executor 源码，保留 run/resume 的公开协议与安全默认值。"""

    return f'''"""{agent_id} 的离线默认 executor；业务实现应保持公共 seam。"""

from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)


class ScaffoldAgentExecutor:
    """返回可验证的离线结果，不读取 secret 或授予工具权限。"""

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """返回确定性离线结果，用于验证 registry 与 runtime 的基础连接。"""

        del request, context
        return AgentExecutionResult.completed(
            {{"agent_id": "{agent_id}", "result": "scaffold-ready", "model_provider": "fake"}}
        )

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """明确拒绝审批续跑，因为 scaffold 默认没有可授权的副作用。"""

        del request, context, grant
        return AgentExecutionResult.failed("scaffold agent has no approval-gated action")


executor = ScaffoldAgentExecutor()
'''


def _schemas_source() -> str:
    """生成 scaffold 输入输出 DTO 源码，避免模板把 provider SDK 对象暴露为契约。"""

    return '''"""scaffold agent 的类型化输入输出边界。"""

from agent_harness.contracts.dto import HarnessDTO


class ScaffoldInput(HarnessDTO):
    """后续业务实现可扩展，但不得把 provider object 放入 DTO。"""

    prompt: str


class ScaffoldOutput(HarnessDTO):
    """默认离线 smoke 输出。"""

    agent_id: str
    result: str
    model_provider: str
'''


def _write_text(path: Path, content: str) -> None:
    """创建父目录后以 UTF-8 写入 staging 文件；调用方保证目标仍位于隔离根内。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


__all__ = ["PACKAGE_INIT", "render_staged_package"]
