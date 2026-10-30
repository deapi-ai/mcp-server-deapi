"""Jobs that outlive one tool call.

A job can run for an hour, but a tool call cannot stay open that long (Cloudflare
ends a response it waited ~100s for with a 524). So a call waits at most
`tool_max_wait`, hands back the running job, and check_job_status(job_id,
wait_seconds) resumes the wait.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from deapi_mcp.config import PollingConfig
from deapi_mcp.polling_manager import PollingManager
from deapi_mcp.schemas import JobStatusData, JobStatusResponse, ToolResult
from deapi_mcp.tools._responses import build_job_response
from deapi_mcp.webhooks import registry


def status(state, **extra):
    return JobStatusResponse(data=JobStatusData(status=state, **extra))


def fast_settings(settings, max_wait, timeout=900.0, initial_delay=0.0):
    settings.get_polling_config.return_value = PollingConfig(
        initial_delay=initial_delay, max_delay=initial_delay, timeout=timeout
    )
    settings.tool_max_wait = max_wait
    settings.webhook_fallback_poll_interval = 45.0


class TestPollerBudget:
    async def test_job_type_timeout_still_caps_the_wait(self):
        with patch("deapi_mcp.polling_manager.settings") as settings:
            fast_settings(settings, max_wait=85, timeout=60)
            assert PollingManager(AsyncMock(), "audio").max_wait == 60
            assert PollingManager(AsyncMock(), "audio", max_wait=30).max_wait == 30

    async def test_never_sleeps_past_the_end_of_the_wait(self):
        """A 30s backoff must not carry the call 29s beyond its budget."""
        client = AsyncMock()
        client.get_job_status = AsyncMock(return_value=status("processing"))
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with (
            patch("deapi_mcp.polling_manager.settings") as settings,
            patch("deapi_mcp.polling_manager.asyncio.sleep", side_effect=fake_sleep),
        ):
            fast_settings(settings, max_wait=0.5, initial_delay=30.0)
            result = await PollingManager(client, "video").poll_until_complete("job-1")

        assert result.in_progress is True
        assert slept and max(slept) <= 0.5

    async def test_a_running_job_stays_tracked_for_its_webhook(self):
        """A later check_job_status(wait_seconds) should still wake on the callback."""
        client = AsyncMock()
        client.get_job_status = AsyncMock(return_value=status("processing"))
        registry.track("job-hook")
        try:
            with patch("deapi_mcp.polling_manager.settings") as settings:
                fast_settings(settings, max_wait=0)
                await PollingManager(client, "video").poll_until_complete("job-hook")
            assert registry.is_tracked("job-hook")

            client.get_job_status = AsyncMock(
                return_value=status("done", result_url="https://r/v.mp4")
            )
            with patch("deapi_mcp.polling_manager.settings") as settings:
                fast_settings(settings, max_wait=10)
                done = await PollingManager(client, "video").poll_until_complete("job-hook")
            assert done.success is True and not done.in_progress
            assert not registry.is_tracked("job-hook")
        finally:
            registry.forget("job-hook")

    async def test_progress_reaches_the_client_without_a_ctx_argument(self):
        """Tools do not pass ctx; the poller takes the request's context from FastMCP."""
        client = AsyncMock()
        client.get_job_status = AsyncMock(
            side_effect=[status("processing", progress=50.0), status("done", result_url="u")]
        )
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        with (
            patch("deapi_mcp.polling_manager.settings") as settings,
            patch("deapi_mcp.polling_manager._current_context", return_value=ctx),
        ):
            fast_settings(settings, max_wait=10)
            await PollingManager(client, "image").poll_until_complete("job-2")

        sent = [c.kwargs["progress"] for c in ctx.report_progress.await_args_list]
        assert 50.0 in sent


class TestInProgressAnswer:
    def test_says_how_to_resume_and_not_to_resubmit(self):
        result = ToolResult(success=True, in_progress=True, job_id="job-3", progress=12.5)
        with patch("deapi_mcp.config.settings") as settings:
            settings.tool_max_wait = 85
            out = build_job_response(result, "job-3")

        assert out["success"] is True and out["done"] is False
        assert out["job_id"] == "job-3"
        assert out["progress"] == 12.5
        assert "check_job_status(job_id='job-3', wait_seconds=85)" in out["message"]
        assert "result_url" not in out

    async def test_a_generation_tool_hands_back_its_running_job(self):
        from deapi_mcp.tools.video import video_description

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.submit_job = AsyncMock(return_value=MagicMock(data=MagicMock(request_id="job-4")))
        polling = MagicMock()
        polling.poll_until_complete = AsyncMock(
            return_value=ToolResult(success=True, in_progress=True, job_id="job-4")
        )
        with (
            patch("deapi_mcp.tools.video.get_client", return_value=client),
            patch("deapi_mcp.tools.video.PollingManager", return_value=polling),
        ):
            out = await video_description(model="Marlin_2B", video_url="https://youtu.be/x")

        assert out["done"] is False and out["job_id"] == "job-4"


class TestCheckJobStatusWait:
    def _client(self, *statuses):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get_job_status = AsyncMock(side_effect=list(statuses))
        return client

    async def test_waits_and_returns_the_finished_result_like_the_tool_would(self):
        from deapi_mcp.tools.utility import check_job_status

        transcript = json.dumps({"text": "Hello", "segments": [{"start": 0, "end": 1}]})
        client = self._client(
            status("processing"), status("done", result=transcript, result_url="https://r/t")
        )
        with (
            patch("deapi_mcp.tools.utility.get_client", return_value=client),
            patch("deapi_mcp.polling_manager.settings") as settings,
        ):
            fast_settings(settings, max_wait=85)
            out = await check_job_status("job-5", wait_seconds=60)

        assert out["done"] is True and out["status"] == "done"
        assert out["result"] == "Hello"
        assert out["segments"] == [{"start": 0, "end": 1}]
        assert out["result_url"] == "https://r/t"

    async def test_wait_is_capped_by_the_server(self):
        from deapi_mcp.tools.utility import check_job_status

        client = self._client(status("done", result_url="u"))
        with (
            patch("deapi_mcp.tools.utility.get_client", return_value=client),
            patch("deapi_mcp.tools.utility.settings") as settings,
            patch("deapi_mcp.tools.utility.PollingManager") as polling_cls,
        ):
            settings.tool_max_wait = 85.0
            polling_cls.return_value.poll_until_complete = AsyncMock(
                return_value=ToolResult(success=True, result_url="u")
            )
            await check_job_status("job-6", wait_seconds=3600)

        assert polling_cls.call_args.kwargs["max_wait"] == 85.0

    async def test_without_wait_it_is_still_a_snapshot(self):
        from deapi_mcp.tools.utility import check_job_status

        client = self._client(status("processing", progress=30.0))
        with patch("deapi_mcp.tools.utility.get_client", return_value=client):
            out = await check_job_status("job-7")

        assert out["status"] == "processing" and out["progress"] == 30.0
        assert "done" not in out
        client.get_job_status.assert_awaited_once()
