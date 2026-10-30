"""Pytest configuration and fixtures for deAPI MCP Server tests."""

from unittest.mock import AsyncMock, Mock

import pytest
from starlette.testclient import TestClient

import deapi_mcp.oauth_endpoints as oauth_endpoints
from deapi_mcp.config import settings


@pytest.fixture(scope="session")
def app_client():
    """The full app. build_app() mutates the module-level mcp, so it is built once per session."""
    from deapi_mcp.server_remote import build_app

    mp = pytest.MonkeyPatch()
    mp.setenv("DEAPI_JWT_SECRET_KEY", "x" * 40)
    mp.setenv("PUBLIC_BASE_URL", "https://mcp.test")
    mp.setattr(oauth_endpoints, "_jwt_secret_key", None)
    mp.setattr(oauth_endpoints, "_fernet", None)
    mp.setattr(settings, "enrich_tool_descriptions", False)
    app = build_app()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        mp.undo()


@pytest.fixture(scope="session")
def auth_headers(app_client):
    return {
        "Authorization": f"Bearer {oauth_endpoints.create_jwt('test-deapi-token-123')}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


@pytest.fixture
def mock_deapi_client():
    """Mock DeapiClient for testing."""
    client = AsyncMock()
    client.base_url = "https://api.deapi.ai"
    return client


@pytest.fixture
def mock_context():
    """Mock MCP context for testing."""
    context = Mock()
    context.info = Mock()
    context.warning = Mock()
    context.error = Mock()
    context.report_progress = Mock()
    return context


@pytest.fixture
def sample_job_response():
    """Sample job response from deAPI."""
    return {
        "success": True,
        "job_id": "test-job-123",
        "status": "pending",
        "message": "Job submitted successfully",
    }


@pytest.fixture
def sample_balance_response():
    """Sample balance response from deAPI."""
    return {"success": True, "balance": 100.50, "currency": "USD"}


@pytest.fixture
def sample_models_response():
    """Sample models list response from deAPI."""
    return {
        "success": True,
        "models": [
            {
                "id": "stable-diffusion-xl",
                "name": "Stable Diffusion XL",
                "type": "image",
                "capabilities": ["text-to-image", "image-to-image"],
            },
            {
                "id": "whisper-large-v3",
                "name": "Whisper Large V3",
                "type": "audio",
                "capabilities": ["transcription"],
            },
        ],
    }
