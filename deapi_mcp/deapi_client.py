"""HTTP client for deAPI with authentication forwarding and retry logic."""

from collections.abc import Mapping, Sequence
from typing import IO, Any, Dict, Optional, Tuple, Union, cast

import httpx

# Low-level request client from the deAPI SDK. These are SDK-internal modules for
# now; promote them to the SDK's public API in a later SDK release. The MCP keeps
# its own client surface (below) and delegates only transport, retry and auth
# forwarding to the SDK, so tool code is unchanged.
from deapi_sdk._client import AsyncHTTPClient
from deapi_sdk._config import ClientConfig
from deapi_sdk._exceptions import DeapiError as SdkError
from deapi_sdk._exceptions import NetworkError as SdkNetworkError

from .config import settings
from .observability import note_api_error, note_network_error
from .schemas import (
    BalanceResponse,
    JobRequestResponse,
    JobStatusResponse,
    ModelsResponse,
)
from .webhooks import internal_webhooks_enabled, registry

# The models endpoint caps `limit` at 50 server-side and defaults to 25.
# Requesting the cap keeps the number of round trips low; the page loop in
# get_models() is what guarantees completeness.
MODELS_PAGE_SIZE = 50

# Multipart uploads in either shape httpx accepts: a mapping of field name to
# (filename, content, mime type), or a sequence of (field name, that tuple) pairs,
# which allows a repeated field. Content is raw bytes or a binary file object.
UploadPart = Tuple[str, Union[bytes, IO[bytes]], str]
UploadFiles = Union[Mapping[str, UploadPart], Sequence[Tuple[str, UploadPart]]]

# Mirrors the deAPI API's webhook validation rules:
#   webhook_url    nullable|url:https|max:2048|required_with:webhook_secret
#   webhook_secret sometimes|nullable|string|min:32|max:255
WEBHOOK_SECRET_MIN_LENGTH = 32
WEBHOOK_SECRET_MAX_LENGTH = 255
WEBHOOK_URL_MAX_LENGTH = 2048


def _error_message(body: Dict[str, Any], fallback: str) -> str:
    """The most specific reason an error body gives.

    Laravel's own 422 summary only repeats the first field error ("... (and 1
    more error)"), and some endpoints answer "The given data was invalid." or a
    bare `{"error": ...}`, leaving the actual cause in `errors`. The caller can
    only fix what it is told, so every field message is included.
    """
    message = body.get("message") or body.get("error")
    if not isinstance(message, str) or not message:
        message = fallback

    details = []
    errors = body.get("errors")
    if isinstance(errors, dict):
        for field, msgs in errors.items():
            for msg in msgs if isinstance(msgs, list) else [msgs]:
                details.append(f"{field}: {msg}")
    if not details or len(details) == 1 and details[0].endswith(message):
        return message
    return f"{message} [{'; '.join(details)}]"


