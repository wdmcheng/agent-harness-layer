"""中英文维护文档引用链合同测试。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "service-app"


def test_deep_documentation_link_chain_is_bilingual() -> None:
    """英文入口不能落入中文正文，深度文档与模板示例必须成对维护。"""

    pairs = (
        (ROOT / "docs" / "framework-positioning.md", "framework-positioning.zh-CN.md"),
        (ROOT / "docs" / "architecture" / "README.md", "README.zh-CN.md"),
        (ROOT / "docs" / "adapter-contracts.md", "adapter-contracts.zh-CN.md"),
        (
            ROOT / "docs" / "context-and-trust-boundary.md",
            "context-and-trust-boundary.zh-CN.md",
        ),
        (ROOT / "docs" / "eval-observability-loop.md", "eval-observability-loop.zh-CN.md"),
        (ROOT / "docs" / "extension-guide.md", "extension-guide.zh-CN.md"),
        (ROOT / "docs" / "release-process.md", "release-process.zh-CN.md"),
        (ROOT / "docs" / "security-policy.md", "security-policy.zh-CN.md"),
        (
            ROOT / "docs" / "adr" / "0001-p0-service-boundaries.md",
            "0001-p0-service-boundaries.zh-CN.md",
        ),
        (
            ROOT / "docs" / "adr" / "0002-vendor-adapter-isolation.md",
            "0002-vendor-adapter-isolation.zh-CN.md",
        ),
        (
            ROOT / "docs" / "adr" / "0003-redis-runtime-license-policy.md",
            "0003-redis-runtime-license-policy.zh-CN.md",
        ),
        (TEMPLATE / "docs" / "README.md", "README.zh-CN.md"),
        (TEMPLATE / "docs" / "examples.md", "examples.zh-CN.md"),
    )

    for english_path, chinese_name in pairs:
        chinese_path = english_path.with_name(chinese_name)
        english = english_path.read_text(encoding="utf-8")
        chinese = chinese_path.read_text(encoding="utf-8")

        assert chinese_path.exists()
        assert f"[简体中文]({chinese_name})" in english
        assert f"[English]({english_path.name})" in chinese

    root_english = (ROOT / "README.md").read_text(encoding="utf-8")
    root_chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    building_english = (ROOT / "docs" / "building-an-agent.md").read_text(encoding="utf-8")
    building_chinese = (ROOT / "docs" / "building-an-agent.zh-CN.md").read_text(encoding="utf-8")
    template_english = (TEMPLATE / "README.md").read_text(encoding="utf-8")
    template_chinese = (TEMPLATE / "README.zh-CN.md").read_text(encoding="utf-8")

    for path in (
        "docs/framework-positioning.md",
        "docs/architecture/README.md",
        "docs/extension-guide.md",
        "docs/adapter-contracts.md",
        "docs/context-and-trust-boundary.md",
        "docs/security-policy.md",
        "docs/eval-observability-loop.md",
        "docs/release-process.md",
        "docs/adr/0001-p0-service-boundaries.md",
    ):
        assert path in root_english
        assert path.replace(".md", ".zh-CN.md") in root_chinese

    assert "templates/service-app/docs/examples.md" in building_english
    assert "templates/service-app/docs/examples.zh-CN.md" in building_chinese
    assert "docs/examples.md" in template_english
    assert "docs/examples.zh-CN.md" in template_chinese

    language_markers = {
        ROOT / "docs" / "adapter-contracts.md": "## Contract levels",
        ROOT / "docs" / "context-and-trust-boundary.md": "## Current flow",
        ROOT / "docs" / "eval-observability-loop.md": "## Base path remains unchanged",
        ROOT / "docs" / "extension-guide.md": "## Extension principles",
        ROOT / "docs" / "release-process.md": "## Bottom line",
        ROOT / "docs" / "security-policy.md": "## Identity, authentication, and permissions",
        TEMPLATE / "docs" / "examples.md": "## Add your own Agent",
    }
    for path, marker in language_markers.items():
        assert marker in path.read_text(encoding="utf-8")

    shared_contract_markers = {
        ROOT / "docs" / "framework-positioning.md": (
            "DynamicWorkflow",
            "Capability matrix",
            "Framework Positioning",
        ),
        ROOT / "docs" / "architecture" / "README.md": (
            "GraphState",
            "make smoke-service",
            "ADR-0001",
        ),
        ROOT / "docs" / "adapter-contracts.md": (
            "HarnessDTO",
            "SQLAlchemyUnitOfWork",
            "make smoke-service",
        ),
        ROOT / "docs" / "context-and-trust-boundary.md": (
            "ContextAssembler",
            "Last-Event-ID",
            "WebSocket",
        ),
        ROOT / "docs" / "eval-observability-loop.md": (
            "no-approved-cases",
            "deterministic_multilabel_v1",
            "needs_review",
        ),
        ROOT / "docs" / "extension-guide.md": (
            "AgentRegistry",
            "ApprovedToolExecutor",
            "RetrievalProvider",
        ),
        ROOT / "docs" / "release-process.md": (
            "0.11.29",
            "hosted-unverified",
            "make release-dry-run",
        ),
        ROOT / "docs" / "security-policy.md": (
            "TokenVerifier",
            "require-approval",
            "make license-check",
        ),
        ROOT / "docs" / "adr" / "0001-p0-service-boundaries.md": (
            "XAUTOCLAIM",
            "make smoke-service",
            "CanonicalEvent",
        ),
        ROOT / "docs" / "adr" / "0002-vendor-adapter-isolation.md": (
            "contracts/boundaries.py",
            "Provider",
            "make quality",
        ),
        ROOT / "docs" / "adr" / "0003-redis-runtime-license-policy.md": (
            "redis:7.2.14@sha256:",
            "BSD-3-Clause",
            "SERVICE_APP_REDIS_IMAGE",
        ),
        TEMPLATE / "docs" / "README.md": ("ai-agent-guide", "examples", "API-Contract.md"),
        TEMPLATE / "docs" / "examples.md": (
            "examples.rag_assistant",
            "agent-harness scaffold agent",
            "approved/",
        ),
    }
    for english_path, markers in shared_contract_markers.items():
        english = english_path.read_text(encoding="utf-8")
        chinese = english_path.with_name(
            "README.zh-CN.md"
            if english_path.name == "README.md"
            else english_path.name.removesuffix(".md") + ".zh-CN.md"
        ).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in english
            assert marker in chinese
