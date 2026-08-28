"""mcp — Model Context Protocol: foreign tools as first-class citizens.

``config`` loads server definitions; ``client`` speaks JSON-RPC over the
``transports`` (stdio/http); ``bridge`` adapts remote tools into local
``FunctionTool``s — same schema, same dispatcher pipeline, same error
vocabulary, so a remote tool is indistinguishable from a local one until
the wire fails.
"""

from prodagent.mcp.bridge import adapt_mcp_tools, qualified_name
from prodagent.mcp.client import MCPClient, MCPToolInfo
from prodagent.mcp.config import MCPServerConfig, expand_env, load_mcp_servers
from prodagent.mcp.registry import MCPRegistry

__all__ = [
    "MCPClient",
    "MCPRegistry",
    "MCPToolInfo",
    "MCPServerConfig",
    "adapt_mcp_tools",
    "qualified_name",
    "expand_env",
    "load_mcp_servers",
]