class DeapiAPIError(Exception):
    """Custom exception for deAPI errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response = response
        note_api_error(status_code, response)


class DeapiRequestError(DeapiAPIError):
    """A request rejected on this side before it was sent.

    Raised by the checks that mirror the API's own validation rules, so the
    caller sees the same 422-style message it would have got from the server,
    without the round trip. Being a DeapiAPIError, every tool already reports
    it as an API error rather than an unexpected one.
    """

    def __init__(self, message: str):
        super().__init__(message, status_code=422)


def webhook_payload_fields(
    webhook_url: Optional[str], webhook_secret: Optional[str]
) -> Dict[str, str]:
    """Return the webhook fields to add to a job payload, validated locally.

    The API enforces these rules itself, but a 422 arriving after the caller
    has assembled a whole request is a poor way to learn a secret is too short.
    Checking here gives one clear message and no wasted round trip.

    Returns an empty dict when neither value is given.
    """
    if not webhook_url and not webhook_secret:
        return {}

    if webhook_secret and not webhook_url:
        raise DeapiRequestError("webhook_secret requires webhook_url")

    assert webhook_url  # both early exits above leave a non-empty URL here
    if not webhook_url.lower().startswith("https://"):
        raise DeapiRequestError("webhook_url must use https")
    if len(webhook_url) > WEBHOOK_URL_MAX_LENGTH:
        raise DeapiRequestError(f"webhook_url must be at most {WEBHOOK_URL_MAX_LENGTH} characters")

    fields = {"webhook_url": webhook_url}
    if webhook_secret:
        if not WEBHOOK_SECRET_MIN_LENGTH <= len(webhook_secret) <= WEBHOOK_SECRET_MAX_LENGTH:
            raise DeapiRequestError(
                f"webhook_secret must be {WEBHOOK_SECRET_MIN_LENGTH}-"
                f"{WEBHOOK_SECRET_MAX_LENGTH} characters"
            )
        fields["webhook_secret"] = webhook_secret
    return fields


# One shared httpx.AsyncClient (connection pool) for the whole process, injected
# into every per-request SDK client so users share a single transport instead of
# each request opening its own pool. Auth is sent per request by the SDK client,
# so one transport safely serves every user.
_shared_http: Optional[httpx.AsyncClient] = None


def _shared_http_client() -> httpx.AsyncClient:
    global _shared_http
    if _shared_http is None or _shared_http.is_closed:
        _shared_http = httpx.AsyncClient(timeout=httpx.Timeout(settings.http_timeout))
    return _shared_http


async def aclose_shared_http_client() -> None:
    """Close the shared transport. Call once on server shutdown."""
    global _shared_http
    if _shared_http is not None and not _shared_http.is_closed:
        await _shared_http.aclose()
    _shared_http = None


class DeapiClient:
    """Async client for the deAPI REST API.

    Keeps the MCP's own request surface (submit_job, get_job_status, ...) and
    delegates transport to the deAPI SDK's HTTP client over a shared connection
    pool, with per-request auth, safe retries and a capped Retry-After.
    """

    def __init__(self, api_token: str):
        """Initialize deAPI client with user's API token.

        Args:
            api_token: User's deAPI Bearer token
        """
        self.api_token = api_token
        self.base_url = settings.deapi_api_base_url
        self.api_version = settings.deapi_api_version
        self._http: Optional[AsyncHTTPClient] = None

    async def __aenter__(self) -> "DeapiClient":
        """Async context manager entry."""
        config = ClientConfig(
            api_key=self.api_token,
            base_url=self.base_url,
            timeout=settings.http_timeout,
            max_retries=settings.max_retries,
            api_version=self.api_version,
        )
        # Share the process-wide transport; auth rides each request, so the pool
        # is safe to reuse across users. We don't own it, so our close is a no-op.
        self._http = AsyncHTTPClient(config, _shared_http_client(), owns_http_client=False)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit. The transport is shared, so nothing to close."""
        self._http = None

    async def _send(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        files: Optional[UploadFiles] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send one request over the shared SDK transport.

        The SDK client forwards auth per request, retries only what is safe to
        resend (never a POST that may have reached the server) and caps the
        honoured Retry-After. A non-2xx response arrives as a typed SDK error,
        translated here to DeapiAPIError (preserving the message the tools show);
        a transport failure propagates for _request to note and re-raise.
        """
        if self._http is None:
            raise RuntimeError("Client not initialized. Use async context manager.")

        url = f"{self.base_url}/api/{self.api_version}/{endpoint}"
        try:
            return cast(
                Dict[str, Any],
                await self._http.request(
                    method,
                    url,
                    json=json_data,
                    data=data,
                    # The SDK annotates uploads as a list of bytes tuples but hands
                    # them to httpx unchanged, which accepts every UploadFiles shape.
                    files=files,  # type: ignore[arg-type]
                    params=params,
                ),
            )
        except SdkNetworkError:
            raise
        except SdkError as exc:
            body = exc.body if isinstance(exc.body, dict) else None
            status = exc.status_code
            if body is not None:
                message = _error_message(body, f"HTTP {status}" if status else str(exc))
            else:
                message = exc.message or (f"HTTP {status}" if status else str(exc))
            raise DeapiAPIError(message=message, status_code=status, response=body) from exc

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        files: Optional[UploadFiles] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Send a request, noting a transport failure for the call's outcome."""
        try:
            return await self._send(
                method, endpoint, data=data, json_data=json_data, files=files, params=params
            )
        except SdkNetworkError:
            note_network_error()
            raise

    async def submit_job(
        self,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        files: Optional[UploadFiles] = None,
        webhook_url: Optional[str] = None,
        webhook_secret: Optional[str] = None,
    ) -> JobRequestResponse:
        """Submit a job to deAPI.

        Args:
            endpoint: API endpoint (e.g., 'audio/transcriptions', 'images/generations')
            data: Form data
            json_data: JSON data
            files: Files for upload
            webhook_url: HTTPS URL the API calls when the job finishes. Belongs to
                the caller, not to this server: the tool still polls and returns
                the result as usual, and the webhook is a side effect for the
                caller's own systems.
            webhook_secret: Key the API uses to sign the callback
                (HMAC-SHA256 over timestamp + "." + body). Requires webhook_url.

        Returns:
            JobRequestResponse with request_id

        Raises:
            DeapiRequestError: If the webhook fields break a rule the API would
                reject with a 422 (e.g. webhook_secret without webhook_url).
                Failing here keeps the message readable and saves the round trip.
        """
        webhook_fields: Mapping[str, Optional[str]] = webhook_payload_fields(
            webhook_url, webhook_secret
        )

        # The API takes one webhook per job. A caller's own URL always wins;
        # only when they gave none do we register ourselves, so the tool can
        # wait on the callback instead of polling.
        use_internal = not webhook_fields and internal_webhooks_enabled()
        if use_internal:
            webhook_fields = {
                "webhook_url": settings.webhook_receiver_url,
                "webhook_secret": settings.webhook_secret,
            }

        if webhook_fields:
            # Whichever body the tool built is the one the API will read; add the
            # webhook fields to that one rather than introducing a second body.
            if json_data is not None:
                json_data = {**json_data, **webhook_fields}
            else:
                data = {**(data or {}), **webhook_fields}

        response_data = await self._request(
            method="POST",
            endpoint=endpoint,
            data=data,
            json_data=json_data,
            files=files,
        )
        response = JobRequestResponse(**response_data)

        if use_internal:
            # Tracking after submit is safe: a callback that beats us here is
            # kept by the registry and found already set.
            registry.track(response.data.request_id)

        return response

    async def get_job_status(self, job_id: str) -> JobStatusResponse:
        """Get status of a submitted job.

        Args:
            job_id: Job request ID (UUID)

        Returns:
            JobStatusResponse with current status and result
        """
        response_data = await self._request(
            method="GET",
            endpoint=f"jobs/{job_id}",
        )
        return JobStatusResponse(**response_data)

    async def get_balance(self) -> BalanceResponse:
        """Get user's account balance.

        Returns:
            BalanceResponse with current balance
        """
        response_data = await self._request(
            method="GET",
            endpoint="account/balance",
        )
        return BalanceResponse(**response_data)

    async def get_models(self) -> ModelsResponse:
        """Get list of available models, following pagination to the last page.

        The endpoint is paginated and defaults to 25 items per page, while the API
        caps `limit` at 50 server-side (`min($request->input('limit', 25), 50)`).
        Requesting a large limit is therefore not sufficient on its own — once the
        catalogue exceeds the cap, a single request silently truncates again. The
        page loop is what actually makes this correct.

        Returns:
            ModelsResponse whose `data` holds every model across all pages, and
            whose `meta` is the first page's — `total` and `last_page` describe
            the whole catalogue, so they stay accurate after the pages are joined.
        """
        first_page = await self._request(
            method="GET",
            endpoint="models",
            params={"limit": MODELS_PAGE_SIZE},
        )
        response = ModelsResponse(**first_page)

        last_page = response.meta.last_page if response.meta else None
        if not last_page or last_page <= 1:
            return response

        for page in range(2, last_page + 1):
            page_data = await self._request(
                method="GET",
                endpoint="models",
                params={"limit": MODELS_PAGE_SIZE, "page": page},
            )
            response.data.extend(ModelsResponse(**page_data).data)

        return response

    async def calculate_price(
        self,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        files: Optional[UploadFiles] = None,
    ) -> Dict[str, Any]:
        """Calculate price for an operation.

        Args:
            endpoint: Price calculation endpoint (e.g., 'images/generations/price')
            data: Form data
            json_data: JSON data
            files: Files for upload

        Returns:
            Price calculation response
        """
        return await self._request(
            method="POST",
            endpoint=endpoint,
            data=data,
            json_data=json_data,
            files=files,
        )

    async def post_sync(
        self,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        files: Optional[UploadFiles] = None,
    ) -> Dict[str, Any]:
        """POST to a synchronous (non-job) endpoint and return the raw response.

        For endpoints that return a result directly instead of a job_id (e.g.,
        prompt enhancements). Same retry/error semantics as submit_job.
        """
        return await self._request(
            method="POST",
            endpoint=endpoint,
            data=data,
            json_data=json_data,
            files=files,
        )


def get_client(api_token: Optional[str] = None) -> DeapiClient:
    """Get a new deAPI client instance.

    If no api_token is provided, attempts to get it from the request context
    (set by authentication middleware).

    Args:
        api_token: Optional deAPI Bearer token. If not provided,
                  uses token from authentication context.

    Returns:
        DeapiClient instance ready for use with async context manager

    Raises:
        ValueError: If no token available in context and none provided
    """
    if api_token is None:
        # Try to get token from auth context
        from .auth import get_current_token

        api_token = get_current_token()

    return DeapiClient(api_token)
