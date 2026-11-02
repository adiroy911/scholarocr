# ScholarOCR as a ChatGPT app (hosted MCP server)

`scholarocr-remote` is a second, separate MCP entry point that ChatGPT can reach over the public
internet. The local server (`scholarocr-mcp`, stdio or localhost HTTP) is unchanged and still the
right choice for Antigravity CLI, Claude Code and other local agents.

| | `scholarocr-mcp` (local) | `scholarocr-remote` (hosted) |
|---|---|---|
| Transport | stdio, or streamable HTTP on localhost | streamable HTTP at `https://<domain>/mcp` |
| How documents arrive | local file paths behind `SCHOLAROCR_ALLOWED_DIRS` | ChatGPT file parameters (`download_url`), or base64 fallback |
| Users | one (you) | many; each gets an isolated sandbox |
| Who reads the pages | the client's model (host mode, no key) or the configured key provider | ChatGPT's model (host mode, default) or the user's own optional key; the operator's key only in operator-key mode |
| Exports | files written on disk | short-lived signed download links |

Everything below was written on 2026-11-02 against the OpenAI documentation of that date. OpenAI
renames and moves these pages often. Section 7 lists which statements were checked against
official pages and which were not.

---

## 1. Architecture

```
ChatGPT ──HTTPS──▶ reverse proxy (TLS) ──▶ scholarocr-remote (uvicorn, Starlette)
                                            ├─ /mcp                     MCP streamable HTTP (bearer token required)
                                            ├─ /.well-known/oauth-*     OAuth metadata (RFC 8414 / RFC 9728)
                                            ├─ /register /authorize /token /revoke   OAuth 2.1 AS (MCP SDK handlers)
                                            ├─ /byok/connect            consent page: key optional (host mode)
                                            ├─ /download/{signed}       export downloads (HMAC-signed, 15 min)
                                            ├─ /.well-known/openai-apps-challenge   domain verification (optional)
                                            └─ /healthz
                                                    │ per request: access token → user id
                                                    ▼
                                ServicePool ── one ScholarOCR service per user (existing service layer)
                                   │             Settings: data_dir = users/<id>/data, allowed_dirs = [users/<id>/inbox],
                                   │             own key if linked, else host mode (operator key never by default)
                                   ▼
                        users/<id>/inbox/<random>/…   staged uploads, deleted right after ingestion
                        users/<id>/data/              that user's db, documents, crops, exports
                        auth.db                       encrypted keys, OAuth clients, hashed codes/tokens
```

Code lives in `src/scholarocr/remote/`:

| File | Role |
|---|---|
| `server.py` | MCP tools, the UI resource, HTTP routes, `create_app()`, `main()` |
| `oauth.py` | OAuth authorization-server provider for the MCP SDK, the `/byok/connect` consent page (key optional) and the one-time `/byok/key` link page |
| `store.py` | SQLite store: Fernet-encrypted keys, OAuth clients, pending requests, hashed codes and tokens |
| `services.py` | Per-user `ScholarOCR` instances and enforcement of bring-your-own-key |
| `sandbox.py` | Filename sanitising, magic-byte typing, size and quota limits, SSRF-guarded downloads, base64 |
| `ratelimit.py` | Per-user sliding windows: calls per minute, OCR calls per hour, pages per day |
| `retention.py` | Janitor that deletes old documents, stale staging files, idle users and expired OAuth rows |
| `widget.html` | The single self-contained HTML/JS UI resource |

The pipeline is not duplicated. Remote tools stage the bytes and then call the same
`ScholarOCR.process / get_content / get_page / get_source_region / verify / export / classify /
delete` methods the CLI and local server use.

### 1.1 How files reach the server

ChatGPT does not let a remote server see the user's disk. The documented mechanism is **file
params**. A tool lists its file inputs in `_meta["openai/fileParams"]`, and ChatGPT replaces them
with file objects of the form `{download_url, file_id, mime_type?, file_name?}`.
`ocr_upload` declares:

```json
"_meta": {"openai/fileParams": ["files"], ...}
"files": {"type": "array", "items": {"$ref": "#/$defs/OpenAIFile"}}
"OpenAIFile": {"properties": {"download_url": {"type": "string"}, "file_id": {"type": "string"},
               "mime_type": {"type": "string"}, "file_name": {"type": "string"}},
               "required": ["download_url", "file_id"]}
```

The reference page says arrays of file objects are allowed and that all four properties must be
declared, or the Scan Tools step rejects the schema.

On a call the server:

1. Rejects the call before fetching anything if the user has no key, has hit a rate limit, or sent more than
   `SCHOLAROCR_REMOTE_MAX_FILES` files.
2. Downloads each `download_url` over **HTTPS only**. Every resolved address must be public
   (loopback, private, link-local and metadata ranges are refused). Redirects are re-checked, at most 3
   are followed, and the size limit is enforced while streaming. An optional host allowlist can be set with
   `SCHOLAROCR_REMOTE_DOWNLOAD_HOSTS`.
3. Decides the type from **magic bytes**, never from the claimed name or MIME type. Accepted types are
   PDF, PNG, JPEG, WebP, TIFF, GIF and BMP. DOCX is opt-in with `SCHOLAROCR_REMOTE_ALLOW_DOCX=1`.
4. Writes the bytes to `users/<id>/inbox/<random>/NN_<sanitised name>`. The name is reduced to a safe basename,
   so `../../x` becomes `x`. Per-file, per-call and per-user quota limits apply.
