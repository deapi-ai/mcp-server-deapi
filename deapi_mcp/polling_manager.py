"""Smart adaptive polling manager for async job completion."""

import asyncio
import logging
import time
from typing import Optional

from fastmcp import Context

from .config import PollingConfig, settings
from .deapi_client import DeapiAPIError, DeapiClient
from .observability import note_job_failure
from .schemas import JobStatus, JobStatusResponse, ToolResult
from .webhooks import registry

logger = logging.getLogger(__name__)


def _current_context() -> Optional[Context]:
    """The MCP context of the tool call being served, when there is one.

    Taken from FastMCP rather than threaded through every tool's signature, so
    each wait reports progress to the client (and to clients that reset their
    call timeout on progress, keeps the call alive). Outside a request — tests,
    scripts — there is none and the poller runs silently, as before.

    The context is used for progress notifications only. Status lines go to the
    server's own logger, not to the client: the MCP logging capability
    (``ctx.info`` and friends, ``notifications/message``) is deprecated as of
    the 2026-07-28 protocol revision.
    """
    try:
        from fastmcp.server.dependencies import get_context

        return get_context()
    except Exception:
        return None


class PollingManager:
    """Manages adaptive polling for async job completion."""

    def __init__(self, client: DeapiClient, job_type: str, max_wait: Optional[float] = None):
        """Initialize polling manager.

        Args:
            client: Initialized DeapiClient instance
            job_type: Type of job for adaptive polling config (audio, image, video, etc.)
            max_wait: Longest this call waits, in seconds. Defaults to
                settings.tool_max_wait; the job type's own `timeout` caps it too.
                Reaching it is not a failure: the job keeps running and the
                result says so (`in_progress`), with the job_id to resume from.
        """
        self.client = client
        self.job_type = job_type
        self.config: PollingConfig = settings.get_polling_config(job_type)
        self.max_wait = min(
            max_wait if max_wait is not None else settings.tool_max_wait, self.config.timeout
        )

    async def _wait_before_next_poll(self, job_id: str, delay: float, remaining: float) -> None:
        """Pause between status checks.

        For a job the API will call us back about, block on that callback with
        the fallback interval as a cap, so a completed job is noticed at once
        and a lost callback still gets polled. Otherwise sleep the adaptive
        delay as before.
        """
        # Never sleep past the end of this call's wait: the last status check
        # should land just before it, not one long backoff after.
        remaining = max(remaining, 0.0)
        if not registry.is_tracked(job_id):
            await asyncio.sleep(min(delay, remaining))
            return

        event = registry.track(job_id)
        try:
            await asyncio.wait_for(
                event.wait(), timeout=min(settings.webhook_fallback_poll_interval, remaining)
            )
        except asyncio.TimeoutError:
            return  # safety-net poll
        # Woken by the callback. Clear so a non-terminal wake (should not happen,
        # but a replayed delivery could cause one) does not spin the loop.
        event.clear()

    @staticmethod
    async def _report(
        ctx: Optional[Context],
        progress: float,
        last_reported: Optional[float],
        message: str,
    ) -> Optional[float]:
        """Send a progress notification, carrying ``message`` as the status line.

        Progress must rise from one notification to the next, so a value that
        is not above the last one sent is skipped. Returns the last value sent.
        Best effort: a notification that cannot be delivered must not turn a
        job's outcome into an error.
        """
        if ctx is None or (last_reported is not None and progress <= last_reported):
            return last_reported
        try:
            await ctx.report_progress(progress=progress, total=100.0, message=message)
        except Exception:
            logger.debug("Could not send a progress notification", exc_info=True)
            return last_reported
        return progress

    def _calculate_next_delay(self, current_delay: float, attempt: int) -> float:
        """Calculate next polling delay with exponential backoff.

        Args:
            current_delay: Current delay in seconds
            attempt: Current attempt number

        Returns:
            Next delay in seconds, capped at max_delay
        """
        next_delay = current_delay * self.config.backoff_factor
        return min(next_delay, self.config.max_delay)

    async def poll_until_complete(
        self,
        job_id: str,
        ctx: Optional[Context] = None,
    ) -> ToolResult:
        """Poll job status until completion or timeout.

        Args:
            job_id: Job request ID to poll
            ctx: Optional MCP context for progress reporting

        Returns:
            ToolResult with final job status and result. Never raises for a job
            that was submitted: a timeout, a 404, repeated API errors or a status
            response this client cannot read all come back as a failed ToolResult
            that still carries ``job_id``. The job may well finish anyway (and has
            been paid for), and ``check_job_status`` can still reach it — but only
            if the caller was told the id.
        """
        ctx = ctx or _current_context()
        result: Optional[ToolResult] = None
        try:
            result = await self._poll_until_complete(job_id, ctx)
            return result
        finally:
            # Once the job is over nobody will listen for it again. A job still
            # running stays tracked, so a later check_job_status(wait_seconds)
            # wakes on its callback too; the registry's TTL sweep bounds it.
            if result is None or not result.in_progress:
                registry.forget(job_id)

    async def _poll_until_complete(
        self,
        job_id: str,
        ctx: Optional[Context],
    ) -> ToolResult:
        start_time = time.time()
        current_delay = self.config.initial_delay
        attempt = 0
        last_progress = None
        last_status: Optional[JobStatus] = None
        reported: Optional[float] = None  # last progress value sent to the client

        logger.debug("Polling %s job %s", self.job_type, job_id)
        reported = await self._report(
            ctx, 0.0, reported, f"Job {job_id} submitted, waiting for the {self.job_type} job"
        )

        while True:
            attempt += 1
            elapsed = time.time() - start_time

            # This call has waited long enough. The job has not failed — it is
            # still running, paid for, and reachable by its id — so say exactly
            # that instead of reporting an error.
            if attempt > 1 and elapsed >= self.max_wait:
                logger.debug("Job %s still running after %.0fs", job_id, elapsed)
                return ToolResult(
                    success=True,
                    in_progress=True,
                    job_id=job_id,
                    status=last_status or JobStatus.PROCESSING,
                    progress=last_progress,
                    metadata={"elapsed_time": elapsed, "attempts": attempt - 1},
                )

            # Poll job status
            try:
                status_response: JobStatusResponse = await self.client.get_job_status(job_id)
                status_data = status_response.data
                current_status = status_data.status
                last_status = current_status

                # Report progress, with the status as its message, when it rose
                if status_data.progress is not None:
                    reported = await self._report(
                        ctx,
                        status_data.progress,
                        reported,
                        f"Job {job_id} {current_status.value}: "
                        f"{status_data.progress:.0f}% after {elapsed:.0f}s",
                    )
                    last_progress = status_data.progress

                if attempt % 5 == 0:  # every 5th attempt, to avoid spam
                    logger.debug(
                        "Job %s status: %s (elapsed: %.1fs, attempt: %d)",
                        job_id,
                        current_status.value,
                        elapsed,
                        attempt,
                    )

                # Check if job is complete
                if current_status == JobStatus.DONE:
                    logger.debug("Job %s completed after %.1fs", job_id, elapsed)
                    await self._report(
                        ctx, 100.0, reported, f"Job {job_id} completed after {elapsed:.0f}s"
                    )

                    return ToolResult(
                        success=True,
                        job_id=job_id,
                        status=current_status,
                        result=status_data.result,
                        result_url=status_data.result_url,
                        price=status_data.price,
                        results_alt_formats=status_data.results_alt_formats,
                        prompt_boost=status_data.prompt_boost,
                        # The API's own metadata (title, channel, views) when the job
                        # asked for it with include_metadata; ours goes in `metadata`.
                        source_metadata=status_data.metadata,
                        metadata={
                            "elapsed_time": elapsed,
                            "attempts": attempt,
                        },
                    )

                elif current_status.is_failure:
                    note_job_failure(status_data.error_reason)
                    # Prefer the API's own reason. Falling back to a generic message
                    # would discard the only information the caller can act on.
                    error_msg = status_data.error_reason or f"Job {job_id} failed"
                    if status_data.error_code:
                        error_msg = f"{error_msg} ({status_data.error_code})"
                    # The tool result carries the error to the client.
                    logger.info("Job %s failed: %s", job_id, error_msg)

                    return ToolResult(
                        success=False,
                        job_id=job_id,
                        status=current_status,
                        error=error_msg,
                        error_code=status_data.error_code,
                        retryable=status_data.retryable,
                        refunded=status_data.refunded,
                        metadata={
                            "elapsed_time": elapsed,
                            "attempts": attempt,
                        },
                    )

                # Job still in progress, wait before next poll
                if status_data.progress is not None:
                    last_progress = status_data.progress
                remaining = self.max_wait - (time.time() - start_time)
                await self._wait_before_next_poll(job_id, current_delay, remaining)
                current_delay = self._calculate_next_delay(current_delay, attempt)

            except DeapiAPIError as e:
                # API error during status check
                logger.warning("Error checking job %s status: %s", job_id, e)

                # If it's a 404, the job might not exist
                if e.status_code == 404:
                    return ToolResult(
                        success=False,
                        job_id=job_id,
                        error="Job not found",
                        metadata={"elapsed_time": elapsed, "attempts": attempt},
                    )

                # For other errors, retry with delay
                if attempt < 3:  # Retry a few times for transient errors
                    logger.info("Retrying job %s status (attempt %d/3)", job_id, attempt + 1)
                    await asyncio.sleep(current_delay)
                    current_delay = self._calculate_next_delay(current_delay, attempt)
                else:
                    # Too many errors, give up
                    return ToolResult(
                        success=False,
                        job_id=job_id,
                        error=str(e),
                        metadata={"elapsed_time": elapsed, "attempts": attempt},
                    )

            except Exception as e:
                # Anything else is a defect on this side of the wire — typically a
                # status payload the schema does not model yet. Retrying cannot
                # help, and letting it propagate would turn a paid, possibly
                # finished job into an anonymous "Unexpected error".
                error_msg = (
                    f"Could not read the status of job {job_id}: {e}. "
                    "The job may still complete; query it with check_job_status."
                )
                logger.warning("Could not read the status of job %s", job_id, exc_info=True)
                return ToolResult(
                    success=False,
                    job_id=job_id,
                    error=error_msg,
                    metadata={"elapsed_time": elapsed, "attempts": attempt},
                )
