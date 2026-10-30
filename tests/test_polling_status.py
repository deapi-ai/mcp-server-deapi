"""Polling status reaches the client as progress, never through the logging capability.

The MCP logging capability (`notifications/message`, `ctx.info` / `ctx.error`) is
deprecated as of the 2026-07-28 protocol revision (SEP-2577), and on 2026-07-28
connections a client only receives it after opting in per request. Progress
notifications are not deprecated, so the poller puts its status line in their
`message` and keeps everything else in the server's own logger.
"""

import logging
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

from fastmcp import Client, FastMCP

from deapi_mcp.deapi_client import DeapiAPIError
from deapi_mcp.polling_manager import PollingManager

from .test_long_jobs import fast_settings, status

LOGGING_METHODS = ("debug", "info", "notice", "warning", "error", "critical", "log")


def make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    for name in LOGGING_METHODS:
        setattr(ctx, name, AsyncMock())
    return ctx


def assert_no_client_logging(ctx: MagicMock) -> None:
    for name in LOGGING_METHODS:
        method = getattr(ctx, name)
        assert method.await_count == 0, f"ctx.{name} was awaited: {method.await_args_list}"


async def poll(client, ctx, job_id="job-1", max_wait=10):
    with (
        patch("deapi_mcp.polling_manager.settings") as settings,
        patch("deapi_mcp.polling_manager._current_context", return_value=ctx),
    ):
        fast_settings(settings, max_wait=max_wait)
        return await PollingManager(client, "image").poll_until_complete(job_id)


class TestStatusViaProgress:
    async def test_status_lines_ride_progress_notifications(self):
        client = AsyncMock()
        client.get_job_status = AsyncMock(
            side_effect=[
                status("pending"),
                status("processing", progress=50.0),
                status("done", result_url="https://r/i.png"),
            ]
        )
        ctx = make_ctx()

        result = await poll(client, ctx)

        assert result.success is True
        assert_no_client_logging(ctx)
        calls = [c.kwargs for c in ctx.report_progress.await_args_list]
        assert [c["progress"] for c in calls] == [0.0, 50.0, 100.0]
        assert all(c["total"] == 100.0 for c in calls)
        assert "job-1 submitted" in calls[0]["message"]
        assert "processing" in calls[1]["message"] and "50%" in calls[1]["message"]
        assert "completed" in calls[2]["message"]

    async def test_progress_never_repeats_or_goes_back(self):
        """Progress must rise between notifications; an unchanged value is not resent."""
        client = AsyncMock()
        client.get_job_status = AsyncMock(
            side_effect=[
                status("processing", progress=0.0),
                status("processing", progress=30.0),
                status("processing", progress=30.0),
                status("done", progress=100.0, result_url="u"),
            ]
        )
        ctx = make_ctx()

        await poll(client, ctx)

        sent = [c.kwargs["progress"] for c in ctx.report_progress.await_args_list]
        assert sent == [0.0, 30.0, 100.0]

    async def test_still_running_job_is_logged_server_side_only(self, caplog):
        client = AsyncMock()
        client.get_job_status = AsyncMock(return_value=status("processing"))
        ctx = make_ctx()

        with caplog.at_level(logging.DEBUG, logger="deapi_mcp.polling_manager"):
            result = await poll(client, ctx, max_wait=0)

        assert result.in_progress is True
        assert_no_client_logging(ctx)
        assert "still running" in caplog.text

    async def test_undeliverable_progress_does_not_fail_the_job(self):
        client = AsyncMock()
        client.get_job_status = AsyncMock(return_value=status("done", result_url="u"))
        ctx = make_ctx()
        ctx.report_progress = AsyncMock(side_effect=RuntimeError("client went away"))

        result = await poll(client, ctx)

        assert result.success is True and result.result_url == "u"


class TestErrorsStayServerSide:
    async def test_failed_job(self, caplog):
        client = AsyncMock()
        client.get_job_status = AsyncMock(
            return_value=status("error", error_reason="NSFW content", error_code="E42")
        )
        ctx = make_ctx()

        with caplog.at_level(logging.INFO, logger="deapi_mcp.polling_manager"):
            result = await poll(client, ctx)

        assert result.success is False
        assert result.error == "NSFW content (E42)"
        assert_no_client_logging(ctx)
        assert "NSFW content (E42)" in caplog.text

    async def test_api_errors_and_retries(self, caplog):
        client = AsyncMock()
        client.get_job_status = AsyncMock(side_effect=DeapiAPIError("boom", status_code=500))
        ctx = make_ctx()

        with caplog.at_level(logging.INFO, logger="deapi_mcp.polling_manager"):
            result = await poll(client, ctx)

        assert result.success is False and result.job_id == "job-1"
        assert_no_client_logging(ctx)
        assert "Retrying" in caplog.text and "boom" in caplog.text

    async def test_unreadable_status(self, caplog):
        client = AsyncMock()
        client.get_job_status = AsyncMock(side_effect=ValueError("unexpected payload"))
        ctx = make_ctx()

        with caplog.at_level(logging.WARNING, logger="deapi_mcp.polling_manager"):
            result = await poll(client, ctx)

        assert result.success is False
        assert "check_job_status" in result.error
        assert_no_client_logging(ctx)
        assert "Could not read the status of job job-1" in caplog.text


class TestOverTheWire:
    """A real FastMCP server and client: what the client receives, and no deprecation."""

    async def test_client_gets_progress_messages_and_no_log_messages(self):
        api = AsyncMock()
        api.get_job_status = AsyncMock(
            side_effect=[
                status("processing", progress=40.0),
                status("done", result_url="https://r/i.png"),
            ]
        )
        mcp = FastMCP("polling-test")

        async def generate() -> str:
            # No ctx argument: the poller takes the request's context from FastMCP.
            result = await PollingManager(api, "image").poll_until_complete("job-7")
            return result.result_url or ""

        mcp.tool()(generate)

        progress: list[tuple[float, float | None, str | None]] = []
        log_messages: list[object] = []

        async def on_progress(value: float, total: float | None, message: str | None) -> None:
            progress.append((value, total, message))

        async def on_log(message: object) -> None:
            log_messages.append(message)

        with (
            patch("deapi_mcp.polling_manager.settings") as settings,
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            fast_settings(settings, max_wait=10)
            async with Client(mcp, progress_handler=on_progress, log_handler=on_log) as client:
                result = await client.call_tool("generate", {})

        assert result.data == "https://r/i.png"
        assert [p[0] for p in progress] == [0.0, 40.0, 100.0]
        assert all(p[2] for p in progress), "every progress notification carries a status line"
        assert log_messages == []
        deprecations = [str(w.message) for w in caught if "logging capability" in str(w.message)]
        assert deprecations == []
