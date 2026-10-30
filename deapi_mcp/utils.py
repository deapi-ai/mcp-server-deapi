"""Utility functions for deAPI MCP server."""

import asyncio
import base64
import io
import ipaddress
import logging
import re
import socket
from typing import Tuple, cast
from urllib.parse import urljoin, urlsplit

import httpx

from .observability import note_input_error

logger = logging.getLogger(__name__)


class InputError(ValueError):
    """The caller's input is unusable. A ValueError, so existing handlers still catch it."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        note_input_error()


def parse_image_input(image_input: str) -> Tuple[bytes, str]:
    """Parse image input and return file data with filename.

    Accepts:
    - Data URI: data:image/png;base64,iVBORw0KGg...
    - Base64 string: iVBORw0KGg...
    - URL: https://example.com/image.png (will raise error - not supported yet)

    Args:
        image_input: Image as data URI, base64 string, or URL

    Returns:
        Tuple of (image_bytes, filename_with_extension)

    Raises:
        ValueError: If input format is invalid or URLs are provided
    """
    # Check if it's a data URI
    data_uri_pattern = r"^data:image/(png|jpeg|jpg|gif|bmp|webp);base64,(.+)$"
    match = re.match(data_uri_pattern, image_input, re.IGNORECASE)

    if match:
        # Extract mime type and base64 data
        mime_subtype = match.group(1).lower()
        base64_data = match.group(2)

        # Normalize mime type
        if mime_subtype == "jpeg":
            mime_subtype = "jpg"

        try:
            image_bytes = base64.b64decode(base64_data)
            filename = f"image.{mime_subtype}"
            return image_bytes, filename
        except Exception as e:
            raise InputError(f"Failed to decode base64 data: {str(e)}")

    # Check if it's a URL
    if image_input.startswith(("http://", "https://")):
        # URLs are fetched by prepare_image_upload_async before this parser is
        # reached; landing here means a caller used the sync path with a URL.
        raise InputError(
            "Expected a data URI (data:image/png;base64,...) or a base64 string, got a URL. "
            "Use prepare_image_upload_async for URL inputs."
        )

    # Assume it's raw base64 without data URI wrapper
    try:
        image_bytes = base64.b64decode(image_input)
        # Default to PNG if no mime type specified
        filename = "image.png"
        return image_bytes, filename
    except Exception as e:
        raise InputError(
            f"Invalid image input. Expected data URI (data:image/png;base64,...) "
            f"or base64 string, but got: {str(e)}"
        )


def prepare_image_upload(
    image_input: str, field_name: str = "image"
) -> Tuple[str, Tuple[str, io.BytesIO, str]]:
    """Prepare image for multipart/form-data upload.

    Args:
        image_input: Image as data URI or base64 string
        field_name: Form field name (default: "image")

    Returns:
        Tuple of (field_name, (filename, file_obj, mime_type))
        Ready for httpx files parameter

    Example:
        >>> field_name, file_tuple = prepare_image_upload(data_uri, "first_frame_image")
        >>> files = {field_name: file_tuple}
        >>> response = await client.post(url, files=files)
    """
    image_bytes, filename = parse_image_input(image_input)

    # Extract mime type from filename extension
    ext = filename.split(".")[-1].lower()
    mime_mapping = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "webp": "image/webp",
    }
    mime_type = mime_mapping.get(ext, "image/png")

    # Create BytesIO object for httpx
    file_obj = io.BytesIO(image_bytes)

    return field_name, (filename, file_obj, mime_type)


def parse_audio_input(audio_input: str) -> Tuple[bytes, str]:
    """Parse audio input and return file data with filename.

    Accepts:
    - Data URI: data:audio/mp3;base64,SUQzBAA...
    - Base64 string: SUQzBAA...

    Args:
        audio_input: Audio as data URI or base64 string

    Returns:
        Tuple of (audio_bytes, filename_with_extension)

    Raises:
        ValueError: If input format is invalid
    """
    # Check if it's a data URI
    data_uri_pattern = r"^data:audio/(aac|mp3|mpeg|ogg|wav|webm|flac|x-flac);base64,(.+)$"
    match = re.match(data_uri_pattern, audio_input, re.IGNORECASE)

    if match:
        mime_subtype = match.group(1).lower()
        base64_data = match.group(2)

        # Normalize mime subtypes
        ext_mapping = {
            "mpeg": "mp3",
            "x-flac": "flac",
        }
        ext = ext_mapping.get(mime_subtype, mime_subtype)

        try:
            audio_bytes = base64.b64decode(base64_data)
            filename = f"audio.{ext}"
            return audio_bytes, filename
        except Exception as e:
            raise InputError(f"Failed to decode base64 audio data: {str(e)}")

    # Check if it's a URL (handled separately by fetch_audio_from_url)
    if audio_input.startswith(("http://", "https://")):
        raise InputError(
            "URL inputs should be handled by fetch_audio_from_url. "
            "Use prepare_audio_upload_async for URL support."
        )

    # Assume it's raw base64 without data URI wrapper
    try:
        audio_bytes = base64.b64decode(audio_input)
        filename = "audio.mp3"
        return audio_bytes, filename
    except Exception as e:
        raise InputError(
            f"Invalid audio input. Expected data URI (data:audio/mp3;base64,...) "
            f"or base64 string, but got: {str(e)}"
        )


def parse_video_input(video_input: str) -> Tuple[bytes, str]:
    """Parse video input and return file data with filename.

    Accepts:
    - Data URI: data:video/mp4;base64,AAAAIGZ0eXBpc29t...
    - Base64 string: AAAAIGZ0eXBpc29t...
    - URL: https://example.com/video.mp4 (will raise error - not supported yet)

    Args:
        video_input: Video as data URI, base64 string, or URL

    Returns:
        Tuple of (video_bytes, filename_with_extension)

    Raises:
        ValueError: If input format is invalid or URLs are provided
    """
    # Check if it's a data URI
    data_uri_pattern = r"^data:video/(mp4|avi|mov|webm|mkv|mpeg|mpg|flv|wmv);base64,(.+)$"
    match = re.match(data_uri_pattern, video_input, re.IGNORECASE)

    if match:
        # Extract mime type and base64 data
        mime_subtype = match.group(1).lower()
        base64_data = match.group(2)

        # Normalize mime type
        if mime_subtype == "mpeg":
            mime_subtype = "mpg"

        try:
            video_bytes = base64.b64decode(base64_data)
            filename = f"video.{mime_subtype}"
            return video_bytes, filename
        except Exception as e:
            raise InputError(f"Failed to decode base64 video data: {str(e)}")

    # Check if it's a URL
    if video_input.startswith(("http://", "https://")):
        # URLs are fetched by prepare_video_upload_async before this parser is
        # reached; landing here means a caller used the sync path with a URL.
        raise InputError(
            "Expected a data URI (data:video/mp4;base64,...) or a base64 string, got a URL. "
            "Use prepare_video_upload_async for URL inputs."
        )

    # Assume it's raw base64 without data URI wrapper
    try:
        video_bytes = base64.b64decode(video_input)
        # Default to MP4 if no mime type specified
        filename = "video.mp4"
        return video_bytes, filename
    except Exception as e:
        raise InputError(
            f"Invalid video input. Expected data URI (data:video/mp4;base64,...) "
            f"or base64 string, but got: {str(e)}"
        )


def prepare_video_upload(
    video_input: str, field_name: str = "video"
) -> Tuple[str, Tuple[str, io.BytesIO, str]]:
    """Prepare video for multipart/form-data upload.

    Args:
        video_input: Video as data URI or base64 string
        field_name: Form field name (default: "video")

    Returns:
        Tuple of (field_name, (filename, file_obj, mime_type))
        Ready for httpx files parameter

    Example:
        >>> field_name, file_tuple = prepare_video_upload(data_uri, "video")
        >>> files = {field_name: file_tuple}
        >>> response = await client.post(url, files=files)
    """
    video_bytes, filename = parse_video_input(video_input)

    # Extract mime type from filename extension
    ext = filename.split(".")[-1].lower()
    mime_mapping = {
        "mp4": "video/mp4",
        "avi": "video/x-msvideo",
        "mov": "video/quicktime",
        "webm": "video/webm",
        "mkv": "video/x-matroska",
        "mpeg": "video/mpeg",
        "mpg": "video/mpeg",
        "flv": "video/x-flv",
        "wmv": "video/x-ms-wmv",
    }
    mime_type = mime_mapping.get(ext, "video/mp4")

    # Create BytesIO object for httpx
    file_obj = io.BytesIO(video_bytes)

    return field_name, (filename, file_obj, mime_type)


def prepare_audio_upload(
    audio_input: str, field_name: str = "audio"
) -> Tuple[str, Tuple[str, io.BytesIO, str]]:
    """Prepare audio for multipart/form-data upload.

    Args:
        audio_input: Audio as data URI or base64 string
        field_name: Form field name (default: "audio")

    Returns:
        Tuple of (field_name, (filename, file_obj, mime_type))
        Ready for httpx files parameter
    """
    audio_bytes, filename = parse_audio_input(audio_input)

    ext = filename.split(".")[-1].lower()
    mime_mapping = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "flac": "audio/flac",
        "ogg": "audio/ogg",
        "aac": "audio/aac",
        "webm": "audio/webm",
    }
    mime_type = mime_mapping.get(ext, "audio/mpeg")

    file_obj = io.BytesIO(audio_bytes)

    return field_name, (filename, file_obj, mime_type)


def form_bool(value: bool) -> str:
    r"""Render a boolean for a multipart/form-data field.

    A form field is always a string, and Laravel's built-in ``boolean`` rule
    accepts only ``true/false/0/1`` and the strings ``"0"``/``"1"`` — so
    ``"true"`` fails it with a 422 (``enhance_prompt`` on the image endpoints).
    The API's own ``App\Rules\Boolean`` is laxer but also takes ``"1"``/``"0"``,
    which makes this the one spelling every rule accepts.
    """
    return "1" if value else "0"


def is_url(value: str) -> bool:
    """Check if the given string is a URL."""
    return value.startswith(("http://", "https://"))


# --- SSRF-safe URL fetching -------------------------------------------------
# A caller-supplied media URL is fetched by the server, so it must never be used
# to reach internal addresses, to follow a redirect onto one, or to exhaust
# memory. This mirrors the guard the backend already applies to webhook
# delivery: resolve the host, require every resolved IP to be public, pin the
# connection to a validated IP (so DNS rebinding cannot swap it between the
# check and the connect), refuse auto-redirects and re-validate each hop, cap
# the body, and return one generic error so nothing becomes a status oracle.

_FETCH_GENERIC_ERROR = "Could not retrieve the file from the provided URL."
_MAX_REDIRECTS = 3
_CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")
# Never-media content types; refused so the fetchers cannot pull internal pages.
_REFUSED_CONTENT_TYPES = ("text/", "application/json", "application/xml")

# Per-type body caps (bytes): generous but bounded, so one request cannot OOM a pod.
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_AUDIO_BYTES = 100 * 1024 * 1024
MAX_VIDEO_BYTES = 300 * 1024 * 1024


def _ip_is_public(ip_str: str) -> bool:
    """True only for a routable public address (IPv4-mapped IPv6 and CGNAT handled)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.version == 4 and ip in _CGNAT_RANGE:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _resolve_public_ip(host: str, port: int) -> str:
    """Resolve host; require EVERY resolved address to be public; return one to pin."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise InputError(_FETCH_GENERIC_ERROR)
    ips = {cast(str, info[4][0]) for info in infos}
    if not ips or not all(_ip_is_public(ip) for ip in ips):
        raise InputError(_FETCH_GENERIC_ERROR)
    return sorted(ips)[0]


async def safe_fetch(url: str, *, max_bytes: int, timeout: float) -> Tuple[bytes, str]:
    """Fetch an http(s) URL with SSRF protections; return (body, content_type).

    Blocks non-public hosts, pins the connection to a validated IP, handles
    redirects manually and re-validates each hop, caps the body, and raises a
    single generic ``InputError`` on any failure.
    """
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https"):
                raise InputError(_FETCH_GENERIC_ERROR)
            host = parts.hostname
            if not host:
                raise InputError(_FETCH_GENERIC_ERROR)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            pin = await _resolve_public_ip(host, port)

            authority = host if parts.port is None else f"{host}:{parts.port}"
            pin_host = f"[{pin}]" if ":" in pin else pin
            pinned_url = parts._replace(netloc=f"{pin_host}:{port}").geturl()
            request = client.build_request(
                "GET",
                pinned_url,
                headers={"Host": authority, "Accept": "*/*"},
                # Keep TLS SNI and certificate verification bound to the real host
                # even though we connect to the pinned IP.
                extensions={"sni_hostname": host},
            )
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError:
                raise InputError(_FETCH_GENERIC_ERROR)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise InputError(_FETCH_GENERIC_ERROR)
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 400:
                    raise InputError(_FETCH_GENERIC_ERROR)
                content_type = response.headers.get("content-type", "").lower()
                if content_type.startswith(_REFUSED_CONTENT_TYPES):
                    raise InputError(_FETCH_GENERIC_ERROR)
                chunks = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise InputError(_FETCH_GENERIC_ERROR)
                    chunks.append(chunk)
                return b"".join(chunks), content_type
            except httpx.HTTPError:
                raise InputError(_FETCH_GENERIC_ERROR)
            finally:
                await response.aclose()
    raise InputError(_FETCH_GENERIC_ERROR)


async def fetch_image_from_url(url: str, timeout: float = 30.0) -> Tuple[bytes, str]:
    """Fetch image from URL and return bytes with filename.

    Fetched through ``safe_fetch``: non-public hosts, redirects onto them,
    oversized bodies and non-media content are refused.
    """
    content, content_type = await safe_fetch(url, max_bytes=MAX_IMAGE_BYTES, timeout=timeout)
    ext = "png"  # default
    if "jpeg" in content_type or "jpg" in content_type:
        ext = "jpg"
    elif "png" in content_type:
        ext = "png"
    elif "gif" in content_type:
        ext = "gif"
    elif "webp" in content_type:
        ext = "webp"
    elif "bmp" in content_type:
        ext = "bmp"
    else:
        url_lower = url.lower()
        for img_ext in ["jpg", "jpeg", "png", "gif", "webp", "bmp"]:
            if f".{img_ext}" in url_lower:
                ext = "jpg" if img_ext == "jpeg" else img_ext
                break
    return content, f"image.{ext}"


async def prepare_image_upload_async(
    image_input: str, field_name: str = "image"
) -> Tuple[str, Tuple[str, io.BytesIO, str]]:
    """Prepare image for multipart/form-data upload, supporting URLs.

    This is an async version that can fetch images from URLs.

    Args:
        image_input: Image as data URI, base64 string, or URL
        field_name: Form field name (default: "image")

    Returns:
        Tuple of (field_name, (filename, file_obj, mime_type))
        Ready for httpx files parameter
    """
    # Check if it's a URL - if so, fetch it first
    if is_url(image_input):
        image_bytes, filename = await fetch_image_from_url(image_input)
    else:
        # Use existing sync parser for data URI / base64
        image_bytes, filename = parse_image_input(image_input)

    # Extract mime type from filename extension
    ext = filename.split(".")[-1].lower()
    mime_mapping = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "webp": "image/webp",
    }
    mime_type = mime_mapping.get(ext, "image/png")

    # Create BytesIO object for httpx
    file_obj = io.BytesIO(image_bytes)

    return field_name, (filename, file_obj, mime_type)


async def fetch_audio_from_url(url: str, timeout: float = 30.0) -> Tuple[bytes, str]:
    """Fetch audio from URL and return bytes with filename.

    Args:
        url: URL of the audio to fetch
        timeout: Request timeout in seconds

    Returns:
        Tuple of (audio_bytes, filename_with_extension)

    Raises:
        ValueError: If URL cannot be fetched or is not valid audio
    """
    content, content_type = await safe_fetch(url, max_bytes=MAX_AUDIO_BYTES, timeout=timeout)
    ext = "mp3"  # default
    if "wav" in content_type:
        ext = "wav"
    elif "flac" in content_type:
        ext = "flac"
    elif "ogg" in content_type:
        ext = "ogg"
    elif "aac" in content_type:
        ext = "aac"
    elif "webm" in content_type:
        ext = "webm"
    elif "mpeg" in content_type or "mp3" in content_type:
        ext = "mp3"
    else:
        url_lower = url.lower()
        for audio_ext in ["mp3", "wav", "flac", "ogg", "aac", "webm"]:
            if f".{audio_ext}" in url_lower:
                ext = audio_ext
                break
    return content, f"audio.{ext}"


async def prepare_audio_upload_async(
    audio_input: str, field_name: str = "audio"
) -> Tuple[str, Tuple[str, io.BytesIO, str]]:
    """Prepare audio for multipart/form-data upload, supporting URLs.

    Args:
        audio_input: Audio as data URI, base64 string, or URL
        field_name: Form field name (default: "audio")

    Returns:
        Tuple of (field_name, (filename, file_obj, mime_type))
        Ready for httpx files parameter
    """
    if is_url(audio_input):
        audio_bytes, filename = await fetch_audio_from_url(audio_input)
    else:
        audio_bytes, filename = parse_audio_input(audio_input)

    ext = filename.split(".")[-1].lower()
    mime_mapping = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "flac": "audio/flac",
        "ogg": "audio/ogg",
        "aac": "audio/aac",
        "webm": "audio/webm",
    }
    mime_type = mime_mapping.get(ext, "audio/mpeg")

    file_obj = io.BytesIO(audio_bytes)

    return field_name, (filename, file_obj, mime_type)


async def fetch_video_from_url(url: str, timeout: float = 60.0) -> Tuple[bytes, str]:
    """Fetch video from URL and return bytes with filename.

    Args:
        url: URL of the video to fetch
        timeout: Request timeout in seconds

    Returns:
        Tuple of (video_bytes, filename_with_extension)

    Raises:
        ValueError: If URL cannot be fetched or is not valid video
    """
    content, content_type = await safe_fetch(url, max_bytes=MAX_VIDEO_BYTES, timeout=timeout)
    ext = "mp4"  # default
    if "webm" in content_type:
        ext = "webm"
    elif "avi" in content_type or "x-msvideo" in content_type:
        ext = "avi"
    elif "quicktime" in content_type or "mov" in content_type:
        ext = "mov"
    elif "x-matroska" in content_type or "mkv" in content_type:
        ext = "mkv"
    elif "mpeg" in content_type:
        ext = "mpg"
    elif "x-flv" in content_type:
        ext = "flv"
    elif "x-ms-wmv" in content_type:
        ext = "wmv"
    else:
        url_lower = url.lower()
        for vid_ext in ["mp4", "webm", "avi", "mov", "mkv", "mpg", "flv", "wmv"]:
            if f".{vid_ext}" in url_lower:
                ext = vid_ext
                break
    return content, f"video.{ext}"


async def prepare_video_upload_async(
    video_input: str, field_name: str = "video"
) -> Tuple[str, Tuple[str, io.BytesIO, str]]:
    """Prepare video for multipart/form-data upload, supporting URLs.

    Args:
        video_input: Video as data URI, base64 string, or URL
        field_name: Form field name (default: "video")

    Returns:
        Tuple of (field_name, (filename, file_obj, mime_type))
        Ready for httpx files parameter
    """
    if is_url(video_input):
        video_bytes, filename = await fetch_video_from_url(video_input)
    else:
        video_bytes, filename = parse_video_input(video_input)

    ext = filename.split(".")[-1].lower()
    mime_mapping = {
        "mp4": "video/mp4",
        "avi": "video/x-msvideo",
        "mov": "video/quicktime",
        "webm": "video/webm",
        "mkv": "video/x-matroska",
        "mpeg": "video/mpeg",
        "mpg": "video/mpeg",
        "flv": "video/x-flv",
        "wmv": "video/x-ms-wmv",
    }
    mime_type = mime_mapping.get(ext, "video/mp4")

    file_obj = io.BytesIO(video_bytes)

    return field_name, (filename, file_obj, mime_type)
