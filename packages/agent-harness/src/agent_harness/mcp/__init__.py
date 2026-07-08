"""MCP vendor-neutral seam。"""

from agent_harness.mcp.client import FakeMCPClient as FakeMCPClient
from agent_harness.mcp.client import MCPClient as MCPClient
from agent_harness.mcp.client import MCPToolDescriptor as MCPToolDescriptor

_MCP_EXPORTS = ["FakeMCPClient", "MCPClient", "MCPToolDescriptor"]

__all__ = [*_MCP_EXPORTS]  # pyright: ignore[reportUnsupportedDunderAll]
