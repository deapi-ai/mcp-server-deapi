"""Every model slug a tool description names must exist in the catalogue.

An agent that cannot find the example on the "Available models" list ignores
it and takes the first familiar name — which is how partner test models ended
up as the default choice (DEV-2075). Slugs live in two places a catalogue
change can silently break: the ``model`` parameter's description and its
default value.

The catalogue needs a token, which CI does not have, so the gate is a
committed snapshot; with ``DEAPI_API_TOKEN`` available the live catalogue is
checked as well, and ``python -m tests.test_description_slugs --refresh``
rewrites the snapshot.
"""

import inspect
import json
import os
import re
import sys
import typing
from pathlib import Path

import pytest

from deapi_mcp.tools import audio, embedding, image, prompt, utility, video

SNAPSHOT = Path(__file__).parent / "fixtures" / "catalogue_slugs.json"
TOOL_MODULES = (audio, embedding, image, prompt, utility, video)
# Slugs are quoted in the descriptions: (e.g., 'Flux1schnell', 'ZImageTurbo_INT8')
QUOTED = re.compile(r"'([A-Za-z0-9][A-Za-z0-9_.\-]*)'")


def _field_description(annotation) -> str:
    if typing.get_origin(annotation) is not typing.Annotated:
        return ""
    for meta in typing.get_args(annotation)[1:]:
        desc = getattr(meta, "description", None)
        if desc:
            return desc
    return ""


def slugs_named_by_tools() -> dict:
    """{slug: 'tool.model (example|default)'} for every slug a tool names."""
    found = {}
    for module in TOOL_MODULES:
        for name, fn in inspect.getmembers(module, inspect.iscoroutinefunction):
            if name.startswith("_") or fn.__module__ != module.__name__:
                continue
            param = inspect.signature(fn).parameters.get("model")
            if param is None:
                continue
            for slug in QUOTED.findall(_field_description(param.annotation)):
                found[slug] = f"{name}.model (example)"
            if isinstance(param.default, str):
                found[param.default] = f"{name}.model (default)"
    return found


def _token() -> str:
    token = os.environ.get("DEAPI_API_TOKEN")
    if not token and Path(".env").exists():
        for line in Path(".env").read_text().splitlines():
            if line.startswith("DEAPI_API_TOKEN="):
                token = line.partition("=")[2].strip()
    return token or ""


def fetch_catalogue_slugs(token: str) -> list:
    import httpx

    r = httpx.get(
        "https://api.deapi.ai/api/v2/models",
        params={"limit": 50},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    assert body["meta"]["total"] == len(body["data"]), "catalogue is paged; extend the fetch"
    return sorted(m["slug"] for m in body["data"])


def test_tools_name_at_least_the_known_examples():
    found = slugs_named_by_tools()
    assert {"Flux1schnell", "WhisperLargeV3", "Ben2", "RealESRGAN_x4"} <= found.keys(), found


def test_every_named_slug_is_in_the_snapshot():
    snapshot = set(json.loads(SNAPSHOT.read_text())["slugs"])
    stale = {s: where for s, where in slugs_named_by_tools().items() if s not in snapshot}
    assert not stale, f"slugs not in the catalogue snapshot: {stale}"


@pytest.mark.skipif(not _token(), reason="no DEAPI_API_TOKEN; live catalogue not checked")
def test_every_named_slug_is_in_the_live_catalogue():
    live = set(fetch_catalogue_slugs(_token()))
    stale = {s: where for s, where in slugs_named_by_tools().items() if s not in live}
    assert not stale, f"slugs not in the live catalogue: {stale}"
    snapshot = set(json.loads(SNAPSHOT.read_text())["slugs"])
    assert snapshot == live, "snapshot differs from the live catalogue — run with --refresh"


if __name__ == "__main__":
    if "--refresh" not in sys.argv:
        sys.exit("usage: python -m tests.test_description_slugs --refresh")
    import datetime

    slugs = fetch_catalogue_slugs(_token() or sys.exit("DEAPI_API_TOKEN not found"))
    SNAPSHOT.write_text(
        json.dumps(
            {
                "_comment": "Slugs in /api/v2/models. Refresh: python -m tests.test_description_slugs --refresh (needs DEAPI_API_TOKEN in .env or the environment).",
                "fetched": datetime.date.today().isoformat(),
                "slugs": slugs,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"{len(slugs)} slugs written to {SNAPSHOT}")
