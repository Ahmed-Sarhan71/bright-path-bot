"""2-day follow-up: message people who asked about a course but have not registered.
 
Runs in a background thread. Turn it on with FOLLOWUP_ENABLED=true.
A follow-up is sent only when all of these are true:
  - the row's "Registered" cell is "No"
  - the enquiry is at least FOLLOWUP_DELAY_HOURS old (default 48)
  - the row has a WhatsApp number and "Follow-up sent" is empty
  - the bot is not paused for that customer (a team member took over)
  - it is inside opening hours (Saturday to Thursday, 9 am to 9 pm Dubai time)
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
 
import httpx
 
import db
import sheets
 
logger = logging.getLogger("bright-path.followup")
 
DUBAI = ZoneInfo("Asia/Dubai")
 
 
def _enabled() -> bool:
    return os.getenv("FOLLOWUP_ENABLED", "").strip().lower() in ("1", "true", "yes")
 
 
def _open_now(now: datetime) -> bool:
    # Saturday to Thursday, 9 am to 9 pm. Friday (weekday 4) is closed.
    return now.weekday() != 4 and 9 <= now.hour < 21
 
 
def is_due(row: dict, now: datetime, delay_hours: float) -> bool:
    if row.get("Registered", "").strip().lower() != "no":
        return False
    if row.get("Follow-up sent") or not row.get("WhatsApp", "").isdigit():
        return False
    try:
        asked_at = datetime.strptime(row["Timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=DUBAI)
    except (KeyError, ValueError):
        return False
    return now - asked_at >= timedelta(hours=delay_hours)
 
 
def send_template(to: str, language: str, name: str, course: str) -> tuple[bool, str]:
    """Send the approved follow-up template. Returns (ok, detail)."""
    arabic = language == "ar"
    template = os.getenv(
        "FOLLOWUP_TEMPLATE_AR" if arabic else "FOLLOWUP_TEMPLATE_EN",
        "course_followup_ar" if arabic else "course_followup_en",
    )
    version = os.getenv("WHATSAPP_API_VERSION", "v23.0")
    url = f"https://graph.facebook.com/{version}/{os.environ['WHATSAPP_PHONE_NUMBER_ID']}/messages"
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": template,
                    "language": {"code": "ar" if arabic else "en"},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                {"type": "text", "text": name or "there"},
                                {"type": "text", "text": course},
                            ],
                        }
                    ],
                },
            },
            timeout=20,
        )
    except httpx.HTTPError as exc:
        return False, f"network error: {exc}"
    if response.status_code >= 400:
        return False, f"{response.status_code} {response.text[:300]}"
    try:
        db.record_sent(response.json()["messages"][0]["id"])
    except (ValueError, KeyError, IndexError, TypeError):
        pass
    return True, "sent"
 
 
def run_once() -> int:
    """Check the Sheet once and send the follow-ups that are due. Returns how many were sent."""
    now = datetime.now(DUBAI)
    if not _open_now(now):
        return 0
    delay = float(os.getenv("FOLLOWUP_DELAY_HOURS", "48"))
    sent = 0
    for row in sheets.pending_rows():
        if not is_due(row, now, delay):
            continue
        if db.is_paused(f"wa:{row['WhatsApp']}"):
            continue  # a team member is handling this customer
        ok, detail = send_template(row["WhatsApp"], row.get("Language", ""), row.get("Name", ""), row.get("Course", ""))
        if ok:
            sheets.mark_followup(row["_row"], now.strftime("%Y-%m-%d %H:%M:%S"))
            sent += 1
        else:
            # Mark the failure so we do not retry (and fail) every cycle. Clear the cell to retry.
            logger.error("Follow-up to row %s failed: %s", row["_row"], detail)
            sheets.mark_followup(row["_row"], "failed")
    return sent
 
 
def _loop() -> None:
    minutes = float(os.getenv("FOLLOWUP_CHECK_MINUTES", "30"))
    while True:
        try:
            count = run_once()
            if count:
                logger.info("Sent %s follow-up(s)", count)
        except Exception:
            logger.exception("Follow-up check failed")
        time.sleep(minutes * 60)
 
 
def start_background() -> None:
    """Start the checker thread (does nothing unless FOLLOWUP_ENABLED=true)."""
    if not _enabled():
        logger.info("Follow-ups are off (set FOLLOWUP_ENABLED=true to turn them on)")
        return
    threading.Thread(target=_loop, name="followups", daemon=True).start()
 