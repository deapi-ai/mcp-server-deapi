"""Helpers for resolving request parameters from the model cache.

Uses the global model cache (populated by ModelEnrichmentMiddleware, or on
demand via ``ensure_model_cache``) to resolve model defaults and features, so
neither the price tools nor the generation tools need hardcoded parameter
values. No single number suits every model: a ``steps`` of 20 is over the
limit on Flux1schnell (max 10) and Flux_2_Klein (fixed at 4), and the video
models each fix ``fps`` at a different value.
"""

from typing import Any, Dict, Optional, Tuple

from ..middleware import get_cached_model
from ..schemas import ModelInfo


def _get_model_info_section(model: Optional[ModelInfo], section: str) -> Dict[str, Any]:
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


def feature_enabled(model_slug: str, feature: str) -> bool:
    """True only when the card turns the flag on (true/1).

    Unknown or absent counts as off: a knob is sent only to a model known
    to take it, never guessed at a model whose card is not loaded.
    """
    value = get_model_features(model_slug).get(feature)
    return value is not None and str(value).lower() in ("1", "true")


def resolve_upscale_factor(
    model_slug: str, scale: Optional[int]
) -> Tuple[Optional[int], Optional[str]]:
    """The `scale` to send for a video upscale, or an error to return instead.

    A model that publishes `min_scale`/`max_scale` requires the field and one
    that publishes neither rejects it (VideoUpscaleRequest). Unset on a ranged
    model, the smallest factor is used. With no card at all the caller's value
    is passed through for the API to judge.
    """
    card = get_cached_model(model_slug)
    if card is None:
        return scale, None
    limits = get_model_limits(model_slug)
    lo, hi = limits.get("min_scale"), limits.get("max_scale")
    if lo is None and hi is None:
        if scale is not None:
            return None, (
                f"{model_slug} has a fixed upscale factor and does not take 'scale'; leave it unset."
            )
        return None, None
    if scale is None:
        scale = _to_number(lo if lo is not None else hi)
    return scale, None


def resolve_from_card(model_slug: str, user_params: Dict[str, Any]) -> Dict[str, Any]:
    """Fill each unset parameter from the model card's defaults.

    A user value always wins. A key the caller left unset and the card does
    not default is omitted, so the API — which knows whether that model needs
    it — can say so, rather than this client inventing a number that might be
    out of range. Unlike ``resolve_generation_params`` nothing is guessed:
    this is for the payload that actually runs the job.
    """
    defaults = get_model_defaults(model_slug)
    resolved: Dict[str, Any] = {}
    for key, value in user_params.items():
        if value is None:
            value = _to_number(defaults.get(key))
        if value is not None:
            resolved[key] = value
    return resolved


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
        "width": 512,
        "height": 512,
        "steps": 4,
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
