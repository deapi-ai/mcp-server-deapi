"""Tests for DEV-2019: caller-supplied webhooks on generation tools.

The API accepts webhook_url / webhook_secret on every generation endpoint and
MCP exposed neither. These belong to the caller: the tool still polls and
returns the result, and the webhook notifies the caller's own systems.

Validation is done once, in the client, so the eighteen tools that forward
the fields cannot drift from each other or from the API's rules.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deapi_mcp.deapi_client import DeapiClient, DeapiRequestError, webhook_payload_fields

URL = "https://hooks.example.com/deapi"
SECRET = "x" * 32


class TestWebhookPayloadFields:
    """Mirror of BaseClientRequest::webhookRules()."""

    def test_nothing_given_adds_nothing(self):
        assert webhook_payload_fields(None, None) == {}

    def test_url_alone_is_allowed(self):
        assert webhook_payload_fields(URL, None) == {"webhook_url": URL}

    def test_secret_requires_url(self):
        """required_with:webhook_secret — the API rejects a secret on its own."""
        with pytest.raises(DeapiRequestError, match="requires webhook_url"):
            webhook_payload_fields(None, SECRET)

    def test_url_must_be_https(self):
        with pytest.raises(DeapiRequestError, match="https"):
            webhook_payload_fields("http://hooks.example.com/x", None)

    @pytest.mark.parametrize("length", [31, 256])
    def test_secret_length_bounds(self, length):
        with pytest.raises(DeapiRequestError, match="32-255"):
            webhook_payload_fields(URL, "s" * length)

    @pytest.mark.parametrize("length", [32, 255])
    def test_secret_length_edges_accepted(self, length):
        assert webhook_payload_fields(URL, "s" * length)["webhook_secret"] == "s" * length


class TestSubmitJobInjection:
    """The fields join whichever body the tool built — never a second body."""

    @pytest.fixture
    def client(self):
        c = DeapiClient(api_token="t")
        c._request = AsyncMock(return_value={"data": {"request_id": "job-1"}})
        return c

    @pytest.mark.asyncio
    async def test_joins_json_body(self, client):
        await client.submit_job(
            "images/generations", json_data={"prompt": "p"}, webhook_url=URL, webhook_secret=SECRET
        )
        sent = client._request.call_args.kwargs
        assert sent["json_data"] == {"prompt": "p", "webhook_url": URL, "webhook_secret": SECRET}
        assert sent["data"] is None

    @pytest.mark.asyncio
    async def test_joins_multipart_form(self, client):
        await client.submit_job(
            "audio/transcriptions",
            data={"model": "m"},
            files={"source_file": ("a.mp3", b"x", "audio/mpeg")},
            webhook_url=URL,
        )
        sent = client._request.call_args.kwargs
        assert sent["data"] == {"model": "m", "webhook_url": URL}
        assert sent["json_data"] is None

    @pytest.mark.asyncio
    async def test_absent_leaves_payload_untouched(self, client):
        await client.submit_job("x", json_data={"a": 1})
        assert client._request.call_args.kwargs["json_data"] == {"a": 1}

    @pytest.mark.asyncio
    async def test_does_not_mutate_callers_dict(self, client):
        """Tools may reuse their payload dict; injection must not write into it."""
        payload = {"prompt": "p"}
        await client.submit_job("x", json_data=payload, webhook_url=URL)
        assert payload == {"prompt": "p"}


class TestEveryGenerationToolForwardsWebhooks:
    """Each of the 18 generation tools must accept both fields and pass them on."""

    GENERATION_TOOLS = [
        ("deapi_mcp.tools.audio", "audio_transcription"),
        ("deapi_mcp.tools.audio", "text_to_audio"),
        ("deapi_mcp.tools.audio", "video_file_transcription"),
        ("deapi_mcp.tools.audio", "video_url_transcription"),
        ("deapi_mcp.tools.audio", "text_to_music"),
        ("deapi_mcp.tools.audio", "audio_url_transcription"),
        ("deapi_mcp.tools.image", "text_to_image"),
        ("deapi_mcp.tools.image", "image_to_image"),
        ("deapi_mcp.tools.image", "image_to_text"),
        ("deapi_mcp.tools.image", "image_remove_background"),
        ("deapi_mcp.tools.image", "image_upscale"),
        ("deapi_mcp.tools.video", "image_to_video"),
        ("deapi_mcp.tools.video", "text_to_video"),
        ("deapi_mcp.tools.video", "audio_to_video"),
        ("deapi_mcp.tools.video", "video_description"),
        ("deapi_mcp.tools.video", "video_upscale"),
        ("deapi_mcp.tools.embedding", "text_to_embedding"),
    ]

    @pytest.mark.parametrize("module,name", GENERATION_TOOLS)
    def test_signature_accepts_webhook_fields(self, module, name):
        import importlib

        fn = getattr(importlib.import_module(module), name)
        params = inspect.signature(fn).parameters
        assert "webhook_url" in params, f"{name} lacks webhook_url"
        assert "webhook_secret" in params, f"{name} lacks webhook_secret"
        assert params["webhook_url"].default is None
        assert params["webhook_secret"].default is None

    def test_price_tools_do_not_accept_webhooks(self):
        """Price endpoints don't merge webhookRules(); exposing them there would mislead."""
        import deapi_mcp.tools.image as image

        assert "webhook_url" not in inspect.signature(image.text_to_image_price).parameters

    @pytest.mark.asyncio
    async def test_json_tool_forwards_to_client(self):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        job = MagicMock()
        job.data.request_id = "job-1"
        client.submit_job = AsyncMock(return_value=job)
        poll = MagicMock()
        poll.success = True
        poll.result = None
        poll.result_url = "u"
        poll.metadata = {}
        polling = MagicMock()
        polling.poll_until_complete = AsyncMock(return_value=poll)

        with (
            patch("deapi_mcp.tools.image.get_client", return_value=client),
            patch("deapi_mcp.tools.image.PollingManager", return_value=polling),
            patch("deapi_mcp.tools._price_helpers.get_model_defaults", return_value={}),
            patch("deapi_mcp.tools.image.ensure_model_cache", new=AsyncMock()),
        ):
            from deapi_mcp.tools.image import text_to_image

            await text_to_image(prompt="p", model="m", webhook_url=URL, webhook_secret=SECRET)

        kwargs = client.submit_job.call_args.kwargs
        assert kwargs["webhook_url"] == URL
        assert kwargs["webhook_secret"] == SECRET

    @pytest.mark.asyncio
    async def test_multipart_tool_forwards_to_client(self):
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        job = MagicMock()
        job.data.request_id = "job-1"
        client.submit_job = AsyncMock(return_value=job)
        poll = MagicMock()
        poll.success = True
        poll.result = "text"
        poll.result_url = None
        poll.metadata = {}
        polling = MagicMock()
        polling.poll_until_complete = AsyncMock(return_value=poll)

        with (
            patch("deapi_mcp.tools.audio.get_client", return_value=client),
            patch("deapi_mcp.tools.audio.PollingManager", return_value=polling),
            patch(
                "deapi_mcp.tools.audio.prepare_audio_upload_async",
                AsyncMock(return_value=("source_file", ("a.mp3", b"x", "audio/mpeg"))),
            ),
        ):
            from deapi_mcp.tools.audio import audio_transcription

            await audio_transcription(
                audio="data:audio/mp3;base64,AA", include_ts=True, webhook_url=URL
            )

        kwargs = client.submit_job.call_args.kwargs
        assert kwargs["webhook_url"] == URL
        assert kwargs["webhook_secret"] is None

    @pytest.mark.asyncio
    async def test_invalid_webhook_is_a_clean_tool_error(self):
        """A secret without a URL must not reach the API or surface as a traceback."""
        real = DeapiClient(api_token="t")
        real._request = AsyncMock()

        with (
            patch("deapi_mcp.tools.image.get_client", return_value=real),
            patch("deapi_mcp.tools._price_helpers.get_model_defaults", return_value={}),
            patch("deapi_mcp.tools.image.ensure_model_cache", new=AsyncMock()),
        ):
            from deapi_mcp.tools.image import text_to_image

            result = await text_to_image(prompt="p", model="m", webhook_secret=SECRET)

        assert result["success"] is False
        # QA: it read "Unexpected error: ..." — a rule the API enforces is not unexpected.
        assert result["error"] == "API error: webhook_secret requires webhook_url"
        real._request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_webhook_is_not_mistaken_for_a_bad_file(self):
        """Tools with a file-format ValueError handler must not relabel a webhook error."""
        real = DeapiClient(api_token="t")
        real._request = AsyncMock()

        with (
            patch("deapi_mcp.tools.video.get_client", return_value=real),
            patch(
                "deapi_mcp.tools.video.prepare_video_upload_async",
                new=AsyncMock(return_value=("video", ("v.mp4", b"x", "video/mp4"))),
            ),
        ):
            from deapi_mcp.tools.video import video_upscale

            result = await video_upscale(
                video="data:video/mp4;base64,AAAA",
                model="m",
                webhook_url="http://insecure.example/x",
            )

        assert result["success"] is False
        assert result["error"] == "API error: webhook_url must use https"
        real._request.assert_not_awaited()
