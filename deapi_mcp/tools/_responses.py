"""Helpers for building the dict a tool returns after a job completes.

Tools used to assemble this dict by hand at each call site, which is how
``result_url`` came to be included in the file-producing tools and omitted from
the four transcription tools: a transcript was assumed to always arrive inline.
Building the response in one place keeps every tool returning the same shape.
"""

import json
from typing import Any, Dict

from ..observability import note_input_error
from ..schemas import ToolResult


def input_error_response(message: str) -> Dict[str, Any]:
    """The answer for input this server rejects before calling the API, counted as such."""
    note_input_error()
    return {"success": False, "error": message}


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
    if result.in_progress is True:
        return build_in_progress_response(result, job_id)

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
    if result.source_metadata is not None:
        # Paid for with include_metadata; dropping it would charge for nothing.
        response["source_metadata"] = result.source_metadata
    if result.price is not None:
        response["price"] = result.price
    if result.results_alt_formats is not None:
        response["results_alt_formats"] = result.results_alt_formats
    if result.prompt_boost is not None:
        # The prompt the model actually saw, when enhance_prompt rewrote it.
        response["prompt_boost"] = result.prompt_boost.model_dump()
    return response


def build_price_response(price_response: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a /price answer, flagging a quote that is not a real price.

    For a partner model whose cost is settled after the job runs (seedance, the
    gpt-image and nano-banana test models), the API may answer `price: 0` with
    `estimate_basis: "unavailable"`. Passed through bare, that reads as free.
    """
    data = price_response.get("data", {}) or {}
    response: Dict[str, Any] = {"success": True, "price": data}
    if isinstance(data, dict) and data.get("estimate_basis") == "unavailable":
        response["warning"] = (
            "No price estimate is available for this request: the 0 above is not the cost. "
            "The job is charged after it runs; check the job's price when it finishes."
        )
    return response


def build_in_progress_response(result: ToolResult, job_id: str) -> Dict[str, Any]:
    """The answer for a job still running when this call's wait ran out.

    Not an error: the job was accepted and paid for and is still working. The
    caller only needs to know how to pick the wait up again, so it is told in
    so many words, with the exact call to make.
    """
    from ..config import settings

    wait = max(1, round(settings.tool_max_wait))
    response: Dict[str, Any] = {
        "success": True,
        "done": False,
        "status": result.status.value if result.status else "processing",
        "job_id": job_id,
        "message": (
            f"The job is still running. Call check_job_status(job_id='{job_id}', "
            f"wait_seconds={wait}) to wait for it; repeat until done is true. "
            "Do not submit the job again: it is already paid for."
        ),
    }
    if result.progress is not None:
        response["progress"] = result.progress
    return response


def decode_transcript(response: Dict[str, Any]) -> Dict[str, Any]:
    """Unpack a structured transcript into fields an LLM can read directly.

    A model pinned to `output_format: json` (WhisperLargeV3Ct2) always returns
    its transcript as a JSON document — `{"text", "language", "ts_level",
    "segments": [...]}` — even with timestamps off, while the others return
    plain text. Passed through as is, the JSON arrives as one escaped string.
    """
    raw = response.get("result")
    if not response.get("success") or not isinstance(raw, str) or not raw.lstrip().startswith("{"):
        return response
    try:
        doc = json.loads(raw)
    except ValueError:
        return response
    if not isinstance(doc, dict) or "text" not in doc:
        return response

    response["result"] = doc["text"]
    for key in ("language", "ts_level", "diarization_available", "segments"):
        if doc.get(key) is not None:
            response[key] = doc[key]
    return response
