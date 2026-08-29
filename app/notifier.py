"""Feishu text notification without any LLM.

Reads credentials from environment variables first (FEISHU_APP_ID /
FEISHU_APP_SECRET / FEISHU_OPEN_ID), falling back to QwenPaw's
~/.qwenpaw/config.json channels.feishu settings. Sends via Feishu OpenAPI
im/v1/messages (tenant_access_token). Never prints secrets.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from app.config import Settings

logger = logging.getLogger(__name__)


def _read_qwenpaw_feishu_config() -> dict[str, str]:
    config_path = Path.home() / ".qwenpaw" / "config.json"
    if not config_path.exists():
        return {}
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        try:
            import json5  # type: ignore

            cfg = json5.load(config_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cannot read feishu config from %s: %s", config_path, exc)
            return {}
    feishu = (cfg or {}).get("channels", {}).get("feishu", {}) or {}
    return {
        "app_id": str(feishu.get("app_id", "")),
        "app_secret": str(feishu.get("app_secret", "")),
        "open_id": str(feishu.get("open_id", "")),
    }


def resolve_credentials(settings: Settings) -> tuple[str, str, str]:
    """Return (app_id, app_secret, open_id); empty strings mean not configured.
    Env vars win per-field, missing fields fall back to ~/.qwenpaw/config.json."""
    env_id, env_secret, env_openid = (
        settings.feishu_app_id,
        settings.feishu_app_secret,
        settings.feishu_open_id,
    )
    cfg = _read_qwenpaw_feishu_config() if not (env_id and env_secret) else {}
    app_id = env_id or cfg.get("app_id", "")
    app_secret = env_secret or cfg.get("app_secret", "")
    open_id = env_openid or cfg.get("open_id", "")
    return app_id, app_secret, open_id


def _tenant_token(app_id: str, app_secret: str) -> str:
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": app_id, "app_secret": app_secret}, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"feishu token error: {data.get('msg', data)}")
    return data["tenant_access_token"]


def send_text(settings: Settings, text: str, open_id: str | None = None) -> bool:
    """Send a plain-text message to the configured Feishu user.

    Returns True on success, False when credentials are absent or sending
    failed (so the caller can decide to just log it).
    """
    app_id, app_secret, default_open_id = resolve_credentials(settings)
    receiver = open_id or default_open_id
    if not app_id or not app_secret or not receiver:
        logger.warning("feishu not configured, skip notification")
        return False
    try:
        token = _tenant_token(app_id, app_secret)
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {
            "receive_id": receiver,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("feishu send failed: %s", data.get("msg", data))
            return False
        logger.info("feishu notification sent")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("feishu send error: %s", exc)
        return False