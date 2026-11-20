"""Account and API key tools, and the switch that lets MCP change keys."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deapi_mcp.config import settings
from deapi_mcp.deapi_client import DeapiAPIError
from deapi_mcp.tools import account
from deapi_mcp.tools.account import KEYS_DASHBOARD_URL


@pytest.fixture
def api():
    """A stand-in DeapiClient whose _request the test scripts."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client._request = AsyncMock(return_value={"data": {"ok": True}})
    with patch("deapi_mcp.tools.account.get_client", return_value=client):
        yield client


@pytest.fixture
def managing(monkeypatch):
    monkeypatch.setattr(settings, "key_management_tools", True)


# --- reading ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call, endpoint, params",
    [
        (lambda: account.get_account(), "account", None),
        (
            lambda: account.get_account_usage(from_date="2026-11-01", group_by="model"),
            "account/usage",
            {"from": "2026-11-01", "group_by": "model"},
        ),
        (
            lambda: account.list_payments(page=2, limit=10),
            "account/payments",
            {"page": 2, "limit": 10},
        ),
        (lambda: account.list_api_keys(), "keys", None),
        (lambda: account.get_api_key(), "key", None),
        (lambda: account.get_api_key(key_id=7), "keys/7", None),
        (lambda: account.get_api_key_usage(), "key/usage", None),
        (
            lambda: account.get_api_key_usage(key_id=7, to_date="2026-11-20"),
            "keys/7/usage",
            {"to": "2026-11-20"},
        ),
    ],
)
async def test_reads_call_the_right_endpoint(api, call, endpoint, params):
    result = await call()
    assert result["success"] is True
    api._request.assert_awaited_once_with("GET", endpoint, params=params)


async def test_an_api_key_gets_a_clear_admin_message_and_the_link(api):
    api._request.side_effect = DeapiAPIError("Forbidden", status_code=403)
    result = await account.list_api_keys()
    assert result["success"] is False
    assert "ADMIN key" in result["error"]
    assert result["action_url"] == KEYS_DASHBOARD_URL


async def test_unknown_key_is_not_found(api):
    api._request.side_effect = DeapiAPIError("Not Found", status_code=404)
    assert "Not found" in (await account.get_api_key(key_id=99))["error"]


# --- managing, switched off (the default) ----------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: account.create_api_key(name="Cursor"),
        lambda: account.update_api_key(key_id=3, active=False),
        lambda: account.delete_api_key(key_id=3),
    ],
    ids=["create", "update", "delete"],
)
async def test_management_off_changes_nothing_and_gives_the_link(api, call):
    assert settings.key_management_tools is False
    result = await call()
    assert result == {
        "success": False,
        "error": account.MANAGEMENT_OFF,
        "action_url": KEYS_DASHBOARD_URL,
        "changed": False,
    }
    api._request.assert_not_awaited()


# --- managing, switched on --------------------------------------------------------------------


async def test_create_sends_only_the_given_fields_and_warns_about_the_secret(api, managing):
    api._request.return_value = {"data": {"id": 9, "name": "Cursor", "secret": "s3cr3t"}}
    result = await account.create_api_key(name="Cursor", spend_limit=5.0, reset_interval="daily")
    api._request.assert_awaited_once_with(
        "POST", "keys", json_data={"name": "Cursor", "limit": 5.0, "reset_interval": "daily"}
    )
    assert result["success"] is True and result["key"]["secret"] == "s3cr3t"
    assert "only once" in result["note"]


async def test_update_sends_a_patch_and_needs_a_field(api, managing):
    await account.update_api_key(key_id=3, active=False)
    api._request.assert_awaited_once_with("PATCH", "keys/3", json_data={"active": False})
    api._request.reset_mock()
    result = await account.update_api_key(key_id=3)
    assert result["success"] is False
    api._request.assert_not_awaited()


async def test_delete_sends_a_delete(api, managing):
    result = await account.delete_api_key(key_id=3)
    api._request.assert_awaited_once_with("DELETE", "keys/3")
    assert result == {"success": True, "deleted": 3}


# --- registration -----------------------------------------------------------------------------


def _tools():
    from deapi_mcp.server_remote import mcp

    return {t.name: t for t in asyncio.run(mcp.list_tools(run_middleware=False))}


def test_tools_are_registered_and_annotated():
    tools = _tools()
    for name in (
        "get_account",
        "get_account_usage",
        "list_payments",
        "list_api_keys",
        "get_api_key",
        "get_api_key_usage",
    ):
        assert tools[name].annotations.read_only_hint is True, name
    # The module's server is built with the switch off: management tools only point to the
    # dashboard, so they are read-only and clients need not ask before calling them
    for name in ("create_api_key", "update_api_key", "delete_api_key"):
        assert tools[name].annotations.read_only_hint is True, name
        assert tools[name].annotations.destructive_hint is False, name


def test_switched_on_the_management_tools_are_destructive(monkeypatch):
    from fastmcp import FastMCP

    from deapi_mcp.server_remote import register_core_tools

    monkeypatch.setattr(settings, "key_management_tools", True)
    server = FastMCP(name="t")
    register_core_tools(server)
    tools = {t.name: t for t in asyncio.run(server.list_tools(run_middleware=False))}
    for name in ("create_api_key", "update_api_key", "delete_api_key"):
        assert tools[name].annotations.destructive_hint is True, name
        assert tools[name].annotations.read_only_hint is False, name
    assert tools["create_api_key"].annotations.idempotent_hint is False
    assert tools["delete_api_key"].annotations.idempotent_hint is True


def test_date_and_id_parameters_are_validated_in_the_schema():
    tools = _tools()
    usage = tools["get_account_usage"].parameters["properties"]["from_date"]
    assert any("pattern" in o for o in usage.get("anyOf", [usage]))
    key_id = tools["delete_api_key"].parameters["properties"]["key_id"]
    assert key_id.get("minimum") == 1