5. Calls `ScholarOCR.process()`. Its `PathPolicy` only allows that user's inbox, and the service keeps its own
   read-only preserved copy. The staging batch is deleted in a `finally` block.

`ocr_upload_base64` is a fallback for MCP clients without file uploads, with a small cap
(`SCHOLAROCR_REMOTE_MAX_BASE64_MB`, default 3 MB). It is not declared as a file param. Set
`SCHOLAROCR_REMOTE_ALLOW_BASE64=0` to drop it from a public listing.

**No remote tool accepts a path.** `ocr_document(file_path=…)` and `export_document(output_dir=…)`
exist only in the local server. `tests/remote/test_tool_surface.py` fails if any remote tool schema
contains `file_path`, `path`, `paths`, `output_dir`, `folder` or `directory`.

### 1.2 Sign-in, host mode and the optional key

The OpenAI auth guide documents **OAuth 2.1 only** for authenticated apps: the authorization-code flow
with PKCE `S256`, with CIMD or DCR client registration. ChatGPT "cannot present custom API keys", and the
submission guidelines list API keys as restricted data that tools must not ask for. Tools may also be
anonymous (`noauth`), and ChatGPT sends `_meta["openai/subject"]`, "an anonymized user id ... for the
purposes of rate limiting and identification" (developers.openai.com/plugins/reference). The docs do not
say that id is stable across conversations or devices, and they point to authentication for data that
must survive sessions ("Authenticate the user so the MCP server can map each request to the correct
account", developers.openai.com/plugins/build/chatgpt-ui). ScholarOCR stores each user's documents for
up to 24 h and exports them later, so it keeps OAuth for identity.

ScholarOCR runs its own small OAuth authorization server (the MCP SDK's
`OAuthAuthorizationServerProvider`). Signing in creates an account; **a key is optional**:

```
ChatGPT → GET /authorize   (SDK validates client, redirect_uri, PKCE)  → 302 /byok/connect?req=…
User    → /byok/connect    default: "Continue without a key" (host mode)
                           optional: choose Gemini or OpenRouter and paste a key; ticks consent
Server  → account id = "u_" + 24 random hex (never derived from a key); key encrypted (Fernet, key
          derived from SCHOLAROCR_REMOTE_SECRET) if one was given
        → 302 back to ChatGPT's redirect_uri with ?code=…&state=…
ChatGPT → POST /token      (SDK verifies the PKCE verifier) → opaque access token (1 h) + refresh token (30 d, rotated)
ChatGPT → /mcp with Authorization: Bearer …  → token → account id → that user's sandbox (and key)
```

Per account, `services.user_settings()` decides how pages are read, after removing `GEMINI_API_KEY`,
`GOOGLE_API_KEY` and `OPENROUTER_API_KEY` from the operator environment:

1. the account has its own key → that provider, billed to that key;
2. `SCHOLAROCR_REMOTE_OPERATOR_KEY_MODE=1` → the operator's key (the consent page then also offers
   "Use this server's quota");
3. otherwise → **host mode**: no model client is built at all; pages that need vision wait for ChatGPT's
   own model (section 1.5).

* The key never appears in the conversation, in tool inputs, in tool outputs (`account_status`
  shows only the last 4 characters) or in logs.
* **Link or unlink later.** `manage_model_key(action="link")` returns a one-time URL
  (`/byok/key?t=…`, 15 minutes, stored as a SHA-256 hash) where the user pastes a key in the browser;
  the account id does not change, documents are kept. `manage_model_key(action="unlink")` deletes the key
  and the account falls back to host mode. Keys are never accepted as tool arguments.
* Reconnecting the app (a new OAuth grant) creates a new, empty account. Documents expire after 24 h
  anyway. Accounts created by 0.1, whose ids were derived from the key, keep working.
* The key is checked for format only (OpenRouter keys must start with `sk-or-`). A bad key shows up as an
  `auth_error` on the first OCR call.
* `delete_my_data` (or retention for idle users) deletes the account, key, tokens and the whole sandbox.
  Existing tokens then fail with 401 and ChatGPT has to sign in again.
* The operator's key is used only with **`SCHOLAROCR_REMOTE_OPERATOR_KEY_MODE=1`**. If that mode is on
  and the operator configured no key, tools return `operator_key_missing`.

Since the key is now optional, the review risk of collecting third-party model keys on the consent
page is smaller: a reviewer can use the app without any key. The optional key field is still a
restricted-data question to describe in the review notes (section 6).

### 1.3 Tools

| Tool | Annotations (read-only / destructive / open-world) | Model |
|---|---|---|
| `ocr_upload` (file params) | false / false / true | host mode or the account's key |
| `ocr_upload_base64` | false / false / true | same |
| `get_page_image` (renders the page-image widget) | true / false / false | host mode: hands one page to ChatGPT's model |
| `submit_page_content`, `submit_review` | false / false / false | host mode: stores ChatGPT's transcription / verdict |
| `get_document_status`, `get_document_content`, `get_page_content`, `get_source_region`, `list_documents` | true / false / false | none |
| `show_document_pages` (renders the viewer widget) | true / false / false | none |
| `verify_document` | false / false / true | key: `review=true` calls the review model; host: returns `review_candidates` |
| `export_document` (signed links) | false / false / false | none |
| `classify_document` | false / false / true | needs a key; `model_unavailable` in host mode |
| `manage_job` | false / false / true | none |
| `delete_document` | false / **true** / false | none |
| `account_status` | true / false / false | none |
| `manage_model_key` | false / false / false | none (returns a link; never takes a key) |
| `delete_my_data` | false / **true** / false | none |

