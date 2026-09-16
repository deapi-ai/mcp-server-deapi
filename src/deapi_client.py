"""HTTP client for deAPI with authentication forwarding and retry logic."""

from typing import Any, Dict, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import settings
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

# Mirrors BaseClientRequest::webhookRules() in the API:
#   webhook_url    nullable|url:https|max:2048|required_with:webhook_secret
#   webhook_secret sometimes|nullable|string|min:32|max:255
WEBHOOK_SECRET_MIN_LENGTH = 32
WEBHOOK_SECRET_MAX_LENGTH = 255
WEBHOOK_URL_MAX_LENGTH = 2048


class DeapiAPIError(Exception):
    """Custom exception for deAPI errors."""

    def __init__(
        self, message: str, status_code: Optional[int] = None, response: Optional[Dict] = None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


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


class DeapiClient:
    """Async HTTP client for deAPI REST API."""

    def __init__(self, api_token: str):
        """Initialize deAPI client with user's API token.

        Args:
            api_token: User's deAPI Bearer token
        """
        self.api_token = api_token
        self.base_url = settings.deapi_api_base_url
        self.api_version = settings.deapi_api_version
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        """Async context manager entry."""
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(settings.http_timeout),
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Accept": "application/json",
            },
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()

    def _get_headers(self, additional_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Get headers with authentication.

        Args:
            additional_headers: Optional additional headers to include

        Returns:
            Complete headers dictionary
        """
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        }
        if additional_headers:
            headers.update(additional_headers)
        return headers

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make HTTP request with retry logic.

        Retries automatically on TimeoutException and NetworkError (up to 3 attempts
        with exponential backoff). Non-retryable errors (HTTP 4xx/5xx) raise immediately.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            data: Form data for multipart/form-data requests
            json_data: JSON data for application/json requests
            files: Files for multipart upload
            params: Query string parameters

        Returns:
            Response JSON data

        Raises:
            DeapiAPIError: If request fails after retries or returns HTTP error
            httpx.TimeoutException: Propagated for tenacity retry (converted to DeapiAPIError after max retries)
            httpx.NetworkError: Propagated for tenacity retry (converted to DeapiAPIError after max retries)
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use async context manager.")

        url = f"/api/{self.api_version}/{endpoint}"

        if files:
            # Multipart form data request
            response = await self._client.request(
                method=method,
                url=url,
                data=data,
                files=files,
                params=params,
            )
        elif data:
            # Form data request
            response = await self._client.request(
                method=method,
                url=url,
                data=data,
                params=params,
            )
        else:
            # JSON request
            response = await self._client.request(
                method=method,
                url=url,
                json=json_data,
                params=params,
            )

        # Check for HTTP errors (not retried — these are definitive responses)
        if response.status_code >= 400:
            error_data = None
            try:
                error_data = response.json()
            except Exception:
                pass

            error_msg = f"HTTP {response.status_code}"
            if error_data and isinstance(error_data, dict):
                error_msg = error_data.get("message", error_msg)

            raise DeapiAPIError(
                message=error_msg,
                status_code=response.status_code,
                response=error_data,
            )

        return response.json()

    async def submit_job(
        self,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
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
        webhook_fields = webhook_payload_fields(webhook_url, webhook_secret)

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
        files: Optional[Dict[str, Any]] = None,
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
        files: Optional[Dict[str, Any]] = None,
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
