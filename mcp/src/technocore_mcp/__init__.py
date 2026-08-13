"""MCP front end for technocore-chat. See server.py for the tools, protocol.py for the wire.

Deliberately thin: importing `technocore_mcp.server` must keep meaning the module, so the
`Server` instance living inside it is not re-exported here to shadow it.
"""

from .server import VERSION, main

__all__ = ["VERSION", "main"]
