"""Tests for model-aware tool description enrichment middleware."""

import asyncio
import time

import pytest
from pydantic import BaseModel
from unittest.mock import AsyncMock, MagicMock, patch

from src.middleware import (
    INFERENCE_TYPE_TO_TOOLS,
    ModelEnrichmentMiddleware,
    _build_enrichment_block,
    _cache,
    _fetch_and_index_models,
    _format_model_info,
    get_cached_model,
    get_cached_models_for_tool,
)
from src.schemas import ModelInfo, ModelsResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_model(
    slug: str,
    inference_types: list,
    info=None,
    loras=None,
) -> ModelInfo:
    """Create a ModelInfo for testing."""
    return ModelInfo(
        name=slug,
        slug=slug,
        inference_types=inference_types,
        info=info,
        loras=loras,
    )


class FakeTool(BaseModel):
    """Minimal Pydantic model that mimics FastMCP Tool for testing."""
    name: str
    description: str = ""


def _reset_cache():
    """Reset the global model cache to a clean state."""
    _cache.tool_models.clear()
    _cache.models_by_slug.clear()
    _cache.enrichments.clear()
    _cache.last_fetched = 0.0


# ---------------------------------------------------------------------------
# _format_model_info tests
# ---------------------------------------------------------------------------

class TestFormatModelInfo:
    def test_full_image_model(self):
        model = make_model("Flux1schnell", ["txt2img"], info={
            "limits": {
                "min_steps": 1, "max_steps": 10,
                "min_width": 256, "max_width": 2048,
                "min_height": 256, "max_height": 2048,
                "min_guidance": 0, "max_guidance": 20,
            },
            "defaults": {"steps": "4", "guidance": "0"},
            "features": {"supports_guidance": "0"},
        })
        result = _format_model_info(model)
        assert "`Flux1schnell`" in result
        assert "steps=1-10" in result
        assert "default 4" in result
        assert "size=256-2048x256-2048" in result
        assert "FIXED" in result
        assert "guidance=0" in result

    def test_model_with_guidance_range(self):
        model = make_model("SDModel", ["txt2img"], info={
            "limits": {
                "min_steps": 1, "max_steps": 50,
                "min_guidance": 1, "max_guidance": 20,
            },
            "defaults": {"steps": "20", "guidance": "7.5"},
            "features": {"supports_guidance": "1"},
        })
        result = _format_model_info(model)
        assert "guidance=1-20" in result
        assert "default 7.5" in result
        assert "FIXED" not in result

    def test_empty_info_list(self):
        model = make_model("SimpleModel", ["img-rmbg"], info=[])
        result = _format_model_info(model)
        assert result == "  - `SimpleModel`"

    def test_none_info(self):
        model = make_model("NoneModel", ["img-rmbg"], info=None)
        result = _format_model_info(model)
        assert result == "  - `NoneModel`"

    def test_video_model_with_fps_and_frames(self):
        model = make_model("VideoGen", ["txt2video"], info={
            "limits": {
                "min_steps": 1, "max_steps": 50,
                "min_fps": 30, "max_fps": 30,
                "min_frames": 10, "max_frames": 120,
            },
            "defaults": {"steps": "1"},
            "features": {},
        })
        result = _format_model_info(model)
        assert "fps=30 (fixed)" in result
        assert "frames=10-120" in result
        assert "steps=1-50" in result

    def test_video_model_with_fps_range(self):
        model = make_model("VideoFlex", ["txt2video"], info={
            "limits": {"min_fps": 24, "max_fps": 60},
            "defaults": {},
            "features": {},
        })
        result = _format_model_info(model)
        assert "fps=24-60" in result
        assert "(fixed)" not in result

    def test_model_with_loras(self):
        model = make_model("LoraModel", ["txt2img"], info={
            "limits": {},
            "defaults": {},
            "features": {},
        }, loras=[{"name": "style1"}, {"name": "style2"}])
        result = _format_model_info(model)
        assert "2 LoRAs available" in result

    def test_model_with_empty_sub_dicts(self):
        model = make_model("Bare", ["txt2img"], info={
            "limits": {},
            "defaults": {},
            "features": {},
        })
        result = _format_model_info(model)
        assert result == "  - `Bare`"


# ---------------------------------------------------------------------------
# _build_enrichment_block tests
# ---------------------------------------------------------------------------

