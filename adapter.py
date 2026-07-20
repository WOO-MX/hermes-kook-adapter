"""
KOOK (开黑啦) Platform Adapter for Hermes Agent.

A plugin-based gateway adapter that connects to KOOK via WebSocket + REST API
and relays messages to/from the Hermes agent.

See README.md for setup instructions; see constants.py / ws_handler.py /
messaging.py / standalone.py for implementation details.

Package layout:
    __init__.py          — re-exports register()
    adapter.py           — KookAdapter (thin orchestrator, this file)
    constants.py         — KOOK API constants
    config_helpers.py    — dependency checks, config validation, env enablement
    standalone.py        — standalone sender & interactive setup wizard
    ws_handler.py        — KookWebSocketMixin (connect/WS/listen/reconnect)
    messaging.py         — KookMessagingMixin (send/upload/HTTP helpers)
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional, Set

from gateway.platforms.base import (
    BasePlatformAdapter,
    _ssrf_redirect_guard,
    safe_url_for_log,
)
from gateway.platforms._http_client_limits import platform_httpx_limits
from gateway.config import Platform

from .config_helpers import AIOHTTP_AVAILABLE, HTTPX_AVAILABLE
from .config_helpers import httpx
from .config_helpers import check_requirements, validate_config, is_connected, _env_enablement  # noqa: F811
from .constants import API_TIMEOUT, MAX_MESSAGE_LENGTH  # noqa: F811
from .standalone import _standalone_send, interactive_setup  # noqa: F811
from .ws_handler import KookWebSocketMixin
from .messaging import KookMessagingMixin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# KookAdapter — composes WebSocket and messaging mixins with Hermes base
# ---------------------------------------------------------------------------

class KookAdapter(KookWebSocketMixin, KookMessagingMixin, BasePlatformAdapter):
    """KOOK Bot adapter using WebSocket Gateway + REST API."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    @property
    def _log_tag(self) -> str:
        return "KOOK"

    def __init__(self, config, **kwargs):
        platform = Platform("kook")
        super().__init__(config=config, platform=platform)

        extra = config.extra or {}
        self._token = str(extra.get("token") or os.getenv("KOOK_TOKEN", "")).strip()
        if self._token and not self._token.startswith("Bot "):
            self._token = f"Bot {self._token}"

        self._home_channel = str(
            extra.get("home_channel") or os.getenv("KOOK_HOME_CHANNEL", "")
        ).strip() or None

        self._channel_prompt = str(
            extra.get("channel_prompt") or os.getenv("KOOK_CHANNEL_PROMPT", "")
        ).strip() or None

        # Access control
        self._allow_all = bool(
            extra.get("allow_all_users")
            or os.getenv("KOOK_ALLOW_ALL_USERS", "").strip().lower() in ("1", "true", "yes")
        )
        self._allowed_users: Set[str] = set()
        raw_allowed = extra.get("allowed_users") or []
        if isinstance(raw_allowed, str):
            raw_allowed = [u.strip() for u in raw_allowed.split(",") if u.strip()]
        if isinstance(raw_allowed, list):
            self._allowed_users = set(str(u).strip() for u in raw_allowed)
        env_allowed = os.getenv("KOOK_ALLOWED_USERS", "").strip()
        if env_allowed:
            self._allowed_users.update(u.strip() for u in env_allowed.split(",") if u.strip())

        # Connection state
        self._http_client: Optional[httpx.AsyncClient] = None
        self._ws = None  # aiohttp ClientWebSocketResponse
        self._session = None  # aiohttp ClientSession
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._sn: int = 0
        self._session_id: Optional[str] = None

        # Message dedup
        self._seen_msg_ids: Dict[str, float] = {}

        # Bot's own user ID (fetched during connect)
        self._bot_user_id: Optional[str] = None

        # Channel cache
        self._channel_cache: Dict[str, Dict[str, Any]] = {}

        # Busy-guard
        self._last_msg_id: Dict[str, str] = {}
        self._typing_sent_at: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "KOOK"

    @property
    def enforces_own_access_policy(self) -> bool:
        """KOOK gates DM/group access at intake."""
        return True

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Obtain WebSocket gateway URL and open connection."""
        if not AIOHTTP_AVAILABLE:
            self._set_fatal_error("kook_missing_dep", "aiohttp not installed", retryable=True)
            logger.warning("[%s] aiohttp not installed. Run: pip install aiohttp", self._log_tag)
            return False
        if not HTTPX_AVAILABLE:
            self._set_fatal_error("kook_missing_dep", "httpx not installed", retryable=True)
            logger.warning("[%s] httpx not installed. Run: pip install httpx", self._log_tag)
            return False
        if not self._token:
            self._set_fatal_error("kook_missing_token", "KOOK_TOKEN is required", retryable=True)
            logger.warning("[%s] KOOK_TOKEN is required", self._log_tag)
            return False

        if not self._acquire_platform_lock("kook-token", self._token[:20], "KOOK token"):
            return False

        try:
            # Create HTTP client
            proxy_url = os.getenv("KOOK_PROXY", "").strip() or None
            from gateway.platforms.base import resolve_proxy_url
            proxy_url = resolve_proxy_url("KOOK_PROXY", target_hosts="www.kookapp.cn") or proxy_url

            self._http_client = httpx.AsyncClient(
                timeout=API_TIMEOUT,
                follow_redirects=True,
                event_hooks={"response": [_ssrf_redirect_guard]},
                limits=platform_httpx_limits(),
                proxy=proxy_url,
            )

            # 1. Get WebSocket gateway URL
            gateway_url = await self._get_gateway_url()
            logger.info("[%s] Gateway URL: %s", self._log_tag, safe_url_for_log(gateway_url))

            # 1.5 Fetch bot's own user ID
            await self._fetch_bot_user_id()

            # 2. Open WebSocket
            await self._open_ws(gateway_url)

            # 3. Start listeners
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            self._mark_connected()
            logger.info("[%s] Connected", self._log_tag)
            return True

        except Exception as exc:
            message = f"KOOK startup failed: {exc}"
            self._set_fatal_error("kook_connect_error", message, retryable=True)
            logger.error("[%s] %s", self._log_tag, message, exc_info=True)
            await self._cleanup()
            self._release_platform_lock()
            return False

    async def disconnect(self) -> None:
        """Close all connections and stop listeners."""
        self._running = False
        self._mark_disconnected()

        for task in [self._listen_task, self._heartbeat_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._listen_task = None
        self._heartbeat_task = None

        await self._cleanup()
        self._release_platform_lock()
        logger.info("[%s] Disconnected", self._log_tag)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        """Return Authorization header — self._token already includes 'Bot ' prefix."""
        return {"Authorization": self._token}


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="kook",
        label="KOOK (开黑啦)",
        adapter_factory=lambda cfg: KookAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["KOOK_TOKEN"],
        install_hint="pip install aiohttp httpx",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="KOOK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="KOOK_ALLOWED_USERS",
        allow_all_env="KOOK_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🎮",
        pii_safe=True,
        allow_update_command=True,
    )
