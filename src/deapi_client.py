"""HTTP client for deAPI with authentication forwarding and retry logic."""

import asyncio
from typing import Any, Dict, Optional
import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from .config import settings
from .schemas import (
    JobRequestResponse,
    JobStatusResponse,
    BalanceResponse,
    ModelsResponse,
)


class DeapiAPIError(Exception):
    """Custom exception for deAPI errors."""

    def __init__(self, message: str, status_code: Optional[int] = None, response: Optional[Dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


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

        Returns:
            Response JSON data

        Raises:
            DeapiAPIError: If request fails after retries or returns HTTP error
            httpx.TimeoutException: Propagated for tenacity retry (converted to DeapiAPIError after max retries)
            httpx.NetworkError: Propagated for tenacity retry (converted to DeapiAPIError after max retries)
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use async context manager.")

        url = f"/api/{self.api_version}/client/{endpoint}"

        if files:
            # Multipart form data request
            response = await self._client.request(
                method=method,
                url=url,
                data=data,
                files=files,
            )
        elif data:
            # Form data request
            response = await self._client.request(
                method=method,
                url=url,
                data=data,
            )
        else:
            # JSON request
            response = await self._client.request(
                method=method,
                url=url,
                json=json_data,
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
    ) -> JobRequestResponse:
        """Submit a job to deAPI.

        Args:
            endpoint: API endpoint (e.g., 'audiofile2txt', 'txt2img')
            data: Form data
            json_data: JSON data
            files: Files for upload

        Returns:
            JobRequestResponse with request_id
        """
        response_data = await self._request(
            method="POST",
            endpoint=endpoint,
            data=data,
            json_data=json_data,
            files=files,
        )
        return JobRequestResponse(**response_data)

    async def get_job_status(self, job_id: str) -> JobStatusResponse:
        """Get status of a submitted job.

        Args:
            job_id: Job request ID (UUID)

        Returns:
            JobStatusResponse with current status and result
        """
        response_data = await self._request(
            method="GET",
            endpoint=f"request-status/{job_id}",
        )
        return JobStatusResponse(**response_data)

    async def get_balance(self) -> BalanceResponse:
        """Get user's account balance.

        Returns:
            BalanceResponse with current balance
        """
        response_data = await self._request(
            method="GET",
            endpoint="balance",
        )
        return BalanceResponse(**response_data)

    async def get_models(self) -> ModelsResponse:
        """Get list of available models.

        Returns:
            ModelsResponse with available models
        """
        response_data = await self._request(
            method="GET",
            endpoint="models",
        )
        return ModelsResponse(**response_data)

    async def calculate_price(
        self,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Calculate price for an operation.

        Args:
            endpoint: Price calculation endpoint (e.g., 'txt2img/price-calculation')
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