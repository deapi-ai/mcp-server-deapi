"""Model-aware tool description enrichment middleware for FastMCP.

Intercepts tools/list responses and appends available model information
(slugs, parameter limits, guidance requirements) to each tool's description.
This helps LLMs choose correct models and parameters without needing to call
get_available_models first.

Opt-in via DEAPI_ENRICH_TOOL_DESCRIPTIONS=true env var.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, cast

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool

from .auth import current_deapi_token
from .observability import errors_tolerated
from .schemas import ModelInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inference type → MCP tool name mapping
# ---------------------------------------------------------------------------

TEST_MODEL_STATUS = "test_model"
TEST_MODEL_NOTE = (
    "  Models marked (test model) are experimental: they may change or disappear. "
    "Prefer an unmarked model unless the caller asks for one of them."
)

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
    # One tool, two types: the API picks video2desc for a URL and video_file2desc for an upload.
    "video2desc": ["video_description", "video_description_price"],
    "video_file2desc": ["video_description", "video_description_price"],
    "vid-upscale": ["video_upscale", "video_upscale_price"],
}


# ---------------------------------------------------------------------------
# Model cache
# ---------------------------------------------------------------------------


@dataclass
class _ModelCache:
    """One account's model catalogue, indexed by tool name and by slug."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tool_models: Dict[str, List[ModelInfo]] = field(default_factory=dict)
    models_by_slug: Dict[str, ModelInfo] = field(default_factory=dict)
    enrichments: Dict[str, str] = field(default_factory=dict)
    # None = never. Not 0.0: monotonic() counts from boot, so on a host up for less
    # than the TTL a 0.0 stamp would read as a fresh fetch.
    last_fetched: Optional[float] = None
    last_miss_refresh: Optional[float] = None
    key: str = ""

    def is_stale(self, ttl: float) -> bool:
        return self.last_fetched is None or (time.monotonic() - self.last_fetched) > ttl


DEFAULT_CACHE_TTL = 300.0
# A model missing from a fresh catalogue (e.g. just granted to the account)
# triggers a refetch at most this often, so unknown slugs cannot flood the API.
MISS_REFRESH_INTERVAL = 30.0
MAX_CACHED_USERS = 1000
# Tokens whose catalogue has not been fetched successfully yet. Any string is
# accepted as a bearer token, so these are kept apart: random tokens can only
# churn this small pool, never evict users whose catalogue loaded.
MAX_UNCONFIRMED_USERS = 256


def _set_bounded(
    entries: "OrderedDict[str, _ModelCache]", key: str, cache: _ModelCache, limit: int
) -> None:
    entries[key] = cache
    while len(entries) > limit:
        entries.popitem(last=False)


class _PerUserCaches:
    """One catalogue per deAPI token, least recently used evicted first.

    Accounts do not all see the same models, so a catalogue fetched with one
    user's token must never describe or default another user's calls. Entries
    are keyed by an HMAC of the token under a per-process random key, so the
    raw token is never held as a key and the key means nothing outside this
    process. An entry joins the main pool only once a fetch with its token
    succeeds (``confirm``).
    """

    def __init__(
        self,
        max_entries: int = MAX_CACHED_USERS,
        max_unconfirmed: int = MAX_UNCONFIRMED_USERS,
    ) -> None:
        self.max_entries = max_entries
        self.max_unconfirmed = max_unconfirmed
        self._entries: "OrderedDict[str, _ModelCache]" = OrderedDict()
        self._unconfirmed: "OrderedDict[str, _ModelCache]" = OrderedDict()
        self._hmac_key = secrets.token_bytes(32)

    def _user_key(self, token: Optional[str]) -> str:
        if not token:
            return ""
        return hmac.new(self._hmac_key, token.encode(), hashlib.sha256).hexdigest()

    def current(self) -> _ModelCache:
        """The cache of the user whose token is on the current request."""
        key = self._user_key(current_deapi_token.get())
        cache = self._entries.get(key)
        if cache is not None:
            self._entries.move_to_end(key)
            return cache
        cache = self._unconfirmed.get(key)
        if cache is not None:
            self._unconfirmed.move_to_end(key)
            return cache
        cache = _ModelCache(key=key)
        _set_bounded(self._unconfirmed, key, cache, self.max_unconfirmed)
        return cache

    def confirm(self, cache: _ModelCache) -> None:
        """Move a cache whose fetch succeeded into the main pool."""
        if self._unconfirmed.get(cache.key) is cache:
            del self._unconfirmed[cache.key]
            _set_bounded(self._entries, cache.key, cache, self.max_entries)

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._unconfirmed.clear()


_caches = _PerUserCaches()


