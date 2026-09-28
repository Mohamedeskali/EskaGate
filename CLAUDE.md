# EskaGate — API key test console + local AI gateway

Local web app (Python standard library only, no pip installs) that:
1. **Tests API keys**: finds a key's models (`/models`, `/v1/models`), sends a test prompt to each, and streams results live to the page.
2. **Monitors saved keys**: re-checks them on an interval and flags added/removed models.
3. **Runs a local AI gateway**: one local key (`sk-local-...`) sits in front of many real provider keys, with failover and OpenAI <-> Anthropic format translation.
4. **Switches coding agents** (Claude Code, opencode, pi, Hermes, custom) onto the gateway and restores their original config afterwards.

The user writes in Moroccan Darija or Arabic. Reply in the language they use. The UI is translated into Arabic/Darija (default, RTL), English and French (LTR); all text lives in `i18n/{ar,en,fr}.json`. Every new UI string goes into all three files (Darija for `ar`), never hard-coded (see **Translations** below).

## Files

| File | What it is |
|---|---|
| `api_web_dashboard_v2.py` | HTTP server (`ThreadingHTTPServer`), tester logic, and the whole page (HTML/CSS/JS) in one raw string `INDEX_HTML = r"""..."""`. ~4100 lines. |
| `gateway.py` | Gateway: provider/key store, `dispatch()` with failover and cooldowns, request/response/stream translation, logs. |
| `i18n.py`, `i18n/` | Translations: `ar.json`, `en.json`, `fr.json` (flat, same keys, `{name}` placeholders), `t()` helper, saved language, and a CLI (`python3 i18n.py <key> [name=value] [lang=xx]`) used by the shell scripts. |
| `tests/test_i18n.py` | `python3 -m unittest discover tests`: same keys in all files, same placeholders, every used key exists, no unused keys, each language renders with the right `dir`, no Arabic left in the en/fr page, `node --check` on the scripts. |
| `agents.py` | Agent config switching (enable/disable with backup and restore), custom agents, per-agent model filter, icons. |
| `alerts.py` | Telegram alerts. Sets `gateway.ON_EVENT` (gateway never imports it); events `key_failed`, `provider_down`, `key_ok`, `quota_low`, `request` (start/end, for the idle check), `request_logged` (from `add_log`, for the daily summary counters). Dedup per (provider or `agent:<name>`, cause) for `DEDUP` = 300 s. Threads: sender (logs every alert to the history), ticker every 10 s (`check_idle`, `check_summary`, stats save), command poller (`getUpdates` long polling; `handle_command` for /status, /switch; only the saved chat). While the poller runs, "🔎 جيبو" (`tg.find`) uses the chat it saw, since Telegram allows one update reader. `ESKAGATE_TELEGRAM_API` overrides the API base for tests. |
| `phone.py` | Phone access: a second `ThreadingHTTPServer` on the private LAN IP (same port, `server.lan = True`), started/stopped from the page. Token in memory, rotated on stop; off after every restart. |
| `qr.py` | Stdlib QR encoder (byte mode, level M, versions 1-10) returning SVG. Verified with a zxing decoder over all masks and versions. |
| `eskali_api_launcher.sh` | `start` (background + opens browser) / `stop`. PID in `~/.api-test-console/eskali_api.pid`, log in `eskali_api.log`. Port from `ESKALI_API_PORT` (default 8000). |
| `install_eskali_api.sh` | Installs the apps-menu `.desktop` entry, icon and desktop shortcut (generated, not stored in the repo). `--uninstall` removes them. |
| `run-api-dashboard.sh`, `Run API Dashboard.bat`, `create_shortcut.vbs` | Foreground run on Linux / Windows. |
| `README.md` | User-facing overview and run instructions (Linux / Windows). |
| `get.sh` | One-line Ubuntu install/update (`curl … | bash`): checks python3 ≥3.8 and git (prints the apt command, never sudo), clones or `git pull --ff-only` into `~/EskaGate` (`ESKAGATE_DIR`, `ESKAGATE_REPO` override for tests), runs the installer, restarts a copy running from that folder after an update, starts the app. `--uninstall` (piped: `bash -s -- --uninstall`) removes the folder and the menu entry only when it points there; data is kept. |
| `get.ps1` | One-line Windows install/update (`irm … | iex`): finds Python 3.8+ (winget installs 3.12 if missing), mirrors the GitHub ZIP into `%LOCALAPPDATA%\EskaGate`, writes `EskaGate.cmd` with the exact python path, Start Menu + Desktop shortcuts. `-Uninstall` via `& ([scriptblock]::Create((irm …))) -Uninstall`; the switch is on the inner `& { param() } @args` block because a top-level `param()` under `iex` binds into the caller's scope. ASCII-only, no `exit`, 5.1-compatible syntax. Not yet run on real Windows. |
| `LICENSE` | MIT, copyright holder from `git config user.name`. |
| `docs/screenshots/` | README screenshots, taken from a demo copy with fake data (own `HOME`/`API_CONSOLE_HOME`/port). Never shoot the real instance. |
| `assets/` | `eskali_api_icon.png` (Linux app + notifications), `api_dashboard_icon.ico` (Windows shortcut). The page favicon is inline in `INDEX_HTML`. |