class TestBuildEnrichmentBlock:
    def test_multiple_models(self):
        models = [
            make_model("Model1", ["txt2img"], info=None),
            make_model("Model2", ["txt2img"], info=None),
        ]
        result = _build_enrichment_block(models)
        assert "---" in result
        assert "Available models:" in result
        assert "`Model1`" in result
        assert "`Model2`" in result

    def test_empty_list(self):
        result = _build_enrichment_block([])
        assert result == ""

    def test_single_model_with_details(self):
        models = [make_model("DetailedModel", ["txt2img"], info={
            "limits": {"min_steps": 1, "max_steps": 50},
            "defaults": {"steps": "20"},
            "features": {},
        })]
        result = _build_enrichment_block(models)
        assert "---" in result
        assert "`DetailedModel`" in result
        assert "steps=1-50" in result


# ---------------------------------------------------------------------------
# _fetch_and_index_models tests
# ---------------------------------------------------------------------------

class TestFetchAndIndexModels:
    @pytest.fixture(autouse=True)
    def reset(self):
        _reset_cache()
        yield
        _reset_cache()

    @pytest.mark.asyncio
    async def test_indexes_models_by_tool_name(self):
        mock_models = [
            make_model("ImgModel", ["txt2img"], info=None),
            make_model("VidModel", ["txt2video", "img2video"], info=None),
        ]
        mock_response = ModelsResponse(data=mock_models)

        mock_client = AsyncMock()
        mock_client.get_models = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.deapi_client.get_client", return_value=mock_client):
            await _fetch_and_index_models()

        assert "text_to_image" in _cache.tool_models
        assert "text_to_image_price" in _cache.tool_models
        assert "text_to_video" in _cache.tool_models
        assert "image_to_video" in _cache.tool_models
        assert _cache.last_fetched > 0

    @pytest.mark.asyncio
    async def test_builds_enrichment_blocks(self):
        mock_models = [
            make_model("ImgModel", ["txt2img"], info={
                "limits": {"min_steps": 1, "max_steps": 50},
                "defaults": {"steps": "20"},
                "features": {},
            }),
        ]
        mock_response = ModelsResponse(data=mock_models)

        mock_client = AsyncMock()
        mock_client.get_models = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.deapi_client.get_client", return_value=mock_client):
            await _fetch_and_index_models()

        assert "text_to_image" in _cache.enrichments
        assert "Available models:" in _cache.enrichments["text_to_image"]
        assert "`ImgModel`" in _cache.enrichments["text_to_image"]

    @pytest.mark.asyncio
    async def test_ignores_unknown_inference_types(self):
        mock_models = [
            make_model("UnknownModel", ["some_new_type"], info=None),
        ]
        mock_response = ModelsResponse(data=mock_models)

        mock_client = AsyncMock()
        mock_client.get_models = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.deapi_client.get_client", return_value=mock_client):
            await _fetch_and_index_models()

        assert len(_cache.tool_models) == 0
        assert len(_cache.enrichments) == 0


# ---------------------------------------------------------------------------
# Cache staleness tests
# ---------------------------------------------------------------------------

class TestCacheStaleness:
    @pytest.fixture(autouse=True)
    def reset(self):
        _reset_cache()
        yield
        _reset_cache()

    def test_stale_when_never_fetched(self):
        assert _cache.is_stale(300.0) is True

    def test_not_stale_when_just_fetched(self):
        _cache.last_fetched = time.monotonic()
        assert _cache.is_stale(300.0) is False

    def test_stale_after_ttl(self):
        _cache.last_fetched = time.monotonic() - 301.0
        assert _cache.is_stale(300.0) is True

    def test_not_stale_within_ttl(self):
        _cache.last_fetched = time.monotonic() - 100.0
        assert _cache.is_stale(300.0) is False


# ---------------------------------------------------------------------------
# ModelEnrichmentMiddleware tests
# ---------------------------------------------------------------------------