class _CurrentUserCache:
    """``_cache.x`` reads and writes the calling user's ``_ModelCache``."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_caches.current(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(_caches.current(), name, value)


_cache = _CurrentUserCache()


def _needs_miss_refresh(cache: _ModelCache, model: Optional[str]) -> bool:
    return (
        bool(model)
        and cache.last_fetched is not None
        and model not in cache.models_by_slug
        and (
            cache.last_miss_refresh is None
            or (time.monotonic() - cache.last_miss_refresh) > MISS_REFRESH_INTERVAL
        )
    )


async def ensure_model_cache(ttl: float = DEFAULT_CACHE_TTL, model: Optional[str] = None) -> None:
    """Populate or refresh the caller's model cache when it is older than ``ttl``.

    Tools call this before reading a card, so a call that arrives before any
    tools/list — or right after a restart — still resolves the model's own
    defaults instead of guessing. Passing ``model`` also refetches a fresh
    catalogue that lacks that slug (at most once per MISS_REFRESH_INTERVAL),
    so a model the account gained since the last fetch is found. A failed
    fetch is logged and stamped like a refresh, so an unreachable catalogue
    is retried once per TTL rather than on every call.
    """
    cache = _caches.current()
    if not cache.is_stale(ttl) and not _needs_miss_refresh(cache, model):
        return
    async with cache.lock:
        stale = cache.is_stale(ttl)
        if not stale and not _needs_miss_refresh(cache, model):
            return
        if not stale:
            cache.last_miss_refresh = time.monotonic()
        try:
            # The catalogue is optional here: a failed fetch is survived, so an error
            # it notes must not become the label of the tool call that triggered it.
            with errors_tolerated():
                await _fetch_and_index_models(cache)
        except Exception:
            logger.warning("Failed to fetch the model catalogue", exc_info=True)
            cache.last_fetched = time.monotonic()


# ---------------------------------------------------------------------------
# Public cache accessors
# ---------------------------------------------------------------------------


def get_cached_model(slug: str) -> Optional[ModelInfo]:
    """Look up a model by slug from the cache.

    Returns None if the model is not in the cache (e.g., cache not yet
    populated or model slug unknown).
    """
    return cast(Optional[ModelInfo], _cache.models_by_slug.get(slug))


def get_cached_models_for_tool(tool_name: str) -> List[ModelInfo]:
    """Get all cached models available for a given tool name."""
    return cast(List[ModelInfo], _cache.tool_models.get(tool_name, []))


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_model_info(model: ModelInfo) -> str:
    """Format a single model into a concise one-line description.

    Shows slug and key parameters: steps, size, guidance, fps, frames, LoRAs.
    """
    head = f"  - `{model.slug}`"
    if is_test_model(model):
        head += " (test model)"

    info = model.info
    if not isinstance(info, dict):
        return head

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
        types = model.inference_types if isinstance(model.inference_types, list) else []
        label: Optional[str] = (
            "input size" if types and set(types) <= _INPUT_SIZED_TYPES else "size"
        )
        parts.append(f"{label}={min_w}-{max_w}x{min_h}-{max_h}")
    edits = isinstance(model.inference_types, list) and "img2img" in model.inference_types
    if edits and _flag(features, "supports_custom_output_size") is False:
        # Image edits: width/height are dropped and the output keeps the input's size.
        parts.append("output size = input size")

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
    # Input images only exist on edits; a txt2img-only card still carries the count.
    bounds_limits = (
        limits if edits else {k: v for k, v in limits.items() if k != "max_input_images"}
    )
    parts.extend(_format_request_bounds(bounds_limits, defaults))
    parts.extend(_format_transcription(features, limits))

    # A reference/driving audio window on a model that is neither TTS (shown on
    # its languages line) nor a multi-reference video model (shown under refs).
    max_ref_audios = limits.get("max_ref_audios")
    if not model.languages and not (
        isinstance(max_ref_audios, (int, float)) and max_ref_audios > 0
    ):
        types = model.inference_types if isinstance(model.inference_types, list) else []
        label = (
            "ref_audio"
            if "txt2music" in types
            else "audio2video audio" if "audio2video" in types else None
        )
        window = (
            _range(limits, "min_ref_audio_duration", "max_ref_audio_duration", label, "s")
            if label
            else None
        )
        if window:
            parts.append(window)

    # The API silently drops negative_prompt on these; say so where it would be sent.
    types = model.inference_types if isinstance(model.inference_types, list) else []
    if _flag(features, "supports_negative_prompt") is False and set(types) & _GENERATION_TYPES:
        parts.append("no negative_prompt")

    # LoRAs: the API validates `name`, not the display name, so list the names.
    if model.loras:
        names: List[Any] = [
            getattr(lora, "name", None) or (lora.get("name") if isinstance(lora, dict) else None)
            for lora in model.loras
        ]
        names = [n for n in names if n]
        parts.append(
            f"loras: {', '.join(names)}" if names else f"{len(model.loras)} LoRAs available"
        )

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

    line = f"{head}: {', '.join(parts)}" if parts else head
    return f"{line}\n{tts_line}" if tts_line else line


def is_test_model(model: ModelInfo) -> bool:
    """Whether the catalogue flags this model as a test model."""
    return model.status == TEST_MODEL_STATUS


def _range(
    limits: Dict[str, Any], lo_key: str, hi_key: str, label: str, unit: str = ""
) -> Optional[str]:
    """'label=lo-hi' when the card publishes both ends, else None."""
    lo, hi = limits.get(lo_key), limits.get(hi_key)
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None:
        if str(lo) == str(hi):
            return f"{label}={lo}{unit} (fixed)"
        return f"{label}={lo}-{hi}{unit}"
    return f"{label}≤{hi}{unit}" if hi is not None else f"{label}≥{lo}{unit}"


def _format_request_bounds(
    limits: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None
) -> List[str]:
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
    # `dimension_multiple` is the real grid, and the API snaps to it rather than
    # rejecting. `resolution_step` is the web UI's slider step and is never
    # enforced (ValidatesDimensionLimits), so it is not shown.
    step = limits.get("dimension_multiple")
    if step and str(step) not in ("0", "1"):
        out.append(f"w/h rounded down to a multiple of {step}")
    if limits.get("max_pixels"):
        out.append(f"≤{limits['max_pixels'] / 1e6:.2f}Mpx")
    if limits.get("max_side"):
        out.append(f"side≤{limits['max_side']}")
    if limits.get("max_ratio"):
        out.append(f"aspect≤{limits['max_ratio']}")
    if limits.get("max_input_images"):
        out.append(f"up to {limits['max_input_images']} input image(s)")
    if limits.get("quality_options"):
        quality = f"quality: {'|'.join(map(str, limits['quality_options']))}"
        default_quality = (defaults or {}).get("quality")
        if default_quality:
            quality += f" (default {default_quality})"
        out.append(quality)

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

    # --- img2video reference mode (MiniMax-H3 and alike)
    refs = _format_reference_limits(limits)
    if refs:
        out.append(refs)

    # --- video description
    if limits.get("available_presets"):
        out.append(f"presets: {'|'.join(map(str, limits['available_presets']))}")

    # --- video / upscale / music / embedding / description
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


def _format_reference_limits(limits: Dict[str, Any]) -> Optional[str]:
    """'refs: ≤3 images, ≤1 video (2-5s), ≤3 audio (1-10s), ≤3 files total', or None.

    The API publishes the effective counts only on a model that supports at least
    one reference type, and an unsupported type as 0, so a positive count is the
    signal. Without this line an agent cannot tell which video model takes
    ref_images at all, let alone how many.
    """
    parts: List[str] = []
    for key, noun, lo_key, hi_key in (
        ("max_ref_images", "images", None, None),
        ("max_ref_videos", "video", "min_ref_video_duration", "max_ref_video_duration"),
        ("max_ref_audios", "audio", "min_ref_audio_duration", "max_ref_audio_duration"),
    ):
        count = limits.get(key)
        if not isinstance(count, (int, float)) or count <= 0:
            continue
        item = f"≤{count} {noun}"
        lo, hi = (limits.get(lo_key), limits.get(cast(str, hi_key))) if lo_key else (None, None)
        if lo is not None and hi is not None:
            item += f" ({lo}-{hi}s)"
        parts.append(item)
    if not parts:
        return None
    total = limits.get("max_ref_files")
    if isinstance(total, (int, float)) and total > 0:
        parts.append(f"≤{total} files total")
    return "refs: " + ", ".join(parts)


# Types whose card size bounds describe the uploaded input, not an output.
_INPUT_SIZED_TYPES = {"img-upscale", "img-rmbg", "img2txt", "vid-upscale", "vid-rmbg"}
# Types whose request carries a negative_prompt.
_GENERATION_TYPES = {"txt2img", "img2img", "txt2video", "img2video", "audio2video"}


def _flag(features: Dict[str, Any], name: str) -> Optional[bool]:
    """A feature flag as a bool, or None when the card does not set it.

    Cards carry booleans on newer models and "0"/"1" strings on older ones.
    """
    value = features.get(name)
    if value is None:
        return None
    return str(value).lower() in ("1", "true")


def _format_transcription(features: Dict[str, Any], limits: Dict[str, Any]) -> List[str]:
    """What a speech-to-text model can do, beyond its ts_level list.

    WhisperLargeV3 and WhisperLargeV3Ct2 differ in exactly these: only Ct2
    diarizes, and only Ct2 returns a JSON document (text + segments) instead
    of plain text. Diarizing on WhisperLargeV3 is a 422.
    """
    out: List[str] = []
    if _flag(features, "supports_timestamps") is False:
        out.append("no timestamps")
    if _flag(features, "supports_diarization"):
        out.append("diarize")
    fmt = limits.get("output_format")
    if fmt == "json":
        out.append("output=json (text + segments)")
    elif fmt:
        out.append(f"output={fmt}")
    langs = limits.get("languages")
    if isinstance(langs, list) and langs:
        out.append(f"lang: {'|'.join(map(str, langs))}")
    return out


# Which feature flag names which TTS mode. Each model advertises exactly one.
_TTS_MODE_FEATURES = (
    ("supports_voice_clone", "voice_clone"),
    ("supports_voice_design", "voice_design"),
    ("supports_custom_voice", "custom_voice"),
)

_VOICES_SHOWN_PER_LANGUAGE = 25


def _format_tts_languages(
    model: ModelInfo, features: Dict[str, Any], limits: Dict[str, Any]
) -> Optional[str]:
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
    # One list, or one list that every other merely lacks a few of (the Qwen3
    # CustomVoice model has no 'Ryan' in Chinese): print it once, plus the gaps.
    widest = max(distinct, key=len) if distinct else ()
    if distinct and all(set(vs) <= set(widest) for vs in distinct):
        voices = list(widest)
        shown = ", ".join(voices[:_VOICES_SHOWN_PER_LANGUAGE])
        more = len(voices) - _VOICES_SHOWN_PER_LANGUAGE
        suffix = f", +{more}" if more > 0 else ""
        notes = []
        voiceless = [lang.slug for lang, vs in zip(langs, voice_sets) if not vs]
        if voiceless:
            notes.append(f"no voice listed for: {', '.join(voiceless)}")
        for lang, vs in zip(langs, voice_sets):
            missing = [v for v in voices if vs and v not in vs]
            if missing:
                notes.append(f"{lang.slug} lacks {', '.join(missing)}")
        note = f" ({'; '.join(notes)})" if notes else ""
        same = "same in every language" if len(distinct) == 1 else "shared"
        return (
            f"      langs: {', '.join(lang.slug for lang in langs)} ({len(langs)}); "
            f"voices ({len(voices)}, {same}): {shown}{suffix}{note}"
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
    """Build the enrichment text block for a list of models.

    Standard models are listed first, in catalogue order; test models go last
    and are marked. The catalogue itself interleaves them, and an agent that
    finds no anchor in the description takes the first familiar name — which
    was a partner test model on text_to_image and image_to_image.
    """
    if not models:
        return ""
    ordered = sorted(models, key=is_test_model)  # stable: catalogue order within each group
    lines = ["---", "Available models:"]
    for model in ordered:
        lines.append(_format_model_info(model))
    if any(is_test_model(m) for m in ordered):
        lines.append(TEST_MODEL_NOTE)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Model fetching & indexing
# ---------------------------------------------------------------------------


async def _fetch_and_index_models(cache: Optional[_ModelCache] = None) -> None:
    """Fetch the caller's models from deAPI and index them by tool name."""
    from .deapi_client import get_client

    if cache is None:
        cache = _caches.current()

    async with get_client() as client:
        response = await client.get_models()

    tool_models: Dict[str, List[ModelInfo]] = {}
    for model in response.data:
        inference_types = model.inference_types
        if isinstance(inference_types, list):
            for itype in inference_types:
                tool_names = INFERENCE_TYPE_TO_TOOLS.get(itype, [])
                for tool_name in tool_names:
                    listed = tool_models.setdefault(tool_name, [])
                    # Two types can route to one tool (video2desc, video_file2desc);
                    # list the model once.
                    if model not in listed:
                        listed.append(model)

    enrichments: Dict[str, str] = {}
    for tool_name, models in tool_models.items():
        block = _build_enrichment_block(models)
        if block:
            enrichments[tool_name] = block

    cache.tool_models = tool_models
    cache.models_by_slug = {m.slug: m for m in response.data}
    cache.enrichments = enrichments
    cache.last_fetched = time.monotonic()
    _caches.confirm(cache)

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

        enrichments = _caches.current().enrichments
        if not enrichments:
            return tools

        enriched: List[Tool] = []
        for tool in tools:
            enrichment = enrichments.get(tool.name)
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