Every tool sets all three hints explicitly, as the guidelines require. Every tool carries
`_meta.securitySchemes` (`oauth2`, scope `scholarocr`, or `noauth` with `SCHOLAROCR_REMOTE_AUTH=none`).
The reference calls this the `_meta` back-compat mirror. The MCP Python SDK's `Tool` model has no
top-level `securitySchemes` field, so only the mirror is emitted. Results return a concise
`structuredContent`, with the same JSON mirrored as text in `content`.

### 1.4 UI component

`ui://scholarocr/page-viewer-v1.html` (MIME `text/html;profile=mcp-app`) is a single HTML file with
inline CSS and JS and no external requests. Resource `_meta`:

```json
{"ui": {"prefersBorder": true, "domain": "<SCHOLAROCR_REMOTE_WIDGET_DOMAIN or public URL>",
        "csp": {"connectDomains": [], "resourceDomains": []}},
 "openai/widgetDescription": "…"}
```

Only `show_document_pages` points at it, through `_meta.ui.resourceUri` plus the ChatGPT alias
`openai/outputTemplate`. The widget:

* performs the MCP Apps handshake (`ui/initialize` → `ui/notifications/initialized`) and renders
  `ui/notifications/tool-result` → `structuredContent`;
* falls back to `window.openai.toolOutput` when it is present (ChatGPT);
* highlights elements by uncertainty level (medium amber, high orange, unresolved red dashed). It
  has a "flagged only" filter and Previous/Next buttons that call `show_document_pages` through
  `tools/call` or `window.openai.callTool`;
* renders all data with `textContent` (no `innerHTML`), follows the host theme or
  `prefers-color-scheme`, and reports its height with `ui/notifications/size-changed`.

To change the HTML in a way that could break cached copies, publish a new URI (`…-v2.html`) and
update the tool, as the UI guide asks.

### 1.5 Host mode in ChatGPT: getting the page image to the model

Host mode needs ChatGPT's model to look at a page image. What the official sources say (2026-11-03):

* The Plugins reference (developers.openai.com/plugins/reference, formerly `/apps-sdk/reference`) lists
  tool-result `content` and `structuredContent` as "surfaced to the model and the component" and `_meta`
  as "delivered only to the component". It never says image content blocks reach the model as vision.
* OpenAI forum reports say they do not, at least not reliably: thread 1375446 (an image-only result shows
  as `{}`; an "OpenAI_Support" reply says it "should not currently be treated as a guaranteed" path and
  recommends text or `structuredContent`), thread 1399928 (a ~3 KB PNG displayed, a 3 MiB PNG did not),
  thread 1360430.
* The documented way is the component: "Make images visible to the model" in
  developers.openai.com/plugins/build/chatgpt-ui. Widget state `imageIds` = "File IDs the model should
  receive on later turns", for files "uploaded with `window.openai.uploadFile`", selected, received as
  file params, or returned as tool-result file references. Caveat: openai/openai-apps-sdk-examples
  issue #170 ("Images uploaded return sediment://file_* IDs but model cannot access image content") is
  still open.
* The MCP Apps extension spec (github.com/openai/mcp-extensions, docs/spec.md) lets a component send
  image content with `ui/update-model-context`, "Supported" on Desktop and Android, "not yet supported"
  on Web.

So `get_page_image` returns:

* `content`: an image block (for other MCP clients, and in case ChatGPT shows it) plus the rules and
  page info as text;
* `structuredContent`: the page info only (no base64, so the model's context stays small);
* `_meta["scholarocr/pageImage"]`: the PNG/JPEG as base64, for the component only;
* `openai/outputTemplate` = `ui://scholarocr/page-image-v1.html`.

The page-image component (`remote/page_image.html`, inline JS, no network access) shows the page,
uploads it with `window.openai.uploadFile`, stores the file id in widget state `imageIds`, and sends one
follow-up message (`window.openai.sendFollowUpMessage`, "Here is page N … call submit_page_content") so
the model gets the image on that turn. On MCP Apps hosts it also sends `ui/update-model-context` with
an image block. A "Send this page to the assistant" button repeats the follow-up. The tool text tells the
model to say so instead of guessing when it cannot see an image.

This path is built from documentation and has **not been tried in ChatGPT**. Check in developer mode:
the image appears in the widget (CSP may block `data:` images), `uploadFile` succeeds, and the model can
read the page on the follow-up turn. If any step fails, the fallback is a user key (`manage_model_key`).

For page photos the user attached to the chat, ChatGPT's model usually sees the attachment itself;
ScholarOCR still prepares, validates and stores the transcription.

The consumer Gemini app's custom connectors (support.google.com/gemini/answer/17209137; US, English,
personal accounts) support MCP up to 2025-11-25 over streamable HTTP, but Google's guide says
`structuredContent` is not supported and data should go in `TextContent`
(developers.google.com/connected-apps/mcp/server-optimization). Nothing says image results reach the
model, so host mode there is untested and not claimed.

---

## 2. Configuration

