"""Account and API key tools: read the account, its usage and payments, and manage keys.

Every endpoint here except the current key's own details and usage needs an ADMIN key; an
API-role key gets 403, which the tools turn into a message with the dashboard link.

Creating, changing and deleting keys is behind ``DEAPI_KEY_MANAGEMENT_TOOLS`` (off by
default). Off, the three tools still exist but change nothing: they answer with the
dashboard link, so the assistant can tell the user where to do it. On, they act, and are
annotated as destructive so clients ask the user first. A created key's secret is shown
once, in that tool's result, so it passes through the conversation: turn the flag on only
where that is acceptable.
"""

from typing import Annotated, Any, Awaitable, Callable, Literal, Optional

from pydantic import Field

from ..config import settings
from ..deapi_client import DeapiAPIError, get_client

KEYS_DASHBOARD_URL = "https://app.deapi.ai/settings/api-keys"
USAGE_DASHBOARD_URL = "https://app.deapi.ai"

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$"
DateField = Annotated[
    Optional[str],
    Field(
        pattern=DATE_PATTERN,
        description="Start or end of the window, YYYY-MM-DD (or YYYY-MM-DDTHH:MM). Optional",
    ),
]
PageField = Annotated[Optional[int], Field(ge=1, description="Page number, from 1")]
LimitField = Annotated[Optional[int], Field(ge=1, le=100, description="Items per page (max 100)")]
KeyIdField = Annotated[int, Field(ge=1, description="The key's numeric id (see list_api_keys)")]

MANAGEMENT_OFF = (
    "Creating, changing and deleting API keys through MCP is turned off on this server. "
    "Do it in the deAPI dashboard."
)


def _error(exc: DeapiAPIError) -> dict[str, Any]:
    if exc.status_code == 403:
        return {
            "success": False,
            "error": (
                "This needs an ADMIN key. The key this connection uses is an API key, which can "
                "only read its own details and usage. Use the deAPI dashboard, or connect with "
                "an ADMIN key."
            ),
            "action_url": KEYS_DASHBOARD_URL,
        }
    if exc.status_code == 404:
        return {"success": False, "error": "Not found: no such key on this account."}
    return {"success": False, "error": f"API error: {exc}"}


def _usage_params(
    from_date: Optional[str], to_date: Optional[str], group_by: Optional[str]
) -> Optional[dict[str, Any]]:
    params = {"from": from_date, "to": to_date, "group_by": group_by}
    return {k: v for k, v in params.items() if v is not None} or None


def _page_params(page: Optional[int], limit: Optional[int]) -> Optional[dict[str, Any]]:
    params = {"page": page, "limit": limit}
    return {k: v for k, v in params.items() if v is not None} or None


