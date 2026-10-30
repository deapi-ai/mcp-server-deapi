"""Tests for utility functions - audio and video upload helpers."""

import base64
import io
from unittest.mock import AsyncMock, patch

import pytest

from deapi_mcp.utils import (
    fetch_audio_from_url,
    fetch_video_from_url,
    is_url,
    parse_audio_input,
    prepare_audio_upload,
    prepare_audio_upload_async,
    prepare_video_upload_async,
)

# =============================================================================
# parse_audio_input tests
# =============================================================================


class TestParseAudioInput:
    def test_mp3_data_uri(self):
        raw = b"fake-mp3-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:audio/mp3;base64,{encoded}"

        audio_bytes, filename = parse_audio_input(data_uri)
        assert audio_bytes == raw
        assert filename == "audio.mp3"

    def test_wav_data_uri(self):
        raw = b"fake-wav-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:audio/wav;base64,{encoded}"

        audio_bytes, filename = parse_audio_input(data_uri)
        assert audio_bytes == raw
        assert filename == "audio.wav"

    def test_flac_data_uri(self):
        raw = b"fake-flac-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:audio/flac;base64,{encoded}"

        audio_bytes, filename = parse_audio_input(data_uri)
        assert audio_bytes == raw
        assert filename == "audio.flac"

    def test_mpeg_normalizes_to_mp3(self):
        raw = b"fake-mpeg-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:audio/mpeg;base64,{encoded}"

        audio_bytes, filename = parse_audio_input(data_uri)
        assert audio_bytes == raw
        assert filename == "audio.mp3"

    def test_x_flac_normalizes_to_flac(self):
        raw = b"fake-flac-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:audio/x-flac;base64,{encoded}"

        audio_bytes, filename = parse_audio_input(data_uri)
        assert filename == "audio.flac"

    def test_raw_base64(self):
        raw = b"fake-audio-data"
        encoded = base64.b64encode(raw).decode()

        audio_bytes, filename = parse_audio_input(encoded)
        assert audio_bytes == raw
        assert filename == "audio.mp3"  # defaults to mp3

    def test_url_raises_error(self):
        with pytest.raises(ValueError, match="prepare_audio_upload_async"):
            parse_audio_input("https://example.com/audio.mp3")

    def test_invalid_base64_raises_error(self):
        with pytest.raises(ValueError, match="Invalid audio input"):
            parse_audio_input("not-valid-base64!!!")

    def test_ogg_data_uri(self):
        raw = b"fake-ogg-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:audio/ogg;base64,{encoded}"

        audio_bytes, filename = parse_audio_input(data_uri)
        assert filename == "audio.ogg"

    def test_webm_data_uri(self):
        raw = b"fake-webm-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:audio/webm;base64,{encoded}"

        audio_bytes, filename = parse_audio_input(data_uri)
        assert filename == "audio.webm"

    def test_aac_data_uri(self):
        raw = b"fake-aac-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:audio/aac;base64,{encoded}"

        audio_bytes, filename = parse_audio_input(data_uri)
        assert filename == "audio.aac"


# =============================================================================
# prepare_audio_upload tests
# =============================================================================


class TestPrepareAudioUpload:
    def test_returns_correct_structure(self):
        raw = b"fake-mp3-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:audio/mp3;base64,{encoded}"

        field_name, (filename, file_obj, mime_type) = prepare_audio_upload(data_uri, "audio")

        assert field_name == "audio"
        assert filename == "audio.mp3"
        assert mime_type == "audio/mpeg"
        assert isinstance(file_obj, io.BytesIO)
        assert file_obj.read() == raw

    def test_custom_field_name(self):
        raw = b"fake-data"
        encoded = base64.b64encode(raw).decode()

        field_name, _ = prepare_audio_upload(encoded, "my_audio")
        assert field_name == "my_audio"

    def test_wav_mime_type(self):
        raw = b"fake-wav-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:audio/wav;base64,{encoded}"

        _, (_, _, mime_type) = prepare_audio_upload(data_uri)
        assert mime_type == "audio/wav"

    def test_flac_mime_type(self):
        raw = b"fake-flac-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:audio/flac;base64,{encoded}"

        _, (_, _, mime_type) = prepare_audio_upload(data_uri)
        assert mime_type == "audio/flac"


# =============================================================================
# prepare_audio_upload_async tests
# =============================================================================


class TestPrepareAudioUploadAsync:
    @pytest.mark.asyncio
    async def test_base64_input(self):
        raw = b"fake-mp3-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:audio/mp3;base64,{encoded}"

        field_name, (filename, file_obj, mime_type) = await prepare_audio_upload_async(data_uri)

        assert field_name == "audio"
        assert filename == "audio.mp3"
        assert mime_type == "audio/mpeg"
        assert file_obj.read() == raw

    @pytest.mark.asyncio
    async def test_url_input(self):
        fake_content = b"fake-audio-from-url"

        with patch(
            "deapi_mcp.utils.safe_fetch", AsyncMock(return_value=(fake_content, "audio/wav"))
        ):
            field_name, (filename, file_obj, mime_type) = await prepare_audio_upload_async(
                "https://example.com/audio.wav", "audio"
            )

        assert field_name == "audio"
        assert filename == "audio.wav"
        assert mime_type == "audio/wav"
        assert file_obj.read() == fake_content


# =============================================================================
# fetch_audio_from_url tests
# =============================================================================