Remote mode reads `SCHOLAROCR_REMOTE_*` variables plus the usual model selection
(`GEMINI_MODEL`, `OPENROUTER_MODEL`, …). The loader reads `.env` from the **current directory**
only and does not strip inline `# comments`, so put comments on their own lines.

| Variable | Default | Meaning |
|---|---|---|
| `SCHOLAROCR_REMOTE_PUBLIC_URL` | `http://127.0.0.1:8000` | Public origin ChatGPT uses (OAuth issuer, `/mcp` resource, link base). Must be `https://` for ChatGPT. |
| `SCHOLAROCR_REMOTE_SECRET` | random per start (warning) | ≥32 characters. Encrypts stored keys and signs download links. **Required in production**: a new value makes stored keys unreadable. |
| `SCHOLAROCR_REMOTE_HOST` / `_PORT` | `127.0.0.1` / `8000` | Bind address. Use `0.0.0.0` in a container behind a proxy. |
| `SCHOLAROCR_REMOTE_AUTH` | `oauth` | `none` = one shared sandbox for every caller. **Local testing only.** |
| `SCHOLAROCR_REMOTE_OPERATOR_KEY_MODE` | `0` | `1` = users without their own key run on the operator's key. `0`: they run in host mode. |
| `SCHOLAROCR_REMOTE_DATA_DIR` | `%LOCALAPPDATA%\ScholarOCR\remote` | Root for `auth.db` and `users/`. |
| `SCHOLAROCR_REMOTE_MAX_FILE_MB` | `20` | Per file |
| `SCHOLAROCR_REMOTE_MAX_FILES` | `10` | Files per call |
| `SCHOLAROCR_REMOTE_MAX_CALL_MB` | `50` | Total per call |
| `SCHOLAROCR_REMOTE_MAX_BASE64_MB` | `3` | Per base64 file (also raises the HTTP body limit to fit) |
| `SCHOLAROCR_REMOTE_ALLOW_BASE64` | `1` | Expose `ocr_upload_base64` |
| `SCHOLAROCR_REMOTE_ALLOW_DOCX` | `0` | Accept DOCX uploads |
| `SCHOLAROCR_REMOTE_MAX_PAGES` | `100` | Pages per document |
| `SCHOLAROCR_REMOTE_USER_QUOTA_MB` | `500` | Disk per user |
| `SCHOLAROCR_REMOTE_DOWNLOAD_HOSTS` | (any public host) | Comma-separated allowlist for `download_url` hosts |
| `SCHOLAROCR_REMOTE_CALLS_PER_MINUTE` | `30` | Per user, all tools (0 disables) |
| `SCHOLAROCR_REMOTE_OCR_CALLS_PER_HOUR` | `30` | Per user |
| `SCHOLAROCR_REMOTE_PAGES_PER_DAY` | `300` | Per user, rolling 24 h |
| `SCHOLAROCR_REMOTE_RETENTION_HOURS` | `24` | Documents and all derived data |
| `SCHOLAROCR_REMOTE_KEY_RETENTION_DAYS` | `30` | Idle users: key, tokens and sandbox removed |
| `SCHOLAROCR_REMOTE_SWEEP_INTERVAL_S` | `900` | Janitor interval (it also runs once at start-up) |
| `SCHOLAROCR_REMOTE_DOWNLOAD_LINK_TTL_S` | `900` | Export link lifetime |
| `SCHOLAROCR_REMOTE_WIDGET_DOMAIN` | public URL | `_meta.ui.domain`. Must be unique per plugin at submission. |
| `SCHOLAROCR_REMOTE_DOMAIN_CHALLENGE` | unset | Token served at `/.well-known/openai-apps-challenge` |
| `SCHOLAROCR_REMOTE_ALLOWED_ORIGINS` | – | Extra `Origin` values for the DNS-rebinding check (chatgpt.com and chat.openai.com are included by default; requests without `Origin` always pass) |
| `SCHOLAROCR_REMOTE_STATELESS` / `_JSON_RESPONSE` | `0` / `0` | Streamable-HTTP modes |

The per-user service ignores the operator's `SCHOLAROCR_ALLOWED_DIRS`, `SCHOLAROCR_DATA_DIR`,
`SCHOLAROCR_RETENTION_DAYS`, `SCHOLAROCR_MAX_FILE_MB` and `SCHOLAROCR_MAX_PAGES`.

Rate-limit counters and MCP sessions live in process memory. Run **one replica**, or add a shared
store and sticky sessions (or `SCHOLAROCR_REMOTE_STATELESS=1`) before scaling out.

---

## 3. Local testing

Run everything from the project folder, because `.env` is read from the current directory.

```powershell
cd path\to\scholarocr
uv pip install --python .venv\Scripts\python.exe -e ".[remote]"   # cryptography/uvicorn are already present
.venv\Scripts\python.exe -m pytest tests\remote -q                  # 49 tests, no network, model mocked
```

Start the server on localhost:

```powershell
$env:SCHOLAROCR_REMOTE_SECRET = "<48 random characters>"
$env:SCHOLAROCR_REMOTE_DATA_DIR = "$env:TEMP\scholarocr-remote"
.venv\Scripts\scholarocr-remote.exe --port 8000 --public-url http://127.0.0.1:8000
# probe
curl.exe http://127.0.0.1:8000/healthz
curl.exe http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp
```

