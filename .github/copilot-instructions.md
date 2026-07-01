# Hermes KOOK Adapter — Copilot Instructions

## Build, test, and lint

There is no test framework, linter config, or build step in this repository. The project is a single-file
plugin deployed by copying files into the Hermes plugin directory.

Install dependencies:

```bash
pip install aiohttp httpx
```

To deploy, run `install.sh` (or use the one-liner: `curl -fsSL https://raw.githubusercontent.com/WOO-MX/hermes-kook-adapter/main/install.sh | bash`).

## Architecture

This is a **plugin adapter** for the [Hermes Agent](https://github.com/WOO-MX/hermes) gateway framework.
It is **not** a standalone application — it runs as a dynamically loaded module inside the Hermes
gateway process.

**Core file:** `adapter.py` (~1042 lines) — the entire adapter in one file.

```
adapter.py
├── Module-level helpers (check_requirements, validate_config, is_connected, _env_enablement)
├── _standalone_send()         — REST-only sender for cron delivery (no live gateway needed)
├── interactive_setup()         — CLI setup wizard invoked by `hermes platform setup`
├── KookAdapter(BasePlatformAdapter) — the main adapter class
│   ├── connect/disconnect      — WebSocket lifecycle
│   ├── _listen_loop            — reads WS frames, dispatches via _handle_frame → _handle_event
│   ├── _handle_event           — access control, @mention gate, builds MessageEvent → handle_message()
│   ├── send/send_image/send_document/send_voice — REST API message delivery
│   ├── _upload_asset           — file upload to KOOK CDN (POST /api/v3/asset/create)
│   ├── _api_post/_api_get      — HTTP helpers wrapping httpx
│   ├── _reconnect              — exponential backoff, max 5 attempts
│   └── _cleanup                — close WS + HTTP clients
└── register(ctx)               — Hermes plugin entry point (wires adapter into the gateway)
```

**Data flow:**
1. KOOK WebSocket sends frame → `_listen_loop` → `_handle_frame` → `_handle_event`
2. `_handle_event` applies filters (self-message, dedup, access control, @mention gate)
3. Builds a `MessageEvent` with `source` (chat_id, user_id, etc.) and dispatches to the Hermes agent
   via `self.handle_message(event)` (inherited from `BasePlatformAdapter`)
4. The agent processes the message and calls `adapter.send(chat_id, response)` to reply

## Key conventions

### Hermes plugin protocol

`register(ctx)` is the **only** public symbol. The `__init__.py` re-exports it. The function registers
all callbacks the Hermes framework needs:

- `adapter_factory` — creates `KookAdapter` instances
- `check_fn`, `validate_config`, `is_connected` — module-level functions (not methods)
- `standalone_sender_fn` — used for cron delivery when the gateway is not running
- `env_enablement_fn` — allows env-only configs to appear in gateway status UI
- `setup_fn` — interactive setup wizard

When adding features, always update the `register()` call with any new callbacks the Hermes
framework provides.

### Configuration dual-path

All settings can come from **either** environment variables **or** `config.yaml` `extra` dict:

| Env var | config.yaml key |
|---------|-----------------|
| `KOOK_TOKEN` | `extra.token` |
| `KOOK_HOME_CHANNEL` | `extra.home_channel` |
| `KOOK_ALLOWED_USERS` | `extra.allowed_users` |
| `KOOK_ALLOW_ALL_USERS` | `extra.allow_all_users` |
| `KOOK_PROXY` | (env only, resolved via `resolve_proxy_url()`) |

The `KookAdapter.__init__` reads both sources and merges them. **Every new config option
must support both paths.**

### Lazy dependency imports

Both `aiohttp` and `httpx` are imported at module top level, but availability is tracked via
`AIOHTTP_AVAILABLE` / `HTTPX_AVAILABLE` flags. The `check_requirements()` function gates
loading. Do not import these unconditionally inside the adapter class — use the gate functions.

### KOOK API constants

All KOOK-specific magic numbers (message types, signal types, limits) are defined as module-level
constants (e.g., `MSG_TYPE_KMARKDOWN = 9`, `SIGNAL_PING = 2`). Always reference these constants,
never inline the numbers.

### HTTP helpers

- `_api_post(url, body)` → returns `SendResult(success=, message_id=/ error=)` — for message sending
- `_api_get(url, params)` → returns `{"success": True/False, "data": ... / "error": ...}` — for queries
- `_auth_headers()` returns `{"Authorization": self._token}` — note the token **already** includes
  the `"Bot "` prefix (normalized in `__init__`), do not double-prefix it.

### Message handling pipeline

Incoming messages go through these gates **in order**:
1. Self-message filter (author_id == bot_user_id)
2. Dedup (5-second window via `_seen_msg_ids`)
3. Access control (allowed_users allowlist)
4. @mention gate (group chats only — checks `extra.mention` array and `(met)id(met)` content pattern)

### WebSocket frame handling

KOOK uses signal-based WebSocket protocol. The gateway may send **compressed BINARY frames**
(zlib) when `compress=1`. The `_listen_loop` handles both `TEXT` and `BINARY` frame types.
Frame dispatch is in `_handle_frame` based on the `s` (signal) field: 0=event, 1=hello, 2=ping,
3=pong, 5=reconnect.

### Reconnect strategy

On WS `CLOSED` or `ERROR`, retry up to 5 times with exponential backoff (base 2s, max 300s).
The reconnect re-fetches the gateway URL and opens a fresh WebSocket — it does not use
KOOK's session resume protocol.

### Channel cache

`_channel_cache` maps `channel_id → {name, type, guild_id}` with a 5-minute TTL. Used by
`get_chat_info()`. When adding channel metadata, keep the TTL reasonable (300s) and always
include a `_cached_at` timestamp for invalidation.
