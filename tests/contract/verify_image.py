"""Contract checks that run INSIDE the built image (CI stage `verify`).

These are deliberately not part of the pytest suite: they assert things about the
*image* — the dependency versions baked into it and the server it actually boots —
which a suite running against the checkout with its own resolved environment cannot
see. Both checks below correspond to a failure that already happened here and that
the unit suite did not catch:

  * `fastmcp` is declared unpinned (>=2.0.0). 4.x moved `fastmcp.tools.tool`, so an
    image built today imported a module that no longer existed. Tests passed; the pod
    crash-looped.
  * `deapi_api_base_url` sits behind `env_prefix="DEAPI_"` and so resolved to
    `DEAPI_DEAPI_API_BASE_URL`. `DEAPI_API_BASE_URL` from the ConfigMap was silently
    ignored and the sandbox would have driven billable jobs on production deAPI.

Run inside the image, no pytest needed (the image carries runtime deps only):

    python tests/contract/verify_image.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Prefer the image's own copy of the application over the CI checkout, so this
# verifies what was actually built. Falls back to the repo root when run locally.
APP_DIR = (
    "/app"
    if os.path.isdir("/app/src")
    else os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

PORT = int(os.environ.get("VERIFY_PORT", "8931"))
SANDBOX_API = "https://depin-api-sandbox.deapi.dev"
PUBLIC_URL = "https://depin-mcp-sandbox.deapi.dev"

failures: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - report every failure, never abort early
        failures.append(f"{name}: {exc}")
        print(f"  FAIL  {name}\n        {exc}")
    else:
        print(f"  ok    {name}")


def check_imports() -> None:
    """Every module the server touches must import against the baked-in deps."""
    subprocess.run(
        [sys.executable, "-c", "import src.server_remote, src.middleware, src.fastmcp_auth"],
        cwd=APP_DIR,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def check_config_env_is_honoured() -> None:
    """DEAPI_API_BASE_URL from the ConfigMap must actually reach Settings."""
    env = {**os.environ, "DEAPI_API_BASE_URL": SANDBOX_API}
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from src.config import Settings; print(Settings().deapi_api_base_url)",
        ],
        cwd=APP_DIR,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.strip()
    if out != SANDBOX_API:
        raise AssertionError(
            f"Settings resolved {out!r}, not the value set in the environment. "
            "A deploy would silently talk to the wrong deAPI."
        )


def check_server_boots_and_is_healthy() -> None:
    """The image must actually serve /health — that is what the k8s probes hit."""
    env = {
        **os.environ,
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(PORT),
        "PUBLIC_BASE_URL": PUBLIC_URL,
        "DEAPI_JWT_SECRET_KEY": "verify-stage-only-not-a-real-secret",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.server_remote"],
        cwd=APP_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 60
        last: Exception | None = None
        while time.time() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"server exited early (code {proc.returncode}):\n{proc.communicate()[0][-2000:]}"
                )
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3) as resp:
                    body = json.loads(resp.read())
                if body.get("status") != "healthy":
                    raise AssertionError(f"/health returned {body!r}")
                return
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
                last = exc
                time.sleep(1)
        raise AssertionError(f"/health never became reachable within 60s (last error: {last})")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    print(f"Image contract checks (APP_DIR={APP_DIR})")
    check("modules import against baked-in dependencies", check_imports)
    check("DEAPI_API_BASE_URL is honoured by Settings", check_config_env_is_honoured)
    check("server boots and /health reports healthy", check_server_boots_and_is_healthy)

    print()
    if failures:
        print(f"{len(failures)} contract check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All image contract checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
