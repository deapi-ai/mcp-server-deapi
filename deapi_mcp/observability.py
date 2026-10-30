"""Core observability sink: what happened during one tool call, classified once.

This is the lightweight, dependency-free half of observability that the core
genuinely needs: a per-call ``CallOutcome`` kept on a ContextVar, the ``note_*``
recorders the client/tools/polling call as they go, and the ``classify``
functions that turn an outcome into a single label.

It carries NO Prometheus/Redis dependency. A deployment that wants real metrics
injects its recorders through the sinks below; with nothing injected every sink
is a no-op, so the core records nothing and depends on nothing.
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastmcp.exceptions import NotFoundError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from pydantic import ValidationError as PydanticValidationError

_NO_BALANCE_CODES = ("insufficient_balance", "key_limit_exceeded")
_REJECTED_REASONS = (
    "SOURCE_PRIVATE",
    "SOURCE_RESTRICTED",
    "SOURCE_UNPROCESSABLE",
    "INPUT_TOO_LARGE",
)


@dataclass
class CallOutcome:
    api_status: Optional[int] = None
    api_code: Optional[str] = None
    network_error: bool = False
    job_error_reason: Optional[str] = None
    job_failed: bool = False
    input_error: bool = False


_current: ContextVar[Optional[CallOutcome]] = ContextVar("deapi_mcp_call_outcome", default=None)


def begin_call() -> tuple[CallOutcome, Token[Optional[CallOutcome]]]:
    outcome = CallOutcome()
    return outcome, _current.set(outcome)


def end_call(token: Token[Optional[CallOutcome]]) -> None:
    _current.reset(token)


def note_api_error(status: Optional[int], body: Optional[dict[str, Any]]) -> None:
    outcome = _current.get()
    if outcome is None:
        return
    outcome.api_status = status
    code = body.get("code") if isinstance(body, dict) else None
    outcome.api_code = code if isinstance(code, str) else None


def note_network_error() -> None:
    outcome = _current.get()
    if outcome is not None:
        outcome.network_error = True


def note_job_failure(error_reason: Optional[str]) -> None:
    outcome = _current.get()
    if outcome is not None:
        outcome.job_failed = True
        outcome.job_error_reason = error_reason


def note_input_error() -> None:
    outcome = _current.get()
    if outcome is not None:
        outcome.input_error = True


def classify_api(status: Optional[int], code: Optional[str]) -> str:
    if status == 401:
        return "bad_key"
    if code in _NO_BALANCE_CODES:
        return "no_balance"
    if status == 429:
        return "rate_limited"
    if status is not None and 400 <= status < 500:
        return "invalid_input"
    return "deapi_error"


def classify_job(error_reason: Optional[str]) -> str:
    return "job_rejected" if error_reason in _REJECTED_REASONS else "job_failed"


def classify(
    result: Optional[dict[str, Any]], outcome: CallOutcome, exc: Optional[BaseException]
) -> str:
    if exc is not None:
        if isinstance(exc, (FastMCPValidationError, PydanticValidationError, NotFoundError)):
            return "invalid_input"
        return "internal_error"
    if isinstance(result, dict) and result.get("success") is True:
        return "pending" if result.get("done") is False else "ok"
    if outcome.job_failed:
        return classify_job(outcome.job_error_reason)
    if outcome.api_status is not None or outcome.api_code is not None:
        return classify_api(outcome.api_status, outcome.api_code)
    if outcome.network_error:
        return "deapi_error"
    if outcome.input_error:
        return "invalid_input"
    return "internal_error"


# ---------------------------------------------------------------------------
# Injectable sinks (dependency inversion for the optional metrics overlay)
#
# The core calls these; by default they do nothing. A metrics overlay registers
# its real recorder once at startup (alongside the overlay it passes to
# ``create_app``), and from then on the core's calls reach it. This keeps the core
# free of any Prometheus/Redis dependency.
# ---------------------------------------------------------------------------

AuthRejectedSink = Callable[[str], None]


def _noop_auth_rejected(reason: str) -> None:
    """Default auth-rejected sink: record nothing."""


_auth_rejected_sink: AuthRejectedSink = _noop_auth_rejected


def set_auth_rejected_sink(sink: Optional[AuthRejectedSink]) -> None:
    """Install the auth-rejected recorder (or reset to the no-op with ``None``)."""
    global _auth_rejected_sink
    _auth_rejected_sink = sink if sink is not None else _noop_auth_rejected


def record_auth_rejected(reason: str) -> None:
    """Report a rejected authentication to whatever sink is installed.

    A no-op unless a metrics overlay installs a recorder via
    :func:`set_auth_rejected_sink`.
    """
    _auth_rejected_sink(reason)
