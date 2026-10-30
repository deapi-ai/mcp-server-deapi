"""deapi-mcp: the importable, SDK-consuming core of the deAPI MCP server.

The package is importable: a deployment can build its own server from this core
and layer a metrics/observability overlay on top via
``create_app(server, metrics=...)``. The stock server injects no overlay.

Entry points (see :mod:`deapi_mcp.server_remote`):
    - ``register_core_tools(server)`` — register every deAPI tool on a FastMCP instance
    - ``create_app(server=None, *, metrics=None)`` — build the Starlette app; ``metrics``
      is an optional overlay the core never requires
    - ``build_app()`` — the stock server app: the core with no overlay
    - ``main()`` — uvicorn entry point

The import package is ``deapi_mcp`` and the distribution is ``deapi-mcp``.
"""
