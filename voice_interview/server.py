# server.py
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
import subprocess, os, sys

app = FastAPI()

class StartReq(BaseModel):
    room: str
    identity: str
    candidate_id: str
    process_id: str
    instructions: Optional[str] = None

DEFAULT_INSTRUCTIONS = ("You are a friendly interviewer...")

@app.post("/start")
async def start(req: StartReq):
    env = os.environ.copy()
    env["AGENT_INSTRUCTIONS"] = req.instructions or DEFAULT_INSTRUCTIONS
    env["AGENT_CANDIDATE_ID"] = req.candidate_id
    env["AGENT_PROCESS_ID"]   = req.process_id
    # optional: where to POST turns (n8n webhook)
    # env["TURN_WEBHOOK_URL"] = "https://n8n.example.com/webhook/interview/turn"

    cmd = [sys.executable, "-u", "/app/agent_entry.py", "connect", "--room", req.room]
    subprocess.Popen(cmd, env=env)
    return {"ok": True, "room": req.room, "identity": req.identity}
