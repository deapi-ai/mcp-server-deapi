"""The page served at ``GET /``: how to connect an MCP client to this server.

One static HTML document: inline CSS, no script, no external resource, so a strict
Content-Security-Policy holds (the stylesheet is allowed by its hash, nothing else
loads). Client sections are ``<details>`` blocks, which open and close without
JavaScript. The endpoint URL comes from ``PUBLIC_BASE_URL`` (or the request), and
the tool list from the server's own registry, so the page never drifts from what
the server actually serves.
"""

import base64
import hashlib
import os
from html import escape
from typing import Any, Optional, Sequence

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from . import __version__

KEYS_URL = "https://app.deapi.ai/settings/api-keys"
DOCS_URL = "https://docs.deapi.ai/"
MCP_DOCS_URL = "https://docs.deapi.ai/execution-modes-and-integrations/mcp-server"
REPO_URL = "https://github.com/deapi-ai/mcp-server-deapi"
SECURITY_URL = "https://github.com/deapi-ai/mcp-server-deapi/blob/main/SECURITY.md"
OAUTH_CLIENT_ID = "deapi-mcp"

# Tool modules (deapi_mcp.tools.<name>) in page order, with their heading.
CATEGORIES = (
    ("image", "Image"),
    ("video", "Video"),
    ("audio", "Audio and speech"),
    ("embedding", "Embeddings"),
    ("prompt", "Prompts"),
    ("utility", "Account and jobs"),
)

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1f;--muted:#5f6368;--line:#e3e3e0;--card:#fff;
--code:#f3f3f1;--accent:#3346d3;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececee;--muted:#a1a1a8;
--line:#2c2c31;--card:#1b1b1f;--code:#232328;--accent:#8c9bff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}
main{max-width:46rem;margin:0 auto;padding:3rem 1rem 4rem}
h1{font-size:2rem;line-height:1.2;margin:0 0 .5rem}
h2{font-size:1.25rem;margin:2.5rem 0 .75rem}
h3{font-size:1rem;margin:1.5rem 0 .5rem}
p{margin:.5rem 0}
a{color:var(--accent)}
.lead{color:var(--muted);font-size:1.075rem;margin-bottom:1.5rem}
.endpoint{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:1rem 1.25rem}
.endpoint .label{color:var(--muted);font-size:.85rem;margin:0}
.endpoint code{font-size:1.05rem;background:none;padding:0}
code{font:.9em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--code);
padding:.1em .35em;border-radius:5px;overflow-wrap:anywhere}
pre{background:var(--code);border-radius:8px;padding:.85rem 1rem;overflow-x:auto;margin:.5rem 0}
pre code{background:none;padding:0;user-select:all}
.endpoint code,td code{user-select:all}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:.25rem 1.25rem;margin:.6rem 0}
details[open]{padding-bottom:1rem}
summary{cursor:pointer;font-weight:600;padding:.6rem 0}
.note{color:var(--muted);font-size:.9rem}
ol,ul{padding-left:1.4rem}
li{margin:.25rem 0}
table{border-collapse:collapse;width:100%;font-size:.95rem}
td{border-top:1px solid var(--line);padding:.45rem .5rem .45rem 0;vertical-align:top}
td:first-child{white-space:nowrap;padding-right:1rem}
pre code{overflow-wrap:normal}
@media (max-width:560px){td{display:block;border-top:0;padding:0}
td:first-child{border-top:1px solid var(--line);padding-top:.45rem}
td:last-child{padding-bottom:.45rem}}
footer{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--line);color:var(--muted);
font-size:.9rem}
footer a{margin-right:1rem}
"""

_STYLE_HASH = base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; "
        f"style-src 'sha256-{_STYLE_HASH}'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    # The page names the host it was reached on, so a shared cache must not keep it.
    "Cache-Control": "no-cache",
}


def _first_sentence(description: Optional[str]) -> str:
    lines = (description or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def _tool_category(tool: Any) -> str:
    module = getattr(getattr(tool, "fn", None), "__module__", "") or ""
    return module.rsplit(".", 1)[-1]


def _code(text: str) -> str:
    return f"<pre><code>{escape(text)}</code></pre>"


def _tools_html(tools: Sequence[Any]) -> str:
    """The tool list, grouped by category. Price tools are counted, not listed."""
    generators = [t for t in tools if not t.name.endswith("_price")]
    price_count = len(tools) - len(generators)
    known = {key for key, _ in CATEGORIES}
    groups = [(key, title) for key, title in CATEGORIES]
    if any(_tool_category(t) not in known for t in generators):
        groups.append(("", "Other"))

    parts = []
    for key, title in groups:
        members = [
            t
            for t in generators
            if (_tool_category(t) == key if key else _tool_category(t) not in known)
        ]
        if not members:
            continue
        rows = "".join(
            f"<tr><td><code>{escape(t.name)}</code></td>"
            f"<td>{escape(_first_sentence(t.description))}</td></tr>"
            for t in members
        )
        parts.append(f"<h3>{escape(title)}</h3><table>{rows}</table>")
    if price_count:
        parts.append(
            f"<p class=note>Plus {price_count} <code>*_price</code> tools that quote "
            "the cost of a job before it runs.</p>"
        )
    return "".join(parts)


def render_landing(base_url: str, tools: Sequence[Any]) -> str:
    """The whole page for a server reachable at ``base_url``."""
    base = base_url.rstrip("/")
    mcp_url = f"{base}/mcp"
    e_url = escape(mcp_url)

    claude_code = (
        f"claude mcp add --transport http deapi {mcp_url} \\\n"
        '  --header "Authorization: Bearer $DEAPI_API_KEY"'
    )
    claude_code_json = (
        "{\n"
        '  "mcpServers": {\n'
        '    "deapi": {\n'
        '      "type": "http",\n'
        f'      "url": "{mcp_url}",\n'
        '      "headers": { "Authorization": "Bearer ${DEAPI_API_KEY}" }\n'
        "    }\n"
        "  }\n"
        "}"
    )
    cursor_json = (
        "{\n"
        '  "mcpServers": {\n'
        '    "deapi": {\n'
        f'      "url": "{mcp_url}",\n'
        '      "headers": { "Authorization": "Bearer ${env:DEAPI_API_KEY}" }\n'
        "    }\n"
        "  }\n"
        "}"
    )
    cursor_oauth_json = (
        "{\n"
        '  "mcpServers": {\n'
        '    "deapi": {\n'
        f'      "url": "{mcp_url}",\n'
        '      "auth": {\n'
        f'        "CLIENT_ID": "{OAUTH_CLIENT_ID}",\n'
        '        "CLIENT_SECRET": "${env:DEAPI_API_KEY}"\n'
        "      }\n"
        "    }\n"
        "  }\n"
        "}"
    )
    vscode_json = (
        "{\n"
        '  "inputs": [\n'
        '    { "type": "promptString", "id": "deapi-key",\n'
        '      "description": "deAPI API key", "password": true }\n'
        "  ],\n"
        '  "servers": {\n'
        '    "deapi": {\n'
        '      "type": "http",\n'
        f'      "url": "{mcp_url}",\n'
        '      "headers": { "Authorization": "Bearer ${input:deapi-key}" }\n'
        "    }\n"
        "  }\n"
        "}"
    )
    codex_toml = (
        "[mcp_servers.deapi]\n" f'url = "{mcp_url}"\n' 'bearer_token_env_var = "DEAPI_API_KEY"'
    )
    gemini_json = (
        "{\n"
        '  "mcpServers": {\n'
        '    "deapi": {\n'
        f'      "httpUrl": "{mcp_url}",\n'
        '      "headers": { "Authorization": "Bearer <YOUR_DEAPI_API_KEY>" }\n'
        "    }\n"
        "  }\n"
        "}"
    )
    curl = (
        f"curl -s {mcp_url} \\\n"
        '  -H "Authorization: Bearer $DEAPI_API_KEY" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        '  -H "Accept: application/json, text/event-stream" \\\n'
        """  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'"""
    )

    tool_count = len(tools)
    return f"""<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name=referrer content=no-referrer>
<title>deAPI MCP Server</title>
<meta name=description content="Connect Claude, ChatGPT, Cursor, VS Code, Codex and other MCP clients to deAPI: image, video, audio, transcription, OCR and embedding models.">
<style>{CSS}</style>
</head>
<body>
<main>
<h1>deAPI MCP Server</h1>
<p class=lead>Generate images, video, speech and music, transcribe audio and video, read text
from images and create embeddings, all from your AI assistant. {tool_count} tools over one
endpoint, billed to your deAPI balance.</p>

<div class=endpoint>
<p class=label>MCP endpoint · Streamable HTTP</p>
<code>{e_url}</code>
</div>
<p class=note>This server speaks Streamable HTTP only. There is no SSE endpoint; a client
that can only use SSE needs an update.</p>

<h2>1. Get an API key</h2>
<p>Create a key at <a href="{KEYS_URL}">app.deapi.ai/settings/api-keys</a>. Jobs run on your
deAPI balance, so treat the key like a password. The examples below read it from the
<code>DEAPI_API_KEY</code> environment variable where the client allows that, which keeps it
out of config files and shell history.</p>

<h2>2. Connect your client</h2>

<details open>
<summary>Claude (claude.ai and Claude Desktop)</summary>
<ol>
<li>Open <b>Customize → Connectors</b> and choose <b>Add custom connector</b>.</li>
<li>Name it <code>deAPI</code> and paste the endpoint <code>{e_url}</code>.</li>
<li>Open <b>Advanced settings</b>, choose <b>Use your own OAuth client</b> and enter
Client ID <code>{OAUTH_CLIENT_ID}</code> and, as Client Secret, your deAPI API key.</li>
<li>Click <b>Add</b>, then <b>Connect</b>.</li>
</ol>
<p class=note>Where your plan offers <b>Request headers</b> for custom connectors, you can
instead choose no sign-in and add the header <code>Authorization: Bearer &lt;your key&gt;</code>.
On Team and Enterprise plans an owner adds connectors under Organization settings.</p>
</details>

<details>
<summary>ChatGPT</summary>
<ol>
<li>On chatgpt.com, open <b>Settings → Apps &amp; Connectors → Advanced settings</b> and turn on
<b>Developer mode</b>.</li>
<li>Choose <b>Create</b>, name the app <code>deAPI</code> and enter the MCP server URL
<code>{e_url}</code>.</li>
<li>Set <b>Authentication</b> to <b>OAuth</b> and enter Client ID
<code>{OAUTH_CLIENT_ID}</code> and, as Client Secret, your deAPI API key.</li>
<li>Confirm and create the app, then enable it in a chat.</li>
</ol>
<p class=note>Developer mode depends on your ChatGPT plan and is set up on the web.</p>
</details>

<details>
<summary>Claude Code</summary>
{_code(claude_code)}
<p>Or share it with a project in <code>.mcp.json</code>:</p>
{_code(claude_code_json)}
</details>

<details>
<summary>Cursor</summary>
<p>In <code>~/.cursor/mcp.json</code> (or <code>.cursor/mcp.json</code> in a project):</p>
{_code(cursor_json)}
<p>If you prefer Cursor's OAuth sign-in, use the pre-registered client instead:</p>
{_code(cursor_oauth_json)}
</details>

<details>
<summary>VS Code (GitHub Copilot)</summary>
<p>In <code>.vscode/mcp.json</code>. VS Code asks for the key once and stores it securely:</p>
{_code(vscode_json)}
</details>

<details>
<summary>Codex CLI</summary>
<p>In <code>~/.codex/config.toml</code>. Codex sends the variable's value as a Bearer token:</p>
{_code(codex_toml)}
</details>

<details>
<summary>Gemini CLI</summary>
<p>In <code>~/.gemini/settings.json</code>. Use <code>httpUrl</code>: in Gemini CLI
<code>url</code> means SSE.</p>
{_code(gemini_json)}
<p class=note>This file holds the key in plain text; keep it private.</p>
</details>

<details>
<summary>Any other MCP client</summary>
<p>Use the endpoint <code>{e_url}</code> with transport Streamable HTTP (sometimes called
just "HTTP") and send your key in one of these headers:</p>
<ul>
<li><code>Authorization: Bearer &lt;your key&gt;</code></li>
<li><code>X-API-Key: &lt;your key&gt;</code></li>
</ul>
<p>A client that insists on OAuth can use the pre-registered client: Client ID
<code>{OAUTH_CLIENT_ID}</code>, Client Secret = your API key, Authorization Code flow with
PKCE (S256). The server does not offer dynamic client registration.</p>
</details>

<h2>3. Check that it works</h2>
<p>Ask your assistant to <i>"check my deAPI balance"</i>, or list the tools from a terminal:</p>
{_code(curl)}
<p>A key deAPI does not accept is refused with <code>401</code> as soon as the client
connects, so a client that reports an authentication error is telling you to fix the key.
Never put the key in the URL; it is only read from the headers above.</p>

<h2>Things to try</h2>
<ul>
<li>"Generate a 1024×1024 image of a lighthouse at dusk, watercolor style."</li>
<li>"Transcribe this video and give me timestamps: &lt;link&gt;"</li>
<li>"Read the text in this screenshot."</li>
<li>"Remove the background from this photo, then upscale it."</li>
<li>"Turn this image into a five-second clip."</li>
<li>"How much would that cost before you run it?"</li>
</ul>

<h2>Long jobs</h2>
<p>Video and long transcriptions can take minutes. A tool call waits up to 45 seconds; if
the job is still running it returns <code>done: false</code> with a <code>job_id</code>, and
<code>check_job_status</code> picks the wait up again. Your assistant does this for you.</p>

<h2>Tools</h2>
{_tools_html(tools)}

<footer>
<p><a href="{MCP_DOCS_URL}">MCP server docs</a><a href="{DOCS_URL}">deAPI docs</a><a
href="{KEYS_URL}">API keys</a><a href="{REPO_URL}">Source</a><a
href="{SECURITY_URL}">Security</a></p>
<p>deAPI MCP Server {escape(__version__)}</p>
</footer>
</main>
</body>
</html>
"""


def public_base_url(request: Request) -> str:
    """The URL clients reach this server on: PUBLIC_BASE_URL, else the request's own."""
    return os.getenv("PUBLIC_BASE_URL") or f"{request.url.scheme}://{request.url.netloc}"


def landing_response(base_url: str, tools: Sequence[Any]) -> Response:
    return HTMLResponse(render_landing(base_url, tools), headers=SECURITY_HEADERS)