**MCP Inspector:** connect to `http://127.0.0.1:8000/mcp` with transport "Streamable HTTP". An
Inspector build with OAuth support should discover the metadata, register itself, and open
`/byok/connect` in your browser. This was not tried here. For a quick look without
OAuth, use `--auth none` together with `SCHOLAROCR_REMOTE_OPERATOR_KEY_MODE=1`, or call only
read-only tools.

**ChatGPT with a tunnel:**

1. Start the server with `--public-url https://<your-tunnel-host>`. The public URL must equal the
   host ChatGPT calls, because the OAuth issuer, the resource and the DNS-rebinding allowlist are
   all derived from it.
2. Open a tunnel to `127.0.0.1:8000`. The OpenAI "Connect from ChatGPT" page mentions OpenAI's
   **Secure MCP Tunnel** and generic HTTPS forwarding services. Any HTTPS tunnel that preserves the
   `Host` header works. Tunnels are for testing only: submission needs a stable public HTTPS
   endpoint.
3. In ChatGPT, enable developer mode, then go to **Plugins → + → Add custom MCP server**. Enter a
   name and the URL `https://<tunnel-host>/mcp` (or choose *Tunnel* and select it), choose OAuth,
   accept the risk notice and create it. ChatGPT registers itself through `/register` (DCR), sends
   you to `/byok/connect`, and receives a token.
4. Attach a PDF or a page photo and ask: "OCR this with ScholarOCR and show me the uncertain
   parts." Expected calls: `ocr_upload`, then `show_document_pages`.
5. After changing tools or metadata: restart the server, open the connection in ChatGPT Plugins,
   select **Refresh**, and start a new conversation.

This build was **not** tested against a live ChatGPT session. That would require a public tunnel
and an account, both out of scope here. Everything runs in-process in `tests/remote/`, including a
full OAuth + PKCE + DCR flow and MCP over streamable HTTP through an ASGI transport.

---

## 4. Deployment

Any host that can run a long-lived Python process with a persistent disk and HTTPS will do: a VM, Fly.io,
Render, Railway, Cloud Run with a mounted volume (min instances 1), and so on.

```bash
docker build -f Dockerfile.remote -t scholarocr-remote .
docker run -d --name scholarocr -p 127.0.0.1:8000:8000 -v scholarocr-data:/data \
  -e SCHOLAROCR_REMOTE_PUBLIC_URL=https://ocr.example.com \
  -e SCHOLAROCR_REMOTE_SECRET="$(openssl rand -base64 48)" \
  -e FORWARDED_ALLOW_IPS=172.17.0.1 \
  scholarocr-remote
```

Put a TLS-terminating reverse proxy (Caddy, nginx, a cloud load balancer) in front of it, and
forward the original `Host` header. `.dockerignore` keeps `.env`, `.venv`, tests and docs out of
the image. No model key belongs in the container.

Production checklist:

* Keep `SCHOLAROCR_REMOTE_SECRET` stable and stored in a secret manager. Back up `/data/remote/auth.db`
  together with it, or accept that users must re-link after a loss.
* Restrict egress to the public internet (no access to cloud metadata or the internal network). This
  covers the DNS-rebinding window left by the download guard.
* Set `SCHOLAROCR_REMOTE_DOWNLOAD_HOSTS` once you know the host ChatGPT uses for `download_url`
  (OpenAI does not document it; see section 7).
* Request bodies are capped by the MCP SDK at 4 MB, raised automatically when the base64 cap is
  larger. Configure the same cap or a larger one at the proxy.
* Logs never contain document text or keys. uvicorn access logs do contain `/download/<token>`
  URLs, so treat access logs as sensitive or turn them off.
* Run one replica (see section 2).

### 4.1 Deploy to Fly.io

`fly.toml` in the project root is ready for this. It builds `Dockerfile.remote` on Fly's remote
builder, so Docker is not needed on Windows. It runs **one** always-on Machine in `bom` (Mumbai)
with a 1 GB volume at `/data`. The server keeps its data in `/data/remote`. The file holds no
secrets. Its comments explain the auto-stop and memory choices. Checked against docs.fly.io on
2026-11-03.

**App name.** `fly.toml` uses `scholarocr`, so the public URL is `https://scholarocr.fly.dev`. Fly
app names are global. If `fly apps create` reports the name as taken, pick something like
`scholarocr-som`, then change `app = ...` **and both** `https://scholarocr.fly.dev` values in
`[env]` (`SCHOLAROCR_REMOTE_PUBLIC_URL`, `SCHOLAROCR_REMOTE_WIDGET_DOMAIN`). The public URL must
exactly match the host ChatGPT calls. Use the new name wherever `scholarocr` appears below.

Run these in PowerShell from the project folder:

```powershell
cd path\to\scholarocr

# 1. Install flyctl. Follow the official page (Windows section): https://fly.io/docs/flyctl/install/
#    Then open a new terminal and check:
fly version

# 2. Log in (opens the browser). New organisations need a payment method; there is no free tier.
fly auth login

# 3. Create the app (name must match fly.toml).
fly apps create scholarocr

# 4. Create the volume in the same region. Fly warns that a single volume has no redundancy;
#    answer y (one replica is required anyway; see "Backups" below).
fly volumes create scholarocr_data --app scholarocr --region bom --size 1

# 5. Set the fixed secret. The value is generated in memory, sent to Fly and never written to disk
#    or shown; your shell history keeps only the expression. Fly secrets cannot be read back. Never
#    run this again unless you mean to rotate it, because a new value makes every stored key and
#    token unreadable and all users must re-link.
$b = New-Object byte[] 48; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); fly secrets set --app scholarocr --stage "SCHOLAROCR_REMOTE_SECRET=$([Convert]::ToBase64String($b))"; Remove-Variable b

# 6. Deploy using Fly's remote builder. --ha=false stops Fly from adding a spare second Machine.
fly deploy --remote-only --ha=false

# 7. Confirm exactly one Machine, started, in bom, with passing checks.
fly status --app scholarocr
fly machine list --app scholarocr
fly logs --app scholarocr --no-tail
```

