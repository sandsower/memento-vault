"""Entry point for `python -m memento` — starts the MCP server.

Usage:
    python -m memento                         # stdio (default, local)
    python -m memento --transport streamable-http  # HTTP (remote/Docker)

Environment variables:
    MEMENTO_TRANSPORT: Transport protocol (stdio, sse, streamable-http)
    MEMENTO_HOST: Bind address for HTTP (default: 0.0.0.0)
    MEMENTO_PORT: Port for HTTP (default: 8745)
    MEMENTO_API_KEY: Bearer token for HTTP auth
"""

from memento.mcp_server import main

main()
