"""
KOOK (开黑啦) Platform Adapter for Hermes Agent.

A plugin-based gateway adapter that connects to KOOK via WebSocket + REST API
and relays messages to/from the Hermes agent.

KOOK API reference: https://developer.kookapp.cn/doc/

Configuration via environment variables:
    KOOK_TOKEN          — Bot Token (required)
    KOOK_HOME_CHANNEL   — Default channel ID for cron delivery
    KOOK_ALLOWED_USERS  — Comma-separated user IDs allowed to interact
    KOOK_ALLOW_ALL_USERS— Allow any user (dev mode)
    KOOK_PROXY          — Proxy URL (SOCKS5/HTTP)

Or in config.yaml:
    gateway:
      platforms:
        kook:
          enabled: true
          extra:
            token: "Bot_xxxxxxxx"
            home_channel: "channel_id"
            allowed_users: ["user_id1", "user_id2"]
            allow_all_users: false
"""

import asyncio
import json
import logging
import os
import time
import uuid
import zlib
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy imports — pulled from Hermes core at runtime
# ---------------------------------------------------------------------------

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    _ssrf_redirect_guard,
    cache_image_from_bytes,
    cache_image_from_url,
    should_send_media_as_audio,
    safe_url_for_log,
)
from gateway.platforms._http_client_limits import platform_httpx_limits
from gateway.config import Platform
from gateway.platforms.helpers import strip_markdown

# ---------------------------------------------------------------------------
# KOOK API constants
# ---------------------------------------------------------------------------

API_BASE = "https://www.kookapp.cn/api/v3"
TOKEN_PREFIX = "Bot"

# KOOK message types (channel_type field in WebSocket events)
CHANNEL_TYPE_GROUP = "GROUP"
CHANNEL_TYPE_PERSON = "PERSON"

# KOOK message content types (type field in messages)
MSG_TYPE_TEXT = 1       # Plain text
MSG_TYPE_IMAGE = 2      # Image
MSG_TYPE_VIDEO = 3      # Video
MSG_TYPE_FILE = 4       # File
MSG_TYPE_AUDIO = 8      # Audio
MSG_TYPE_KMARKDOWN = 9  # KMarkdown (supports **bold**, *italic*, ```code```, > quote)
MSG_TYPE_CARD = 10      # Card message

# WebSocket signal types
SIGNAL_EVENT = 0         # Dispatch event
SIGNAL_HELLO = 1         # Server hello (connection established)
SIGNAL_PING = 2          # Server ping
SIGNAL_PONG = 3          # Client pong response
SIGNAL_RESUME = 4        # Resume session
SIGNAL_RECONNECT = 5     # Server requests reconnect

# Limits
MAX_MESSAGE_LENGTH = 20000   # KOOK KMarkdown content limit
HEARTBEAT_INTERVAL = 30      # Seconds between heartbeat pings
API_TIMEOUT = 30             # HTTP request timeout
RECONNECT_BACKOFF_BASE = 2   # Base seconds for exponential backoff
MAX_RECONNECT_BACKOFF = 300  # Max backoff seconds
DEDUP_WINDOW_SECONDS = 5     # Deduplicate identical messages within this window


