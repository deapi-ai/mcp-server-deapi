"""The page served at ``GET /``: how to connect an MCP client to this server.

One static HTML document: inline CSS, one small inline script, no external resource,
so a strict Content-Security-Policy holds (the stylesheet and the script are allowed by
their hashes, nothing else loads or runs). The script only adds copy buttons: they stay
hidden without it, and every value can still be selected with one click. Client sections
are ``<details>`` blocks, which open and close without JavaScript. The endpoint URL comes from ``PUBLIC_BASE_URL`` (or the request), and
the tool list from the server's own registry, so the page never drifts from what
the server actually serves.
"""

import base64
import hashlib
import os
import re
from html import escape
from typing import Any, Optional, Sequence

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from . import __version__
from .config import settings

KEYS_URL = "https://app.deapi.ai/settings/api-keys"
DOCS_URL = "https://docs.deapi.ai/"
MCP_DOCS_URL = "https://docs.deapi.ai/execution-modes-and-integrations/mcp-server"
REPO_URL = "https://github.com/deapi-ai/mcp-server-deapi"
SECURITY_URL = "https://github.com/deapi-ai/mcp-server-deapi/blob/main/SECURITY.md"
OAUTH_CLIENT_ID = "deapi-mcp"
HOME_URL = "https://deapi.ai/"

# The deAPI logo (as in the deAPI docs), inline so the page loads nothing.
LOGO_SVG = '<svg class=logo xmlns="http://www.w3.org/2000/svg" viewBox="0 0 81.585 30.472" role=img aria-label=deAPI><g><path d="M34.149,19.256v-4.547l1.615-1.631h3.105l.925.8v-4.187h2.07v11.196h-1.944v-1.207l-1.208,1.207h-2.948l-1.615-1.631ZM38.525,19.131l1.27-1.302v-2.038l-1.066-.957h-1.913l-.596.611v3.074l.596.611h1.709Z" fill="#ffba00"/><path d="M43.59,19.288v-4.579l1.615-1.631h4.407l1.631,1.631v2.963h-5.583v1.004l.502.517h2.556l.47-.486v-.407h2.039v1.018l-1.553,1.568h-4.501l-1.584-1.599ZM49.173,16.231v-.91l-.533-.549h-2.447l-.533.549v.91h3.513Z" fill="#ffba00"/><path d="M55.884,9.911h1.945l4.014,10.977h-2.164l-.894-2.43h-3.858l-.894,2.43h-2.164l4.014-10.977ZM58.315,16.7l-1.459-4.171h-.031l-1.411,4.171h2.901Z" fill="#ffba00"/><path d="M62.941,9.911h6.885l1.678,1.694v3.841l-1.694,1.71h-4.736v3.732h-2.133v-10.977ZM68.854,15.368l.549-.549v-2.572l-.549-.55h-3.78v3.67h3.78Z" fill="#ffba00"/><path d="M73.15,9.911h2.133v10.977h-2.133v-10.977Z" fill="#ffba00"/></g><g><path d="M27.865,11.713v-2h-3.511v-2.595l-1-1h-2.595v-2.276l-2-1.155v3.43h-2.523V1.231l-.25-.144h-1.5l-.25.144v4.887h-2.523v-3.431l-2,1.155v2.276h-2.595l-1,1v2.595h-3.511v2h3.511v2.523h-3.511v2h3.511v2.522h-3.511v2h3.511v2.596l1,1h2.595v2.275l2,1.155v-3.43h2.523v4.887l.25.144h1.5l.25-.144v-4.887h2.523v3.43l2-1.155v-2.275h2.595l1-1v-2.596h3.511v-2h-3.511v-2.522h3.511v-2h-3.511v-2.523h3.511ZM22.354,22.355h-14.236v-14.236h14.236v14.236Z" fill="#ffba00"/><polygon points="20.742 9.691 18.222 9.691 9.727 20.781 12.247 20.781 20.742 9.691" fill="#ffba00"/></g></svg>'

# Tool modules (deapi_mcp.tools.<name>) in page order, with their heading.
CATEGORIES = (
    ("image", "Image"),
    ("video", "Video"),
    ("audio", "Audio and speech"),
    ("embedding", "Embeddings"),
    ("prompt", "Prompts"),
    ("utility", "Account and jobs"),
    ("account", "Account usage and API keys"),
)

