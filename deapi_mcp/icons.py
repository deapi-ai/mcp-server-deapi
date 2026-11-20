"""deAPI's own icon: the favicon of this server's pages and the icon MCP clients show.

The PNGs ship in the package (``deapi_mcp/static``), so nothing is fetched from elsewhere.
They are served at ``/favicon.ico`` and ``/icon-<size>.png`` and announced in the MCP
``serverInfo`` (``icons``, MCP 2025-11-25), which clients such as ChatGPT use for the
connector's picture.
"""

import base64
from functools import lru_cache
from importlib.resources import files
from typing import Optional

from mcp.types import Icon
from starlette.requests import Request
from starlette.responses import Response

ICON_SIZES = (32, 64, 128)
FAVICON_SIZE = 64
WEBSITE_URL = "https://deapi.ai"

ICON_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "X-Content-Type-Options": "nosniff",
}


@lru_cache(maxsize=None)
def icon_bytes(size: int) -> bytes:
    if size not in ICON_SIZES:
        raise ValueError(f"no {size}px icon")
    return files("deapi_mcp").joinpath(f"static/icon-{size}.png").read_bytes()


def mcp_icons(base_url: Optional[str] = None) -> list[Icon]:
    """The icons for ``serverInfo``: https URLs when the public URL is known, then data URIs.

    A data URI (allowed by the spec) works whatever host the server is reached on; an
    https URL suits clients that only fetch those.
    """
    icons: list[Icon] = []
    if base_url:
        base = base_url.rstrip("/")
        icons += [
            Icon(src=f"{base}/icon-{size}.png", mimeType="image/png", sizes=[f"{size}x{size}"])
            for size in (128, 64)
        ]
    icons.append(
        Icon(
            src="data:image/png;base64," + base64.b64encode(icon_bytes(128)).decode(),
            mimeType="image/png",
            sizes=["128x128"],
        )
    )
    return icons


async def favicon(request: Request) -> Response:
    return Response(icon_bytes(FAVICON_SIZE), media_type="image/png", headers=ICON_HEADERS)


async def sized_icon(request: Request) -> Response:
    size = request.path_params["size"]
    if size not in ICON_SIZES:
        return Response("Not Found", status_code=404, media_type="text/plain")
    return Response(icon_bytes(size), media_type="image/png", headers=ICON_HEADERS)
