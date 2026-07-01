"""Standalone sender and interactive setup — usable without a live gateway."""

import os
import logging

from .constants import API_BASE, API_TIMEOUT, MSG_TYPE_KMARKDOWN

logger = logging.getLogger(__name__)


async def _standalone_send(chat_id: str, content: str, extra: dict) -> dict:
    """Send a message via KOOK REST API without a live adapter."""
    from .config_helpers import HTTPX_AVAILABLE, httpx
    from gateway.platforms.helpers import strip_markdown

    if not HTTPX_AVAILABLE:
        return {"error": "httpx not installed"}

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
