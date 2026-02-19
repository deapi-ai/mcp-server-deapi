"""Model-aware tool description enrichment middleware for FastMCP.

Intercepts tools/list responses and appends available model information
(slugs, parameter limits, guidance requirements) to each tool's description.
This helps LLMs choose correct models and parameters without needing to call
get_available_models first.

Opt-in via DEAPI_ENRICH_TOOL_DESCRIPTIONS=true env var.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import mcp.types as mt
from fastmcp.server.middleware import Middleware, MiddlewareContext, CallNext
from fastmcp.tools.tool import Tool

from .schemas import ModelInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inference type → MCP tool name mapping
# ---------------------------------------------------------------------------

INFERENCE_TYPE_TO_TOOLS: Dict[str, List[str]] = {
    "txt2img": ["text_to_image", "text_to_image_price"],
    "img2img": ["image_to_image", "image_to_image_price"],
    "txt2video": ["text_to_video", "text_to_video_price"],
    "img2video": ["image_to_video", "image_to_video_price"],
    "txt2audio": ["text_to_audio", "text_to_audio_price"],
    "txt2embedding": ["text_to_embedding", "text_to_embedding_price"],
    "audio_file2text": ["audio_transcription", "audio_transcription_price"],
    "audio2text": ["audio_url_transcription", "audio_url_transcription_price"],
    "video2text": ["video_url_transcription", "video_url_transcription_price"],
    "video_file2text": ["video_file_transcription", "video_file_transcription_price"],
    "img2txt": ["image_to_text", "image_to_text_price"],
    "img-rmbg": ["image_remove_background", "image_remove_background_price"],
    "img-upscale": ["image_upscale", "image_upscale_price"],
}


# ---------------------------------------------------------------------------
# Model cache
# ---------------------------------------------------------------------------

@dataclass
class _ModelCache:
    """Thread-safe cache for model information indexed by tool name."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tool_models: Dict[str, List[ModelInfo]] = field(default_factory=dict)
    models_by_slug: Dict[str, ModelInfo] = field(default_factory=dict)
    enrichments: Dict[str, str] = field(default_factory=dict)
    last_fetched: float = 0.0

    def is_stale(self, ttl: float) -> bool:
        return (time.monotonic() - self.last_fetched) > ttl


_cache = _ModelCache()


# ---------------------------------------------------------------------------
# Public cache accessors
# ---------------------------------------------------------------------------

def get_cached_model(slug: str) -> Optional[ModelInfo]:
    """Look up a model by slug from the cache.

    Returns None if the model is not in the cache (e.g., cache not yet
    populated or model slug unknown).
    """
    return _cache.models_by_slug.get(slug)


def get_cached_models_for_tool(tool_name: str) -> List[ModelInfo]:
    """Get all cached models available for a given tool name."""
    return _cache.tool_models.get(tool_name, [])


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_model_info(model: ModelInfo) -> str:
    """Format a single model into a concise one-line description.

    Shows slug and key parameters: steps, size, guidance, fps, frames, LoRAs.
    """
    info = model.info
    if not isinstance(info, dict):
        return f"  - `{model.slug}`"

    limits = info.get("limits") or {}
    defaults = info.get("defaults") or {}
    features = info.get("features") or {}

    parts: List[str] = []

    # Steps
    min_steps = limits.get("min_steps")
    max_steps = limits.get("max_steps")
    default_steps = defaults.get("steps")
    if min_steps is not None and max_steps is not None:
        step_str = f"steps={min_steps}-{max_steps}"
        if default_steps is not None:
            step_str += f" (default {default_steps})"
        parts.append(step_str)

    # Size (width x height)
    min_w = limits.get("min_width")
    max_w = limits.get("max_width")
    min_h = limits.get("min_height")
    max_h = limits.get("max_height")
    if all(v is not None for v in [min_w, max_w, min_h, max_h]):
        parts.append(f"size={min_w}-{max_w}x{min_h}-{max_h}")

    # Guidance
    supports_guidance = str(features.get("supports_guidance", "1"))
    if supports_guidance == "0":
        g_val = defaults.get("guidance", "0")
        parts.append(f"guidance={g_val} (FIXED, must use this value)")
    else:
        min_g = limits.get("min_guidance")
        max_g = limits.get("max_guidance")
        default_g = defaults.get("guidance")
        if min_g is not None and max_g is not None:
            g_str = f"guidance={min_g}-{max_g}"
            if default_g is not None:
                g_str += f" (default {default_g})"
            parts.append(g_str)

    # FPS (video models)
    min_fps = limits.get("min_fps")
    max_fps = limits.get("max_fps")
    if min_fps is not None and max_fps is not None:
        if str(min_fps) == str(max_fps):
            parts.append(f"fps={min_fps} (fixed)")
        else:
            parts.append(f"fps={min_fps}-{max_fps}")

    # Frames (video models)
    min_frames = limits.get("min_frames")
    max_frames = limits.get("max_frames")
    if min_frames is not None and max_frames is not None:
        parts.append(f"frames={min_frames}-{max_frames}")

    # LoRAs
    if model.loras:
        parts.append(f"{len(model.loras)} LoRAs available")

    if parts:
        return f"  - `{model.slug}`: {', '.join(parts)}"
    return f"  - `{model.slug}`"


