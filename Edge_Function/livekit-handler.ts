// deno-lint-ignore-file no-explicit-any
import { serve } from "https://deno.land/std/http/server.ts";
import { AccessToken } from "https://esm.sh/livekit-server-sdk@^2";
const LK_KEY = Deno.env.get("LIVEKIT_API_KEY");
const LK_SECRET = Deno.env.get("LIVEKIT_API_SECRET");
const LK_URL = Deno.env.get("LIVEKIT_URL");
const LK_EGRESS_URL = Deno.env.get("LIVEKIT_EGRESS_URL") ?? LK_URL; // optional override
const AGENT_NAME = "interviewer-agent"; // must match your worker
serve(async (req)=>{
  try {
    const contentType = req.headers.get("content-type") || "";
    const body = contentType.includes("application/json") ? await req.json() : {};
    const action = String(body.action ?? "").toLowerCase();
    const mode = String(body.mode);
    // ttl (seconds)
    const ttlSec = Number(body.ttlSec ?? 3600);
    if (!Number.isFinite(ttlSec) || ttlSec <= 0) {
      return bad(400, "ttlSec must be a positive number (seconds)");
    }
    // PARTICIPANT TOKEN (auto-dispatch agent on participant connect) ===
    if (action === "participant-token") {
      const roomName = String(body.roomName || "");
      const identity = String(body.identity || "");
      if (!roomName || !identity) {
        return bad(400, "roomName and identity are required");
      }
      // This metadata is delivered to your Python agent at ctx.job.metadata
      const agentMetadata = body.agentMetadata ?? body.metadata ?? {};
      const metadataJson = JSON.stringify(agentMetadata);
      // Optional participant-visible metadata
      const participantMetadata = JSON.stringify(body.participantMetadata ?? {});
      const at = new AccessToken(LK_KEY, LK_SECRET, {
        identity,
        ttl: ttlSec,
        metadata: participantMetadata
      });
      at.addGrant({
        roomJoin: true,
        room: roomName,
        canPublish: true,
        canSubscribe: true,
        canPublishData: true
      });
      // v2 SDK accepts a plain object for roomConfig; no RoomAgentDispatch import required
      let agent_name = "";
      if (mode === "text") {
        agent_name = "text-interviewer";
      }
      if (mode === "voice") {
        agent_name = "voice-interviewer";
      }
      at.roomConfig = {
        agents: [
          {
            agentName: agent_name,
            metadata: metadataJson
          }
        ]
      };
      const token = await at.toJwt();
      return json({
        token,
        url: LK_URL
      });
    }
    return bad(400, "Unknown action. Use one of: admin-token, participant-token, dispatch-agent, start-file-egress, stop-egress");
  } catch (e) {
    return json({
      error: String(e)
    }, 400);
  }
});
// Helpers
function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json"
    }
  });
}
function bad(status, message) {
  return json({
    error: message
  }, status);
}
