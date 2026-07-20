"""Send methods, asset upload, and HTTP helpers."""

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .config_helpers import httpx
from .constants import (
    API_BASE, API_TIMEOUT,
    MSG_TYPE_KMARKDOWN, MSG_TYPE_IMAGE, MSG_TYPE_FILE, MSG_TYPE_AUDIO,
)
from gateway.platforms.base import SendResult, cache_image_from_url

logger = logging.getLogger(__name__)


class KookMessagingMixin:
    """Mixin providing message-sending and API helpers for KookAdapter.

    Expects the adapter to provide these attributes (set in __init__):
      - self._token, self._log_tag
      - self._http_client (httpx.AsyncClient)
      - self._channel_cache, self._last_msg_id
      - self._mark_disconnected()
      - self._set_fatal_error(key, msg, retryable)
      - self._release_platform_lock()
    """

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
        try:
            if image_url.startswith(("http://", "https://")):
                if "kookapp.cn" in image_url:
                    return await self._send_cdn_image(chat_id, image_url)
                else:
                    cached_path = await cache_image_from_url(image_url)
                    asset_url = await self._upload_asset(cached_path)
                    if asset_url:
                        return await self._send_cdn_image(chat_id, asset_url)
                    else:
                        content = f"![image]({image_url})"
                        if caption:
                            content = f"{caption}\n{content}"
                        return await self.send(chat_id, content)
            else:
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
        """Get channel/DM information."""
        if chat_id in self._channel_cache:
            cached = self._channel_cache[chat_id]
            if time.time() - cached.get("_cached_at", 0) < 300:
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
    # HTTP helpers
    # ------------------------------------------------------------------

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
