# agent_entry.py
import os
import re
import json
import uuid
import time
import asyncio
import datetime
from collections import deque
from typing import Optional, Any, Dict, Tuple

import httpx
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    function_tool,
    RunContext,
    RoomInputOptions,
    RoomOutputOptions,
    RoomIO,
    AutoSubscribe,
)
from livekit.plugins.openai import realtime

# =========================
# Realtime model config
# =========================
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-4o-mini-realtime-preview")
DEFAULT_VOICE = os.getenv("REALTIME_VOICE", "verse")

DEFAULT_INSTRUCTIONS = os.getenv("AGENT_INSTRUCTIONS") or (
    "Say only: 'A problem occurred. End of interview.' "
)

# =========================
# Webhook (optional)
# =========================
TURN_WEBHOOK_URL = os.getenv("TURN_WEBHOOK_URL", "")

# =========================
# Optional ENV fallbacks for IDs
# =========================
def _maybe_int(v: Any) -> Optional[int]:
    try:
        if v is None or isinstance(v, bool):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None

CANDIDATE_ID_INT_ENV = _maybe_int(os.getenv("AGENT_CANDIDATE_ID", ""))
PROCESS_ID_INT_ENV = _maybe_int(os.getenv("AGENT_PROCESS_ID", ""))

# =========================
# Supabase config
# =========================
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SERVICE_ROLE = (
    os.getenv("SUPABASE_SERVICE_ROLE")
    or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or ""
)

SB_INTERVIEWS_URL = f"{SUPABASE_URL}/rest/v1/interviews" if SUPABASE_URL else ""
SB_MESSAGES_URL = f"{SUPABASE_URL}/rest/v1/interview_messages" if SUPABASE_URL else ""

SB_HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE or "",
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}" if SUPABASE_SERVICE_ROLE else "",
    "Content-Type": "application/json",
}

if not SUPABASE_URL:
    print("[Supabase] Missing SUPABASE_URL – interview persistence disabled.")
if not SUPABASE_SERVICE_ROLE:
    print("[Supabase] Missing SUPABASE_SERVICE_ROLE – interview persistence disabled.")
else:
    print("[Supabase] Service role present, REST inserts enabled.")

# =========================
# De-dupe helper (short window)
# =========================
class RecentDeduper:
    def __init__(self, window_seconds: float = 5.0, max_items: int = 200):
        self.window = window_seconds
        self.q: deque[Tuple[str, float]] = deque(maxlen=max_items)
        self.set = set()

    def _prune(self):
        now = time.time()
        while self.q and (now - self.q[0][1]) > self.window:
            key, _ = self.q.popleft()
            self.set.discard(key)

    def seen(self, key: str) -> bool:
        self._prune()
        if key in self.set:
            return True
        self.set.add(key)
        self.q.append((key, time.time()))
        return False

deduper = RecentDeduper()

# =========================
# Supabase helpers
# =========================
async def _sb_get_one(url: str, params: Dict[str, str]) -> Optional[dict]:
    if not (url and SUPABASE_SERVICE_ROLE):
        return None
    q = {**params, "limit": 1, "order": "started_at.desc", "select": "*"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=SB_HEADERS, params=q)
        if r.status_code >= 300:
            print("Supabase GET error:", r.status_code, r.text, "| params:", q)
            return None
        rows = r.json()
        return rows[0] if rows else None
    except Exception as e:
        print("Supabase GET exception:", repr(e), "| params:", q)
        return None


async def _sb_insert_returning_one(url: str, payload: dict) -> Optional[dict]:
    if not (url and SUPABASE_SERVICE_ROLE):
        return None
    headers = {**SB_HEADERS, "Prefer": "return=representation"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, headers=headers, json=payload)
        if r.status_code >= 300:
            print("Supabase INSERT error:", r.status_code, r.text, "| payload:", payload)
            return None
        rows = r.json()
        return rows[0] if rows else None
    except Exception as e:
        print("Supabase INSERT exception:", repr(e), "| payload:", payload)
        return None


