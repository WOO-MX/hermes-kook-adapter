"""WebSocket connection, listen loop, frame handling, reconnect, and cleanup."""

import asyncio
import json
import logging
import os
import time
import zlib
from typing import Optional

from .config_helpers import AIOHTTP_AVAILABLE, HTTPX_AVAILABLE
from .config_helpers import aiohttp, httpx
from .constants import (
    API_BASE, API_TIMEOUT,
    SIGNAL_EVENT, SIGNAL_HELLO, SIGNAL_PING, SIGNAL_PONG, SIGNAL_RECONNECT,
    MSG_TYPE_TEXT, MSG_TYPE_IMAGE, MSG_TYPE_VIDEO, MSG_TYPE_FILE,
    MSG_TYPE_AUDIO, MSG_TYPE_KMARKDOWN, MSG_TYPE_CARD,
    CHANNEL_TYPE_GROUP,
    DEDUP_WINDOW_SECONDS,
    HEARTBEAT_INTERVAL,
    RECONNECT_BACKOFF_BASE, MAX_RECONNECT_BACKOFF,
)
from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    _ssrf_redirect_guard,
    cache_image_from_url,
    safe_url_for_log,
)
from gateway.platforms._http_client_limits import platform_httpx_limits

logger = logging.getLogger(__name__)


class KookWebSocketMixin:
    """Mixin providing WebSocket lifecycle for KookAdapter.

    Expects the adapter to provide these attributes (set in __init__):
      - self._token, self._log_tag
      - self._http_client (httpx.AsyncClient)
      - self._ws, self._session (aiohttp)
      - self._listen_task, self._heartbeat_task
      - self._sn, self._session_id
      - self._seen_msg_ids, self._bot_user_id, self._last_msg_id
      - self._running (bool)
      - self._mark_connected(), self._mark_disconnected()
      - self._set_fatal_error(key, msg, retryable)
      - self._release_platform_lock()
    """

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
                    break
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error("[%s] WebSocket error, reconnecting...", self._log_tag)
                await self._reconnect()
                if not self._ws:
                    break

        logger.info("[%s] Listen loop ended", self._log_tag)

    async def _handle_frame(self, frame: dict) -> None:
        """Process a single WebSocket frame from KOOK."""
        signal = frame.get("s", -1)

        if signal == SIGNAL_HELLO:
            session_id = frame.get("d", {}).get("session_id", "?")
            logger.info("[%s] Signal HELLO received, session_id=%s", self._log_tag, session_id)

        elif signal == SIGNAL_PING:
            self._sn = frame.get("sn", self._sn)
            await self._send_ws_frame(SIGNAL_PONG, sn=self._sn)

        elif signal == SIGNAL_RECONNECT:
            logger.info("[%s] Server requested reconnect", self._log_tag)
            await self._reconnect()

        elif signal == SIGNAL_EVENT:
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
        target_id = data.get("target_id", "")
        msg_id = data.get("msg_id", "")
        msg_type = data.get("type", 0)
        content = data.get("content", "")
        author_id = data.get("author_id", "")
        extra = data.get("extra", {})
        extra_author = (extra or {}).get("author", {}) if isinstance(extra, dict) else {}
        author_name = extra_author.get("username", "") or author_id or "unknown"

        logger.info("[%s] EVENT detail: self=%s type=%s ch_type=%s target=%s author=%s(%s) content=%s",
                    self._log_tag, data.get("self"), msg_type, channel_type,
                    target_id[:12] if target_id else "", author_id[:12] if author_id else "?",
                    author_name, (content or "")[:80])

        # Skip messages from our own bot
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
            stale = [k for k, v in self._seen_msg_ids.items() if now - v > DEDUP_WINDOW_SECONDS * 2]
            for k in stale:
                del self._seen_msg_ids[k]

        if not target_id or not author_id:
            logger.info("[%s] EVENT skipped: missing target_id or author_id", self._log_tag)
            return

        if msg_id:
            self._last_msg_id[target_id] = msg_id

        # Access control
        if not self._allow_all and self._allowed_users:
            if author_id not in self._allowed_users:
                logger.info("[%s] EVENT skipped: user %s not in allowlist", self._log_tag, author_id[:12])
                return

        chat_type = "group" if channel_type == CHANNEL_TYPE_GROUP else "dm"

        # @mention gate (GROUP only)
        if chat_type == "group":
            mentioned = False
            if isinstance(extra, dict):
                mention_list = extra.get("mention") or []
                mention_all = extra.get("mention_all", False)
                if self._bot_user_id and self._bot_user_id in mention_list:
                    mentioned = True
                if mention_all:
                    mentioned = True
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

        if msg_type == MSG_TYPE_TEXT:
            text = content
            message_type = MessageType.TEXT
        elif msg_type == MSG_TYPE_KMARKDOWN:
            text = content
            message_type = MessageType.TEXT
        elif msg_type == MSG_TYPE_IMAGE:
            text = "[图片]"
            message_type = MessageType.PHOTO
            img_url = extra.get("attachments", {}).get("url", "") if isinstance(extra, dict) else ""
            if img_url:
                try:
                    cached_path = await cache_image_from_url(img_url)
                    media_urls.append(cached_path)
                except Exception:
                    pass
        elif msg_type == MSG_TYPE_VIDEO:
            text = "[视频]"
            message_type = MessageType.VIDEO
        elif msg_type == MSG_TYPE_FILE:
            text = f"[文件] {extra.get('filename', '')}" if isinstance(extra, dict) else "[文件]"
            message_type = MessageType.DOCUMENT
        elif msg_type == MSG_TYPE_AUDIO:
            text = "[语音]"
            message_type = MessageType.VOICE
        elif msg_type == MSG_TYPE_CARD:
            text = "[卡片消息]"
            message_type = MessageType.TEXT
        else:
            text = content or f"[未知消息类型: {msg_type}]"
            message_type = MessageType.TEXT

        if not text and not media_urls:
            return

        kook_persona = (
            "你是 KOOK 频道的女性 AI 助手「赫尔墨斯」，常驻语音开黑频道。\n"
            "你是一个资深游戏玩家，时刻关注版本更新、赛事动态、装备评测等游戏资讯。\n"
            "回答游戏相关问题前，必须先搜索最新信息再作答，不要凭记忆瞎编。\n"
            "性格清冷毒舌，惜字如金。回复简短带刺，精准犀利。\n"
            "你精通各类游戏，尤其是「猎杀对决」「Dota 2」「彩虹六号：围攻」。\n"
            "从枪法身法、兵线运营、干员配装到硬件优化、外设避坑，来者不拒。\n"
            "不闲聊、不寒暄、不主动搭话。用中文。不知道就说不知道，别装。"
        )

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
    # WebSocket helpers
    # ------------------------------------------------------------------

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
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if not self._running:
                break

    # ------------------------------------------------------------------
    # Reconnect
    # ------------------------------------------------------------------

    async def _reconnect(self) -> None:
        """Reconnect on server request or connection loss."""
        from gateway.platforms.base import resolve_proxy_url

        backoff = RECONNECT_BACKOFF_BASE
        max_attempts = 5

        for attempt in range(max_attempts):
            if not self._running:
                return
            try:
                await self._cleanup()
                proxy_url = os.getenv("KOOK_PROXY", "").strip() or None
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