CSS = """
:root{--bg:#11100d;--bg-deep:#0e0d0b;--card:#1a1710;--line:#363021;--line-soft:#262217;
--fg:#f2f0ea;--muted:#bcbcbc;--dim:#938367;--amber:#ffba00;--amber-hi:#feca00;
--sans:Switzer,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--muted);font:15px/1.7 var(--mono)}
.bar{height:4px;background:linear-gradient(90deg,#5c4a12,var(--amber),#5c4a12)}
header{max-width:58rem;margin:1.25rem auto 0;padding:.9rem 1.25rem;display:flex;
align-items:center;gap:1rem;border:1px solid var(--line-soft);border-radius:10px;
background:var(--bg-deep)}
header .home{display:flex;align-items:center;text-decoration:none}
.logo{height:2.4rem;width:auto;display:block}
.pill{font-size:.75rem;color:var(--amber);border:1px solid var(--line);border-radius:999px;
padding:.1rem .6rem;letter-spacing:.04em}
header nav{margin-left:auto;display:flex;gap:.6rem;flex-wrap:wrap}
.btn{display:inline-block;font:700 .85rem/1 var(--mono);text-decoration:none;
padding:.8rem 1.1rem;border-radius:4px;border:1px solid var(--amber);color:var(--amber)}
.btn.solid{background:var(--amber);color:#11100d}
.btn:hover{background:var(--amber-hi);border-color:var(--amber-hi);color:#11100d}
main{max-width:52rem;margin:0 auto;padding:3.5rem 1.25rem 4rem}
.facts{text-align:center;color:var(--fg);font-size:.9rem;margin:0}
.facts span{margin:0 .7rem;white-space:nowrap}
.facts span::before{content:"•";margin-right:.5rem}
h1{font:600 clamp(2.4rem,7vw,4.2rem)/1.05 var(--sans);color:#fff;text-align:center;
margin:.9rem 0 1rem;letter-spacing:-.01em}
h1 em{font-style:normal;color:var(--amber)}
.lead{text-align:center;font-size:1.05rem;max-width:40rem;margin:0 auto 2rem}
.cta{display:flex;justify-content:center;gap:.8rem;flex-wrap:wrap;margin-bottom:2.5rem}
h2{font:600 1.6rem/1.2 var(--sans);color:#fff;margin:3.5rem 0 1rem}
h2 small{display:block;font:500 .8rem var(--mono);color:var(--amber);letter-spacing:.08em;
text-transform:uppercase;margin-bottom:.4rem}
h3{font:600 1.05rem var(--sans);color:var(--fg);margin:1.8rem 0 .6rem}
p{margin:.6rem 0}
a{color:var(--amber)}
b,strong,i{color:var(--fg)}
.endpoint{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--amber);
border-radius:8px;padding:1rem 1.25rem}
.endpoint .label{color:var(--dim);font-size:.8rem;margin:0;letter-spacing:.04em}
.endpoint code{font-size:1.1rem;background:none;padding:0;color:var(--amber)}
code{font:.92em var(--mono);color:var(--fg);background:var(--line-soft);padding:.1em .35em;
border-radius:4px;overflow-wrap:anywhere}
pre{background:var(--bg-deep);border:1px solid var(--line-soft);border-radius:8px;
padding:.9rem 1rem;overflow-x:auto;margin:.6rem 0}
pre code{background:none;padding:0;user-select:all;overflow-wrap:normal;color:#e9e4d6}
.endpoint code,td code{user-select:all}
.copy{display:inline-flex;align-items:center;gap:.4rem;max-width:100%;vertical-align:middle}
.copy-btn{font:600 .72rem/1 var(--mono);color:var(--amber);background:transparent;
border:1px solid var(--line);border-radius:4px;padding:.3rem .55rem;cursor:pointer;
white-space:nowrap;flex:none}
.copy-btn:hover{border-color:var(--amber)}
.copy-btn:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.copy-btn.done{background:var(--amber);border-color:var(--amber);color:#11100d}
.snippet{position:relative}
.snippet pre{padding-right:5rem}
.snippet .copy-btn{position:absolute;top:.5rem;right:.5rem;background:var(--bg-deep)}
.snippet .copy-btn.done{background:var(--amber)}
.endpoint .copy{display:flex;justify-content:space-between;gap:1rem}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:.2rem 1.25rem;margin:.6rem 0}
details[open]{padding-bottom:1rem;border-color:#4a4029}
summary{cursor:pointer;font:600 1rem var(--sans);color:var(--fg);padding:.75rem 0}
summary::marker{color:var(--amber)}
.note{color:var(--dim);font-size:.85rem}
ol,ul{padding-left:1.4rem}
li{margin:.3rem 0}
li::marker{color:var(--amber)}
table{border-collapse:collapse;width:100%;font-size:.9rem}
td{border-top:1px solid var(--line-soft);padding:.5rem .5rem .5rem 0;vertical-align:top}
td:first-child{white-space:nowrap;padding-right:1rem}
td code{color:var(--amber);background:none;padding:0}
@media (max-width:640px){header{flex-wrap:wrap}header nav{margin-left:0;width:100%}
.facts span{display:inline-block;margin:.1rem .5rem}
td{display:block;border-top:0;padding:0}
td:first-child{border-top:1px solid var(--line-soft);padding-top:.5rem}
td:last-child{padding-bottom:.5rem}}
footer{margin-top:4rem;padding-top:1.25rem;border-top:1px solid var(--line-soft);
color:var(--dim);font-size:.85rem;display:flex;flex-wrap:wrap;gap:.5rem 1.25rem;
align-items:center}
footer .logo{height:1.5rem}
footer a{color:var(--muted);text-decoration:none}
footer a:hover{color:var(--amber)}
footer .version{margin-left:auto}
"""

