"""Transcription tools default to WhisperLargeV3Ct2 and describe `lang` the same way.

WhisperLargeV3 ignores `lang`; Ct2 costs the same and honours it, so every
transcription tool and its price tool must default to Ct2 and tell the agent when to set `lang`.
"""

import inspect
from typing import Any, get_type_hints

import pytest

from deapi_mcp.schemas import AudioTranscriptionRequest
from deapi_mcp.tools import audio
from deapi_mcp.tools.audio import DEFAULT_TRANSCRIPTION_MODEL

TRANSCRIPTION_TOOLS = [
    audio.audio_transcription,
    audio.audio_transcription_price,
    audio.audio_url_transcription,
    audio.audio_url_transcription_price,
    audio.video_file_transcription,
    audio.video_file_transcription_price,
    audio.video_url_transcription,
    audio.video_url_transcription_price,
]


def _field_description(fn: Any, name: str) -> str:
    hint = get_type_hints(fn, include_extras=True)[name]
    return next(m.description for m in hint.__metadata__ if hasattr(m, "description"))


def test_default_model_is_ct2() -> None:
    assert DEFAULT_TRANSCRIPTION_MODEL == "WhisperLargeV3Ct2"


@pytest.mark.parametrize("fn", TRANSCRIPTION_TOOLS, ids=lambda f: f.__name__)
def test_tool_defaults_to_ct2(fn: Any) -> None:
    assert inspect.signature(fn).parameters["model"].default == DEFAULT_TRANSCRIPTION_MODEL


@pytest.mark.parametrize("fn", TRANSCRIPTION_TOOLS, ids=lambda f: f.__name__)
def test_lang_tells_when_to_set_it_and_what_a_wrong_code_does(fn: Any) -> None:
    description = _field_description(fn, "lang")
    assert "whenever you know the language" in description
    assert "translation instead of a transcript" in description


def test_request_schema_defaults_to_ct2() -> None:
    assert AudioTranscriptionRequest.model_fields["model"].default == DEFAULT_TRANSCRIPTION_MODEL
