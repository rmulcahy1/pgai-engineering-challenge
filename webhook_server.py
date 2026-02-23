import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from openai import OpenAI
from scenarios import get_scenario

# FastAPI app Twilio hits for call instructions and speech callbacks.
app = FastAPI()


def load_env(path: str = ".env") -> None:
    # Minimal .env loader (keeps one-command local setup simple).
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                # Keep shell-provided vars if already set.
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def now_utc() -> str:
    # Use UTC timestamps so transcript ordering is unambiguous.
    return datetime.now(timezone.utc).isoformat()


def log_line(call_sid: str, role: str, text: str) -> None:
    # One transcript file per call SID.
    path = Path("transcripts") / f"{call_sid}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{now_utc()}] {role}: {text}\n")


def build_turn_twiml(say_text: str, action_url: str) -> str:
    # Speak bot reply, then listen for next agent utterance.
    safe = escape(say_text)
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<Response>"
        f"<Say>{safe}</Say>"
        f"<Gather input=\"speech\" action=\"{action_url}\" method=\"POST\" "
        "speechTimeout=\"auto\" timeout=\"10\" actionOnEmptyResult=\"true\" />"
        "</Response>"
    )


def build_listen_twiml(action_url: str) -> str:
    # Listen-only TwiML used at call start and for retry-on-silence.
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<Response>"
        f"<Gather input=\"speech\" action=\"{action_url}\" method=\"POST\" "
        "speechTimeout=\"auto\" timeout=\"10\" actionOnEmptyResult=\"true\" />"
        "</Response>"
    )


def build_hangup_twiml(say_text: str) -> str:
    # Final message + hard hangup.
    safe = escape(say_text)
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<Response>"
        f"<Say>{safe}</Say>"
        "<Hangup/>"
        "</Response>"
    )


def should_end_from_agent_text(text: str) -> bool:
    # Simple "agent seems done" heuristic to avoid infinite loops.
    lowered = text.lower()
    end_phrases = ("goodbye", "have a good day", "take care", "bye")
    return any(phrase in lowered for phrase in end_phrases)


def generate_reply_with_openai(
    history: list[dict[str, str]], agent_text: str, scenario: dict[str, object]
) -> str:
    # If no OpenAI key is set, return a deterministic fallback reply.
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return "Can you tell me a little more about that?"

    client = OpenAI(api_key=api_key)
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    base_prompt = os.getenv(
        "OPENAI_SYSTEM_PROMPT",
        "You are a patient calling a healthcare office. Keep replies short, natural, and specific.",
    )
    scenario_prompt = str(scenario.get("prompt", ""))

    # Keep prompt compact by sending only recent turns.
    recent = history[-10:]
    history_text = "\n".join(f"{item['role'].upper()}: {item['text']}" for item in recent)
    prompt = (
        f"{base_prompt}\n"
        f"Scenario objective: {scenario_prompt}\n\n"
        "Conversation so far:\n"
        f"{history_text}\n"
        f"AGENT: {agent_text}\n\n"
        "Reply as the patient in one short sentence."
    )

    response = client.responses.create(model=model, input=prompt)
    return (response.output_text or "Can you repeat that?").strip()


load_env()
# Safety guard to prevent unbounded conversations.
MAX_TURNS = int(os.getenv("MAX_TURNS", "8"))
# In-memory state for active calls (keyed by Twilio CallSid).
call_state: dict[str, dict[str, object]] = {}


@app.post("/incoming-call")
async def incoming_call(request: Request) -> PlainTextResponse:
    # Twilio hits this endpoint first when a call connects.
    form = await request.form()
    call_sid = str(form.get("CallSid") or "unknown")
    scenario_id = int(request.query_params.get("scenario", "1"))
    scenario = get_scenario(scenario_id)
    # Start per-call state and transcript header lines.
    call_state[call_sid] = {"turns": 0, "silence": 0, "history": [], "scenario": scenario}
    log_line(call_sid, "SYSTEM", "call started")
    log_line(call_sid, "SYSTEM", f"scenario {scenario['id']}: {scenario['name']}")

    # Tell Twilio to listen for speech and POST it to /heard.
    base = str(request.base_url).rstrip("/")
    twiml = build_listen_twiml(f"{base}/heard")
    return PlainTextResponse(twiml, media_type="text/xml")