# ---------------------------------------------------------------------------
# Requirement checks
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Check that aiohttp and httpx are available."""
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


def validate_config(config) -> bool:
    """Validate that the platform config has enough info to connect."""
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
    return {"extra": extra, "home_channel": home_channel or None}


# ---------------------------------------------------------------------------
# Standalone sender (for cron delivery without live gateway)
# ---------------------------------------------------------------------------

async def _standalone_send(chat_id: str, content: str, extra: dict) -> dict:
    """Send a message via KOOK REST API without a live adapter."""
    token = extra.get("token") or os.getenv("KOOK_TOKEN", "")
    if not token:
        return {"error": "KOOK_TOKEN not configured"}

    plain = strip_markdown(content)
    url = f"{API_BASE}/message/create"
    body = {
        "type": MSG_TYPE_KMARKDOWN,
        "target_id": chat_id,
        "content": plain,
    }
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }

    proxy_url = os.getenv("KOOK_PROXY", "").strip() or None
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT, proxy=proxy_url) as client:
            resp = await client.post(url, json=body, headers=headers)
            data = resp.json()
            if data.get("code") == 0:
                msg_id = data.get("data", {}).get("msg_id", "")
                return {"success": True, "message_id": msg_id}
            return {"error": f"KOOK API error: code={data.get('code')} msg={data.get('message')}"}
    except Exception as e:
        return {"error": f"KOOK standalone send failed: {e}"}


# ---------------------------------------------------------------------------
# Interactive setup
# ---------------------------------------------------------------------------

async def interactive_setup(ctx) -> bool:
    """Interactive setup wizard for KOOK platform."""
    from hermes_cli.gateway import _prompt as prompt_fn

    print()
    print("KOOK (开黑啦) Bot Setup")
    print("──────────────────────")
    print("1. Go to https://developer.kookapp.cn/app/index")
    print("2. Create a new application → Bot")
    print("3. Copy the Bot Token (format: Bot xxxxxxxxxxxx)")
    print()

    token = await prompt_fn("KOOK Bot Token", secret=True)
    if not token:
        print("Setup cancelled.")
        return False

    home_channel = await prompt_fn("Home channel ID (optional, for cron delivery)")
    allowed_users = await prompt_fn("Allowed user IDs, comma-separated (empty = allow all)")

    env_updates = {"KOOK_TOKEN": token}
    if home_channel:
        env_updates["KOOK_HOME_CHANNEL"] = home_channel
    if allowed_users:
        env_updates["KOOK_ALLOWED_USERS"] = allowed_users

    ctx.write_env(env_updates)
    print()
    print("KOOK platform configured!")
    print(f"  Token: {token[:15]}...{token[-4:]}")
    if home_channel:
        print(f"  Home channel: {home_channel}")
    return True


# ---------------------------------------------------------------------------
# KOOK Adapter
# ---------------------------------------------------------------------------

class KookAdapter(BasePlatformAdapter):
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
        # Normalize: ensure token has "Bot " prefix
        if self._token and not self._token.startswith("Bot "):
            self._token = f"Bot {self._token}"

        self._home_channel = str(
            extra.get("home_channel") or os.getenv("KOOK_HOME_CHANNEL", "")
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
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._sn: int = 0  # Sequence number from server heartbeat
        self._session_id: Optional[str] = None  # KOOK session ID for resume

        # Message dedup (KOOK may deliver duplicates)
        self._seen_msg_ids: Dict[str, float] = {}

        # Bot's own user ID (fetched during connect, used to skip self-messages)
        self._bot_user_id: Optional[str] = None

        # Cache: channel_id → {name, type, guild_id}
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

            # 1.5 Fetch bot's own user ID for self-message filtering
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
    # Gateway URL & WebSocket
    # ------------------------------------------------------------------

    async def _get_gateway_url(self) -> str:
        """GET /api/v3/gateway/index → return WebSocket URL."""
        url = f"{API_BASE}/gateway/index"
        headers = self._auth_headers()
        resp = await self._http_client.get(url, headers=headers)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Failed to get gateway URL: code={data.get('code')} msg={data.get('message')}")
        ws_url = data.get("data", {}).get("url", "")
        if not ws_url:
            raise RuntimeError("Gateway URL is empty")
        return ws_url

    async def _fetch_bot_user_id(self) -> None:
        """GET /api/v3/user/me → store bot's own user ID for self-message filtering."""
        try:
            url = f"{API_BASE}/user/me"
            headers = self._auth_headers()
            resp = await self._http_client.get(url, headers=headers)
            data = resp.json()
            if data.get("code") == 0:
                user = data.get("data", {})
                self._bot_user_id = user.get("id", "")
                logger.info("[%s] Bot user ID: %s", self._log_tag, self._bot_user_id)
            else:
                logger.warning("[%s] Failed to fetch bot user ID: code=%s msg=%s",
                               self._log_tag, data.get("code"), data.get("message"))
        except Exception as exc:
            logger.warning("[%s] Could not fetch bot user ID: %s", self._log_tag, exc)
            # Non-fatal — self-message filtering degrades gracefully

    async def _open_ws(self, gateway_url: str) -> None:
        """Open WebSocket connection to the KOOK gateway."""
        proxy_url = os.getenv("KOOK_PROXY", "").strip() or None

        # Build connector (aiohttp >=3.13: connector goes on ClientSession, not ws_connect)
        connector = None
        if proxy_url:
            try:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(proxy_url, rdns=True)
            except ImportError:
                pass

        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=None, sock_read=60),
        )

        self._ws = await self._session.ws_connect(gateway_url)
        logger.info("[%s] WebSocket opened", self._log_tag)

    # ------------------------------------------------------------------
    # Listen loop (incoming WebSocket frames)
    # ------------------------------------------------------------------

    async def _listen_loop(self) -> None:
        """Read frames from WebSocket and dispatch to handlers."""
        logger.info("[%s] Listen loop started", self._log_tag)
        while self._running and self._ws and not self._ws.closed:
            try:
                msg = await self._ws.receive(timeout=60)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[%s] WS receive error: %s", self._log_tag, e)
                await asyncio.sleep(1)
                continue

            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    await self._handle_frame(json.loads(msg.data))
                except json.JSONDecodeError as e:
                    logger.warning("[%s] Invalid JSON frame: %s", self._log_tag, e)
                except Exception as e:
                    logger.error("[%s] Frame handler error: %s", self._log_tag, e, exc_info=True)

            elif msg.type == aiohttp.WSMsgType.BINARY:
                # KOOK gateway uses zlib-compressed BINARY frames when compress=1
                try:
                    decompressed = zlib.decompress(msg.data)
                    await self._handle_frame(json.loads(decompressed))
                except (zlib.error, json.JSONDecodeError) as e:
                    logger.warning("[%s] Invalid BINARY frame: %s", self._log_tag, e)
                except Exception as e:
                    logger.error("[%s] BINARY frame handler error: %s", self._log_tag, e, exc_info=True)

            elif msg.type == aiohttp.WSMsgType.CLOSED:
                logger.info("[%s] WebSocket closed by server, reconnecting...", self._log_tag)
                await self._reconnect()
                if not self._ws:
                    break  # all reconnect attempts failed
                # _reconnect() set up a fresh _ws — continue the loop
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error("[%s] WebSocket error, reconnecting...", self._log_tag)
                await self._reconnect()
                if not self._ws:
                    break  # all reconnect attempts failed
                # _reconnect() set up a fresh _ws — continue the loop

        logger.info("[%s] Listen loop ended", self._log_tag)

    async def _handle_frame(self, frame: dict) -> None:
        """Process a single WebSocket frame from KOOK."""
        signal = frame.get("s", -1)

        if signal == SIGNAL_HELLO:  # 1
            # Server hello — confirm connection
            session_id = frame.get("d", {}).get("session_id", "?")
            logger.info("[%s] Signal HELLO received, session_id=%s", self._log_tag, session_id)

        elif signal == SIGNAL_PING:  # 2
            # Server ping — send pong
            self._sn = frame.get("sn", self._sn)
            await self._send_ws_frame(SIGNAL_PONG, sn=self._sn)

        elif signal == SIGNAL_RECONNECT:  # 5
            # Server requests reconnect
            logger.info("[%s] Server requested reconnect", self._log_tag)
            await self._reconnect()

        elif signal == SIGNAL_EVENT:  # 0
            # Dispatch event — the actual messages
            data = frame.get("d", {})
            logger.info("[%s] Signal EVENT: type=%s channel_type=%s", self._log_tag,
                        data.get("type"), data.get("channel_type"))
            await self._handle_event(data)
        else:
            logger.debug("[%s] Unknown signal: %s", self._log_tag, signal)

    async def _handle_event(self, data: dict) -> None:
        """Process a KOOK event (incoming message, reaction, etc.)."""
        if not isinstance(data, dict):
            return

        channel_type = data.get("channel_type", "")
        target_id = data.get("target_id", "")  # channel_id
        msg_id = data.get("msg_id", "")
        msg_type = data.get("type", 0)
        content = data.get("content", "")
        # author_id is a top-level field in KOOK WebSocket events
        # author details (id, username, avatar) live in data.extra.author
        author_id = data.get("author_id", "")
        extra = data.get("extra", {})
        extra_author = (extra or {}).get("author", {}) if isinstance(extra, dict) else {}
        author_name = extra_author.get("username", "") or author_id or "unknown"

        # Debug: log every event with key fields
        logger.info("[%s] EVENT detail: self=%s type=%s ch_type=%s target=%s author=%s(%s) content=%s",
                    self._log_tag, data.get("self"), msg_type, channel_type,
                    target_id[:12] if target_id else "", author_id[:12] if author_id else "?",
                    author_name, (content or "")[:80])

        # Skip messages from our own bot
        # KOOK WebSocket events do NOT include a "self" field — this check
        # was always a no-op. Use author_id comparison against fetched bot ID instead.
        if data.get("self", False):
            logger.info("[%s] EVENT skipped: self message (legacy flag)", self._log_tag)
            return
        if self._bot_user_id and author_id == self._bot_user_id:
            logger.info("[%s] EVENT skipped: self message (author_id match)", self._log_tag)
            return

        # Dedup
        if msg_id:
            now = time.time()
            if msg_id in self._seen_msg_ids:
                if now - self._seen_msg_ids[msg_id] < DEDUP_WINDOW_SECONDS:
                    logger.info("[%s] EVENT skipped: dedup msg_id=%s", self._log_tag, msg_id[:12])
                    return
            self._seen_msg_ids[msg_id] = now
            # Clean old entries
            stale = [k for k, v in self._seen_msg_ids.items() if now - v > DEDUP_WINDOW_SECONDS * 2]
            for k in stale:
                del self._seen_msg_ids[k]

        if not target_id or not author_id:
            logger.info("[%s] EVENT skipped: missing target_id or author_id", self._log_tag)
            return

        # Track last message ID for typing indicator
        if msg_id:
            self._last_msg_id[target_id] = msg_id

        # Access control
        if not self._allow_all and self._allowed_users:
            if author_id not in self._allowed_users:
                logger.info("[%s] EVENT skipped: user %s not in allowlist", self._log_tag, author_id[:12])
                return

        # Determine chat type
        chat_type = "group" if channel_type == CHANNEL_TYPE_GROUP else "dm"

        # ── @mention gate (GROUP only) ──────────────────────────────────
        # In group channels, only respond when the bot is explicitly @mentioned.
        # DMs always pass through.
        if chat_type == "group":
            mentioned = False
            # Method 1: check extra.mention array (KOOK v3 API)
            if isinstance(extra, dict):
                mention_list = extra.get("mention") or []
                mention_all = extra.get("mention_all", False)
                if self._bot_user_id and self._bot_user_id in mention_list:
                    mentioned = True
                if mention_all:
                    mentioned = True
            # Method 2: check content for (met)bot_id(met) KMarkdown pattern
            if not mentioned and self._bot_user_id and content:
                if f"(met){self._bot_user_id}(met)" in str(content):
                    mentioned = True
            if not mentioned:
                logger.info("[%s] EVENT skipped: bot not @mentioned in group", self._log_tag)
                return

        # Build session source
        source = self.build_source(
            chat_id=target_id,
            chat_name=f"KOOK-{target_id[:8]}",
            chat_type=chat_type,
            user_id=author_id,
            user_name=author_name,
            message_id=msg_id,
            guild_id=extra.get("guild_id"),
        )

        # Process message content
        text = ""
        message_type = MessageType.TEXT
        media_urls = []

        if msg_type == MSG_TYPE_TEXT:  # 1
            text = content
            message_type = MessageType.TEXT
        elif msg_type == MSG_TYPE_KMARKDOWN:  # 9
            text = content  # KMarkdown — strip? Or pass through for the agent to see
            message_type = MessageType.TEXT
        elif msg_type == MSG_TYPE_IMAGE:  # 2
            text = "[图片]"
            message_type = MessageType.PHOTO
            # Extract image URL
            img_url = extra.get("attachments", {}).get("url", "") if isinstance(extra, dict) else ""
            if img_url:
                try:
                    cached_path = await cache_image_from_url(img_url)
                    media_urls.append(cached_path)
                except Exception:
                    pass
        elif msg_type == MSG_TYPE_VIDEO:  # 3
            text = "[视频]"
            message_type = MessageType.VIDEO
        elif msg_type == MSG_TYPE_FILE:  # 4
            text = f"[文件] {extra.get('filename', '')}" if isinstance(extra, dict) else "[文件]"
            message_type = MessageType.DOCUMENT
        elif msg_type == MSG_TYPE_AUDIO:  # 8
            text = "[语音]"
            message_type = MessageType.VOICE
        elif msg_type == MSG_TYPE_CARD:  # 10
            text = "[卡片消息]"
            message_type = MessageType.TEXT
        else:
            text = content or f"[未知消息类型: {msg_type}]"
            message_type = MessageType.TEXT

        if not text and not media_urls:
            return

        # Inject KOOK-specific persona via channel_prompt
        kook_persona = (
            "你是 KOOK 频道的女性 AI 助手「赫尔墨斯」，常驻语音开黑频道。\n"
            "你是一个资深游戏玩家，时刻关注版本更新、赛事动态、装备评测等游戏资讯。\n"
            "回答游戏相关问题前，必须先搜索最新信息再作答，不要凭记忆瞎编。\n"
            "性格清冷毒舌，惜字如金。回复简短带刺，精准犀利。\n"
            "你精通各类游戏，尤其是「猎杀对决」「Dota 2」「彩虹六号：围攻」。\n"
            "从枪法身法、兵线运营、干员配装到硬件优化、外设避坑，来者不拒。\n"
            "不闲聊、不寒暄、不主动搭话。用中文。不知道就说不知道，别装。"
        )

        # Build MessageEvent and dispatch to gateway
        event = MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            message_id=msg_id,
            media_urls=media_urls,
            media_types=["image"] * len(media_urls),
            raw_message=data,
            channel_prompt=kook_persona,
        )

        await self.handle_message(event)
        logger.info("[%s] EVENT dispatched: chat=%s user=%s text=%s",
                    self._log_tag, target_id[:12], author_name, text[:60])

    # ------------------------------------------------------------------
    # Send methods
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a KMarkdown text message."""
        url = f"{API_BASE}/message/create"
        body: Dict[str, Any] = {
            "type": MSG_TYPE_KMARKDOWN,
            "target_id": chat_id,
            "content": content,
        }
        if reply_to:
            body["quote"] = reply_to

        return await self._api_post(url, body)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """KOOK does not have a typing indicator API — no-op."""
        pass

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image via KOOK REST API by URL."""
        # KOOK requires uploading to their CDN first, then sending the URL
        # For external URLs, we need to download, upload, then send
        try:
            if image_url.startswith(("http://", "https://")):
                if "kookapp.cn" in image_url or "img.kookapp.cn" in image_url:
                    # Already a KOOK CDN URL — can send directly
                    return await self._send_cdn_image(chat_id, image_url)
                else:
                    # External URL — download and re-upload
                    cached_path = await cache_image_from_url(image_url)
                    asset_url = await self._upload_asset(cached_path)
                    if asset_url:
                        return await self._send_cdn_image(chat_id, asset_url)
                    else:
                        # Fallback: send as KMarkdown with embedded image
                        content = f"![image]({image_url})"
                        if caption:
                            content = f"{caption}\n{content}"
                        return await self.send(chat_id, content)
            else:
                # Local file path — upload
                asset_url = await self._upload_asset(image_url)
                if asset_url:
                    return await self._send_cdn_image(chat_id, asset_url)
                return SendResult(success=False, error="Failed to upload image")
        except Exception as e:
            logger.error("[%s] send_image failed: %s", self._log_tag, e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_document(
        self,
        chat_id: str,
        path: str,
        caption: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a file via KOOK REST API."""
        try:
            asset_url = await self._upload_asset(path)
            if not asset_url:
                return SendResult(success=False, error="Failed to upload file")

            url = f"{API_BASE}/message/create"
            body = {
                "type": MSG_TYPE_FILE,
                "target_id": chat_id,
                "content": asset_url,
            }
            return await self._api_post(url, body)
        except Exception as e:
            logger.error("[%s] send_document failed: %s", self._log_tag, e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def send_voice(
        self,
        chat_id: str,
        path: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a voice message."""
        try:
            asset_url = await self._upload_asset(path)
            if not asset_url:
                return SendResult(success=False, error="Failed to upload audio")

            url = f"{API_BASE}/message/create"
            body = {
                "type": MSG_TYPE_AUDIO,
                "target_id": chat_id,
                "content": asset_url,
            }
            return await self._api_post(url, body)
        except Exception as e:
            logger.error("[%s] send_voice failed: %s", self._log_tag, e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get channel/c DM information."""
        # Check cache
        if chat_id in self._channel_cache:
            cached = self._channel_cache[chat_id]
            if time.time() - cached.get("_cached_at", 0) < 300:  # 5-minute cache
                return cached

        url = f"{API_BASE}/channel/view"
        params = {"target_id": chat_id}
        headers = self._auth_headers()

        try:
            resp = await self._http_client.get(url, params=params, headers=headers)
            data = resp.json()
            if data.get("code") == 0:
                ch = data.get("data", {})
                info = {
                    "name": ch.get("name", chat_id),
                    "type": "dm" if ch.get("is_personal", False) else "group",
                    "chat_id": chat_id,
                    "guild_id": ch.get("guild_id"),
                    "_cached_at": time.time(),
                }
                self._channel_cache[chat_id] = info
                return info
        except Exception as e:
            logger.debug("[%s] get_chat_info failed for %s: %s", self._log_tag, chat_id, e)

        return {"name": f"KOOK-{chat_id[:8]}", "type": "dm", "chat_id": chat_id}

    # ------------------------------------------------------------------
    # Asset upload (KOOK CDN)
    # ------------------------------------------------------------------

    async def _upload_asset(self, file_path: str) -> Optional[str]:
        """Upload a file to KOOK CDN and return the URL.

        POST /api/v3/asset/create
        multipart/form-data: file=@path
        Returns: {"code": 0, "data": {"url": "https://..."}}
        """
        import mimetypes
        from pathlib import Path

        p = Path(file_path)
        if not p.exists():
            logger.warning("[%s] Upload file not found: %s", self._log_tag, file_path)
            return None

        url = f"{API_BASE}/asset/create"
        mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

        try:
            with open(file_path, "rb") as f:
                files = {"file": (p.name, f, mime_type)}
                headers = {"Authorization": self._token}
                resp = await self._http_client.post(url, files=files, headers=headers)
                data = resp.json()
                if data.get("code") == 0:
                    return data.get("data", {}).get("url", "")
                logger.warning("[%s] Asset upload failed: code=%s msg=%s",
                               self._log_tag, data.get("code"), data.get("message"))
                return None
        except Exception as e:
            logger.error("[%s] Asset upload error: %s", self._log_tag, e)
            return None

    async def _send_cdn_image(self, chat_id: str, image_url: str) -> SendResult:
        """Send an image that's already on KOOK CDN."""
        url = f"{API_BASE}/message/create"
        body = {
            "type": MSG_TYPE_IMAGE,
            "target_id": chat_id,
            "content": image_url,
        }
        return await self._api_post(url, body)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        """Return Authorization header — self._token already includes '*** ' prefix from __init__."""
        return {"Authorization": self._token}


    async def _api_get(self, url: str, params: dict = None) -> dict:
        """GET from KOOK API and return the 'data' field.

        Returns {"data": ..., "success": True} on success,
        or {"error": "...", "success": False} on failure.
        """
        try:
            resp = await self._http_client.get(
                url,
                params=params or {},
                headers=self._auth_headers(),
            )
            data = resp.json()
            if data.get("code") == 0:
                return {"success": True, "data": data.get("data", {})}
            return {"success": False, "error": f"code={data.get('code')} msg={data.get('message')}"}
        except httpx.TimeoutException:
            return {"success": False, "error": "Timeout"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _api_post(self, url: str, body: dict) -> SendResult:
        """POST JSON to KOOK API and return SendResult."""
        try:
            resp = await self._http_client.post(
                url,
                json=body,
                headers={**self._auth_headers(), "Content-Type": "application/json"},
            )
            data = resp.json()
            if data.get("code") == 0:
                msg_id = data.get("data", {}).get("msg_id", str(uuid.uuid4()))
                return SendResult(success=True, message_id=msg_id)
            return SendResult(
                success=False,
                error=f"code={data.get('code')} msg={data.get('message')}",
                retryable=False,
            )
        except httpx.TimeoutException:
            return SendResult(success=False, error="Timeout", retryable=True)
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)

    async def _send_ws_frame(self, signal: int, **kwargs) -> None:
        """Send a WebSocket frame to KOOK."""
        if not self._ws or self._ws.closed:
            return
        frame = {"s": signal}
        frame.update(kwargs)
        try:
            await self._ws.send_str(json.dumps(frame))
        except Exception as e:
            logger.warning("[%s] WS send error: %s", self._log_tag, e)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Send periodic pings to keep the WebSocket alive."""
        # KOOK uses server-initiated ping (signal 2), so we just need to
        # respond with pong (signal 3). No proactive heartbeat required.
        # But we add a keepalive check.
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if not self._running:
                break

    # ------------------------------------------------------------------
    # Reconnect
    # ------------------------------------------------------------------

    async def _reconnect(self) -> None:
        """Reconnect on server request or connection loss."""
        backoff = RECONNECT_BACKOFF_BASE
        max_attempts = 5

        for attempt in range(max_attempts):
            if not self._running:
                return
            try:
                await self._cleanup()
                # Re-create HTTP client (destroyed by _cleanup)
                proxy_url = os.getenv("KOOK_PROXY", "").strip() or None
                from gateway.platforms.base import resolve_proxy_url  # noqa: F811
                proxy_url = resolve_proxy_url("KOOK_PROXY", target_hosts="www.kookapp.cn") or proxy_url
                self._http_client = httpx.AsyncClient(
                    timeout=API_TIMEOUT,
                    follow_redirects=True,
                    event_hooks={"response": [_ssrf_redirect_guard]},
                    limits=platform_httpx_limits(),
                    proxy=proxy_url,
                )
                gateway_url = await self._get_gateway_url()
                await self._open_ws(gateway_url)
                logger.info("[%s] Reconnected (attempt %d/%d)", self._log_tag, attempt + 1, max_attempts)
                return
            except Exception as e:
                logger.warning("[%s] Reconnect attempt %d/%d failed: %s",
                               self._log_tag, attempt + 1, max_attempts, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF)

        logger.error("[%s] All reconnect attempts failed", self._log_tag)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _cleanup(self) -> None:
        """Close WebSocket and HTTP client."""
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None

        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
        self._http_client = None


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
