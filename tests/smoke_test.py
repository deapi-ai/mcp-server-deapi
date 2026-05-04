"""Smoke test: hit real price-calculation endpoints to validate content-types and params.

Usage:
    DEAPI_API_TOKEN=your-token python tests/smoke_test.py

    Or pass token as argument:
    python tests/smoke_test.py your-token

This tests ONLY price-calculation endpoints (zero credit cost).
Validates that the API accepts our content-type and parameter format.
"""

import asyncio
import os
import sys

import httpx

BASE_URL = os.getenv("DEAPI_API_BASE_URL", "https://api.deapi.ai")
API_PREFIX = "/api/v2"


def get_token() -> str:
    """Get API token from env or CLI arg."""
    token = os.getenv("DEAPI_API_TOKEN") or os.getenv("DEAPI_TOKEN")
    if not token and len(sys.argv) > 1:
        token = sys.argv[1]
    if not token:
        print("ERROR: No API token provided.")
        print("  Set DEAPI_API_TOKEN env var or pass as argument:")
        print("  python tests/smoke_test.py <your-token>")
        sys.exit(1)
    return token


# =============================================================================
# Test definitions: each is (name, endpoint, content_type, payload)
# =============================================================================

PRICE_TESTS = [
    {
        "name": "audio/transcriptions/price (duration-based)",
        "endpoint": "audio/transcriptions/price",
        "content_type": "form",
        "payload": {
            "duration_seconds": "120",
            "include_ts": "true",
            "model": "WhisperLargeV3",
        },
    },
    {
        "name": "images/ocr/price (dimension-based)",
        "endpoint": "images/ocr/price",
        "content_type": "form",
        "payload": {
            "width": "1920",
            "height": "1080",
            "model": "Nanonets_Ocr_S_F16",
        },
    },
    {
        "name": "audio/speech/price",
        "endpoint": "audio/speech/price",
        "content_type": "json",
        "payload": {
            "model": "Kokoro",
            "voice": "af_sky",
            "lang": "en-us",
            "speed": 1,
            "format": "flac",
            "sample_rate": 24000,
            "count_text": 100,
        },
    },
    {
        "name": "videos/generations/price",
        "endpoint": "videos/generations/price",
        "content_type": "form",
        "payload": {
            "model": "Ltxv_13B_0_9_8_Distilled_FP8",
            "width": "512",
            "height": "512",
            "frames": "30",
            "steps": "1",
            "fps": "30",
        },
    },
    {
        "name": "embeddings/price",
        "endpoint": "embeddings/price",
        "content_type": "json",
        "payload": {
            "input": "This is a test string for price calculation.",
            "model": "Bge_M3_FP16",
        },
    },
    # videos/background-removals: no production model with inference_type "vid-rmbg" yet — skip
    {
        "name": "videos/upscales/price (dimension-based)",
        "endpoint": "videos/upscales/price",
        "content_type": "form",
        "payload": {
            "width": "1024",
            "height": "1024",
            "model": "RealESRGAN_Vid_x4",
        },
    },
]


async def run_test(client: httpx.AsyncClient, test: dict) -> dict:
    """Run a single price-calculation smoke test."""
    url = f"{API_PREFIX}/{test['endpoint']}"
    name = test["name"]

    try:
        if test["content_type"] == "json":
            response = await client.post(url, json=test["payload"])
        else:
            response = await client.post(url, data=test["payload"])

        status = response.status_code
        body = None
        try:
            body = response.json()
        except Exception:
            body = response.text[:200]

        if 200 <= status < 300:
            return {"name": name, "status": "PASS", "http": status, "body": body}
        elif status == 401:
            return {"name": name, "status": "AUTH", "http": status, "body": body}
        elif status == 422:
            return {"name": name, "status": "FAIL", "http": status, "detail": "Validation error - check params", "body": body}
        else:
            return {"name": name, "status": "FAIL", "http": status, "body": body}

    except Exception as e:
        return {"name": name, "status": "ERROR", "error": str(e)}


async def main():
    token = get_token()

    print(f"Smoke testing {len(PRICE_TESTS)} price-calculation endpoints")
    print(f"API: {BASE_URL}")
    print("-" * 70)

    async with httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=30.0,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    ) as client:
        results = []
        for test in PRICE_TESTS:
            result = await run_test(client, test)
            results.append(result)

            # Print result immediately
            status = result["status"]
            icon = {"PASS": "+", "FAIL": "!", "AUTH": "?", "ERROR": "x"}[status]
            http_code = result.get("http", "---")
            print(f"  [{icon}] {status:5s}  HTTP {http_code}  {result['name']}")

            if status == "FAIL":
                body = result.get("body", "")
                if isinstance(body, dict):
                    msg = body.get("message", body.get("error", ""))
                    if msg:
                        print(f"         -> {msg}")
                    errors = body.get("errors", {})
                    if errors:
                        for field, msgs in errors.items():
                            print(f"         -> {field}: {msgs}")

    # Summary
    print("-" * 70)
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    auth = sum(1 for r in results if r["status"] == "AUTH")
    errors = sum(1 for r in results if r["status"] == "ERROR")

    print(f"Results: {passed} passed, {failed} failed, {auth} auth errors, {errors} errors")

    if auth > 0:
        print("\nAuth errors mean your token is invalid or expired.")
    if failed > 0:
        print("\nFailed tests indicate content-type or parameter mismatches.")
        print("Check the error details above for which fields are wrong.")

    return 0 if (failed == 0 and errors == 0) else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
