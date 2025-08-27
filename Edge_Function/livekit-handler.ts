// deno-lint-ignore-file no-explicit-any
import { serve } from "https://deno.land/std/http/server.ts";
import { AccessToken, EgressClient, AgentDispatchClient } from "https://esm.sh/livekit-server-sdk@^2";
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
    // ttl (seconds)
    const ttlSec = Number(body.ttlSec ?? 3600);
    if (!Number.isFinite(ttlSec) || ttlSec <= 0) {
      return bad(400, "ttlSec must be a positive number (seconds)");
    }
    // === 1) ADMIN TOKEN (for room mgmt, egress, etc.) ===
    if (action === "admin-token") {
      const at = new AccessToken(LK_KEY, LK_SECRET, {
        ttl: ttlSec
      });
      at.addGrant({
        roomCreate: true,
        roomAdmin: true,
        egress: true,
        roomList: true
      });
      const token = await at.toJwt();
      return json({
        token
      });
    }
    // === 2) PARTICIPANT TOKEN (auto-dispatch agent on participant connect) ===
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
      at.roomConfig = {
        agents: [
          {
            agentName: AGENT_NAME,
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
    // === 3) EXPLICIT DISPATCH (optional alternative to token-based dispatch) ===
    if (action === "dispatch-agent") {
      const roomName = String(body.roomName || "");
      if (!roomName) return bad(400, "roomName required");
      const agentMetadata = body.agentMetadata ?? {};
      const client = new AgentDispatchClient(LK_URL, LK_KEY, LK_SECRET);
      const res = await client.createDispatch(roomName, AGENT_NAME, {
        metadata: JSON.stringify(agentMetadata)
      });
      return json({
        ok: true,
        dispatch: res
      });
    }
    // === 4) EGRESS CONTROL (optional) ===
    if (action === "start-file-egress") {
      const roomName = String(body.roomName || "");
      if (!roomName) return bad(400, "roomName required");
      const client = new EgressClient(LK_EGRESS_URL, LK_KEY, LK_SECRET);
      const file = {
        fileType: "MP4",
        filepath: `recordings/${roomName}-${Date.now()}.mp4`
      };
      const res = await client.startRoomCompositeEgress({
        roomName,
        audioOnly: true,
        file
      });
      return json({
        ok: true,
        egressId: res.egressId
      });
    }
    if (action === "stop-egress") {
      const egressId = String(body.egressId || "");
      if (!egressId) return bad(400, "egressId required");
      const client = new EgressClient(LK_EGRESS_URL, LK_KEY, LK_SECRET);
      await client.stopEgress(egressId);
      return json({
        ok: true
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
