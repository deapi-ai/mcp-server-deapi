"""Helpers for building price calculation request payloads.

Uses the global model cache (populated by ModelEnrichmentMiddleware) to
resolve model defaults and features, so price tools don't need hardcoded
parameter values.
"""

from typing import Any, Dict, Optional

from ..middleware import get_cached_model
from ..schemas import ModelInfo


def _get_model_info_section(
    model: Optional[ModelInfo], section: str
) -> Dict[str, Any]:
    """Safely extract a section (defaults, features, limits) from model info."""
    if model is None or not isinstance(model.info, dict):
        return {}
    value = model.info.get(section)
    return value if isinstance(value, dict) else {}


def get_model_defaults(model_slug: str) -> Dict[str, Any]:
    """Get defaults dict from cached model info."""
    return _get_model_info_section(get_cached_model(model_slug), "defaults")


def get_model_features(model_slug: str) -> Dict[str, Any]:
    """Get features dict from cached model info."""
    return _get_model_info_section(get_cached_model(model_slug), "features")


def get_model_limits(model_slug: str) -> Dict[str, Any]:
    """Get limits dict from cached model info."""
    return _get_model_info_section(get_cached_model(model_slug), "limits")


def resolve_generation_params(
    model_slug: str,
    user_params: Dict[str, Any],
) -> Dict[str, Any]:
    """Build generation price params from model defaults + user overrides.

    For each standard generation parameter (width, height, steps, guidance,
    seed, fps, frames), uses the user-provided value if present, otherwise
    falls back to the model's defaults from the cache.

    Guidance is handled specially:
    - If features.supports_guidance is "0" (or false), uses the model's
      fixed default (usually 0).
    - Otherwise uses user value or model default.

    Seed defaults to -1 if not provided by user or model.

    Returns a dict of resolved params (does NOT include 'model' or 'prompt' —
    those are always added by the caller).
    """
    defaults = get_model_defaults(model_slug)
    features = get_model_features(model_slug)

    # Fallback defaults when cache is empty (e.g., first request before
    # tools/list triggers the middleware cache population).
    _fallbacks = {
        "width": 512, "height": 512, "steps": 4,
    }

    params: Dict[str, Any] = {}

    # Standard numeric params: use user value → model default → fallback
    for key in ("width", "height", "steps", "fps", "frames"):
        user_val = user_params.get(key)
        if user_val is not None:
            params[key] = user_val
        elif key in defaults:
            params[key] = _to_number(defaults[key])
        elif key in _fallbacks:
            params[key] = _fallbacks[key]

    # Guidance: respect supports_guidance feature flag
    supports_guidance = str(features.get("supports_guidance", "1"))
    if supports_guidance in ("0", "false", "False"):
        # Model doesn't support custom guidance — use fixed default
        params["guidance"] = _to_number(defaults.get("guidance", 0))
    else:
        user_guidance = user_params.get("guidance")
        if user_guidance is not None:
            params["guidance"] = user_guidance
        elif "guidance" in defaults:
            params["guidance"] = _to_number(defaults["guidance"])
        else:
            params["guidance"] = 0  # safe fallback

    # Seed: always include, default -1
    params["seed"] = user_params.get("seed", -1)

    return params


def _to_number(value: Any) -> Any:
    """Convert string numbers from API defaults to int/float."""
    if isinstance(value, str):
        try:
            if "." in value:
                return float(value)
            return int(value)
        except (ValueError, TypeError):
            return value
    return value
