"""A stand-in for the deAPI API, enough for the auth end-to-end run.

Knows one good key (``E2E_GOOD_KEY``) and answers:

- ``GET /api/v2/account/balance``  200 with a balance for the good key, 401 otherwise;
- ``GET /api/v2/models``           an empty catalogue for the good key, 401 otherwise.

``POST /_control/mode`` with ``{"mode": "up" | "down"}`` makes every API route answer 503,
to simulate an outage; ``GET /_control/calls`` returns how many balance requests arrived
per key kind (``good``/``other``). Run with ``python mock_deapi.py`` (port ``MOCK_PORT``,
default 9000).
"""

import os
from collections import Counter

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

GOOD_KEY = os.environ["E2E_GOOD_KEY"]

state = {"mode": "up"}
balance_calls: Counter[str] = Counter()


def _key(request: Request) -> str:
    return request.headers.get("authorization", "").removeprefix("Bearer ")


def _guard(request: Request) -> JSONResponse | None:
    if state["mode"] == "down":
        return JSONResponse({"message": "Service Unavailable"}, status_code=503)
    if _key(request) != GOOD_KEY:
        return JSONResponse({"message": "Unauthenticated."}, status_code=401)
    return None


async def balance(request: Request) -> JSONResponse:
    balance_calls["good" if _key(request) == GOOD_KEY else "other"] += 1
    return _guard(request) or JSONResponse({"data": {"balance": 12.5, "currency": "USD"}})


async def models(request: Request) -> JSONResponse:
    return _guard(request) or JSONResponse(
        {"data": [], "meta": {"total": 0, "last_page": 1, "per_page": 50, "current_page": 1}}
    )


async def set_mode(request: Request) -> JSONResponse:
    state["mode"] = (await request.json())["mode"]
    return JSONResponse(state)


async def calls(request: Request) -> JSONResponse:
    return JSONResponse(dict(balance_calls))


app = Starlette(
    routes=[
        Route("/api/v2/account/balance", balance),
        Route("/api/v2/models", models),
        Route("/_control/mode", set_mode, methods=["POST"]),
        Route("/_control/calls", calls),
        Route("/health", lambda request: JSONResponse({"status": "ok"})),
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("MOCK_PORT", "9000")), log_level="warning")
