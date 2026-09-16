"""Helpers for building the dict a tool returns after a job completes.

Tools used to assemble this dict by hand at each call site, which is how
``result_url`` came to be included in the file-producing tools and omitted from
the four transcription tools: a transcript was assumed to always arrive inline.
Building the response in one place keeps every tool returning the same shape.
"""

from typing import Any, Dict

from ..schemas import ToolResult


def build_job_response(result: ToolResult, job_id: str) -> Dict[str, Any]:
    """Build a tool's return dict from a completed polling result.

    Carries both payload fields. ``result`` holds inline content (e.g. a
    transcript) and ``result_url`` a downloadable file; which one is populated
    depends on the job type and on ``return_result_in_response``, so a tool
    cannot know in advance which to expect.

    A job that reports success while carrying neither is reported as a failure.
    Success with no content is worse than an error: the caller has no signal
    that anything needs doing, and no way to reach a result that may well exist.

    Args:
        result: Outcome collected by PollingManager.
        job_id: Job identifier, echoed back so the caller can query it later.

    Returns:
        dict with 'success', 'job_id', and either the result fields or 'error'.
    """
    if not result.success:
        failure = {
            "success": False,
            "error": result.error,
            "job_id": job_id,
        }
        # Only present on a real job failure, and each one is actionable: whether
        # to retry, and whether the caller was charged. Omitted when unknown so a
        # null is never mistaken for a definite "no".
        for key, value in (
            ("error_code", result.error_code),
            ("retryable", result.retryable),
            ("refunded", result.refunded),
        ):
            if value is not None:
                failure[key] = value
        return failure

    if result.result is None and result.result_url is None:
        return {
            "success": False,
            "error": (
                "Job completed but returned neither inline content nor a result URL. "
                "The result may be retrievable via check_job_status with this job_id."
            ),
            "job_id": job_id,
        }

    response = {
        "success": True,
        "result": result.result,
        "result_url": result.result_url,
        "job_id": job_id,
        "metadata": result.metadata,
    }
    if result.price is not None:
        response["price"] = result.price
    if result.results_alt_formats is not None:
        response["results_alt_formats"] = result.results_alt_formats
    if result.prompt_boost is not None:
        # The prompt the model actually saw, when enhance_prompt rewrote it.
        response["prompt_boost"] = result.prompt_boost.model_dump()
    return response
