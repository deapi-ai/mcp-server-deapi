"""Smart adaptive polling manager for async job completion."""

import asyncio
import time
from typing import Optional

from fastmcp import Context

from .config import PollingConfig, settings
from .deapi_client import DeapiAPIError, DeapiClient
from .observability import note_job_failure
from .schemas import JobStatus, JobStatusResponse, ToolResult
from .webhooks import registry


def _current_context() -> Optional[Context]:
    """The MCP context of the tool call being served, when there is one.

    Taken from FastMCP rather than threaded through every tool's signature, so
    each wait reports progress to the client (and to clients that reset their
    call timeout on progress, keeps the call alive). Outside a request — tests,
    scripts — there is none and the poller runs silently, as before.
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

        if ctx:
            await ctx.info(f"Job {job_id} submitted. Starting polling for {self.job_type} job...")

        while True:
            attempt += 1
            elapsed = time.time() - start_time

            # This call has waited long enough. The job has not failed — it is
            # still running, paid for, and reachable by its id — so say exactly
            # that instead of reporting an error.
            if attempt > 1 and elapsed >= self.max_wait:
                if ctx:
                    await ctx.info(f"Job {job_id} still running after {elapsed:.0f}s")
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

                # Report progress if available and changed
                if ctx and status_data.progress is not None:
                    if last_progress != status_data.progress:
                        await ctx.report_progress(progress=status_data.progress, total=100.0)
                        last_progress = status_data.progress

                # Log status updates
                if ctx and attempt % 5 == 0:  # Log every 5th attempt to avoid spam
                    await ctx.info(
                        f"Job {job_id} status: {current_status.value} "
                        f"(elapsed: {elapsed:.1f}s, attempt: {attempt})"
                    )

                # Check if job is complete
                if current_status == JobStatus.DONE:
                    if ctx:
                        await ctx.info(f"Job {job_id} completed successfully after {elapsed:.1f}s")

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
                    if ctx:
                        await ctx.error(error_msg)

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
                error_msg = f"Error checking job {job_id} status: {str(e)}"
                if ctx:
                    await ctx.error(error_msg)

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
                    if ctx:
                        await ctx.info(f"Retrying after error (attempt {attempt + 1}/3)...")
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
                if ctx:
                    await ctx.error(error_msg)
                return ToolResult(
                    success=False,
                    job_id=job_id,
                    error=error_msg,
                    metadata={"elapsed_time": elapsed, "attempts": attempt},
                )

    async def poll_with_context(
        self,
        job_id: str,
        ctx: Optional[Context] = None,
        operation_name: str = "operation",
    ) -> ToolResult:
        """Poll with optional rich context reporting for MCP client.

        Args:
            job_id: Job request ID
            ctx: Optional MCP context for progress and logging
            operation_name: Human-readable operation name for logging

        Returns:
            ToolResult with final status
        """
        try:
            if ctx:
                await ctx.info(
                    f"Starting {operation_name} (Job ID: {job_id}). "
                    f"Estimated completion: {self.config.timeout / 60:.1f} minutes max."
                )

            result = await self.poll_until_complete(job_id, ctx)

            if ctx:
                if result.success:
                    await ctx.info(f"{operation_name} completed successfully!")
                else:
                    await ctx.error(f"{operation_name} failed: {result.error}")

            return result

        except Exception as e:
            if ctx:
                await ctx.error(f"Unexpected error in {operation_name}: {str(e)}")
            return ToolResult(
                success=False,
                job_id=job_id,
                error=str(e),
            )
