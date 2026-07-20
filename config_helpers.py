"""Lazy dependency imports and configuration helpers."""

import os
from typing import Optional

# ---------------------------------------------------------------------------
# Lazy imports — pulled from Hermes core at runtime
# ---------------------------------------------------------------------------

try:
    import aiohttp  # noqa: F401
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None  # type: ignore

try:
    import httpx  # noqa: F401
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore


# ---------------------------------------------------------------------------
# Requirement checks
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Check that aiohttp and httpx are available."""
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


def validate_config(config) -> bool:
    """Validate that the platform config has enough info to connect."""
    from .constants import TOKEN_PREFIX
    extra = getattr(config, "extra", {}) or {}
    token = extra.get("token") or os.getenv("KOOK_TOKEN", "")
    return bool(token and token.startswith(f"{TOKEN_PREFIX} "))


def is_connected(adapter) -> bool:
    """Check if the adapter has an active WebSocket connection."""
    return bool(
        adapter and adapter._running
        and adapter._ws is not None
        and not adapter._ws.closed
    )


# ---------------------------------------------------------------------------
# Env-driven enablement (so env-only configs show in gateway status)
# ---------------------------------------------------------------------------

def _env_enablement() -> Optional[dict]:
    token = os.getenv("KOOK_TOKEN", "").strip()
    if not token:
        return None
    extra = {"token": token}
    home_channel = os.getenv("KOOK_HOME_CHANNEL", "").strip()
    if home_channel:
        extra["home_channel"] = home_channel
    allowed_users = os.getenv("KOOK_ALLOWED_USERS", "").strip()
    if allowed_users:
        extra["allowed_users"] = [u.strip() for u in allowed_users.split(",") if u.strip()]
    allow_all = os.getenv("KOOK_ALLOW_ALL_USERS", "").strip().lower()
    if allow_all in ("1", "true", "yes"):
        extra["allow_all_users"] = True
    channel_prompt = os.getenv("KOOK_CHANNEL_PROMPT", "").strip()
    if channel_prompt:
        extra["channel_prompt"] = channel_prompt
    return {"extra": extra, "home_channel": home_channel or None}
