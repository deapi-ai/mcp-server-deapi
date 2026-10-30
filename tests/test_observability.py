from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp.exceptions import NotFoundError
from fastmcp.exceptions import ValidationError as FastMCPValidationError

from deapi_mcp.deapi_client import DeapiAPIError, DeapiClient
from deapi_mcp.observability import (
    CallOutcome,
    begin_call,
    classify,
    classify_api,
    classify_job,
    end_call,
)
from deapi_mcp.polling_manager import PollingManager
from deapi_mcp.schemas import JobStatusData, JobStatusResponse
from deapi_mcp.utils import parse_image_input


@pytest.fixture
def call():
    outcome, token = begin_call()
    yield outcome
    end_call(token)


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (401, None, "bad_key"),
        (422, "insufficient_balance", "no_balance"),
        (402, "key_limit_exceeded", "no_balance"),
        (429, None, "rate_limited"),
        (422, None, "invalid_input"),
        (404, None, "invalid_input"),
        (403, "insufficient_permissions", "invalid_input"),
        (500, None, "deapi_error"),
        (None, None, "deapi_error"),
    ],
)
def test_classify_api_table(status, code, expected):
    assert classify_api(status, code) == expected


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("SOURCE_PRIVATE", "job_rejected"),
        ("SOURCE_RESTRICTED", "job_rejected"),
        ("SOURCE_UNPROCESSABLE", "job_rejected"),
        ("INPUT_TOO_LARGE", "job_rejected"),
        ("CAPACITY_UNAVAILABLE", "job_failed"),
        ("TIMEOUT", "job_failed"),
        ("PROCESSING_FAILED", "job_failed"),
        ("SOURCE_DOWNLOAD_FAILED", "job_failed"),
        ("SOMETHING_NEW", "job_failed"),
        (None, "job_failed"),
    ],
)
def test_classify_job_table(reason, expected):
    assert classify_job(reason) == expected


def test_classify_result():
    empty = CallOutcome()
    assert classify({"success": True}, empty, None) == "ok"
    assert classify({"success": True, "done": False}, empty, None) == "pending"
    assert classify({"success": False}, empty, None) == "internal_error"
    both = CallOutcome(job_failed=True, job_error_reason="SOURCE_PRIVATE", api_status=500)
    assert classify({"success": False}, both, None) == "job_rejected"
    assert classify({"success": False}, CallOutcome(network_error=True), None) == "deapi_error"
    assert classify({"success": False}, CallOutcome(input_error=True), None) == "invalid_input"
    assert classify(None, empty, FastMCPValidationError("bad")) == "invalid_input"
    assert classify(None, empty, RuntimeError("boom")) == "internal_error"


def test_unknown_tool_is_the_callers_mistake():
    exc = NotFoundError("Unknown tool: 'no_such_tool'")
    assert classify(None, CallOutcome(), exc) == "invalid_input"


def test_deapi_error_notes_status_and_code(call):
    DeapiAPIError("x", 422, {"code": "insufficient_balance"})
    assert call.api_status == 422
    assert call.api_code == "insufficient_balance"


def test_noting_outside_a_call_is_harmless():
    DeapiAPIError("x", 500, None)


async def test_outcome_is_shared_with_tasks_created_after_begin_call(call):
    import asyncio

    await asyncio.create_task(asyncio.sleep(0, DeapiAPIError("x", 401, None)))
    assert call.api_status == 401


async def test_request_network_failure_noted(call, monkeypatch):
    import deapi_mcp.deapi_client as dc

    def boom(request):
        raise httpx.ConnectError("down")

    async def _noop(*args, **kwargs):
        return None

    failing = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    # Inject a failing shared transport; no-op the SDK's retry sleep so the test
    # is fast. A ConnectError counts as "never sent", so the SDK retries then
    # raises NetworkError, which _request notes as a transport failure.
    monkeypatch.setattr(dc, "_shared_http_client", lambda: failing)
    monkeypatch.setattr("deapi_sdk._client.asyncio.sleep", _noop)

    async with DeapiClient(api_token="t") as client:
        with pytest.raises(Exception):
            await client._request("GET", "balance")
    assert call.network_error is True
    await failing.aclose()


def test_input_error_is_value_error_and_noted(call):
    with pytest.raises(ValueError):
        parse_image_input("not base64!")
    assert call.input_error is True


async def test_poller_notes_job_failure_reason(call):
    client = AsyncMock()
    client.get_job_status = AsyncMock(
        return_value=JobStatusResponse(
            data=JobStatusData(status="error", error_reason="SOURCE_PRIVATE")
        )
    )
    result = await PollingManager(client, "image").poll_until_complete("job-1")
    assert result.success is False
    assert call.job_error_reason == "SOURCE_PRIVATE"
    assert call.job_failed is True


async def test_check_job_status_failed_notes_reason(call):
    client = AsyncMock()
    client.get_job_status = AsyncMock(
        return_value=JobStatusResponse(
            data=JobStatusData(status="error", error_reason="INPUT_TOO_LARGE")
        )
    )
    with patch("deapi_mcp.tools.utility.get_client", return_value=client):
        from deapi_mcp.tools.utility import check_job_status

        result = await check_job_status("job-1")
    assert result["success"] is False
    assert call.job_error_reason == "INPUT_TOO_LARGE"
    assert call.job_failed is True