@app.post("/heard")
async def heard(request: Request) -> PlainTextResponse:
    # Twilio sends recognized speech here after each <Gather>.
    form = await request.form()
    call_sid = str(form.get("CallSid") or "unknown")
    base = str(request.base_url).rstrip("/")
    action_url = f"{base}/heard"

    # Recover current call state; create default if needed.
    state = call_state.setdefault(call_sid, {"turns": 0, "silence": 0, "history": [], "scenario": get_scenario(1)})
    turns = int(state["turns"])
    silence = int(state["silence"])
    history = state["history"] if isinstance(state["history"], list) else []
    scenario = state.get("scenario") if isinstance(state.get("scenario"), dict) else get_scenario(1)

    agent_text = (form.get("SpeechResult") or "").strip()
    if agent_text:
        # Normal turn with captured agent speech.
        silence = 0
        log_line(call_sid, "AGENT", agent_text)
        history.append({"role": "agent", "text": agent_text})
    else:
        # Handle silence / no transcript cases.
        silence += 1
        agent_text = "[no speech captured]"
        log_line(call_sid, "AGENT", agent_text)
        print("HEARD:", agent_text)
        if turns == 0 and silence < 2:
            # Early silence: retry listening once before ending.
            state["silence"] = silence
            state["history"] = history
            return PlainTextResponse(build_listen_twiml(action_url), media_type="text/xml")
        if silence >= 2:
            # Repeated silence: end gracefully.
            end_reply = "I can call back later. Goodbye."
            log_line(call_sid, "PATIENT", end_reply)
            log_line(call_sid, "SYSTEM", "call ended (silence)")
            call_state.pop(call_sid, None)
            return PlainTextResponse(build_hangup_twiml(end_reply), media_type="text/xml")

    print("HEARD:", agent_text)

    if should_end_from_agent_text(agent_text):
        # Agent said a closing phrase, so wrap up.
        end_reply = "Thanks so much. Goodbye."
        log_line(call_sid, "PATIENT", end_reply)
        log_line(call_sid, "SYSTEM", "call ended (agent goodbye)")
        call_state.pop(call_sid, None)
        return PlainTextResponse(build_hangup_twiml(end_reply), media_type="text/xml")

    # Generate next patient turn with OpenAI.
    reply = generate_reply_with_openai(history, agent_text, scenario)
    turns += 1
    log_line(call_sid, "PATIENT", reply)
    history.append({"role": "patient", "text": reply})

    if turns >= MAX_TURNS:
        # Hard stop to avoid runaway loops.
        end_reply = "Thanks, that answers my questions. Goodbye."
        log_line(call_sid, "PATIENT", end_reply)
        log_line(call_sid, "SYSTEM", "call ended (max turns)")
        call_state.pop(call_sid, None)
        return PlainTextResponse(build_hangup_twiml(end_reply), media_type="text/xml")

    # Persist state for the next /heard callback.
    state["turns"] = turns
    state["silence"] = silence
    state["history"] = history

    # Speak reply and listen for next utterance.
    twiml = build_turn_twiml(reply, action_url)
    return PlainTextResponse(twiml, media_type="text/xml")


@app.post("/call-status")
async def call_status(request: Request) -> dict[str, bool]:
    # Twilio sends final call status updates here.
    form = await request.form()
    call_sid = str(form.get("CallSid") or "unknown")
    status = str(form.get("CallStatus") or "unknown")
    log_line(call_sid, "SYSTEM", f"call ended (twilio status: {status})")

    if status in {"completed", "busy", "failed", "no-answer", "canceled"}:
        # Ensure no stale state remains after terminal statuses.
        call_state.pop(call_sid, None)

    return {"ok": True}
