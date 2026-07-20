# AGENTS.md

Hermes Agent plugin adapter for KOOK (开黑啦). See `.github/copilot-instructions.md` for the full architecture and convention details — it is accurate; read it before non-trivial changes.

## Build / test / lint

None. There is no test suite, linter, or build step. Verification is limited to syntax:

```bash
python -m py_compile adapter.py ws_handler.py messaging.py constants.py config_helpers.py standalone.py
```

Deploy = copy all `.py` files + `plugin.yaml` into `~/.hermes/plugins/platforms/kook/`, then `hermes gateway restart`.

## Not runnable standalone

This is a plugin loaded inside the Hermes gateway process. Modules import `gateway.platforms.*` and `hermes_cli.gateway` from the Hermes core package, which is not in this repo — do not try to run, import-test, or add tests that execute these modules here.

## Conventions that matter

- `register(ctx)` in `adapter.py` is the only public entry point (`__init__.py` re-exports it). New framework callbacks must be wired through `ctx.register_platform(...)` there.
- Every config option must support **both** `config.yaml` `extra` keys and `KOOK_*` env vars; `KookAdapter.__init__` merges both paths (env list/table in copilot-instructions.md).
- Never import `aiohttp`/`httpx` unconditionally — use the lazy-import flags (`AIOHTTP_AVAILABLE`, `HTTPX_AVAILABLE`) from `config_helpers.py`.
- All KOOK API magic numbers live in `constants.py`; reference them, don't inline.
- `self._token` already includes the `"Bot "` prefix (normalized in `__init__`) — never add it again.
