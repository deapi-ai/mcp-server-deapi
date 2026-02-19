"""Pytest configuration and fixtures for deAPI MCP Server tests."""

import pytest
from unittest.mock import AsyncMock, Mock


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
        "message": "Job submitted successfully"
    }


@pytest.fixture
def sample_balance_response():
    """Sample balance response from deAPI."""
    return {
        "success": True,
        "balance": 100.50,
        "currency": "USD"
    }


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
                "capabilities": ["text-to-image", "image-to-image"]
            },
            {
                "id": "whisper-large-v3",
                "name": "Whisper Large V3",
                "type": "audio",
                "capabilities": ["transcription"]
            }
        ]
    }
