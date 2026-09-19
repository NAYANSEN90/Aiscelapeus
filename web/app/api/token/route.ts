import { NextRequest, NextResponse } from "next/server";
import {
  AccessToken,
  RoomAgentDispatch,
  RoomConfiguration,
} from "livekit-server-sdk";
import { createHash, timingSafeEqual } from "node:crypto";
import { liveAccessPolicy } from "@/lib/liveAccessPolicy";

/**
 * Mints a short-lived LiveKit access token.
 *
 * Security note (PRD 11 / mentor recommendation on the Voice Gateway):
 * this route is the auth boundary for the media plane. It issues a token scoped
 * to exactly one room and one identity, with a TTL measured in minutes, and it
 * validates every input before signing. It is deliberately the only place in the
 * web app that touches LIVEKIT_API_SECRET.
 *
 * Production additionally requires separate responder and clinician access
 * codes. They are a demo boundary, not user identity: a real deployment still
 * needs OAuth2/JWT, clinician RBAC and rate limiting at the gateway.
 */

export const dynamic = "force-dynamic";

type Role = "responder" | "clinician";

const ROOM_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_-]{2,63}$/;
const IDENTITY_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
const TOKEN_TTL_SECONDS = 15 * 60;

function accessCodeMatches(presented: unknown, configured: string): boolean {
  if (typeof presented !== "string") return false;
  const expected = createHash("sha256").update(configured).digest();
  const actual = createHash("sha256").update(presented).digest();
  return timingSafeEqual(expected, actual);
}

export async function POST(req: NextRequest) {
  const apiKey = process.env.LIVEKIT_API_KEY;
  const apiSecret = process.env.LIVEKIT_API_SECRET;
  const livekitUrl = process.env.NEXT_PUBLIC_LIVEKIT_URL ?? process.env.LIVEKIT_URL;

  if (!apiKey || !apiSecret || !livekitUrl) {
    return NextResponse.json(
      { error: "Server is missing LiveKit configuration" },
      { status: 500 }
    );
  }

  let body: { room?: unknown; identity?: unknown; role?: unknown; accessCode?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Body must be JSON" }, { status: 400 });
  }

  const room = typeof body.room === "string" ? body.room.trim() : "";
  const identity = typeof body.identity === "string" ? body.identity.trim() : "";
  if (body.role !== "clinician" && body.role !== "responder") {
    return NextResponse.json({ error: "Role must be responder or clinician" }, { status: 400 });
  }
  const role: Role = body.role;

  if (!ROOM_PATTERN.test(room)) {
    return NextResponse.json(
      { error: "Invalid room name. Use 3-64 characters: letters, digits, dash, underscore." },
      { status: 400 }
    );
  }

  if (!IDENTITY_PATTERN.test(identity)) {
    return NextResponse.json(
      { error: "Invalid identity. Use 1-64 characters: letters, digits, dash, underscore." },
      { status: 400 }
    );
  }

  const accessPolicy = liveAccessPolicy(process.env);
  if (accessPolicy.problems.length > 0) {
    return NextResponse.json(
      { error: "Server live access policy is invalid" },
      { status: 500 },
    );
  }
  const configuredAccessCode =
    role === "clinician" ? accessPolicy.clinicianCode : accessPolicy.responderCode;
  if (accessPolicy.required && !configuredAccessCode) {
    return NextResponse.json({ error: "Server live access policy is invalid" }, { status: 500 });
  }
  if (configuredAccessCode && !accessCodeMatches(body.accessCode, configuredAccessCode)) {
    return NextResponse.json({ error: "Invalid live access code" }, { status: 401 });
  }

  const token = new AccessToken(apiKey, apiSecret, {
    identity,
    name: identity,
    ttl: TOKEN_TTL_SECONDS,
    metadata: JSON.stringify({ role }),
  });

  // LiveKit applies token room configuration only when that token creates the
  // room. Either supported role may arrive first, so both carry the same named
  // dispatch. A later join to an existing room does not create a second job.
  token.roomConfig = new RoomConfiguration({
    agents: [new RoomAgentDispatch({ agentName: "aiscelapeus" })],
  });

  token.addGrant({
    room,
    roomJoin: true,
    canPublish: true,
    canSubscribe: true,
    // Only the agent writes triage state. Human participants read it.
    canPublishData: false,
  });

  return NextResponse.json(
    {
      token: await token.toJwt(),
      url: livekitUrl,
      room,
      identity,
      role,
      expiresIn: TOKEN_TTL_SECONDS,
    },
    { headers: { "cache-control": "no-store" } }
  );
}
