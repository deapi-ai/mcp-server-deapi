"""The 401 challenge must point MCP clients at the protected-resource metadata."""

import pytest

METADATA_PATHS = [
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
]


def test_unauthenticated_mcp_post_advertises_resource_metadata(app_client):
    response = app_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})

    assert response.status_code == 401
    assert (
        'resource_metadata="https://mcp.test/.well-known/oauth-protected-resource/mcp"'
        in response.headers["www-authenticate"]
    )


@pytest.mark.parametrize("path", METADATA_PATHS)
def test_protected_resource_metadata_unchanged(app_client, path):
    response = app_client.get(path)

    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "https://mcp.test/mcp"
    assert body["authorization_servers"] == ["https://mcp.test"]
    assert body["scopes_supported"] == []


@pytest.mark.parametrize("path", METADATA_PATHS)
def test_protected_resource_route_registered_once(app_client, path):
    paths = [getattr(route, "path", None) for route in app_client.app.routes]

    assert paths.count(path) == 1