`--stage` stores the secret without restarting anything (no Machine exists yet), and the first
deploy picks it up. If `fly status` ever shows two Machines, run
`fly scale count 1 --app scholarocr`.

**Post-deploy checks.** Run the script from `.venv`. It is read-only and prints PASS/FAIL per check:

```powershell
.venv\Scripts\python.exe scripts\check_remote_deploy.py https://scholarocr.fly.dev
```

It checks the following, which you can also run by hand:

```powershell
# /healthz -> 200 {"status":"ok",...}
curl.exe -i https://scholarocr.fly.dev/healthz
# protected-resource metadata: "resource" = https://scholarocr.fly.dev/mcp, authorization_servers = [https://scholarocr.fly.dev]
curl.exe -s https://scholarocr.fly.dev/.well-known/oauth-protected-resource/mcp
curl.exe -s https://scholarocr.fly.dev/.well-known/oauth-protected-resource
# authorization-server metadata: "issuer" and every *_endpoint on https://scholarocr.fly.dev (never localhost)
curl.exe -s https://scholarocr.fly.dev/.well-known/oauth-authorization-server
# /mcp without a token -> 401 and WWW-Authenticate: Bearer ... resource_metadata="https://scholarocr.fly.dev/..."
curl.exe -i -X POST https://scholarocr.fly.dev/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d "{}"
# plain http is redirected to https (force_https)
curl.exe -I http://scholarocr.fly.dev/healthz
```

If any metadata shows `http://127.0.0.1:8000`, `SCHOLAROCR_REMOTE_PUBLIC_URL` did not reach the
Machine. Check `[env]` in `fly.toml` and redeploy. Then connect ChatGPT to
`https://scholarocr.fly.dev/mcp` as in section 3, steps 3-5.

**Values to add later.** These are not secret, but they are not known yet:

```powershell
# Domain verification token from the OpenAI Platform plugin portal (section 6):
fly secrets set --app scholarocr SCHOLAROCR_REMOTE_DOMAIN_CHALLENGE=<token>
# Once you know the host ChatGPT uses for download_url (empty = any public https host):
fly secrets set --app scholarocr SCHOLAROCR_REMOTE_DOWNLOAD_HOSTS=<host1>,<host2>
```

`fly secrets set` restarts the Machine. You can put them under `[env]` in `fly.toml` instead and
redeploy. Leave `SCHOLAROCR_REMOTE_OPERATOR_KEY_MODE` at `0`.

**Why it never auto-stops.** MCP sessions and the rate-limit windows are in memory, and long OCR
jobs run in the background while ChatGPT polls. If Fly stopped an idle Machine, those sessions
would go stale, the daily page budgets would reset, and a job could die partway through. OAuth
tokens and the encrypted keys are in `auth.db` on the volume, so they survive restarts and
deploys. Each deploy replaces the single Machine, which means a few seconds of downtime and fresh
MCP sessions.

**Memory.** 1 GB on `shared-cpu-1x`, plus 512 MB of swap as a safety net. These measurements of
the real render/preprocess path were taken locally: about 165 MB idle, about 225 MB peak for an
A4 page at 200 dpi, and about 350 MB for a 12 MP photo. Each job keeps 2 pages in flight
(`SCHOLAROCR_CONCURRENCY`). If `fly logs` shows OOM kills or responses are slow under load, run
`fly scale vm shared-cpu-2x --memory 2048 --app scholarocr`.

**Cost** (docs.fly.io pricing page on 2026-11-03; Fly says Machine prices vary by region, so the
Mumbai price may differ slightly):

| Item | Price |
|---|---|
| `shared-cpu-1x`, 1 GB, running all month | about $6.70/month |
| Volume, 1 GB provisioned | $0.15/month (billed even while stopped) |
| Volume snapshots | first 10 GB/month free |
| Shared IPv4 + IPv6, TLS cert for `*.fly.dev` | free |
| Outbound data from India | $0.12/GB |
| Upgrade: `shared-cpu-2x`, 2 GB | about $13.39/month |

Expect roughly **$7 a month** plus egress. Downloads of exports and page images are the main
egress. Run `fly ips list --app scholarocr` to check that no dedicated IPv4 ($2/month) was
allocated.

**Backups.** Fly keeps daily volume snapshots for 5 days by default (`fly volumes snapshots list
<volume-id>`). They may miss the latest data. `auth.db` is useless without the secret, and Fly
secrets cannot be read back. If you need to move hosts without users re-linking, generate the
secret into your password manager first and set it from there.

**Stop, restart, destroy:**

```powershell
fly scale count 0 --app scholarocr     # stop compute billing; the volume and data stay (and keep billing $0.15/GB)
fly scale count 1 --app scholarocr     # start again
fly apps destroy scholarocr            # delete the app, its Machines, volume, snapshots and secrets. Irreversible.
```

