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
from typing import Dict, List, Optional, Sequence

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool

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
    "audio2video": ["audio_to_video", "audio_to_video_price"],
    "txt2audio": ["text_to_audio", "text_to_audio_price"],
    "txt2music": ["text_to_music", "text_to_music_price"],
    "txt2embedding": ["text_to_embedding", "text_to_embedding_price"],
    "audio_file2text": ["audio_transcription", "audio_transcription_price"],
    "audio2text": ["audio_url_transcription", "audio_url_transcription_price"],
    "video2text": ["video_url_transcription", "video_url_transcription_price"],
    "video_file2text": ["video_file_transcription", "video_file_transcription_price"],
    "img2txt": ["image_to_text", "image_to_text_price"],
    "img-rmbg": ["image_remove_background", "image_remove_background_price"],
    "img-upscale": ["image_upscale", "image_upscale_price"],
    "video-replace": ["video_replace", "video_replace_price"],
    "vid-rmbg": ["video_remove_background", "video_remove_background_price"],
    "vid-upscale": ["video_upscale", "video_upscale_price"],
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

DEFAULT_CACHE_TTL = 300.0


async def ensure_model_cache(ttl: float = DEFAULT_CACHE_TTL) -> None:
    """Populate or refresh the model cache when it is older than ``ttl``.

    Tools call this before reading a card, so a call that arrives before any
    tools/list — or right after a restart — still resolves the model's own
    defaults instead of guessing. A failed fetch is logged and stamped like a
    refresh, so an unreachable catalogue is retried once per TTL rather than
    on every call.
    """
    if not _cache.is_stale(ttl):
        return
    async with _cache.lock:
        if not _cache.is_stale(ttl):
            return
        try:
            await _fetch_and_index_models()
        except Exception:
            logger.warning("Failed to fetch the model catalogue", exc_info=True)
            _cache.last_fetched = time.monotonic()


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
        if str(min_steps) == str(max_steps):
            parts.append(f"steps={min_steps} (fixed)")
        else:
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
            if str(min_g) == str(max_g):
                parts.append(f"guidance={min_g} (fixed)")
            else:
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

    # Duration (music models)
    min_dur = limits.get("min_duration")
    max_dur = limits.get("max_duration")
    if min_dur is not None and max_dur is not None:
        parts.append(f"duration={min_dur}-{max_dur}s")

    # BPM (music models)
    min_bpm = limits.get("min_bpm")
    max_bpm = limits.get("max_bpm")
    if min_bpm is not None and max_bpm is not None:
        parts.append(f"bpm={min_bpm}-{max_bpm}")

    # Bounds the API enforces with a 422 that the LLM has no other way to know.
    # Each is shown only when the card publishes it (many are null), and each
    # maps to an error already observed against the live API.
    parts.extend(_format_request_bounds(limits))

    # LoRAs
    if model.loras:
        parts.append(f"{len(model.loras)} LoRAs available")

    # TTS: mode, output format, then languages and voices on their own line
    tts_line = _format_tts_languages(model, features, limits)
    if tts_line:
        mode = next(
            (m for f, m in _TTS_MODE_FEATURES if str(features.get(f)).lower() in ("1", "true")),
            None,
        )
        if mode:
            parts.append(f"mode={mode}")
        fmts = limits.get("output_formats")
        if isinstance(fmts, list) and fmts:
            parts.append(f"format={'/'.join(map(str, fmts))}")

    line = f"  - `{model.slug}`: {', '.join(parts)}" if parts else f"  - `{model.slug}`"
    return f"{line}\n{tts_line}" if tts_line else line


def _range(limits: dict, lo_key: str, hi_key: str, label: str, unit: str = "") -> Optional[str]:
    """'label=lo-hi' when the card publishes both ends, else None."""
    lo, hi = limits.get(lo_key), limits.get(hi_key)
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        if str(lo) == str(hi):
            return f"{label}={lo}{unit} (fixed)"
        return f"{label}={lo}-{hi}{unit}"
    return f"{label}≤{hi}{unit}" if hi is not None else f"{label}≥{lo}{unit}"