History is in git. Don't make backup copies of files; commit instead.

## Data (never commit or print it)

Everything lives in `~/.api-test-console/` (override with `API_CONSOLE_HOME`): the directory is 0700 and the files are 0600, all written through `gateway.write_private()`.

| File | Contents |
|---|---|
| `gateway.json` | Providers, real keys, and the local key |
| `gateway-logs.jsonl` | Gateway log: model, provider, masked key, status, timing. Never message content. |
| `ui-profiles.json` | Saved keys ("مفاتيحي"): name, base_url, api_key, source, lastCheck |
| `ui-formats.json` | Custom copy formats |
| `ui-settings.json` | UI language (`{"lang": "ar"|"en"|"fr"}`); also read by the launcher and installer |
| `agents.json` | Agent enable state and original values |
| `agents-custom.json` | Custom agent specs and icons |
| `agent-models.json` | Per-agent model filter |
| `agent-backups/` | Copies of agent configs taken before Enable |
| `telegram.json` | Telegram bot token, chat ID, enabled, idle minutes, summary on/off and time (token/chat shown masked in the page) |
| `alert-stats.json` | Per-agent counters since the last daily summary, last summary date, quota warnings sent per key per day |
| `alert-history.jsonl` | Every alert sent: time, provider, agent, type, message, sent (capped around 5000 lines) |

Tabs save profiles/formats as per-entry ops (`POST /api/store/<name>` with `{ops}`: create / set+unset / delete, keyed by `name`), applied under a lock by `ui_store_apply()`, so a stale tab can't revive a deleted key or drop a new one. Whole-list `{value}` writes (pages from before this) get 409. localStorage is only a read fallback when the server is unreachable; it is never merged back.

## Server and API

- Phone access (LAN listener): every request, including `/v1/*`, first passes `Handler._lan_gate()`. `?token=` on a GET sets the `eg_phone` cookie (HttpOnly, SameSite=Lax) and redirects without it. No valid token gives 401, and a non-private client IP gives 403. On that listener `_admin_ok()` accepts only the LAN IP as Host/Origin. Routes: `GET /api/phone`, `POST /api/phone/{start,stop}`; Telegram: `GET /api/telegram`, `POST /api/telegram/{save,test,chat-id}`, `GET /api/alerts/history`.
- Quota: `gateway.parse_quota()` reads rate-limit headers (success and error responses) into `key["quota"] = {at, buckets: {requests|tokens|requests_day|tokens_day: {limit, remaining, reset}}}`. It is shown per key in the Providers tab (`quotaHtml()`), and "ما مصرحش" (`quota.not_reported`) when absent. Never estimated, never fetched from another endpoint. `gateway.switch_active_key()` and `provider_status_lines()` back the bot commands.
- Page API: `/api/*`. Requests must carry the header `X-Console: 1`, and a non-localhost `Origin` is rejected with 403. A plain `curl` gets `{"error":"Forbidden"}`, which is expected and not a bug. To inspect state from the shell, import the modules directly, e.g. `python3 -c "import agents; print(agents.list_agents('http://127.0.0.1:8000'))"`.
- Routes: `/api/run` (NDJSON stream), `/api/settings` (`{lang}`), `/api/store/{profiles,formats}`, `/api/providers/{save,delete,import,test,keys/add,keys/check,keys/delete}`, `/api/gateway/{state,regenerate}`, `/api/logs[/clear]`, `/api/agents[/enable,/disable,/models,/custom/save,/custom/delete,/icon]`.
- Gateway endpoints on the same port: `POST /v1/chat/completions` (OpenAI), `POST /v1/messages` (+ `/count_tokens`, Anthropic), `GET /v1/models`. OpenAI clients use `http://127.0.0.1:8000/v1`; Anthropic clients use the bare root.
- **After editing any `.py` file, restart the server** (`./eskali_api_launcher.sh stop && ./eskali_api_launcher.sh start`) and tell the user to press F5. The running process does not reload code, and a missed restart has already caused a "my change doesn't show" report. A restart briefly interrupts agents that are using the gateway.

