import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from openai import OpenAI  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from context import build_system_message  # noqa: E402
import db  # noqa: E402
from notify import send_push  # noqa: E402
from sheets import save_enquiry  # noqa: E402
from privacy import router as privacy_router  # noqa: E402
from takeover import make_link, router as takeover_router  # noqa: E402
from followup import start_background as start_followups  # noqa: E402
from whatsapp import build_router  # noqa: E402

logger = logging.getLogger("bright-path")

COURSES = [
    "Excel for Business",
    "Python Basics",
    "Digital Marketing",
    "PMP Exam Prep",
    "Graphic Design",
]

# Provider settings live in .env, so switching provider means editing 3 values.
client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")

# Built once at startup. Keeping it identical on every request lets prompt caching work.
SYSTEM_MESSAGE = build_system_message()

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_enquiry",
            "description": (
                "Save a course enquiry once you have the person's name, "
                "phone number and course of interest. Call it only once per person."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Person's full name"},
                    "phone": {"type": "string", "description": "Phone number"},
                    "course": {"type": "string", "enum": COURSES},
                },
                "required": ["name", "phone", "course"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify_team",
            "description": (
                "Alert the team on their phone when the person asks a question about Bright Path "
                "Training Centre, its courses, enrolment or payment that you cannot answer from the "
                "center information. Never call it for unrelated questions (personal questions "
                "about you, currency conversion, general knowledge, small talk). "
                "Call it once per unanswered question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The person's question, in their words"},
                    "name": {"type": "string", "description": "Person's name, if known"},
                    "phone": {"type": "string", "description": "Person's phone number, if known"},
                },
                "required": ["question"],
            },
        },
    },
]

# Stops a chatty person from flooding the team's phone.
MAX_ALERTS_PER_SESSION = 3

# Sessions are stored in SQLite, so they survive restarts.
db.init_db()

app = FastAPI(title="Bright Path Course Assistant")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo only; restrict to the website's domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str


def alerts_sent(session: dict) -> int:
    """How many notify_team calls this chat has made so far (counted from the history)."""
    return sum(
        1
        for m in session["messages"]
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
        if tc.get("function", {}).get("name") == "notify_team"
    )


def run_notify_team(session: dict, call) -> str:
    if alerts_sent(session) > MAX_ALERTS_PER_SESSION:  # the current call is already in the history
        return "The team was already alerted. Just tell the person a team member will follow up."
    try:
        args = json.loads(call.function.arguments)
        question = str(args["question"])
    except (ValueError, KeyError):
        return "Error: missing question."

    lines = [f"Question: {question}"]
    if args.get("name"):
        lines.append(f"Name: {args['name']}")
    if args.get("phone"):
        lines.append(f"Phone: {args['phone']}")

    url = url_title = None
    session_id = session.get("id", "")
    if session_id.startswith("wa:"):
        number = session_id[3:]
        lines.append(f"WhatsApp: +{number}")
        link = make_link(number)
        if link:
            # One tap: pauses the bot for this customer, then opens their WhatsApp chat.
            url, url_title = link, "Take over chat (pauses bot)"
        else:
            url, url_title = f"https://wa.me/{number}", "Open WhatsApp chat"
    else:
        lines.append("Channel: web chat")

    sent = send_push("Bright Path: unanswered question", "\n".join(lines), url, url_title)
    if not sent:
        return "The alert could not be sent. Still tell the person a team member will follow up."
    return "The team was alerted. Tell the person a team member will follow up."


def run_tool(session: dict, call) -> str:
    """Run a tool the AI asked for and return a result string for the AI."""
    if call.function.name == "notify_team":
        return run_notify_team(session, call)
    if call.function.name != "save_enquiry":
        return "Unknown tool."
    if session["saved"]:
        return "Enquiry already saved."
    try:
        args = json.loads(call.function.arguments)
        session_id = session.get("id", "")
        whatsapp = session_id[3:] if session_id.startswith("wa:") else ""
        # "ar" if the person wrote in Arabic, so the follow-up uses the right template.
        wrote_arabic = any(
            "؀" <= ch <= "ۿ"
            for m in session["messages"]
            if m.get("role") == "user"
            for ch in str(m.get("content", ""))
        )
        save_enquiry(
            args["name"], args["phone"], args["course"],
            language="ar" if wrote_arabic else "en", whatsapp=whatsapp,
        )
    except Exception:
        logger.exception("Failed to save enquiry")
        return "Error: could not save the enquiry. Tell the person a team member will follow up."
    session["saved"] = True
    return "Enquiry saved successfully."


@app.get("/health")
def health():
    return {"status": "ok"}


def reply_to(session_id: str, text: str) -> str:
    """Load the session, get the AI's reply, and save the session. Used by web chat and WhatsApp."""
    session = db.get_session(session_id)
    session["id"] = session_id  # lets tools know which chat this is
    try:
        return answer(session, text).reply
    finally:
        # Save even if the AI call fails, so the history and "saved" flag are never lost.
        db.save_session(session_id, session)


# A plain `def` (not async) so FastAPI runs the blocking API calls in a thread pool.
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    return ChatResponse(reply=reply_to(req.session_id, req.message))


def answer(session: dict, text: str) -> ChatResponse:
    messages = session["messages"]
    messages.append({"role": "user", "content": text})

    # The AI may call the tool, then needs one more turn to answer the person.
    for _ in range(3):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": SYSTEM_MESSAGE}] + messages,
                tools=TOOLS,
                temperature=0.3,
            )
        except Exception:
            logger.exception("LLM request failed")
            raise HTTPException(status_code=502, detail="The assistant is unavailable right now.")

        msg = response.choices[0].message

        if not msg.tool_calls:
            reply = msg.content or ""
            messages.append({"role": "assistant", "content": reply})
            return ChatResponse(reply=reply)

        messages.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": run_tool(session, call)}
            )

    raise HTTPException(status_code=500, detail="The assistant could not finish the reply.")


# WhatsApp webhook (GET to verify, POST for incoming messages).
app.include_router(build_router(reply_to))

# Privacy policy page (Meta requires a policy URL to publish the app).
app.include_router(privacy_router)

# Link in the Pushover alert that pauses the bot and opens the customer's chat.
app.include_router(takeover_router)

# 2-day follow-up checker (off unless FOLLOWUP_ENABLED=true).
start_followups()