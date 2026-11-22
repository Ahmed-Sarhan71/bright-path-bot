import hashlib
import hmac
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

import db

router = APIRouter()


def _sign(number: str) -> str | None:
    secret = os.getenv("TAKEOVER_SECRET")
    if not secret:
        return None
    return hmac.new(secret.encode(), number.encode(), hashlib.sha256).hexdigest()[:32]


def make_link(number: str) -> str | None:
    """Link that pauses the bot for this customer and opens their WhatsApp chat.

    It goes in the Pushover alert. Returns None if PUBLIC_BASE_URL or TAKEOVER_SECRET is not set.
    """
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    sig = _sign(number)
    if not base or not sig:
        return None
    return f"{base}/takeover?chat={number}&sig={sig}"


@router.get("/takeover")
def takeover(chat: str, sig: str):
    """Pause the bot in one chat, then send the team member to that WhatsApp chat."""
    expected = _sign(chat)
    if not chat.isdigit() or expected is None or not hmac.compare_digest(expected, sig):
        raise HTTPException(status_code=403, detail="Invalid link")
    db.pause_bot(f"wa:{chat}", float(os.getenv("BOT_PAUSE_HOURS", "12")))
    return RedirectResponse(f"https://wa.me/{chat}")