async def _get(endpoint: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    client = get_client()
    async with client:
        return await client._request("GET", endpoint, params=params)


def _data(response: dict[str, Any]) -> Any:
    return response.get("data", response)


# --- reading ---------------------------------------------------------------------------------


async def get_account() -> dict[str, Any]:
    """Get the account's balance, currency and auto top-up settings. Needs an ADMIN key."""
    try:
        return {"success": True, "account": _data(await _get("account"))}
    except DeapiAPIError as e:
        return _error(e)


async def get_account_usage(
    from_date: DateField = None,
    to_date: DateField = None,
    group_by: Annotated[
        Optional[Literal["day", "hour", "type", "model", "key"]],
        Field(description="Split the figures by day, hour, job type, model or key. Optional"),
    ] = None,
) -> dict[str, Any]:
    """Get the whole account's cost, requests and errors over a time window (the Usage screen
    of the dashboard). With group_by=key, 'panel' means jobs from the dashboard itself and
    'deleted' all keys deleted since. Needs an ADMIN key."""
    try:
        response = await _get("account/usage", _usage_params(from_date, to_date, group_by))
        return {"success": True, "usage": response}
    except DeapiAPIError as e:
        return _error(e)


async def list_payments(page: PageField = None, limit: LimitField = None) -> dict[str, Any]:
    """List the account's balance top-ups, newest first. Needs an ADMIN key."""
    try:
        response = await _get("account/payments", _page_params(page, limit))
        return {"success": True, "payments": _data(response), "meta": response.get("meta")}
    except DeapiAPIError as e:
        return _error(e)


async def list_api_keys(page: PageField = None, limit: LimitField = None) -> dict[str, Any]:
    """List the account's API keys, newest first: name, preview, role, status, expiry, spend
    limit and its use, IP allowlist. Secrets are never returned. Needs an ADMIN key."""
    try:
        response = await _get("keys", _page_params(page, limit))
        return {"success": True, "keys": _data(response), "meta": response.get("meta")}
    except DeapiAPIError as e:
        return _error(e)


async def get_api_key(
    key_id: Annotated[
        Optional[int],
        Field(ge=1, description="The key's id; leave out for the key this connection uses"),
    ] = None,
) -> dict[str, Any]:
    """Get one API key's details (never its secret). Without key_id: the key this connection
    uses, which works with any key; another key needs an ADMIN key."""
    try:
        endpoint = "key" if key_id is None else f"keys/{key_id}"
        return {"success": True, "key": _data(await _get(endpoint))}
    except DeapiAPIError as e:
        return _error(e)


async def get_api_key_usage(
    key_id: Annotated[
        Optional[int],
        Field(ge=1, description="The key's id; leave out for the key this connection uses"),
    ] = None,
    from_date: DateField = None,
    to_date: DateField = None,
    group_by: Annotated[
        Optional[Literal["day", "hour", "type", "model"]],
        Field(description="Split the figures by day, hour, job type or model. Optional"),
    ] = None,
) -> dict[str, Any]:
    """Get one API key's cost, requests and errors over a time window. Without key_id: the
    key this connection uses, which works with any key; another key needs an ADMIN key."""
    try:
        endpoint = "key/usage" if key_id is None else f"keys/{key_id}/usage"
        response = await _get(endpoint, _usage_params(from_date, to_date, group_by))
        return {"success": True, "usage": response}
    except DeapiAPIError as e:
        return _error(e)


# --- managing (behind DEAPI_KEY_MANAGEMENT_TOOLS) ------------------------------------------------


def _management_off() -> Optional[dict[str, Any]]:
    if settings.key_management_tools:
        return None
    return {
        "success": False,
        "error": MANAGEMENT_OFF,
        "action_url": KEYS_DASHBOARD_URL,
        "changed": False,
    }


def _key_fields(
    expires_at: Optional[str],
    spend_limit: Optional[float],
    reset_interval: Optional[str],
    ip_whitelist: Optional[list[str]],
) -> dict[str, Any]:
    fields = {
        "expires_at": expires_at,
        "limit": spend_limit,
        "reset_interval": reset_interval,
        "ip_whitelist": ip_whitelist,
    }
    return {k: v for k, v in fields.items() if v is not None}


ExpiresField = Annotated[
    Optional[str],
    Field(pattern=DATE_PATTERN, description="When the key stops working, YYYY-MM-DD. Optional"),
]
SpendLimitField = Annotated[
    Optional[float],
    Field(ge=0, le=999999.999999, description="Spend cap in USD for each reset window. Optional"),
]
ResetField = Annotated[
    Optional[Literal["none", "daily", "weekly", "monthly"]],
    Field(description="How often the spend counter resets. Optional"),
]
IpField = Annotated[
    Optional[list[str]],
    Field(max_length=20, description="IP addresses or CIDR ranges allowed to use the key"),
]


async def create_api_key(
    name: Annotated[str, Field(min_length=1, max_length=255, description="A name for the key")],
    expires_at: ExpiresField = None,
    spend_limit: SpendLimitField = None,
    reset_interval: ResetField = None,
    ip_whitelist: IpField = None,
) -> dict[str, Any]:
    """Create a new API key on the account. Its secret is returned ONCE, in this result: tell
    the user to store it now. Needs an ADMIN key. If key management is turned off on this
    server, nothing is created and the result gives the dashboard link instead."""
    off = _management_off()
    if off:
        return off
    try:
        client = get_client()
        async with client:
            response = await client._request(
                "POST",
                "keys",
                json_data={
                    "name": name,
                    **_key_fields(expires_at, spend_limit, reset_interval, ip_whitelist),
                },
            )
        return {
            "success": True,
            "key": _data(response),
            "note": "The secret is shown only once. Store it now; it cannot be read again.",
        }
    except DeapiAPIError as e:
        return _error(e)


async def update_api_key(
    key_id: KeyIdField,
    active: Annotated[
        Optional[bool], Field(description="False disables the key, True enables it again")
    ] = None,
    expires_at: ExpiresField = None,
    spend_limit: SpendLimitField = None,
    reset_interval: ResetField = None,
    ip_whitelist: IpField = None,
) -> dict[str, Any]:
    """Change an API key: disable or re-enable it, or change its expiry, spend limit, reset
    window or IP allowlist. Only the given fields change. ADMIN keys can be changed only in
    the dashboard. Needs an ADMIN key. If key management is turned off on this server,
    nothing changes and the result gives the dashboard link instead."""
    off = _management_off()
    if off:
        return off
    body = _key_fields(expires_at, spend_limit, reset_interval, ip_whitelist)
    if active is not None:
        body["active"] = active
    if not body:
        return {"success": False, "error": "Nothing to change: give at least one field."}
    try:
        client = get_client()
        async with client:
            response = await client._request("PATCH", f"keys/{key_id}", json_data=body)
        return {"success": True, "key": _data(response)}
    except DeapiAPIError as e:
        return _error(e)


async def delete_api_key(key_id: KeyIdField) -> dict[str, Any]:
    """Permanently delete an API key: it stops working at once. This cannot be undone. ADMIN
    keys can be deleted only in the dashboard. Needs an ADMIN key. If key management is
    turned off on this server, nothing is deleted and the result gives the dashboard link
    instead."""
    off = _management_off()
    if off:
        return off
    try:
        client = get_client()
        async with client:
            await client._request("DELETE", f"keys/{key_id}")
        return {"success": True, "deleted": key_id}
    except DeapiAPIError as e:
        return _error(e)


Tool = Callable[..., Awaitable[dict[str, Any]]]

READ_TOOLS: tuple[Tool, ...] = (
    get_account,
    get_account_usage,
    list_payments,
    list_api_keys,
    get_api_key,
    get_api_key_usage,
)
MANAGEMENT_TOOLS: tuple[Tool, ...] = (create_api_key, update_api_key, delete_api_key)