def _format_request_bounds(limits: dict) -> List[str]:
    """Per-request limits the API validates, grouped by the tool they affect.

    Kept to the ones that produce a 422 the LLM could not have predicted:
    - images: dimension multiples, pixel/side/ratio caps, input-image count,
      accepted quality tiers (1024x1024 on a 1,032,192-px model was a live 422)
    - TTS: text length, speed, sample rate (our Field said speed 0.1-3.0;
      Kokoro accepts 0.5-2)
    - transcription: which ts_level values the model implements
      (WhisperLargeV3 is segment-only; word needs WhisperLargeV3Ct2)
    - video / upscale / music / embedding: their own caps
    """
    out: List[str] = []

    # --- images
    step = limits.get("resolution_step") or limits.get("dimension_multiple")
    if step:
        out.append(f"w/h multiple of {step}")
    if limits.get("max_pixels"):
        out.append(f"≤{limits['max_pixels'] / 1e6:.2f}Mpx")
    if limits.get("max_side"):
        out.append(f"side≤{limits['max_side']}")
    if limits.get("max_ratio"):
        out.append(f"aspect≤{limits['max_ratio']}")
    if limits.get("max_input_images"):
        out.append(f"up to {limits['max_input_images']} input image(s)")
    if limits.get("quality_options"):
        out.append(f"quality: {'|'.join(map(str, limits['quality_options']))}")

    # --- TTS
    for item in (
        _range(limits, "min_text", "max_text", "text", " chars"),
        _range(limits, "min_speed", "max_speed", "speed"),
    ):
        if item:
            out.append(item)
    # The card calls this `available_ratios`; on every TTS model it holds sample rates.
    if limits.get("available_ratios"):
        out.append(f"sample_rate: {'|'.join(map(str, limits['available_ratios']))}")

    # --- transcription
    if limits.get("timestamp_levels"):
        out.append(f"ts_level: {'|'.join(map(str, limits['timestamp_levels']))}")

    # --- video / upscale / music / embedding
    if limits.get("max_video_duration_seconds"):
        out.append(f"video≤{limits['max_video_duration_seconds']}s")
    for item in (
        _range(limits, "min_scale", "max_scale", "scale", "x"),
        _range(limits, "min_caption", "max_caption", "caption", " chars"),
    ):
        if item:
            out.append(item)
    if limits.get("max_input_tokens"):
        out.append(f"≤{limits['max_input_tokens']} tokens/input")
    return out


# Which feature flag names which TTS mode. Each model advertises exactly one.
_TTS_MODE_FEATURES = (
    ("supports_voice_clone", "voice_clone"),
    ("supports_voice_design", "voice_design"),
    ("supports_custom_voice", "custom_voice"),
)

_VOICES_SHOWN_PER_LANGUAGE = 5


def _format_tts_languages(model: ModelInfo, features: dict, limits: dict) -> Optional[str]:
    """One indented line listing a TTS model's languages and voice slugs.

    The `lang` and `voice` values are per-model and not guessable — Kokoro
    takes 'en-us' + 'af_alloy', the Qwen3 models take 'English' — so without
    this an LLM has no way to form a valid request. Kept compact: voices are
    capped per language, and a model whose every language offers the same
    single voice (Chatterbox: 23 languages, all 'Default') is collapsed.
    """
    if not model.languages:
        return None

    langs = model.languages
    voice_sets = [tuple(v.slug for v in lang.voices) for lang in langs]

    # Voice-cloning / voice-design: languages only, the voice comes from elsewhere
    if all(not vs for vs in voice_sets):
        is_clone = str(features.get("supports_voice_clone")).lower() in ("1", "true")
        src = "ref_audio" if is_clone else "instruct"
        # The API rejects a reference clip outside this window; stating it here
        # saves a failed call (a 4.8s sample against a 5s minimum is a real case).
        lo, hi = limits.get("min_ref_audio_duration"), limits.get("max_ref_audio_duration")
        bound = f", {lo}-{hi}s" if is_clone and lo is not None and hi is not None else ""
        return f"      langs: {', '.join(lang.slug for lang in langs)} (voice from {src}{bound})"

    # Same voice list in every language that has one: list it once. Covers
    # Chatterbox (23 languages, all 'default') and the Qwen3 CustomVoice model
    # (the same named voices in every language).
    distinct = {vs for vs in voice_sets if vs}
    if len(distinct) == 1:
        voices = list(next(iter(distinct)))
        shown = ", ".join(voices[:_VOICES_SHOWN_PER_LANGUAGE])
        more = len(voices) - _VOICES_SHOWN_PER_LANGUAGE
        suffix = f", +{more}" if more > 0 else ""
        voiceless = [lang.slug for lang, vs in zip(langs, voice_sets) if not vs]
        note = f" (no voice listed for: {', '.join(voiceless)})" if voiceless else ""
        return (
            f"      langs: {', '.join(lang.slug for lang in langs)} ({len(langs)}); "
            f"voices ({len(voices)}, same in every language): {shown}{suffix}{note}"
        )

    chunks = []
    for lang in langs:
        slugs = [v.slug for v in lang.voices]
        if not slugs:
            chunks.append(lang.slug)
            continue
        shown = ", ".join(slugs[:_VOICES_SHOWN_PER_LANGUAGE])
        more = len(slugs) - _VOICES_SHOWN_PER_LANGUAGE
        suffix = f", +{more}" if more > 0 else ""
        chunks.append(f"{lang.slug} [{len(slugs)}: {shown}{suffix}]")
    return "      langs: " + "; ".join(chunks)


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

    def __init__(self, ttl: float = DEFAULT_CACHE_TTL):
        super().__init__()
        self._ttl = ttl

    async def _ensure_cache_fresh(self) -> None:
        """Refresh model cache if stale."""
        await ensure_model_cache(self._ttl)

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
                    enriched.append(tool.model_copy(update={"description": new_desc}))
                except Exception:
                    logger.warning("Failed to enrich tool %s", tool.name, exc_info=True)
                    enriched.append(tool)
            else:
                enriched.append(tool)
        return enriched
