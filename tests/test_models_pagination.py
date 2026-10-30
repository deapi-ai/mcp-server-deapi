"""Tests for DEV-2019 P1: the model catalogue must not be silently truncated.

The /models endpoint is paginated, defaults to 25 items per page, and caps
`limit` at 50 server-side. Fetching a single page returned 25 of 30 models and
reported 25 as the catalogue size, so the newest models — including the only
one supporting word-level timestamps and diarization — did not exist as far as
any MCP client was concerned. Nothing in the response let the caller detect it.
"""

from unittest.mock import AsyncMock, patch

import pytest

from deapi_mcp.deapi_client import MODELS_PAGE_SIZE, DeapiClient


def make_model(slug):
    """Minimal model payload accepted by ModelInfo."""
    return {"name": slug, "slug": slug, "inference_types": ["txt2img"], "info": {}}


def make_page(slugs, current_page, last_page, total, per_page=MODELS_PAGE_SIZE):
    return {
        "data": [make_model(s) for s in slugs],
        "meta": {
            "total": total,
            "last_page": last_page,
            "per_page": per_page,
            "current_page": current_page,
        },
    }


class TestGetModelsPagination:

    @pytest.mark.asyncio
    async def test_requests_the_maximum_page_size(self):
        """The first request must ask for more than the default 25."""
        client = DeapiClient(api_token="t")
        page = make_page(["a"], current_page=1, last_page=1, total=1)

        with patch.object(client, "_request", AsyncMock(return_value=page)) as req:
            await client.get_models()

        assert req.call_args.kwargs["params"]["limit"] == MODELS_PAGE_SIZE

    @pytest.mark.asyncio
    async def test_follows_all_pages(self):
        """With last_page=3 the client must fetch every page, not just the first."""
        client = DeapiClient(api_token="t")
        pages = [
            make_page(["m1", "m2"], current_page=1, last_page=3, total=6, per_page=2),
            make_page(["m3", "m4"], current_page=2, last_page=3, total=6, per_page=2),
            make_page(["m5", "m6"], current_page=3, last_page=3, total=6, per_page=2),
        ]

        with patch.object(client, "_request", AsyncMock(side_effect=pages)) as req:
            response = await client.get_models()

        assert req.await_count == 3, "Must fetch all three pages"
        assert [m.slug for m in response.data] == ["m1", "m2", "m3", "m4", "m5", "m6"]

        # Pages 2 and 3 must be requested by number.
        assert req.await_args_list[1].kwargs["params"]["page"] == 2
        assert req.await_args_list[2].kwargs["params"]["page"] == 3

    @pytest.mark.asyncio
    async def test_never_returns_fewer_items_than_total(self):
        """Regression: total > per_page must not yield a short list."""
        client = DeapiClient(api_token="t")
        pages = [
            make_page(
                [f"m{i}" for i in range(50)], current_page=1, last_page=2, total=55, per_page=50
            ),
            make_page(
                [f"m{i}" for i in range(50, 55)], current_page=2, last_page=2, total=55, per_page=50
            ),
        ]

        with patch.object(client, "_request", AsyncMock(side_effect=pages)):
            response = await client.get_models()

        assert len(response.data) >= response.meta.total

    @pytest.mark.asyncio
    async def test_single_page_makes_one_request(self):
        """A catalogue that fits on one page must not trigger extra round trips."""
        client = DeapiClient(api_token="t")
        page = make_page(["a", "b"], current_page=1, last_page=1, total=2)

        with patch.object(client, "_request", AsyncMock(return_value=page)) as req:
            response = await client.get_models()

        assert req.await_count == 1
        assert len(response.data) == 2

    @pytest.mark.asyncio
    async def test_response_without_meta_still_parses(self):
        """meta is optional — an unpaginated response must not raise."""
        client = DeapiClient(api_token="t")

        with patch.object(client, "_request", AsyncMock(return_value={"data": [make_model("a")]})):
            response = await client.get_models()

        assert len(response.data) == 1
        assert response.meta is None


class TestGetAvailableModelsCount:

    @pytest.mark.asyncio
    async def test_count_comes_from_meta_total(self):
        """count must report the catalogue's own total, never the fetched length."""
        from deapi_mcp.schemas import ModelsResponse

        response = ModelsResponse(**make_page(["a", "b"], current_page=1, last_page=1, total=30))
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get_models = AsyncMock(return_value=response)

        with patch("deapi_mcp.tools.utility.get_client", return_value=mock_client):
            from deapi_mcp.tools.utility import get_available_models

            result = await get_available_models()

        assert result["success"] is True
        assert result["count"] == 30, "count must come from meta.total"

    @pytest.mark.asyncio
    async def test_count_falls_back_to_length_without_meta(self):
        """With no meta, the fetched length is the only number available."""
        from deapi_mcp.schemas import ModelsResponse

        response = ModelsResponse(data=[make_model("a"), make_model("b")])
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get_models = AsyncMock(return_value=response)

        with patch("deapi_mcp.tools.utility.get_client", return_value=mock_client):
            from deapi_mcp.tools.utility import get_available_models

            result = await get_available_models()

        assert result["count"] == 2