class TestModelEnrichmentMiddleware:
    @pytest.fixture(autouse=True)
    def reset(self):
        _reset_cache()
        yield
        _reset_cache()

    @pytest.mark.asyncio
    async def test_enriches_matching_tools(self):
        # Pre-populate cache
        _cache.enrichments = {
            "text_to_image": "---\nAvailable models:\n  - `TestModel`",
        }
        _cache.last_fetched = time.monotonic()

        tools = [
            FakeTool(name="text_to_image", description="Generate images"),
            FakeTool(name="get_balance", description="Check balance"),
        ]

        async def mock_call_next(ctx):
            return tools

        middleware = ModelEnrichmentMiddleware(ttl=300.0)
        context = MagicMock()

        result = await middleware.on_list_tools(context, mock_call_next)

        assert len(result) == 2
        # text_to_image should be enriched
        assert "Available models:" in result[0].description
        assert "`TestModel`" in result[0].description
        assert result[0].description.startswith("Generate images")
        # get_balance should be unchanged
        assert result[1].description == "Check balance"

    @pytest.mark.asyncio
    async def test_no_enrichment_when_cache_empty(self):
        # Cache is empty (never fetched, but we set last_fetched to avoid fetch)
        _cache.last_fetched = time.monotonic()

        tools = [FakeTool(name="text_to_image", description="Generate images")]

        async def mock_call_next(ctx):
            return tools

        middleware = ModelEnrichmentMiddleware(ttl=300.0)
        context = MagicMock()

        result = await middleware.on_list_tools(context, mock_call_next)

        assert len(result) == 1
        assert result[0].description == "Generate images"

    @pytest.mark.asyncio
    async def test_fetches_models_when_cache_stale(self):
        mock_models = [make_model("FreshModel", ["txt2img"], info=None)]
        mock_response = ModelsResponse(data=mock_models)

        mock_client = AsyncMock()
        mock_client.get_models = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        tools = [FakeTool(name="text_to_image", description="Generate images")]

        async def mock_call_next(ctx):
            return tools

        middleware = ModelEnrichmentMiddleware(ttl=300.0)
        context = MagicMock()

        with patch("src.deapi_client.get_client", return_value=mock_client):
            result = await middleware.on_list_tools(context, mock_call_next)

        assert "`FreshModel`" in result[0].description
        mock_client.get_models.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_graceful_degradation_on_fetch_failure(self):
        mock_client = AsyncMock()
        mock_client.get_models = AsyncMock(side_effect=Exception("API down"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        tools = [FakeTool(name="text_to_image", description="Generate images")]

        async def mock_call_next(ctx):
            return tools

        middleware = ModelEnrichmentMiddleware(ttl=300.0)
        context = MagicMock()

        with patch("src.deapi_client.get_client", return_value=mock_client):
            result = await middleware.on_list_tools(context, mock_call_next)

        # Should return original tools unchanged
        assert len(result) == 1
        assert result[0].description == "Generate images"
        # Should update last_fetched to prevent retry storm
        assert _cache.last_fetched > 0

    @pytest.mark.asyncio
    async def test_fetch_failure_does_not_retry_immediately(self):
        mock_client = AsyncMock()
        mock_client.get_models = AsyncMock(side_effect=Exception("API down"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        tools = [FakeTool(name="text_to_image", description="Generate images")]

        async def mock_call_next(ctx):
            return tools

        middleware = ModelEnrichmentMiddleware(ttl=300.0)
        context = MagicMock()

        with patch("src.deapi_client.get_client", return_value=mock_client):
            await middleware.on_list_tools(context, mock_call_next)
            # Second call should NOT retry — cache TTL prevents it
            await middleware.on_list_tools(context, mock_call_next)

        mock_client.get_models.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_per_tool_enrichment_failure(self):
        """If model_copy fails for one tool, others still get enriched."""
        _cache.enrichments = {
            "text_to_image": "---\nAvailable models:\n  - `Model1`",
            "image_to_image": "---\nAvailable models:\n  - `Model2`",
        }
        _cache.last_fetched = time.monotonic()

        bad_tool = MagicMock()
        bad_tool.name = "text_to_image"
        bad_tool.description = "Generate images"
        bad_tool.model_copy = MagicMock(side_effect=RuntimeError("copy failed"))

        good_tool = FakeTool(name="image_to_image", description="Transform images")
        plain_tool = FakeTool(name="get_balance", description="Check balance")

        async def mock_call_next(ctx):
            return [bad_tool, good_tool, plain_tool]

        middleware = ModelEnrichmentMiddleware(ttl=300.0)
        context = MagicMock()

        result = await middleware.on_list_tools(context, mock_call_next)

        assert len(result) == 3
        # bad_tool: model_copy failed, should return original
        assert result[0].description == "Generate images"
        # good_tool: should be enriched
        assert "`Model2`" in result[1].description
        # plain_tool: no enrichment, unchanged
        assert result[2].description == "Check balance"

    @pytest.mark.asyncio
    async def test_skips_fetch_when_cache_fresh(self):
        _cache.enrichments = {
            "text_to_image": "---\nAvailable models:\n  - `CachedModel`",
        }
        _cache.last_fetched = time.monotonic()

        tools = [FakeTool(name="text_to_image", description="Generate images")]

        async def mock_call_next(ctx):
            return tools

        middleware = ModelEnrichmentMiddleware(ttl=300.0)
        context = MagicMock()

        with patch("src.middleware._fetch_and_index_models") as mock_fetch:
            result = await middleware.on_list_tools(context, mock_call_next)

        mock_fetch.assert_not_awaited()
        assert "`CachedModel`" in result[0].description

    @pytest.mark.asyncio
    async def test_handles_tool_with_none_description(self):
        _cache.enrichments = {
            "text_to_image": "---\nAvailable models:\n  - `TestModel`",
        }
        _cache.last_fetched = time.monotonic()

        tools = [FakeTool(name="text_to_image")]  # description defaults to ""

        async def mock_call_next(ctx):
            return tools

        middleware = ModelEnrichmentMiddleware(ttl=300.0)
        context = MagicMock()

        result = await middleware.on_list_tools(context, mock_call_next)

        assert "Available models:" in result[0].description


# ---------------------------------------------------------------------------
# Mapping coverage
# ---------------------------------------------------------------------------

class TestInferenceTypeMapping:
    def test_all_inference_types_have_two_tools(self):
        """Each inference type should map to both a main tool and a price tool."""
        for itype, tool_names in INFERENCE_TYPE_TO_TOOLS.items():
            assert len(tool_names) == 2, f"{itype} should map to 2 tools"
            price_tools = [t for t in tool_names if t.endswith("_price")]
            main_tools = [t for t in tool_names if not t.endswith("_price")]
            assert len(price_tools) == 1, f"{itype} should have one price tool"
            assert len(main_tools) == 1, f"{itype} should have one main tool"

    def test_expected_inference_types_present(self):
        expected = [
            "txt2img", "img2img", "txt2video", "img2video",
            "txt2audio", "txt2embedding",
            "audio_file2text", "audio2text",
            "video2text", "video_file2text",
            "img2txt", "img-rmbg", "img-upscale",
        ]
        for itype in expected:
            assert itype in INFERENCE_TYPE_TO_TOOLS, f"Missing mapping for {itype}"


# ---------------------------------------------------------------------------
# Public cache accessor tests
# ---------------------------------------------------------------------------

class TestCacheAccessors:
    def setup_method(self):
        _reset_cache()

    def teardown_method(self):
        _reset_cache()

    def test_get_cached_model_found(self):
        model = make_model("Flux1schnell", ["txt2img"], info={
            "defaults": {"steps": 4},
        })
        _cache.models_by_slug["Flux1schnell"] = model
        result = get_cached_model("Flux1schnell")
        assert result is model
        assert result.slug == "Flux1schnell"

    def test_get_cached_model_not_found(self):
        result = get_cached_model("NonExistentModel")
        assert result is None

    def test_get_cached_model_empty_cache(self):
        result = get_cached_model("Flux1schnell")
        assert result is None

    def test_get_cached_models_for_tool_found(self):
        model = make_model("Flux1schnell", ["txt2img"])
        _cache.tool_models["text_to_image"] = [model]
        result = get_cached_models_for_tool("text_to_image")
        assert len(result) == 1
        assert result[0].slug == "Flux1schnell"

    def test_get_cached_models_for_tool_not_found(self):
        result = get_cached_models_for_tool("nonexistent_tool")
        assert result == []

    def test_get_cached_models_for_tool_multiple_models(self):
        m1 = make_model("Flux1schnell", ["txt2img"])
        m2 = make_model("ZImageTurbo_INT8", ["txt2img"])
        _cache.tool_models["text_to_image"] = [m1, m2]
        result = get_cached_models_for_tool("text_to_image")
        assert len(result) == 2

    async def test_fetch_populates_models_by_slug(self):
        model_data = [
            make_model("Flux1schnell", ["txt2img"], info={"defaults": {"steps": 4}}),
            make_model("WhisperLargeV3", ["audio_file2text"]),
        ]
        mock_response = ModelsResponse(data=model_data)
        mock_client = AsyncMock()
        mock_client.get_models = AsyncMock(return_value=mock_response)

        with patch("src.deapi_client.get_client") as mock_get_client:
            mock_get_client.return_value = mock_client
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            await _fetch_and_index_models()

        assert "Flux1schnell" in _cache.models_by_slug
        assert "WhisperLargeV3" in _cache.models_by_slug
        assert _cache.models_by_slug["Flux1schnell"].slug == "Flux1schnell"
