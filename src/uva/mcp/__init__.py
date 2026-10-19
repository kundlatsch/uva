"""MCP (Model Context Protocol): tools from MCP servers as UVA actions.

The servers run in the isolated ``mcp`` gateway container (:mod:`uva.mcp.gateway`);
the bot and the worker only talk to the gateway over HTTP (:mod:`uva.mcp.client`)
and turn its catalog into actions (:mod:`uva.mcp.catalog`). See the MCP section of docs/DOCUMENTATION.md.
"""