class TestFetchAudioFromUrl:
    @pytest.mark.asyncio
    async def test_mp3_content_type(self):
        with patch(
            "deapi_mcp.utils.safe_fetch", AsyncMock(return_value=(b"mp3-data", "audio/mpeg"))
        ):
            content, filename = await fetch_audio_from_url("https://example.com/file.mp3")

        assert content == b"mp3-data"
        assert filename == "audio.mp3"

    @pytest.mark.asyncio
    async def test_flac_from_url_extension(self):
        with patch(
            "deapi_mcp.utils.safe_fetch",
            AsyncMock(return_value=(b"flac-data", "application/octet-stream")),
        ):
            _, filename = await fetch_audio_from_url("https://example.com/music.flac")

        assert filename == "audio.flac"


# =============================================================================
# prepare_video_upload_async tests
# =============================================================================


class TestPrepareVideoUploadAsync:
    @pytest.mark.asyncio
    async def test_base64_input(self):
        raw = b"fake-mp4-data"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:video/mp4;base64,{encoded}"

        field_name, (filename, file_obj, mime_type) = await prepare_video_upload_async(data_uri)

        assert field_name == "video"
        assert filename == "video.mp4"
        assert mime_type == "video/mp4"
        assert file_obj.read() == raw

    @pytest.mark.asyncio
    async def test_url_input(self):
        fake_content = b"fake-video-from-url"

        with patch(
            "deapi_mcp.utils.safe_fetch", AsyncMock(return_value=(fake_content, "video/mp4"))
        ):
            field_name, (filename, file_obj, mime_type) = await prepare_video_upload_async(
                "https://example.com/video.mp4", "video"
            )

        assert field_name == "video"
        assert filename == "video.mp4"
        assert mime_type == "video/mp4"
        assert file_obj.read() == fake_content

    @pytest.mark.asyncio
    async def test_custom_field_name(self):
        raw = b"fake-data"
        encoded = base64.b64encode(raw).decode()

        field_name, _ = await prepare_video_upload_async(encoded, "my_video")
        assert field_name == "my_video"

    @pytest.mark.asyncio
    async def test_webm_data_uri(self):
        raw = b"fake-webm"
        encoded = base64.b64encode(raw).decode()
        data_uri = f"data:video/webm;base64,{encoded}"

        _, (filename, _, mime_type) = await prepare_video_upload_async(data_uri)
        assert filename == "video.webm"
        assert mime_type == "video/webm"


# =============================================================================
# fetch_video_from_url tests
# =============================================================================


class TestFetchVideoFromUrl:
    @pytest.mark.asyncio
    async def test_mp4_content_type(self):
        with patch(
            "deapi_mcp.utils.safe_fetch", AsyncMock(return_value=(b"mp4-data", "video/mp4"))
        ):
            content, filename = await fetch_video_from_url("https://example.com/video.mp4")

        assert content == b"mp4-data"
        assert filename == "video.mp4"

    @pytest.mark.asyncio
    async def test_webm_from_content_type(self):
        with patch(
            "deapi_mcp.utils.safe_fetch", AsyncMock(return_value=(b"webm-data", "video/webm"))
        ):
            _, filename = await fetch_video_from_url("https://example.com/file")

        assert filename == "video.webm"

    @pytest.mark.asyncio
    async def test_fallback_to_url_extension(self):
        with patch(
            "deapi_mcp.utils.safe_fetch",
            AsyncMock(return_value=(b"avi-data", "application/octet-stream")),
        ):
            _, filename = await fetch_video_from_url("https://example.com/clip.avi")

        assert filename == "video.avi"


# =============================================================================
# is_url edge cases
# =============================================================================


class TestNoAttachmentPromise:
    """DEV-2075 bug 2: an attachment in the chat client never reaches a tool
    argument, so no description may claim it does, and the sync parsers must
    not answer a URL with "not yet supported" when the async path handles it."""

    def test_no_tool_description_mentions_attachments(self):
        import inspect

        from deapi_mcp.tools import audio, embedding, image, prompt, utility, video

        offenders = []
        for module in (audio, embedding, image, prompt, utility, video):
            for name, fn in inspect.getmembers(module, inspect.iscoroutinefunction):
                if fn.__module__ != module.__name__:
                    continue
                if "attach" in (fn.__doc__ or "").lower():
                    offenders.append(name)
        assert not offenders, f"descriptions still mention attachments: {offenders}"

    def test_image_parser_names_the_async_path_for_urls(self):
        from deapi_mcp.utils import parse_image_input

        with pytest.raises(ValueError, match="prepare_image_upload_async") as exc:
            parse_image_input("https://example.com/a.png")
        assert "not yet supported" not in str(exc.value)

    def test_video_parser_names_the_async_path_for_urls(self):
        from deapi_mcp.utils import parse_video_input

        with pytest.raises(ValueError, match="prepare_video_upload_async") as exc:
            parse_video_input("https://example.com/a.mp4")
        assert "not yet supported" not in str(exc.value)

    def test_not_yet_supported_is_gone_from_utils(self):
        from pathlib import Path

        import deapi_mcp.utils

        assert "not yet supported" not in Path(deapi_mcp.utils.__file__).read_text()


class TestIsUrl:
    def test_https(self):
        assert is_url("https://example.com") is True

    def test_http(self):
        assert is_url("http://example.com") is True

    def test_data_uri_not_url(self):
        assert is_url("data:audio/mp3;base64,abc") is False

    def test_base64_not_url(self):
        assert is_url("SGVsbG8gV29ybGQ=") is False