async def _sb_insert_messages(payload: dict) -> None:
    if not (SB_MESSAGES_URL and SUPABASE_SERVICE_ROLE):
        return
    headers = {**SB_HEADERS, "Prefer": "return=minimal"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(SB_MESSAGES_URL, headers=headers, json=payload)
        if r.status_code >= 300:
            print("Supabase INSERT message error:", r.status_code, r.text, "| payload:", payload)
    except Exception as e:
        print("Supabase INSERT message exception:", repr(e), "| payload:", payload)


async def _sb_update_interview(interview_id: int, fields: dict) -> None:
    if not (SB_INTERVIEWS_URL and SUPABASE_SERVICE_ROLE):
        return
    headers = {**SB_HEADERS, "Prefer": "return=minimal"}
    params = {"id": f"eq.{interview_id}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.patch(SB_INTERVIEWS_URL, headers=headers, params=params, json=fields)
        if r.status_code >= 300:
            print("Supabase UPDATE error:", r.status_code, r.text, "| fields:", fields)
    except Exception as e:
        print("Supabase UPDATE exception:", repr(e), "| fields:", fields)

# =========================
# Webhook helper
# =========================
async def _post_webhook(payload: dict):
    if not (TURN_WEBHOOK_URL and (payload.get("text") or "").strip()):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(TURN_WEBHOOK_URL, json=payload)
        print("POSTed turn:", r.status_code, payload.get("client_turn_id"))
        if r.status_code >= 300:
            print("POST error body:", r.text)
    except Exception as e:
        print("POST exception:", repr(e))

# =========================
# Interview lifecycle
# =========================
async def get_or_create_running_interview(
    room_name: str,
    candidate_id: Optional[int],
    process_id: Optional[int],
) -> Optional[int]:
    """
    Finds a running interview by (room_name, candidate_id, process_id) if provided.
    Otherwise creates one.
    """
    params = {"room_name": f"eq.{room_name}", "status": "eq.running"}
    if candidate_id is not None:
        params["candidate_id"] = f"eq.{candidate_id}"
    if process_id is not None:
        params["process_id"] = f"eq.{process_id}"

    existing = await _sb_get_one(SB_INTERVIEWS_URL, params)
    if existing and "id" in existing:
        return existing["id"]

    insert_payload = {"room_name": room_name, "status": "running"}
    if candidate_id is not None:
        insert_payload["candidate_id"] = candidate_id
    if process_id is not None:
        insert_payload["process_id"] = process_id

    created = await _sb_insert_returning_one(SB_INTERVIEWS_URL, insert_payload)
    if created and "id" in created:
        print("Created interview id:", created["id"])
        return created["id"]

    print("Failed to get or create interview.")
    return None


async def log_turn_to_db_and_webhook(
    interview_id: Optional[int],
    room_name: str,
    role: str,       # "candidate" | "agent" | "system"
    content: str,
    candidate_id: Optional[int],
    process_id: Optional[int],
    participant_identity: Optional[str] = None,
):
    if not (content or "").strip():
        return

    # ---- de-dupe (role+content within window) ----
    dedupe_key = f"{role}::{content.strip()}"
    if deduper.seen(dedupe_key):
        return

    # 1) Insert into interview_messages (if interview exists)
    if interview_id is not None:
        msg_payload = {
            "interview_id": interview_id,
            "role": role,
            "content": content,
            "ts": datetime.datetime.utcnow().isoformat(),
        }
        await _sb_insert_messages(msg_payload)

    # 2) Forward to webhook if configured
    webhook_payload = {
        "room": room_name,
        "candidate_id": candidate_id,
        "process_id": process_id,
        "speaker": role,
        "text": content,
        "ts": datetime.datetime.utcnow().isoformat(),
        "client_turn_id": str(uuid.uuid4()),
    }
    if participant_identity:
        webhook_payload["participant_identity"] = participant_identity
    await _post_webhook(webhook_payload)

# =========================
# Utilities
# =========================
def _extract_text_from_item(item) -> Optional[str]:
    if not item:
        return None
    text = getattr(item, "text_content", None) or getattr(item, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    parts = []
    for c in getattr(item, "content", []) or []:
        if isinstance(c, dict):
            t = c.get("text") or c.get("content") or c.get("value")
        else:
            t = getattr(c, "text", None) or getattr(c, "content", None) or getattr(c, "value", None)
        if isinstance(t, str) and t.strip():
            parts.append(t)
    return "\n".join(parts) if parts else None


def _parse_int_from_identity(identity: Optional[str]) -> Optional[int]:
    if not identity:
        return None
    m = re.search(r"(\d+)$", identity)
    return int(m.group(1)) if m else None


ROOM_ID_PATTERNS = [
    re.compile(r'^[a-z]+-(\d+)-(\d+)$', re.I),   # e.g., int-4-1 -> (process=4, candidate=1)
    re.compile(r'^.*?:?(\d+)-(\d+)$'),          # e.g., anything:4-1
]

def _parse_ids_from_room_name(room: str) -> tuple[Optional[int], Optional[int]]:
    if not isinstance(room, str):
        return (None, None)
    for pat in ROOM_ID_PATTERNS:
        m = pat.match(room.strip())
        if m:
            try:
                return (int(m.group(1)), int(m.group(2)))
            except Exception:
                pass
    return (None, None)


def _read_job_payload(ctx: JobContext) -> dict:
    """
    Read a flat dict passed to /start. We intentionally ignore job.metadata.
    Accept either ctx.job.input (recommended) or ctx.job.body/params wrappers.
    """
    # Prefer ctx.job.input if it's already a dict
    inp = getattr(ctx.job, "input", None)
    if isinstance(inp, dict) and inp:
        return inp

    # Sometimes frameworks pass a JSON string
    if isinstance(inp, str):
        try:
            obj = json.loads(inp)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    # Fallbacks: try to find a dict-ish "body" in common wrappers
    for key in ("body", "params", "data", "payload"):
        v = getattr(ctx.job, key, None)
        if isinstance(v, dict) and v:
            return v
        if isinstance(v, str):
            try:
                obj = json.loads(v)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

    # LAST resort: see if ctx itself carries a 'body' attribute
    v = getattr(ctx, "body", None)
    if isinstance(v, dict) and v:
        return v
    if isinstance(v, str):
        try:
            obj = json.loads(v)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    return {}

def _get_first(payload: dict, *keys, default=None):
    """Return first present key from payload (supports snake_case & camelCase)."""
    for k in keys:
        if k in payload and payload[k] is not None:
            return payload[k]
    return default

# =========================
# Agent
# =========================
class InterviewAgent(Agent):
    """Agent with a tool the model may call to log the user's transcript."""
    def __init__(self, *, room_name: str, interview_id: Optional[int],
                 candidate_id: Optional[int], process_id: Optional[int],
                 identity: Optional[str], **kwargs):
        super().__init__(**kwargs)
        self._room_name = room_name
        self._interview_id = interview_id
        self._candidate_id = candidate_id
        self._process_id = process_id
        self._identity = identity

    @function_tool(
        name="log_user_transcript",
        description=(
            "Log a verbatim transcript of the user's most recent utterance. "
            "Preserve the original language; do not summarize or add commentary."
        ),
    )
    async def log_user_transcript(self, ctx: RunContext, text: str) -> dict:
        # Candidate turns: primary path (tool). De-duper will avoid duplicates.
        await log_turn_to_db_and_webhook(
            interview_id=self._interview_id,
            room_name=self._room_name,
            role="candidate",
            content=text,
            candidate_id=self._candidate_id,
            process_id=self._process_id,
            participant_identity=self._identity,
        )
        return {"ok": True}

# =========================
# Entrypoint
# =========================
async def entrypoint(ctx: JobContext):
    # Connect (audio only)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Read POST /start body (flat JSON supported)
    payload = _read_job_payload(ctx)
    print("[/start payload]", payload)

    # Accept snake_case & camelCase & the exact keys you send
    candidate_id_from_body = _maybe_int(_get_first(payload, "candidate_id", "candidateId"))
    process_id_from_body   = _maybe_int(_get_first(payload, "process_id", "processId"))
    identity_override      = _get_first(payload, "identity", "participant_identity", "participantIdentity")
    room_name_override     = _get_first(payload, "room", "room_name", "roomName")
    instructions_override  = _get_first(payload, "instructions", "system_prompt", "systemPrompt")

    # Wait for the participant to join
    participant = await ctx.wait_for_participant()

    # Determine room_name (prefer body override)
    room_name = room_name_override or getattr(ctx.room, "name", None) or str(ctx.room)

    # Effective identity for logging
    effective_identity = identity_override or getattr(participant, "identity", None)

    # Resolve IDs: body -> env -> identity -> room-name fallback
    candidate_id_final = (
        candidate_id_from_body
        or CANDIDATE_ID_INT_ENV
        or _parse_int_from_identity(effective_identity)
    )
    process_id_final = process_id_from_body or PROCESS_ID_INT_ENV

    # Fallback from room name like "int-4-1" (process=4, candidate=1)
    if process_id_final is None or candidate_id_final is None:
        p_from_room, c_from_room = _parse_ids_from_room_name(room_name)
        if process_id_final is None:
            process_id_final = p_from_room
        if candidate_id_final is None:
            candidate_id_final = c_from_room

    print(f"[Interview IDs] candidate_id={candidate_id_final} process_id={process_id_final} room={room_name}")

    # Create or reuse interview row
    interview_id = await get_or_create_running_interview(
        room_name=room_name,
        candidate_id=candidate_id_final,
        process_id=process_id_final,
    )
    if interview_id is None:
        print("Failed to get or create interview.")

    # Ensure IDs are patched into the row if they exist (prevents lingering NULLs)
    patch_fields = {}
    if candidate_id_final is not None:
        patch_fields["candidate_id"] = candidate_id_final
    if process_id_final is not None:
        patch_fields["process_id"] = process_id_final
    if patch_fields and interview_id is not None:
        await _sb_update_interview(interview_id, patch_fields)

    # Build session with server-side realtime model
    session = AgentSession(
        vad=None,
        llm=realtime.RealtimeModel(
            model=REALTIME_MODEL,
            voice=DEFAULT_VOICE,
        ),
    )

    # Wire up RoomIO
    room_io = RoomIO(
        agent_session=session,
        room=ctx.room,
        participant=participant,
        input_options=RoomInputOptions(text_enabled=False),
        output_options=RoomOutputOptions(transcription_enabled=False, audio_enabled=True),
    )
    await room_io.start()

    # Determine final system instructions
    instructions_final = (instructions_override or "").strip() or DEFAULT_INSTRUCTIONS

    # Instantiate agent with interview context
    agent = InterviewAgent(
        room_name=room_name,
        interview_id=interview_id,
        candidate_id=candidate_id_final,
        process_id=process_id_final,
        identity=effective_identity,
        instructions=instructions_final,
    )

    # ===== Events =====
    @session.on("user_input_transcribed")
    def _on_user_transcribed(ev):
        # Fallback logging for candidate turns if the tool doesn't fire (deduped).
        is_final = getattr(ev, "is_final", False)
        transcript = getattr(ev, "transcript", None)
        print("user_input_transcribed:", is_final, repr(transcript))
        if is_final:
            txt = (transcript or "").strip()
            if txt:
                asyncio.create_task(
                    log_turn_to_db_and_webhook(
                        interview_id=interview_id,
                        room_name=room_name,
                        role="candidate",
                        content=txt,
                        candidate_id=candidate_id_final,
                        process_id=process_id_final,
                        participant_identity=getattr(ev, "speaker_id", None) or effective_identity,
                    )
                )

    @session.on("conversation_item_added")
    def _on_item(ev):
        item = getattr(ev, "item", None)
        role = getattr(item, "role", None)  # "assistant" | "user" | "system"
        txt = _extract_text_from_item(item) or ""
        print("conversation_item_added:", role, repr(txt[:120]))

        # Define variations of the end phrase
        end_phrases = ["end of interview", "end of the interview"]
        is_end_phrase = any(phrase in txt.lower() for phrase in end_phrases)

        # Map to DB roles (we ignore "user"; candidate is logged via tool & fallback)
        if role == "assistant":
            mapped_role = "agent"
        elif role == "system":
            mapped_role = "system"
        else:
            mapped_role = None

        if mapped_role and txt:
            asyncio.create_task(
                log_turn_to_db_and_webhook(
                    interview_id=interview_id,
                    room_name=room_name,
                    role=mapped_role,
                    content=txt,
                    candidate_id=candidate_id_final,
                    process_id=process_id_final,
                    participant_identity=effective_identity,
                )
            )
            
        if role == "assistant":
            mapped_role = "agent"
            if is_end_phrase:
                print("Agent said an end phrase, closing room.")
                asyncio.create_task(ctx.shutdown())

    @session.on("error")
    def _on_error(ev):
        print("SESSION ERROR:", getattr(ev, "error", ev))

    # On session closed: mark interview completed
    @session.on("closed")
    def _on_closed(ev):
        if interview_id is not None:
            asyncio.create_task(
                _sb_update_interview(
                    interview_id,
                    {"status": "completed", "ended_at": datetime.datetime.utcnow().isoformat()},
                )
            )

    await session.start(agent, room=ctx.room)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