## Agents (`agents.py`)

Built-in agents live in `BUILTIN` (the order is the display order): Claude Code, opencode, pi, Hermes. **Config formats were checked against the installed tools, not guessed**; keep it that way.

| Agent | Config | What Enable writes |
|---|---|---|
| Claude Code | `~/.claude/settings.json` | `env`: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, model vars; removes `ANTHROPIC_API_KEY` |
| opencode | `~/.config/opencode/opencode.json[c]` | `provider.local-gateway` (`@ai-sdk/openai-compatible`) + `model` |
| pi (`@earendil-works/pi-coding-agent`, binary `pi`) | `~/.pi/agent/models.json` **and** `~/.pi/agent/settings.json` | `providers.local-gateway` {`baseUrl` …/v1, `api: "openai-completions"`, `apiKey`, `models: [{id,name}]`}; `defaultProvider` / `defaultModel`. The `Pi` class drives a hidden `_PiSettings` agent (state id `pi-settings`) so both files are switched and restored together. The user's own `atria` provider is left untouched. |
| Hermes | `~/.hermes/config.yaml` | Written through `hermes config set model.*` |
| Custom (from the page) | Any JSON (dot path) or `.env` (KEY=VALUE) file | Only the chosen URL / key / model fields |

Enable/Disable contract: Enable backs up the config first. Disable restores the backup byte-for-byte if the file is unchanged since Enable; otherwise it restores only the values it changed, so the user's edits made in between are kept. When adding an agent, test enable, re-enable and disable against a copy of the configs (`HOME=<scratch dir> API_CONSOLE_HOME=<scratch>/data`), never the real files.

## Translations

- `INDEX_HTML` holds keys, not text: `{{t:key}}` (HTML-escaped), `{{h:key}}` (trusted HTML from the file), `{{i18n:lang|dir|json}}`. `render_index(lang)` fills them per request from the saved language (cached per process, so edits to `i18n/*.json` also need a restart).
- JS uses `T('key', {name: value})` (not `t`, which is a common local variable there). `T()` does not escape: escape user data before it goes into HTML, and wrap `T()` in `escapeHtml()` inside attributes. `LANG` and `RTL` are globals.
- Python uses `i18n.t("key", name=...)` (server messages, `gateway.py`/`agents.py`/`phone.py` errors shown in the page). `key` and `lang` are positional-only, so they also work as placeholders (`{key}` is common in alerts). Messages sent to agents through `/v1/*` stay English.
- Telegram (`alerts.py`): alerts, the daily summary, bot replies (`bot.*`) and settings errors use `i18n.t()` with the saved UI language, read when each message is built, so they follow a language switch at once. Keys `alert.*`, `tg.*`, `bot.*`, `quota.*`. `handle_command` maps `gateway.switch_active_key()` errors by comparing with `i18n.t("err.unknown_provider")` / `i18n.t("err.no_other_key")`. Quota bucket names come from `gateway.quota_label()`. The phone 401/403 page (`_deny`) uses the saved language and direction.
- Shell: `t key name=value` in `eskali_api_launcher.sh` / `install_eskali_api.sh`; without Python it reads the JSON line with `sed`, so `launcher.*`/`install.*` values must not contain `"` or `\`.
- Switching (header `<select id="langSelect">` or the Settings row) calls `setLang()`: POST `/api/settings`, flush unsaved store ops, reload.
- Layout: use logical CSS (`margin-inline-start`, `inset-inline-end`, `border-inline-start`, `text-align: start`), never `left`/`right`, so both directions work. `--slide-x` flips the toast animation.
- `get.sh`, `get.ps1`, `.bat`, `.vbs` stay English (they run before the files exist, or can't read them).

## UI (inside `INDEX_HTML`)

- **Tabs:** اختبار (test), مفاتيحي والمراقبة (keys and monitoring), المزودين (providers), Gateway, الوكلاء (agents). All switch through `showTab(name)`, and the last tab is remembered in prefs (`localStorage` key `api_test_console_prefs`).
- **Side nav:** a fixed bar on the inline-start side (right in Arabic, left in English/French), below the sticky header. `body.nav-collapsed` shows icons only. `toggleNav()` / `setNavCollapsed()` / `initNav()` handle it, and the state is saved as `prefs.navCollapsed`. On screens ≤700px it is always collapsed and opens over the page. CSS vars: `--nav-w`, `--nav-open`, `--nav-closed`, and `--header-h` (measured with a ResizeObserver).
- **Key cards** (`renderArchive()`), built for keys with thousands of models:
  - `modelTags(list, key)` shows the `MODELS_PREVIEW` (12) fastest models plus a "show all" button.
  - The open list is a scroll box with its own search (`modelsOpen`, `modelsQuery`). Both survive re-renders, including input focus and cursor.
  - `diffLine` truncates to 8 names (`shortList`); the full list is in the tooltip.
  - The global search also matches model names.
  - A floating ⬆ button (`#toTop`) returns to the top.