def _build_enrichment_block(models: List[ModelInfo]) -> str:
    """Build the enrichment text block for a list of models."""
    if not models:
        return ""
    lines = ["---", "Available models:"]
    for model in models:
        lines.append(_format_model_info(model))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Model fetching & indexing
# ---------------------------------------------------------------------------

async def _fetch_and_index_models() -> None:
    """Fetch models from deAPI and index by tool name."""
    from .deapi_client import get_client

    async with get_client() as client:
        response = await client.get_models()

    tool_models: Dict[str, List[ModelInfo]] = {}
    for model in response.data:
        inference_types = model.inference_types
        if isinstance(inference_types, list):
            for itype in inference_types:
                tool_names = INFERENCE_TYPE_TO_TOOLS.get(itype, [])
                for tool_name in tool_names:
                    tool_models.setdefault(tool_name, []).append(model)

    enrichments: Dict[str, str] = {}
    for tool_name, models in tool_models.items():
        block = _build_enrichment_block(models)
        if block:
            enrichments[tool_name] = block

    _cache.tool_models = tool_models
    _cache.models_by_slug = {m.slug: m for m in response.data}
    _cache.enrichments = enrichments
    _cache.last_fetched = time.monotonic()

    logger.info(
        "Model cache refreshed: %d models indexed across %d tools",
        sum(len(m) for m in tool_models.values()),
        len(enrichments),
    )


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class ModelEnrichmentMiddleware(Middleware):
    """Middleware that enriches tool descriptions with available model info.

    Intercepts tools/list responses and appends model specs (slugs, parameter
    limits, guidance requirements) to each tool's description. Uses a TTL-based
    cache to avoid fetching models on every request.
    """

    def __init__(self, ttl: float = 300.0):
        super().__init__()
        self._ttl = ttl

    async def _ensure_cache_fresh(self) -> None:
        """Refresh model cache if stale, using double-check locking."""
        if not _cache.is_stale(self._ttl):
            return
        async with _cache.lock:
            if not _cache.is_stale(self._ttl):
                return
            try:
                await _fetch_and_index_models()
            except Exception:
                logger.warning(
                    "Failed to fetch models for description enrichment",
                    exc_info=True,
                )
                # Update timestamp even on failure to prevent retry storm
                _cache.last_fetched = time.monotonic()

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        """Enrich tool descriptions with available model information."""
        tools = await call_next(context)

        await self._ensure_cache_fresh()

        if not _cache.enrichments:
            return tools

        enriched: List[Tool] = []
        for tool in tools:
            enrichment = _cache.enrichments.get(tool.name)
            if enrichment:
                try:
                    new_desc = (tool.description or "") + "\n\n" + enrichment
                    enriched.append(
                        tool.model_copy(update={"description": new_desc})
                    )
                except Exception:
                    logger.warning("Failed to enrich tool %s", tool.name, exc_info=True)
                    enriched.append(tool)
            else:
                enriched.append(tool)
        return enriched
