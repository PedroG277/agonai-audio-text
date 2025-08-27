# dispatch_room.py
import os, asyncio
from livekit import api
from dotenv import load_dotenv

load_dotenv()
LIVEKIT_URL = os.environ.get("LIVEKIT_URL")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET")

async def main(room_name: str, agent_name: str):
    lk = api.LiveKitAPI(url=LIVEKIT_URL, api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET)
    await lk.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(room=room_name, agent_name=agent_name)
    )
    print(f"Dispatch created: {agent_name} -> {room_name}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python dispatch_room.py <room_name> <agent_name>")
        raise SystemExit(2)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