- **Key name link:** the card title links to the key's "المصدر" (source) field when it is an http(s) URL or a bare domain (`sourceUrl()`). It opens in a new tab and rejects other schemes.
- **Copy config menu** on each key: opencode / pi agent (`piagent`) / hermes / custom formats / custom template.
- **Thinking level per agent:** under the model picker, a 🧠 select (`effortSelectHtml()`) shows only for models that `gateway.supports_reasoning()` guesses from the name (sent to the page as `reasoning_models`). The level travels in the model name written to the agent config (`gpt-5@high`, levels in `gateway.EFFORTS`). `dispatch()` strips it for routing and `apply_effort()` writes the upstream parameter: `reasoning_effort` (OpenRouter: `reasoning.effort`), or for Anthropic `thinking` adaptive + `output_config.effort` (4.6+/5 family) or `budget_tokens` (older). A 400 that names the parameter is retried without it and remembered in `NO_EFFORT`. Anthropic thinking comes back to OpenAI clients as `reasoning_content`.

- **Phone layout (≤520px):** compact header (title hidden ≤400px), 2-column stats, key/agent grids use `minmax(0, 1fr)`. Headless Chrome can't go below ~500px wide, so check phone widths through the DevTools protocol (`Emulation.setDeviceMetricsOverride`, driven from Node's built-in WebSocket) and make sure `document.documentElement.scrollWidth` equals the width.

## Working conventions (user preferences)

- **Extend, don't replace:** keep existing controls, IDs, `onclick` handlers and look; add helpers around them.
- Commit to git after each logical change (no `backup/` snapshots).
- Verify before saying something is done:
  - `python3 -c "import ast; ast.parse(open('api_web_dashboard_v2.py').read())"`
  - `python3 -m unittest discover tests` (translations, rendered page, `node --check` on the scripts in all three languages).
  - Screenshot with headless Chrome: `google-chrome --headless=new --screenshot=... --window-size=1400,900 --virtual-time-budget=4000`. Shoot `render_index(lang)` output, not raw `INDEX_HTML` (it only holds keys), in ar and en at least. For data-dependent views, use a copy that stubs `getProfiles()` with fake data, or a scratch server (`HOME=<scratch> API_CONSOLE_HOME=<scratch>/data`, other port) seeded through the API.
  - Do all of this in the session scratchpad, not the project folder.
- Style: match the surrounding code (short comments, compact JS, CSS variables from `:root`, and dark/light via the existing tokens).
- Don't read or print real keys from `~/.api-test-console`, agent configs, or auth files. Mask them (`gateway.mask_key`).

## Current state (2026-09-26)

Done this session, all verified and live on port 8000:
- pi added as a built-in agent.
- Tab bar turned into a collapsible side nav.
- Long model lists made compact (preview, scroll box, per-key search, back-to-top, model search).
- Key title links to its source site.

2026-09-28, branch `feature/i18n-ar-en-fr`: the UI, server messages, launcher and installer are translated (ar/en/fr) with a language switcher and RTL/LTR layout; see **Translations**.

## Pending / open items

- **freebuff** (npm `freebuff`, binary `/usr/bin/freebuff`, data in `~/.config/manicode/`) is **not added**. It has a `/byok add` command with an `openai-compatible` provider and a custom base URL. Connections are stored in `~/.config/freebuff/byok/connections.json` (or `FREEBUFF_BYOK_CONFIG_DIR`), but the key is read from an env var (`credentialRef: "env:VAR"`) or the OS keychain, not from a file. It is also unknown whether `http://127.0.0.1` is accepted (an error message mentions reaching the provider "securely", so it may require https). Next step: the user runs `/byok` inside freebuff and tries the local URL. If it works, add freebuff as a "show the commands to run" agent rather than an automatic file switch. Deeper inspection of the freebuff binary was blocked by the auto-mode classifier; don't retry it.
- pi's agent dir is hard-coded to `~/.pi/agent`. Check whether pi honours an env override (e.g. `PI_CODING_AGENT_DIR`) before supporting one.
- Existing saved keys only become title links once their "المصدر" field holds a URL.
- The user pasted a real provider key in chat on 2026-09-26 and was advised to rotate it.