# Shows the copy buttons and copies a button's data-copy value. Nothing else.
SCRIPT = (
    "document.querySelectorAll('button[data-copy]').forEach(function(b){b.hidden=false;"
    "b.addEventListener('click',function(){var label=b.textContent;"
    "navigator.clipboard.writeText(b.getAttribute('data-copy')).then(function(){"
    "b.textContent='Copied';b.classList.add('done');setTimeout(function(){"
    "b.textContent=label;b.classList.remove('done')},1500)},function(){"
    "b.textContent='Press Ctrl+C'})})})"
)

_STYLE_HASH = base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()
_SCRIPT_HASH = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; "
        f"style-src 'sha256-{_STYLE_HASH}'; "
        f"script-src 'sha256-{_SCRIPT_HASH}'; "
        "img-src 'self'; "
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


def _button(value: str, label: str = "Copy") -> str:
    """A copy button for ``value``; hidden until the page script shows it."""
    return (
        f'<button type=button class=copy-btn hidden data-copy="{escape(value)}" '
        f'aria-label="{escape(label)}: {escape(value)}">Copy</button>'
    )


def _copy(value: str) -> str:
    """An inline value with a copy button next to it."""
    return f"<span class=copy><code>{escape(value)}</code>{_button(value)}</span>"


def _code(text: str) -> str:
    return f"<div class=snippet><pre><code>{escape(text)}</code></pre>{_button(text)}</div>"


def _key_management_note() -> str:
    """What the key tools do on this server: DEAPI_KEY_MANAGEMENT_TOOLS, off by default."""
    if settings.key_management_tools:
        return (
            "<p class=note><b>Key management is on.</b> <code>create_api_key</code>, "
            "<code>update_api_key</code> and <code>delete_api_key</code> change your keys, and "
            "your client asks you before each change. A new key's secret is shown once, in "
            "the conversation. They need an ADMIN key.</p>"
        )
    return (
        "<p class=note><b>Creating, changing and deleting keys is off on this server</b>, for "
        "safety: a new key's secret would pass through the conversation, and a deleted key "
        "cannot be brought back. <code>create_api_key</code>, <code>update_api_key</code> and "
        "<code>delete_api_key</code> change nothing and answer with a link to the "
        f'<a href="{KEYS_URL}">API keys page</a>, where you do it yourself. Reading the '
        "account and the keys needs an ADMIN key; any key can read its own details and "
        "usage.</p>"
    )


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
        if key == "account":
            parts.append(_key_management_note())
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
    url_copy = _copy(mcp_url)
    client_id_copy = _copy(OAUTH_CLIENT_ID)

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
<link rel=icon type=image/png sizes=64x64 href="/icon-64.png">
<meta name=description content="Connect Claude, ChatGPT, Cursor, VS Code, Codex and other MCP clients to deAPI: image, video, audio, transcription, OCR and embedding models.">
<style>{CSS}</style>
</head>
<body>
<div class=bar></div>
<header>
<a class=home href="{HOME_URL}">{LOGO_SVG}</a>
<span class=pill>MCP SERVER</span>
<nav><a class=btn href="{MCP_DOCS_URL}">Docs</a><a class="btn solid"
href="{KEYS_URL}">Get API key</a></nav>
</header>
<main>
<p class=facts><span>{tool_count} tools</span><span>Streamable HTTP</span><span>Pay per
use</span></p>
<h1>deAPI <em>MCP Server</em></h1>
<p class=lead>Generate images, video, speech and music, transcribe audio and video, read text
from images and create embeddings, all from your AI assistant. {tool_count} tools over one
endpoint, billed to your deAPI balance.</p>
<div class=cta><a class="btn solid" href="#connect">Connect your client</a><a class=btn
href="#tools">See the tools</a></div>

<div class=endpoint>
<p class=label>MCP endpoint · Streamable HTTP</p>
{url_copy}
</div>
<p class=note>This server speaks Streamable HTTP only. There is no SSE endpoint; a client
that can only use SSE needs an update.</p>

<h2><small>Step 1</small>Get an API key</h2>
<p>Create a key at <a href="{KEYS_URL}">app.deapi.ai/settings/api-keys</a>. Jobs run on your
deAPI balance, so treat the key like a password. The examples below read it from the
<code>DEAPI_API_KEY</code> environment variable where the client allows that, which keeps it
out of config files and shell history.</p>

