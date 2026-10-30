"""Static bounds on tool parameters must be API-level constants, never a model's limit.

A bound in a ``Field`` does two things: it is published in the tool schema as
a fact, and it rejects the call before the API sees it. Both are wrong when
the real limit lives on the model card — ``frames<=200`` once refused Ltx2.5's
valid 241, and ``width<=2048`` would refuse nano-banana-2's 4096 today. The
API applies the card's own min/max (``applyMinMax`` / ``applySideBounds`` in
the Laravel form requests) and answers with a clear 422; the enrichment block
already prints those limits per model.

What stays is what Laravel enforces regardless of model, as a base rule:
``between:1,10240`` on the pixel-priced tools, ``duration 10-600`` and
``bpm 30-300`` and ``inference_steps 1-100`` and ``guidance_scale 0-20`` on music,
``scale 1-16`` on video upscale, ``speed >= 0.5`` on TTS, and plain sanity floors
(``>= 1``). Music's guidance is card-driven too, but ``Text2MusicRequest`` also
applies ``min:0|max:20`` to every model, so its ceiling is a fact.
"""

import inspect
import typing

from deapi_mcp.tools import audio, embedding, image, prompt, utility, video

TOOL_MODULES = (audio, embedding, image, prompt, utility, video)

# Parameters whose limits come from the model card: they may carry a floor,
# never a ceiling.
CARD_DRIVEN = {"width", "height", "steps", "guidance_scale", "guidance", "frames", "fps", "speed"}

# Ceilings that are Laravel base rules, not card limits. Key: (tool, parameter).
API_CONSTANT_CEILINGS = {
    ("image_remove_background_price", "width"): 10240,
    ("image_remove_background_price", "height"): 10240,
    ("image_to_text_price", "width"): 10240,
    ("image_to_text_price", "height"): 10240,
    ("image_upscale_price", "width"): 10240,
    ("image_upscale_price", "height"): 10240,
    ("video_upscale_price", "width"): 10240,
    ("video_upscale_price", "height"): 10240,
    ("text_to_music", "duration"): 600,
    ("text_to_music_price", "duration"): 600,
    ("text_to_music", "inference_steps"): 100,
    ("text_to_music_price", "inference_steps"): 100,
    ("text_to_music", "bpm"): 300,
    ("text_to_music", "guidance_scale"): 20.0,
    ("video_upscale", "scale"): 16,
    ("video_upscale_price", "scale"): 16,
}


def bounded_parameters():
    """Yield (tool, parameter, {ge/le/gt/lt: value}) for every bounded parameter."""
    for module in TOOL_MODULES:
        for name, fn in inspect.getmembers(module, inspect.iscoroutinefunction):
            if name.startswith("_") or fn.__module__ != module.__name__:
                continue
            for pname, param in inspect.signature(fn).parameters.items():
                if typing.get_origin(param.annotation) is not typing.Annotated:
                    continue
                for meta in typing.get_args(param.annotation)[1:]:
                    for m in getattr(meta, "metadata", None) or []:
                        bounds = {
                            k: getattr(m, k)
                            for k in ("ge", "le", "gt", "lt")
                            if getattr(m, k, None) is not None
                        }
                        if bounds:
                            yield name, pname, bounds


def test_every_ceiling_is_an_api_constant():
    """A ceiling not listed as a Laravel base rule is a card limit typed by hand."""
    unexpected = {
        (tool, pname): bounds
        for tool, pname, bounds in bounded_parameters()
        if ("le" in bounds or "lt" in bounds)
        and API_CONSTANT_CEILINGS.get((tool, pname)) != bounds.get("le", bounds.get("lt"))
    }
    assert not unexpected, f"static ceilings on card-driven parameters: {unexpected}"


def test_card_driven_parameters_carry_no_ceiling():
    """Belt and braces for the names that have burned us: floor only."""
    offenders = {
        (tool, pname): bounds
        for tool, pname, bounds in bounded_parameters()
        if pname in CARD_DRIVEN
        and ("le" in bounds or "lt" in bounds)
        and (tool, pname) not in API_CONSTANT_CEILINGS
    }
    assert not offenders, offenders


def test_api_constant_list_is_not_stale():
    """Every listed constant still exists in a signature — a removed bound must leave the list."""
    present = {(tool, pname) for tool, pname, _ in bounded_parameters()}
    missing = set(API_CONSTANT_CEILINGS) - present
    assert not missing, f"listed as API constants but no longer bounded: {missing}"