**Troubleshooting.** If you see `PermissionError` on `/data/remote` in `fly logs`, the volume is not
owned by the image user (uid 10001). Fix it once with `fly ssh console --app scholarocr -C "chown -R
app:app /data"`, then run `fly apps restart scholarocr`. If health checks fail during the first boot,
read `fly logs`. The check allows 20 s for start-up.

---

## 5. What `tests/remote/` covers

| File | Covers |
|---|---|
| `test_sandbox.py` | filename traversal and odd names, user-id validation, staging confined to the inbox, magic-byte typing (an executable named `.pdf` is rejected), size and quota limits, base64 limits, SSRF blocks (http, file, loopback, RFC 1918, link-local metadata, IPv6 loopback), host allowlist |
| `test_tool_surface.py` | exact remote tool set, no path-like parameters, local server still has `ocr_document`, `openai/fileParams` schema shape, explicit annotations and `securitySchemes`, base64 tool toggle, widget resource MIME, CSP and domain metadata, render-tool-only template binding, `noauth` mode |
| `test_byok.py` | operator keys stripped, no key = host mode (no model client is ever built, the operator key is never used, even for an operator-flagged account when the mode is off), the host loop over MCP (ocr_upload → get_page_image with `_meta` image → submit_page_content → content), user key used, link/unlink keeps the account and documents, operator mode is opt-in, keys encrypted at rest |
| `test_limits_retention.py` | sliding windows, size, file-count and base64 limits over MCP, per-user call limit, daily page budget, retention deletes old documents, stale staging and idle users, `delete_my_data` |
| `test_roundtrip.py` | in-process MCP: `list_tools` → `ocr_upload` → status → content → page → source region → `show_document_pages` → verify → export link → classify → account status; no server paths or keys leak; per-user isolation; base64 fallback; unexpected errors return a generic `internal_error` |
| `test_http_oauth.py` | over HTTP (ASGI): 401 + `WWW-Authenticate`, both protected-resource metadata paths, AS metadata, DCR, `/authorize` → consent page → code (with a key, and without one: host mode), PKCE failure, token, account id independent of the key, MCP over streamable HTTP with the bearer token, the one-time `/byok/key` page (bad key, save, reuse refused), signed download and tampered links, refresh rotation, revocation through `delete_my_data`, Host-header protection |

---

## 6. Submission checklist (manual steps, in order)

Per the OpenAI submission guide, apps are now **submitted and published as plugins**, through the
Plugins page of the OpenAI Platform (`platform.openai.com/plugins`), with **Upload new or existing
plugin**. This settles the conflicting mid-2026 blog posts: the official pages call these
plugins. The page still uses "app" in technical terms such as `apps` / `.app.json` and "MCP app
review".

**Organisation and identity**

- [ ] Choose the OpenAI Platform organisation and project that will own the plugin. A search snippet
      (not seen on the official page itself) says projects with **EU data residency cannot submit**
      plugins that contain apps, so check this before choosing.
- [ ] Complete **individual or business verification** in Platform → Settings → Organization → General.
      The guidelines say all submissions must come from verified individuals or organisations.
- [ ] Make sure the submitter is an organisation owner or has the **Apps Management Write** role.

**Hosting and domain**

- [ ] Deploy to a stable public HTTPS endpoint (no tunnel, no localhost), with
      `SCHOLAROCR_REMOTE_PUBLIC_URL` set to that origin.
- [ ] Choose `SCHOLAROCR_REMOTE_WIDGET_DOMAIN`. The reference calls `_meta.ui.domain` "required when
      submitting a plugin with UI; must be unique per plugin".
- [ ] Domain verification: put the challenge token from the portal in
      `SCHOLAROCR_REMOTE_DOMAIN_CHALLENGE`. The server returns it as plain text at
      `https://<host>/.well-known/openai-apps-challenge`.
- [ ] In the portal, open **MCPs → Connect**, authorise (complete `/byok/connect`; "Continue without a key" is enough), wait for
      **Scan Tools**, fix any issues, then use **Rescan**.
- [ ] Decide whether to keep `ocr_upload_base64` public (`SCHOLAROCR_REMOTE_ALLOW_BASE64=0` hides it).

**Policies and listing**

- [ ] Publish a **privacy policy** covering what is collected (uploaded documents, extracted content,
      the encrypted model key if the user adds one, an IP address in access logs), why, recipients (Google Gemini or OpenRouter
      plus the model host the user picks; your hosting provider), retention (`RETENTION_HOURS`,
      `KEY_RETENTION_DAYS`), and user controls (`delete_my_data`, disconnecting).
- [ ] Publish **terms of service**, a **website** and a **support** URL, all HTTPS. Keep support
      contact details current.
- [ ] Listing fields: `displayName` (≤30 characters), `shortDescription` (≤30), `longDescription`
      (≤4,000), `developerName`, `category`, primary icon (`logo`). Optional: up to 3 `defaultPrompt`
      starter prompts. Screenshots are optional and, per the guidelines, "no longer shown in the
      Directory".
- [ ] Prepare the review materials: `review.demo_recording_url` (a screen recording of the flow),
      **5 positive and 3 negative test cases**, and **reviewer credentials** entered in the dashboard's
      *Review details* form, not in the ZIP. For this app no credentials are needed: the consent page
      has no sign-up, and "Continue without a key" runs host mode. Explain in the review notes that the
      optional key field exists, that tools never ask for it, and why (see 1.2). A login that needs
      sign-up or 2FA is rejected.