<h2 id=connect><small>Step 2</small>Connect your client</h2>

<details open>
<summary>Claude (claude.ai and Claude Desktop)</summary>
<ol>
<li>Open <b>Customize → Connectors</b> and choose <b>Add custom connector</b>.</li>
<li>Name it <code>deAPI</code> and paste the endpoint {url_copy}.</li>
<li>Click <b>Add</b>, then <b>Connect</b>. A deAPI sign-in window opens: paste your API key
and choose <b>Allow access</b>.</li>
</ol>
<p class=note>You can also skip the sign-in window: under <b>Advanced settings</b> choose
<b>Use your own OAuth client</b> with Client ID {client_id_copy} and your API key as
Client Secret, or, where your plan offers <b>Request headers</b>, add
<code>Authorization: Bearer &lt;your key&gt;</code>. On Team and Enterprise plans an owner adds
connectors under Organization settings.</p>
</details>

<details>
<summary>ChatGPT</summary>
<ol>
<li>On chatgpt.com, open <b>Settings → Apps &amp; Connectors → Advanced settings</b> and turn on
<b>Developer mode</b>.</li>
<li>Choose <b>Create</b>, name the app <code>deAPI</code> and enter the MCP server URL
{url_copy}.</li>
<li>Set <b>Authentication</b> to <b>OAuth</b>. Leave the client fields empty: ChatGPT
registers itself, and a deAPI sign-in window asks for your API key.</li>
<li>Confirm and create the app, then enable it in a chat.</li>
</ol>
<p class=note>If ChatGPT asks for client credentials, enter Client ID
{client_id_copy} and your API key as Client Secret.</p>
<p class=note>Developer mode depends on your ChatGPT plan and is set up on the web.</p>
</details>

<details>
<summary>Claude Code</summary>
{_code(claude_code)}
<p>Without <code>--header</code>, run <code>/mcp</code> in Claude Code and choose
<b>Authenticate</b>: the deAPI sign-in window opens in your browser.</p>
<p>Or share it with a project in <code>.mcp.json</code>:</p>
{_code(claude_code_json)}
</details>

<details>
<summary>Cursor</summary>
<p>In <code>~/.cursor/mcp.json</code> (or <code>.cursor/mcp.json</code> in a project):</p>
{_code(cursor_json)}
<p>Or leave out <code>headers</code>: Cursor then opens the deAPI sign-in window, where you
paste your key. To skip that window with OAuth, use the pre-registered client:</p>
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
<p>Use the endpoint {url_copy} with transport Streamable HTTP (sometimes called
just "HTTP") and send your key in one of these headers:</p>
<ul>
<li>{_copy("Authorization: Bearer <your key>")}</li>
<li>{_copy("X-API-Key: <your key>")}</li>
</ul>
<p>A client that uses OAuth needs no setup: it registers itself (dynamic client
registration or a client ID metadata document) and opens the deAPI sign-in window, where you
paste your API key. A client with fixed OAuth credentials can use Client ID
{client_id_copy} with your API key as Client Secret. Both use the Authorization
Code flow with PKCE (S256).</p>
</details>

<h2><small>Step 3</small>Check that it works</h2>
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

<h2 id=tools>Tools</h2>
{_tools_html(tools)}

<footer>
<a href="{HOME_URL}">{LOGO_SVG}</a>
<a href="{MCP_DOCS_URL}">MCP server docs</a><a href="{DOCS_URL}">deAPI docs</a><a
href="{KEYS_URL}">API keys</a><a href="{REPO_URL}">Source</a><a
href="{SECURITY_URL}">Security</a>
<span class=version>MCP Server {escape(__version__)}</span>
</footer>
</main>
<script>{SCRIPT}</script>
</body>
</html>
"""


# A host[:port] as DNS or an IP literal writes it. The page puts the URL into shell commands
# that people copy, so a Host header with anything else (";", "$", "&"...) is not used.
_PLAIN_NETLOC = re.compile(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?")
PLACEHOLDER_BASE_URL = "https://your-mcp-server.example.com"


def public_base_url(request: Request) -> str:
    """The URL clients reach this server on: PUBLIC_BASE_URL, else the request's own.

    Set PUBLIC_BASE_URL in production. Without it the request's Host is used, and only
    when it is a plain host[:port]; otherwise the page shows a placeholder.
    """
    configured = os.getenv("PUBLIC_BASE_URL")
    if configured:
        return configured
    scheme, netloc = request.url.scheme, request.url.netloc
    if scheme in ("http", "https") and _PLAIN_NETLOC.fullmatch(netloc):
        return f"{scheme}://{netloc}"
    return PLACEHOLDER_BASE_URL


def landing_response(base_url: str, tools: Sequence[Any]) -> Response:
    return HTMLResponse(render_landing(base_url, tools), headers=SECURITY_HEADERS)
