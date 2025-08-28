import os
import json
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv

from livekit import rtc
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents.llm.chat_context import ChatContext, ChatMessage
from livekit.plugins.openai import LLM  # OpenAI-compatible; works with OpenRouter base_url
from supabase import create_client

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("interview-agent")

# --- env
LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_BASE = os.environ.get("OPENROUTER_BASE", "https://openrouter.ai/api/v1")
AGENT_NAME = "text-interviewer"
LLM_MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-3.7-sonnet")  # adjust as needed

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

SYSTEM_PROMPT_TEMPLATE = """You are a corporate recruiter conducting a job interview. Use the follow script as a guidline for this interview. The questions in the script are the main questions. Do not ask follow up questions. Do not make remarks on the cadndidate's awnsers longer then one concise sentence. Once all questions of the script have been awnsered, finish the interview by stating on a dedicated message: 'End of interview'. When you enter the interview, greet the candidate with their name immediately; do not wait for the candidate to greet you first.

Job title:
{job_context}

Interview script:
{script}

"""

def prewarm(_ctx: JobContext = None):
    log.info("Prewarm called — nothing to load right now.")

async def entrypoint(ctx: JobContext):
    await ctx.connect()

    # ---- State
    meta = {}
    chat_ctx = ChatContext() # Initialize without messages
    done = asyncio.Event()
    llm_lock = asyncio.Lock()
    interview_id = None

    # ---- Supabase: create interview row (if metadata provided)
    try:
        meta = json.loads(ctx.job.metadata or "{}")
        if meta:
            interview = sb.table("interviews").insert({
                "candidate_id": meta.get("candidate_id"),
                "process_id": meta.get("process_id", 4),
                "status": "running",
                "room_name": ctx.room.name,
            }).execute()
            interview_id = interview.data[0]["id"]
    except Exception as e:
        log.error(f"Failed to create interview record: {e}")

    # ---- System prompt
    sys_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        job_context=json.dumps(meta.get("job_ctx", {}), ensure_ascii=False),
        script=json.dumps(meta.get("script", []), ensure_ascii=False),
    )
    print(sys_prompt)
    chat_ctx.add_message(role="system", content=sys_prompt)

    # ---- LLM (OpenAI-compatible via OpenRouter)
    llm = LLM(
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE,
        model=LLM_MODEL,
        temperature=0.3,
        user=AGENT_NAME,
    )

    async def save_line(role: str, text: str):
        if not interview_id:
            return
        try:
            sb.table("interview_messages").insert({
                "interview_id": interview_id,
                "role": role,
                "content": text
                # "created_at": datetime.utcnow().isoformat()
            }).execute()
        except Exception as e:
            log.warning(f"Failed to save {role} line: {e}")

    async def send_agent_final(text: str):
        """Send ONE final message chunk to the frontend (no streaming)."""
        payload = json.dumps({"type": "agent_text", "text": text, "final": True})
        # publish_data supports bytes or str; topic can be used to filter on the client
        await ctx.room.local_participant.publish_data(payload, reliable=True, topic="agent_text") 

    async def llm_turn():
        """Call the LLM once, accumulate all tokens, then send single payload."""
        async with llm_lock:
            try:
                # Request a stream but DO NOT forward chunks; we accumulate them.
                stream = llm.chat(chat_ctx=chat_ctx, tool_choice="none")
                full_text_parts = []
                async for piece in stream.to_str_iterable():
                    full_text_parts.append(piece)
                response_text = "".join(full_text_parts).strip()

                # Append to context + persist
                chat_ctx.add_message(role="assistant", content=response_text)
                await save_line("agent", response_text)

                # Send ONE completed message to the UI
                await send_agent_final(response_text)

                # Handle end-of-interview sentinel
                if "End of interview" in response_text:
                    log.info("Interview concluded by agent.")
                    done.set()

            except Exception as e:
                log.exception(f"LLM error: {e}")

    async def handle_user_text(text: str):
        # Append user message, persist, then run a turn
        chat_ctx.add_message(role="user", content=text)
        await save_line("user", text)
        await llm_turn()

    # ---- Data handler (correct event name is 'data_received')
    async def on_data_async(packet: rtc.DataPacket): # Corrected reference
        if done.is_set():
            return
        try:
            if not packet.data:
                log.warning("Received empty data packet, skipping")
                return
            msg = json.loads(packet.data.decode("utf-8"))
            if msg.get("type") == "user_text" and (text := msg.get("text", "")).strip():
                await handle_user_text(text.strip())
        except Exception as e:
            log.error(f"Failed to handle data packet: {e}")

    # Synchronous wrapper for the async handler
    def on_data_sync(packet: rtc.DataPacket):
        asyncio.create_task(on_data_async(packet))

    # Register the handler (room event is 'data_received')
    ctx.room.on("data_received", on_data_sync) 

    # Kick off the interview with a user-role placeholder to trigger the agent's greeting
    chat_ctx.add_message(role="user", content="Begin the interview.")
    await llm_turn()
    await done.wait()

if __name__ == "__main__":
    opts = WorkerOptions(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=prewarm,
        agent_name=AGENT_NAME,
        num_idle_processes=2,
    )
    cli.run_app(opts)
