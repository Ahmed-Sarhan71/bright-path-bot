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
from sheets import save_enquiry  # noqa: E402
from privacy import router as privacy_router  # noqa: E402
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
    }
]

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


def run_tool(session: dict, call) -> str:
    """Run a tool the AI asked for and return a result string for the AI."""
    if call.function.name != "save_enquiry":
        return "Unknown tool."
    if session["saved"]:
        return "Enquiry already saved."
    try:
        args = json.loads(call.function.arguments)
        save_enquiry(args["name"], args["phone"], args["course"])
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
app.include_router(privacy_router)