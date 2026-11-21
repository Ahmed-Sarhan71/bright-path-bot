import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

CONTACT = os.getenv("PRIVACY_CONTACT_EMAIL", "your-email@example.com")

PAGE = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy - Bright Path Course Assistant</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; line-height: 1.6; color: #222; }}
  h1 {{ font-size: 1.6rem; }} h2 {{ font-size: 1.15rem; margin-top: 1.6rem; }}
</style>
</head>
<body>
<h1>Privacy Policy</h1>
<p>This policy explains how the Bright Path Training Centre course assistant ("the assistant") handles your information when you message it on WhatsApp or on our website chat.</p>

<h2>What we collect</h2>
<p>The messages you send to the assistant, your WhatsApp phone number, and the details you give us when you ask about a course: your name, phone number and the course you are interested in.</p>

<h2>How we use it</h2>
<p>To answer your questions about our courses, to record your enquiry so a team member can contact you, and to follow up about your enquiry. We do not sell your information.</p>

<h2>Who processes it</h2>
<p>Your messages are processed by service providers that run the assistant: Meta (WhatsApp), an AI model provider that generates the replies, Google (Google Sheets, where enquiries are stored) and our hosting provider. These providers handle data on our behalf.</p>

<h2>How long we keep it</h2>
<p>We keep enquiry details and chat history only as long as needed to handle your enquiry. You can ask us to delete your data at any time.</p>

<h2>Your choices</h2>
<p>You can stop messaging the assistant at any time. To ask for a copy of your data or to have it deleted, contact us at the email below.</p>

<h2>Contact</h2>
<p>Bright Path Training Centre, Al Barsha, Dubai. Email: <a href="mailto:{CONTACT}">{CONTACT}</a></p>
</body>
</html>"""


@router.get("/privacy", response_class=HTMLResponse)
def privacy():
    return PAGE