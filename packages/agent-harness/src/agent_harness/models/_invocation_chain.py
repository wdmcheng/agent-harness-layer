"""Provider-neutral completion route-chain 候选控制器。"""

from agent_harness.models._invocation_chain_approval import _ChainApprovalMixin
from agent_harness.models._invocation_chain_base import ModelApprovalGrantLike
from agent_harness.models._invocation_chain_completion import _ChainCompletionMixin
from agent_harness.models._invocation_chain_evidence import _ChainEvidenceMixin
from agent_harness.models._invocation_chain_routing import _ChainRoutingMixin
from agent_harness.models._invocation_chain_stream import _ChainStreamingMixin


class ModelRouteChainExecutionMixin(
    _ChainCompletionMixin,
    _ChainStreamingMixin,
    _ChainRoutingMixin,
    _ChainApprovalMixin,
    _ChainEvidenceMixin,
):
    """只推进 frozen ordinal；任何切换都来自 durable not-started proof。"""


__all__ = ["ModelApprovalGrantLike", "ModelRouteChainExecutionMixin"]
