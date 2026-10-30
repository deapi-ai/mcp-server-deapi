"""A SIGTERM lets in-flight tool calls finish instead of cutting their SSE streams."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from sse_starlette.sse import AppStatus

from deapi_mcp import server_remote

REPO_ROOT = Path(__file__).resolve().parent.parent

# Runs the real entry point (main() -> build_app() -> uvicorn) with one slow test-only
# tool registered first. It never reaches deAPI: the tool only sleeps.
CHILD = """
import asyncio, os, pathlib
import deapi_mcp.server_remote as sr

async def slow_probe(seconds: float) -> dict:
    \"\"\"Test-only: sleeps, so a SIGTERM can land mid-call.\"\"\"
    pathlib.Path(os.environ["DRAIN_TEST_MARKER"]).touch()
    await asyncio.sleep(seconds)
    return {"success": True, "slept": seconds}

sr.mcp.tool()(slow_probe)
sr.main()
"""

TOOL_SECONDS = 3


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(predicate, timeout: float, proc: subprocess.Popen) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _healthy(base: str) -> bool:
    try:
        return httpx.get(f"{base}/health", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def test_build_app_disables_sse_starlette_drain(app_client):
    assert AppStatus.enable_automatic_graceful_drain is False


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows has no SIGTERM: send_signal() hard-kills the process",
)
def test_sigterm_lets_in_flight_tool_call_finish(tmp_path, auth_headers):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    marker = tmp_path / "tool-started"
    log_path = tmp_path / "server.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DEAPI_", "MCP_"))}
    env.update(
        MCP_HOST="127.0.0.1",
        MCP_PORT=str(port),
        MCP_GRACEFUL_SHUTDOWN_SECONDS="30",
        DEAPI_JWT_SECRET_KEY="x" * 40,
        DEAPI_ENRICH_TOOL_DESCRIPTIONS="false",
        DEAPI_API_BASE_URL="http://127.0.0.1:9",
        DRAIN_TEST_MARKER=str(marker),
        PYTHONDONTWRITEBYTECODE="1",
    )
    result: dict = {}

    def call() -> None:
        try:
            result["response"] = httpx.post(
                f"{base}/mcp",
                headers={**auth_headers, "MCP-Protocol-Version": "2025-06-18"},
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "slow_probe", "arguments": {"seconds": TOOL_SECONDS}},
                },
                timeout=30,
            )
        except httpx.HTTPError as exc:
            result["error"] = repr(exc)

    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD], cwd=REPO_ROOT, env=env, stdout=log, stderr=log
        )
    caller = threading.Thread(target=call, daemon=True)
    try:
        assert _wait_for(lambda: _healthy(base), 20, proc), log_path.read_text()
        caller.start()
        assert _wait_for(marker.exists, 10, proc), log_path.read_text()

        proc.send_signal(signal.SIGTERM)
        caller.join(timeout=30)
        returncode = proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    logs = log_path.read_text()
    assert not caller.is_alive(), f"tool call still pending after shutdown\n{logs}"
    assert "error" not in result, f"{result['error']}\n{logs}"
    response = result["response"]
    assert response.status_code == 200, logs
    data = [line[5:] for line in response.text.splitlines() if line.startswith("data:")]
    assert data, f"no SSE data event, the stream was cut: {response.text!r}\n{logs}"
    payload = json.loads(data[-1])
    assert payload["result"]["isError"] is False
    assert "slept" in json.dumps(payload["result"])
    # uvicorn re-raises the captured SIGTERM once its graceful shutdown has completed.
    assert returncode in (0, -signal.SIGTERM), logs


def test_blank_shutdown_seconds_starts_with_the_default(tmp_path):
    # A blank line in the secrets env (`MCP_GRACEFUL_SHUTDOWN_SECONDS=`) reaches the process as an
    # empty value; it must mean "unset", not crash-loop the rollout.
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "server.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DEAPI_", "MCP_"))}
    env.update(
        MCP_HOST="127.0.0.1",
        MCP_PORT=str(port),
        MCP_GRACEFUL_SHUTDOWN_SECONDS="",
        DEAPI_JWT_SECRET_KEY="x" * 40,
        DEAPI_ENRICH_TOOL_DESCRIPTIONS="false",
        DEAPI_API_BASE_URL="http://127.0.0.1:9",
        PYTHONDONTWRITEBYTECODE="1",
    )
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import deapi_mcp.server_remote as sr; sr.main()"],
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=log,
        )
    try:
        assert _wait_for(lambda: _healthy(base), 20, proc), log_path.read_text()
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_env_int_falls_back_to_the_default_when_unset_or_blank(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("MCP_TEST_INT", raising=False)
    else:
        monkeypatch.setenv("MCP_TEST_INT", raw)
    assert server_remote.env_int("MCP_TEST_INT", 930) == 930


def test_env_int_reads_an_integer(monkeypatch):
    monkeypatch.setenv("MCP_TEST_INT", " 45 ")
    assert server_remote.env_int("MCP_TEST_INT", 930) == 45


def test_env_int_names_the_variable_when_the_value_is_not_an_integer(monkeypatch):
    monkeypatch.setenv("MCP_TEST_INT", "15m")
    with pytest.raises(ValueError, match="MCP_TEST_INT must be an integer, got '15m'"):
        server_remote.env_int("MCP_TEST_INT", 930)