- [ ] Build the plugin ZIP with the package identity and listing metadata, following the "Submit
      plugins" guide. This repository does not generate it.

**Submit**

- [ ] Upload the ZIP, confirm the review information, choose **Submit for review**, and complete the
      policy attestations. Only one review can be active at a time. Feedback arrives by email; to
      appeal, reply to the rejection email.
- [ ] After approval, choose **Publish plugin**. Later MCP tool changes are re-scanned daily.
      Metadata changes need a new ZIP and a new review.

---

## 7. What was verified in official docs

Checked on 2026-11-02 by reading these pages:

* developers.openai.com/apps-sdk/build/mcp-server
* …/build/chatgpt-ui
* …/build/auth
* …/reference
* …/app-submission-guidelines
* …/deploy/submission
* …/deploy/connect-chatgpt
* the MCP Apps specification (modelcontextprotocol/ext-apps, `2026-01-26`)

**Verified**

* Streamable HTTP endpoint, typically `/mcp`. `structuredContent`, `content` and `_meta` semantics:
  `_meta` goes only to the component and is hidden from the model.
* `_meta["openai/fileParams"]` lists top-level file inputs; arrays are allowed. File objects have
  `download_url` and `file_id` (always sent) and `mime_type` and `file_name` (optional, but they must
  be declared).
* UI: `ui://` URI, MIME `text/html;profile=mcp-app`, tool `_meta.ui.resourceUri` with
  `openai/outputTemplate` as the ChatGPT alias, resource `_meta.ui.csp` (`connectDomains`,
  `resourceDomains`, `frameDomains`), `ui.domain`, `ui.prefersBorder`. Bridge methods
  `ui/initialize`, `ui/notifications/tool-input`, `ui/notifications/tool-result`, `tools/call`,
  `ui/notifications/size-changed`. `window.openai` aliases (`toolOutput`, `callTool`, …).
* Auth: OAuth 2.1 authorization code + PKCE `S256`. CIMD preferred, DCR still supported.
  Protected-resource metadata at `/.well-known/oauth-protected-resource` or through
  `WWW-Authenticate`. `securitySchemes` types `noauth` and `oauth2`.
  `_meta["mcp/www_authenticate"]` triggers the OAuth UI. ChatGPT cannot present custom API keys.
  Submission rejects API-key and bearer-token flows and non-empty server headers.
* Annotations: all three hints must be explicit booleans. API keys are restricted data. Verified
  individual or organisation required. Privacy policy, support, test credentials, example prompts.
  Screenshots no longer shown.
* Submission as **plugins** on the Platform Plugins page: Apps Management Write role, domain
  verification path `/.well-known/openai-apps-challenge`, listing field limits, demo recording,
  5 positive and 3 negative test cases, Scan Tools / Rescan, Publish plugin.
* ChatGPT: Plugins → + → Add custom MCP server, *Tunnel* option, Secure MCP Tunnel, Refresh.

**Not verified** (absent from the pages read, or seen only in search snippets or third-party posts)

* **Which ChatGPT plans have developer mode**, and the exact toggle path. Help-center search snippets
  mention Business and Enterprise/Edu and Settings → Apps → Advanced settings. The help-center page
  itself returned HTTP 403 to my fetch. A third-party guide claims Plus and Pro access.
* The **host, lifetime and size limits of `download_url`**. The docs say only "temporary".
  `SCHOLAROCR_REMOTE_DOWNLOAD_HOSTS` is therefore empty by default.
* Which **file types** ChatGPT passes through file params (images only, or PDFs too).
* Whether reviewers accept the **optional** third-party model key field on the consent page (the app is
  fully usable without it).
* Whether ChatGPT's model can read the page image in host mode through `uploadFile` + `imageIds` and the
  follow-up message (section 1.5).
* ChatGPT's **redirect URI**, whether it sends the RFC 8707 `resource` parameter, and which DCR
  `token_endpoint_auth_method` it registers with. The server handles both "resource present" and
  "resource absent", and the tests use a public client (`none`).
* **Tool-call timeouts** in ChatGPT. The server answers synchronously only for small page selections
  (`SCHOLAROCR_SYNC_PAGE_LIMIT`, default 4); larger jobs return a `job_id` to poll.
* That ChatGPT sends an **`Origin` header of `https://chatgpt.com`** to `/mcp`, if it sends one at all.
  Configurable through `SCHOLAROCR_REMOTE_ALLOWED_ORIGINS`.
* The widget's ChatGPT-specific `openai:set_globals` event and the return shape of
  `window.openai.callTool`. The widget feature-detects both and falls back to the standard bridge.
* How to return **file references** in tool results for ChatGPT to download (mentioned, but the shape
  was not checked). Exports use signed HTTPS links instead.
* The EU data-residency restriction (search snippet of the submission page only).
* CIMD is not implemented; the MCP Python SDK provides DCR, which the docs still support.

---

## 8. Known limitations

* No live ChatGPT test has been run. Expect at least one round of fixes after the first real connection.
* A bad user key (optional) is detected only on the first model call, not at link time.
* In-memory rate limits and MCP sessions mean a single replica.
* The DNS-rebinding window in the SSRF guard has to be closed with egress rules.
* Host mode quality depends on ChatGPT's model reading one page image per turn; there is no independent
  second model unless the user links a key.
* Reconnecting the app creates a new account (ids are random); there is no account recovery.
