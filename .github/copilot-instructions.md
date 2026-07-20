# Hermes KOOK Adapter — Copilot Instructions

## Build, test, and lint

There is no test framework, linter config, or build step in this repository. The project is a
single-file plugin deployed by copying all `.py` files and `plugin.yaml` into `~/.hermes/plugins/platforms/kook/`.

Install dependencies:

```bash
pip install aiohttp httpx
```

Deploy to Hermes:

```bash
mkdir -p ~/.hermes/plugins/platforms/kook
cp adapter.py ws_handler.py messaging.py constants.py config_helpers.py standalone.py __init__.py plugin.yaml ~/.hermes/plugins/platforms/kook/
```

Then configure via `~/.hermes/.env` or `~/.hermes/config.yaml` and restart: `hermes gateway restart`.

## Architecture

This is a **plugin adapter** for the [Hermes Agent](https://github.com/WOO-MX/hermes) gateway framework.
It is **not** a standalone application — it runs as a dynamically loaded module inside the Hermes
gateway process.

**Core files:** The adapter is split across 6 Python modules (~236 + 442 + 285 + 85 + 85 + 72
≈ 1205 lines total, well-factored):

```
adapter.py (236 lines)   — KookAdapter orchestrator + register() entry point
ws_handler.py (442 lines)— KookWebSocketMixin: connect, listen loop, frame handling, reconnect, cleanup
messaging.py (285 lines) — KookMessagingMixin: send/send_image/send_document/send_voice, upload, HTTP helpers
standalone.py (85 lines) — _standalone_send, interactive_setup (no gateway process needed)
constants.py (72 lines)  — KOOK API constants (message types, signal types, limits)
config_helpers.py (85 lines)— Lazy imports, check_requirements, validate_config, _env_enablement
```

KookAdapter uses multiple inheritance to compose mixins:
```python
class KookAdapter(KookWebSocketMixin, KookMessagingMixin, BasePlatformAdapter):
```

**Data flow:**
1. KOOK WebSocket sends frame → `KookWebSocketMixin._listen_loop` → `_handle_frame` → `_handle_event`
2. `_handle_event` applies filters (self-message, dedup, access control, @mention gate)
3. Builds a `MessageEvent` with `source` (chat_id, user_id, etc.) and dispatches to the Hermes agent
   via `self.handle_message(event)` (inherited from `BasePlatformAdapter`)
4. The agent processes the message and calls `KookMessagingMixin.send(chat_id, response)` to reply

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
| `KOOK_CHANNEL_PROMPT` | `extra.channel_prompt` |
| `KOOK_PROXY` | (env only, resolved via `resolve_proxy_url()`) |

The `KookAdapter.__init__` reads both sources and merges them. **Every new config option
must support both paths.**

Note on access control: when `allow_all_users` is false, the author must be in
`allowed_users` — an **empty allowlist denies everyone**.

### Lazy dependency imports

Both `aiohttp` and `httpx` are imported in `config_helpers.py` with availability tracked via
`AIOHTTP_AVAILABLE` / `HTTPX_AVAILABLE` flags. The `check_requirements()` function gates
loading. Do not import these unconditionally in other modules — import the flags from
`config_helpers` instead.

### KOOK API constants

All KOOK-specific magic numbers (message types, signal types, limits) are defined in
`constants.py`. Always reference these constants, never inline the numbers.

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
