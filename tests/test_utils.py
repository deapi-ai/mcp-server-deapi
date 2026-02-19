"""Tests for utility functions - audio and video upload helpers."""

import base64
import io
import pytest
from unittest.mock import AsyncMock, patch

from src.utils import (
    parse_audio_input,
    prepare_audio_upload,
    prepare_audio_upload_async,
    parse_video_input,
    prepare_video_upload,
    prepare_video_upload_async,
    fetch_audio_from_url,
    fetch_video_from_url,
    parse_image_input,
    prepare_image_upload_async,
    is_url,
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

        mock_response = AsyncMock()
        mock_response.content = fake_content
        mock_response.headers = {"content-type": "audio/wav"}
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.utils.httpx.AsyncClient", return_value=mock_client):
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
        mock_response = AsyncMock()
        mock_response.content = b"mp3-data"
        mock_response.headers = {"content-type": "audio/mpeg"}
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.utils.httpx.AsyncClient", return_value=mock_client):
            content, filename = await fetch_audio_from_url("https://example.com/file.mp3")

        assert content == b"mp3-data"
        assert filename == "audio.mp3"

    @pytest.mark.asyncio
    async def test_flac_from_url_extension(self):
        mock_response = AsyncMock()
        mock_response.content = b"flac-data"
        mock_response.headers = {"content-type": "application/octet-stream"}
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.utils.httpx.AsyncClient", return_value=mock_client):
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

        mock_response = AsyncMock()
        mock_response.content = fake_content
        mock_response.headers = {"content-type": "video/mp4"}
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.utils.httpx.AsyncClient", return_value=mock_client):
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
        mock_response = AsyncMock()
        mock_response.content = b"mp4-data"
        mock_response.headers = {"content-type": "video/mp4"}
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.utils.httpx.AsyncClient", return_value=mock_client):
            content, filename = await fetch_video_from_url("https://example.com/video.mp4")

        assert content == b"mp4-data"
        assert filename == "video.mp4"

    @pytest.mark.asyncio
    async def test_webm_from_content_type(self):
        mock_response = AsyncMock()
        mock_response.content = b"webm-data"
        mock_response.headers = {"content-type": "video/webm"}
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.utils.httpx.AsyncClient", return_value=mock_client):
            _, filename = await fetch_video_from_url("https://example.com/file")

        assert filename == "video.webm"

    @pytest.mark.asyncio
    async def test_fallback_to_url_extension(self):
        mock_response = AsyncMock()
        mock_response.content = b"avi-data"
        mock_response.headers = {"content-type": "application/octet-stream"}
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.utils.httpx.AsyncClient", return_value=mock_client):
            _, filename = await fetch_video_from_url("https://example.com/clip.avi")

        assert filename == "video.avi"


# =============================================================================
# is_url edge cases
# =============================================================================


class TestIsUrl:
    def test_https(self):
        assert is_url("https://example.com") is True

    def test_http(self):
        assert is_url("http://example.com") is True

    def test_data_uri_not_url(self):
        assert is_url("data:audio/mp3;base64,abc") is False

    def test_base64_not_url(self):
        assert is_url("SGVsbG8gV29ybGQ=") is False
