import logging
import os

import httpx

logger = logging.getLogger("bright-path.notify")


def send_push(title: str, message: str, url: str | None = None, url_title: str | None = None) -> bool:
    """Send a push notification to the team's phone with Pushover. Returns True if it was sent."""
    token = os.getenv("PUSHOVER_TOKEN")
    user = os.getenv("PUSHOVER_USER_KEY")
    if not token or not user:
        logger.warning("Pushover is not configured (PUSHOVER_TOKEN / PUSHOVER_USER_KEY)")
        return False

    data = {
        "token": token,
        "user": user,
        "title": title[:250],
        "message": message[:1024],  # Pushover's limit
    }
    if url:
        data["url"] = url
        if url_title:
            data["url_title"] = url_title

    try:
        response = httpx.post("https://api.pushover.net/1/messages.json", data=data, timeout=10)
    except httpx.HTTPError:
        logger.exception("Pushover request failed")
        return False
    if response.status_code != 200:
        logger.error("Pushover rejected the message: %s %s", response.status_code, response.text)
        return False
    return True