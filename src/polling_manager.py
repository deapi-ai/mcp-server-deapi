"""Smart adaptive polling manager for async job completion."""

import asyncio
import time
from typing import Optional
from fastmcp import Context

from .config import settings, PollingConfig
from .deapi_client import DeapiClient, DeapiAPIError
from .schemas import JobStatus, JobStatusResponse, ToolResult


class PollingTimeoutError(Exception):
    """Raised when polling times out before job completes."""
    pass


class PollingManager:
    """Manages adaptive polling for async job completion."""

    def __init__(self, client: DeapiClient, job_type: str):
        """Initialize polling manager.

        Args:
            client: Initialized DeapiClient instance
            job_type: Type of job for adaptive polling config (audio, image, video, etc.)
        """
        self.client = client
        self.job_type = job_type
        self.config: PollingConfig = settings.get_polling_config(job_type)

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
            ToolResult with final job status and result

        Raises:
            PollingTimeoutError: If job doesn't complete within timeout
            DeapiAPIError: If API request fails
        """
        start_time = time.time()
        current_delay = self.config.initial_delay
        attempt = 0
        last_progress = None

        if ctx:
            await ctx.info(f"Job {job_id} submitted. Starting polling for {self.job_type} job...")

        while True:
            attempt += 1
            elapsed = time.time() - start_time

            # Check timeout
            if elapsed > self.config.timeout:
                error_msg = (
                    f"Job {job_id} timed out after {elapsed:.1f}s "
                    f"(max: {self.config.timeout}s)"
                )
                if ctx:
                    await ctx.error(error_msg)
                raise PollingTimeoutError(error_msg)

            # Poll job status
            try:
                status_response: JobStatusResponse = await self.client.get_job_status(job_id)
                status_data = status_response.data
                current_status = status_data.status

                # Report progress if available and changed
                if ctx and status_data.progress is not None:
                    if last_progress != status_data.progress:
                        await ctx.report_progress(
                            progress=status_data.progress,
                            total=100.0
                        )
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
                        metadata={
                            "elapsed_time": elapsed,
                            "attempts": attempt,
                        },
                    )

                elif current_status == JobStatus.FAILED:
                    error_msg = f"Job {job_id} failed"
                    if ctx:
                        await ctx.error(error_msg)

                    return ToolResult(
                        success=False,
                        job_id=job_id,
                        status=current_status,
                        error=error_msg,
                        metadata={
                            "elapsed_time": elapsed,
                            "attempts": attempt,
                        },
                    )

                # Job still in progress, wait before next poll
                await asyncio.sleep(current_delay)
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

        except PollingTimeoutError as e:
            if ctx:
                await ctx.error(f"{operation_name} timed out: {str(e)}")
            return ToolResult(
                success=False,
                job_id=job_id,
                error=str(e),
            )
        except Exception as e:
            if ctx:
                await ctx.error(f"Unexpected error in {operation_name}: {str(e)}")
            return ToolResult(
                success=False,
                job_id=job_id,
                error=str(e),
            )