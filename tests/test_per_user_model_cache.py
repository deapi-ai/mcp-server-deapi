"""The model cache is per account: one user's catalogue never serves another user."""

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, patch

import pytest

import deapi_mcp.middleware as mw
from deapi_mcp.auth import current_deapi_token
from deapi_mcp.middleware import (
    ModelEnrichmentMiddleware,
    _caches,
    ensure_model_cache,
    get_cached_model,
    get_cached_models_for_tool,
)
from deapi_mcp.schemas import ModelInfo, ModelsResponse

TOKEN_A = "token-a-0123456789"
TOKEN_B = "token-b-0123456789"


def model(slug: str, steps: int = 4) -> ModelInfo:
    return ModelInfo(
        name=slug,
        slug=slug,
        inference_types=["txt2img"],
        info={"defaults": {"steps": steps}},
    )


# Account A has a private model; account B has a different one. Both share Flux.
CATALOGUES = {
    TOKEN_A: [model("Flux1schnell"), model("PrivateA", steps=8)],
    TOKEN_B: [model("Flux1schnell"), model("PrivateB", steps=12)],
}


class FakeApi:
    """get_client() stand-in that answers /models with the caller's own catalogue."""

    def __init__(self):
        self.calls: list[str] = []

    def get_client(self):
        token = current_deapi_token.get()
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)

        async def get_models():
            self.calls.append(token)
            return ModelsResponse(data=list(CATALOGUES[token]))

        client.get_models = get_models
        return client


@contextlib.contextmanager
def as_user(token: str):
    reset = current_deapi_token.set(token)
    try:
        yield
    finally:
        current_deapi_token.reset(reset)


@pytest.fixture(autouse=True)
def clean_caches():
    _caches.clear()
    yield
    _caches.clear()


@pytest.fixture
def api():
    fake = FakeApi()
    with patch("deapi_mcp.deapi_client.get_client", side_effect=fake.get_client):
        yield fake


async def test_each_user_sees_only_their_own_models(api):
    with as_user(TOKEN_A):
        await ensure_model_cache()
    with as_user(TOKEN_B):
        await ensure_model_cache()

    with as_user(TOKEN_A):
        assert get_cached_model("PrivateA") is not None
        assert get_cached_model("PrivateB") is None
    with as_user(TOKEN_B):
        assert get_cached_model("PrivateB").info["defaults"]["steps"] == 12
        assert get_cached_model("PrivateA") is None
        slugs = [m.slug for m in get_cached_models_for_tool("text_to_image")]
        assert slugs == ["Flux1schnell", "PrivateB"]


async def test_tool_descriptions_never_show_another_users_models(api):
    class FakeTool:
        def __init__(self, name):
            self.name = name
            self.description = "Generate an image."

        def model_copy(self, update):
            copy = FakeTool(self.name)
            copy.description = update["description"]
            return copy

    middleware = ModelEnrichmentMiddleware()
    call_next = AsyncMock(return_value=[FakeTool("text_to_image")])

    with as_user(TOKEN_A):
        [tool_a] = await middleware.on_list_tools(None, call_next)
    with as_user(TOKEN_B):
        [tool_b] = await middleware.on_list_tools(None, call_next)

    assert "PrivateA" in tool_a.description and "PrivateB" not in tool_a.description
    assert "PrivateB" in tool_b.description and "PrivateA" not in tool_b.description


async def test_catalogue_is_fetched_once_per_user_within_ttl(api):
    for _ in range(3):
        with as_user(TOKEN_A):
            await ensure_model_cache(model="Flux1schnell")
        with as_user(TOKEN_B):
            await ensure_model_cache(model="Flux1schnell")

    assert sorted(api.calls) == [TOKEN_A, TOKEN_B]


async def test_a_model_missing_from_a_fresh_catalogue_refetches_once(api, monkeypatch):
    with as_user(TOKEN_A):
        await ensure_model_cache()
        assert api.calls == [TOKEN_A]

        # The account gains a model after the catalogue was cached.
        monkeypatch.setitem(CATALOGUES, TOKEN_A, CATALOGUES[TOKEN_A] + [model("NewForA")])
        await ensure_model_cache(model="NewForA")
        assert get_cached_model("NewForA") is not None
        assert len(api.calls) == 2

        # A slug that still does not exist does not refetch again within the interval.
        await ensure_model_cache(model="NoSuchModel")
        await ensure_model_cache(model="NoSuchModel")
        assert len(api.calls) == 2


async def test_the_miss_refresh_is_rate_limited_per_user(api, monkeypatch):
    with as_user(TOKEN_A):
        await ensure_model_cache()
        await ensure_model_cache(model="NoSuchModel")  # refetch #2, starts the interval
    with as_user(TOKEN_B):
        await ensure_model_cache()
        await ensure_model_cache(model="NoSuchModel")  # B has its own interval

    assert api.calls == [TOKEN_A, TOKEN_A, TOKEN_B, TOKEN_B]

    with as_user(TOKEN_A):
        _caches.current().last_miss_refresh = time.monotonic() - mw.MISS_REFRESH_INTERVAL - 1
        await ensure_model_cache(model="NoSuchModel")
    assert api.calls[-1] == TOKEN_A and len(api.calls) == 5


async def test_least_recently_used_user_is_evicted(api, monkeypatch):
    monkeypatch.setattr(_caches, "max_entries", 2)
    tokens = [f"user-{i}-0123456789" for i in range(3)]
    for token in tokens:
        monkeypatch.setitem(CATALOGUES, token, [model(f"Only-{token}")])
        with as_user(token):
            await ensure_model_cache()

    assert len(_caches) == 2
    with as_user(tokens[0]):  # evicted: starts empty again
        assert get_cached_model(f"Only-{tokens[0]}") is None
    with as_user(tokens[2]):
        assert get_cached_model(f"Only-{tokens[2]}") is not None


def test_cache_keys_never_contain_the_token():
    with as_user(TOKEN_A):
        _caches.current()
    [key] = list(_caches._unconfirmed)
    assert TOKEN_A not in key and len(key) == 64


async def test_random_tokens_cannot_evict_users_whose_catalogue_loaded(api, monkeypatch):
    monkeypatch.setattr(_caches, "max_entries", 2)
    monkeypatch.setattr(_caches, "max_unconfirmed", 3)
    with as_user(TOKEN_A):
        await ensure_model_cache()

    # Unknown tokens: the fetch fails (the fake API has no catalogue for them).
    for i in range(20):
        with as_user(f"garbage-{i}-0123456789"):
            await ensure_model_cache()

    assert len(_caches) == 1 and len(_caches._unconfirmed) == 3
    with as_user(TOKEN_A):
        assert get_cached_model("PrivateA") is not None
    assert api.calls.count(TOKEN_A) == 1


async def test_concurrent_calls_of_one_user_fetch_once(api):
    async def call():
        with as_user(TOKEN_A):
            await ensure_model_cache(model="Flux1schnell")

    await asyncio.gather(*[call() for _ in range(10)])
    assert api.calls == [TOKEN_A]


async def test_concurrent_users_in_separate_tasks_stay_apart(api):
    async def call(token, slug):
        with as_user(token):
            await ensure_model_cache()
            await asyncio.sleep(0)
            return get_cached_model(slug) is not None

    results = await asyncio.gather(
        *[call(TOKEN_A, "PrivateA") for _ in range(5)],
        *[call(TOKEN_B, "PrivateB") for _ in range(5)],
        *[call(TOKEN_A, "PrivateB") for _ in range(5)],
    )
    assert results == [True] * 10 + [False] * 5
