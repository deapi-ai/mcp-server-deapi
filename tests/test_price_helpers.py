"""Tests for price calculation payload helpers."""

from unittest.mock import patch

from src.middleware import _cache
from src.schemas import ModelInfo
from src.tools._price_helpers import (
    get_model_defaults,
    get_model_features,
    get_model_limits,
    resolve_generation_params,
    _to_number,
)


def _make_model(slug, info=None):
    return ModelInfo(
        name=slug,
        slug=slug,
        inference_types=["txt2img"],
        info=info,
        loras=None,
    )


def _reset_cache():
    _cache.models_by_slug.clear()
    _cache.tool_models.clear()
    _cache.enrichments.clear()
    _cache.last_fetched = 0.0


class TestToNumber:
    def test_string_int(self):
        assert _to_number("4") == 4

    def test_string_float(self):
        assert _to_number("7.5") == 7.5

    def test_already_int(self):
        assert _to_number(4) == 4

    def test_already_float(self):
        assert _to_number(7.5) == 7.5

    def test_non_numeric_string(self):
        assert _to_number("hello") == "hello"


class TestGetModelDefaults:
    def setup_method(self):
        _reset_cache()

    def teardown_method(self):
        _reset_cache()

    def test_returns_defaults(self):
        model = _make_model("Flux1schnell", info={
            "defaults": {"steps": 4, "width": 768, "height": 768},
        })
        _cache.models_by_slug["Flux1schnell"] = model
        result = get_model_defaults("Flux1schnell")
        assert result == {"steps": 4, "width": 768, "height": 768}

    def test_returns_empty_for_unknown_model(self):
        assert get_model_defaults("UnknownModel") == {}

    def test_returns_empty_for_no_info(self):
        model = _make_model("NoInfo", info=None)
        _cache.models_by_slug["NoInfo"] = model
        assert get_model_defaults("NoInfo") == {}

    def test_returns_empty_for_list_info(self):
        model = _make_model("ListInfo", info=[])
        _cache.models_by_slug["ListInfo"] = model
        assert get_model_defaults("ListInfo") == {}


class TestGetModelFeatures:
    def setup_method(self):
        _reset_cache()

    def teardown_method(self):
        _reset_cache()

    def test_returns_features(self):
        model = _make_model("Flux1schnell", info={
            "features": {"supports_guidance": "0", "supports_steps": "1"},
        })
        _cache.models_by_slug["Flux1schnell"] = model
        result = get_model_features("Flux1schnell")
        assert result["supports_guidance"] == "0"

    def test_returns_empty_for_unknown_model(self):
        assert get_model_features("Unknown") == {}


class TestResolveGenerationParams:
    def setup_method(self):
        _reset_cache()

    def teardown_method(self):
        _reset_cache()

    def _setup_model(self, slug="TestModel", defaults=None, features=None):
        info = {}
        if defaults:
            info["defaults"] = defaults
        if features:
            info["features"] = features
        model = _make_model(slug, info=info)
        _cache.models_by_slug[slug] = model

    def test_uses_model_defaults(self):
        self._setup_model(defaults={
            "steps": 4, "width": 768, "height": 768, "guidance": 0,
        }, features={
            "supports_guidance": "0",
        })
        result = resolve_generation_params("TestModel", {})
        assert result["width"] == 768
        assert result["height"] == 768
        assert result["steps"] == 4
        assert result["guidance"] == 0
        assert result["seed"] == -1  # always defaults to -1

    def test_user_overrides_defaults(self):
        self._setup_model(defaults={
            "steps": 4, "width": 768, "height": 768,
        })
        result = resolve_generation_params("TestModel", {
            "width": 512, "height": 512, "steps": 10,
        })
        assert result["width"] == 512
        assert result["height"] == 512
        assert result["steps"] == 10

    def test_guidance_disabled(self):
        self._setup_model(defaults={"guidance": "0"}, features={
            "supports_guidance": "0",
        })
        result = resolve_generation_params("TestModel", {"guidance": 7.5})
        # Should ignore user value and use model default when supports_guidance=0
        assert result["guidance"] == 0

    def test_guidance_enabled(self):
        self._setup_model(defaults={"guidance": "7.5"}, features={
            "supports_guidance": "1",
        })
        result = resolve_generation_params("TestModel", {})
        assert result["guidance"] == 7.5

    def test_guidance_enabled_with_user_override(self):
        self._setup_model(defaults={"guidance": "7.5"}, features={
            "supports_guidance": "1",
        })
        result = resolve_generation_params("TestModel", {"guidance": 3.0})
        assert result["guidance"] == 3.0

    def test_cache_miss_uses_fallbacks(self):
        """When model not in cache, uses hardcoded fallbacks."""
        result = resolve_generation_params("NonExistent", {})
        assert result["seed"] == -1
        assert result["width"] == 512
        assert result["height"] == 512
        assert result["steps"] == 4
        assert result["guidance"] == 0

    def test_cache_miss_with_user_params(self):
        result = resolve_generation_params("NonExistent", {
            "width": 512, "height": 512,
        })
        assert result["width"] == 512
        assert result["height"] == 512
        assert result["seed"] == -1

    def test_user_seed_override(self):
        self._setup_model(defaults={"steps": 4})
        result = resolve_generation_params("TestModel", {"seed": 42})
        assert result["seed"] == 42

    def test_fps_and_frames(self):
        self._setup_model(defaults={
            "fps": 30, "frames": 120,
        })
        result = resolve_generation_params("TestModel", {})
        assert result["fps"] == 30
        assert result["frames"] == 120

    def test_string_defaults_converted_to_numbers(self):
        self._setup_model(defaults={
            "steps": "4", "width": "768", "height": "768",
        })
        result = resolve_generation_params("TestModel", {})
        assert result["steps"] == 4
        assert result["width"] == 768
        assert isinstance(result["steps"], int)

    def test_none_user_params_ignored(self):
        """None values in user_params should not override defaults."""
        self._setup_model(defaults={"steps": 4, "width": 768})
        result = resolve_generation_params("TestModel", {
            "steps": None, "width": 512,
        })
        assert result["steps"] == 4  # None → falls back to default
        assert result["width"] == 512  # Non-None → used
