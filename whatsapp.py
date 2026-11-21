import hashlib
import hmac
import json
import logging
import os
import re

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

import db

logger = logging.getLogger("bright-path.whatsapp")

FALLBACK = (
    "Sorry, something went wrong. A team member will follow up.\n"
    "عذراً، حدث خطأ. سيتواصل معك أحد أعضاء الفريق."
)
UNSUPPORTED = (
    "Sorry, I can only read text messages. Please type your question.\n"
    "عذراً، أستطيع قراءة الرسائل النصية فقط. من فضلك اكتب سؤالك."
)


def to_whatsapp_format(text: str) -> str:
    """Turn common Markdown into WhatsApp formatting (WhatsApp bold is *one* asterisk)."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.DOTALL)  # **bold** -> *bold*
    text = re.sub(r"__(.+?)__", r"*\1*", text, flags=re.DOTALL)  # __bold__ -> *bold*
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)  # # Title -> *Title*
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.MULTILINE)  # list markers -> bullets
    return text


def send_text(to: str, body: str) -> None:
    """Send a WhatsApp text message through Meta's Cloud API."""
    body = to_whatsapp_format(body)
    version = os.getenv("WHATSAPP_API_VERSION", "v23.0")
    url = f"https://graph.facebook.com/{version}/{os.environ['WHATSAPP_PHONE_NUMBER_ID']}/messages"
    response = httpx.post(
        url,
        headers={"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        },
        timeout=20,
    )
    if response.status_code >= 400:
        logger.error("WhatsApp send failed: %s %s", response.status_code, response.text)
        return
    # Remember the id, so if Meta echoes this message back we know the bot sent it, not a human.
    try:
        db.record_sent(response.json()["messages"][0]["id"])
    except (ValueError, KeyError, IndexError, TypeError):
        pass


def _valid_signature(body: bytes, header: str | None) -> bool:
    """Check that the request really comes from Meta (only if WHATSAPP_APP_SECRET is set)."""
    secret = os.getenv("WHATSAPP_APP_SECRET")
    if not secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def process_message(get_reply, message: dict) -> None:
    """Handle one incoming WhatsApp message. Runs in the background after we answer Meta."""
    try:
        sender = message["from"]
        # Meta retries webhooks, so skip messages we have already handled.
        if not db.claim_message(message["id"]):
            return
        session_id = f"wa:{sender}"
        if db.is_paused(session_id):
            # A team member is handling this chat. Keep the message in the history, but stay quiet.
            if message.get("type") == "text":
                db.add_message(session_id, "user", message["text"]["body"])
            return
        if message.get("type") != "text":
            send_text(sender, UNSUPPORTED)
            return
        try:
            reply = get_reply(f"wa:{sender}", message["text"]["body"])
        except Exception:
            logger.exception("Failed to get an AI reply")
            reply = FALLBACK
        send_text(sender, reply)
    except Exception:
        logger.exception("Failed to process WhatsApp message")


def process_echo(echo: dict) -> None:
    """A message sent from the WhatsApp Business app (a human replied). Pause the bot in that chat."""
    try:
        customer = echo.get("to")
        message_id = echo.get("id", "")
        if not customer or db.was_sent_by_bot(message_id):
            return
        if echo.get("type") in ("revoke", "edit"):
            return  # deleting or editing an old message is not a new reply
        session_id = f"wa:{customer}"
        db.pause_bot(session_id, float(os.getenv("BOT_PAUSE_HOURS", "12")))
        if echo.get("type") == "text":
            # Add the team member's reply to the history, so the bot has the full picture later.
            db.add_message(session_id, "assistant", echo["text"]["body"])
    except Exception:
        logger.exception("Failed to process message echo")


def build_router(get_reply) -> APIRouter:
    """get_reply(session_id, text) -> reply text. It is passed in so this file stays independent."""
    router = APIRouter()

    @router.get("/whatsapp")
    def verify(request: Request):
        """Meta calls this once when you save the webhook, to check the verify token."""
        params = request.query_params
        expected = os.getenv("WHATSAPP_VERIFY_TOKEN")
        if (
            expected
            and params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == expected
        ):
            return PlainTextResponse(params.get("hub.challenge", ""))
        raise HTTPException(status_code=403, detail="Verification failed")

    @router.post("/whatsapp")
    async def receive(request: Request, background: BackgroundTasks):
        body = await request.body()
        if not _valid_signature(body, request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(status_code=403, detail="Bad signature")
        try:
            payload = json.loads(body)
        except ValueError:
            return {"status": "ignored"}

        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []):
                    # Answer Meta right away; the AI reply is sent in the background.
                    background.add_task(process_message, get_reply, message)
                for echo in value.get("message_echoes", []):
                    background.add_task(process_echo, echo)
        return {"status": "ok"}

    return router