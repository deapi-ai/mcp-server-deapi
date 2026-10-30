"""The MCP transport is stateless: no session is created, so any replica can serve any request."""


def test_initialize_returns_no_session_id(app_client, auth_headers):
    r = app_client.post(
        "/mcp",
        headers=auth_headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )
    assert r.status_code == 200
    assert "mcp-session-id" not in {k.lower() for k in r.headers}


def test_tools_list_without_session_header_succeeds(app_client, auth_headers):
    r = app_client.post(
        "/mcp",
        headers={**auth_headers, "MCP-Protocol-Version": "2025-06-18"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert r.status_code == 200
    assert "text_to_image" in r.text


def test_get_mcp_is_405(app_client, auth_headers):
    assert app_client.get("/mcp", headers=auth_headers).status_code == 405
