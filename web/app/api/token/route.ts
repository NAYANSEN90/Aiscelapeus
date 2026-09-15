import { NextRequest, NextResponse } from "next/server";
import { AccessToken } from "livekit-server-sdk";

/**
 * Mints a short-lived LiveKit access token.
 *
 * Security note (PRD 11 / mentor recommendation on the Voice Gateway):
 * this route is the auth boundary for the media plane. It issues a token scoped
 * to exactly one room and one identity, with a TTL measured in minutes, and it
 * validates every input before signing. It is deliberately the only place in the
 * web app that touches LIVEKIT_API_SECRET.
 *
 * Before production this needs a real identity check in front of it (OAuth2/JWT
 * from the responder app, RBAC for the clinician role) plus rate limiting at the
 * gateway. The shape below is what that check would wrap, not a replacement for it.
 */

export const dynamic = "force-dynamic";

type Role = "responder" | "clinician";

const ROOM_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_-]{2,63}$/;
const IDENTITY_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/;
const TOKEN_TTL_SECONDS = 15 * 60;

export async function POST(req: NextRequest) {
  const apiKey = process.env.LIVEKIT_API_KEY;
  const apiSecret = process.env.LIVEKIT_API_SECRET;

  if (!apiKey || !apiSecret) {
    return NextResponse.json(
      { error: "Server is missing LIVEKIT_API_KEY or LIVEKIT_API_SECRET" },
      { status: 500 }
    );
  }

  let body: { room?: unknown; identity?: unknown; role?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Body must be JSON" }, { status: 400 });
  }

  const room = typeof body.room === "string" ? body.room.trim() : "";
  const identity = typeof body.identity === "string" ? body.identity.trim() : "";
  const role: Role = body.role === "clinician" ? "clinician" : "responder";

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

  const token = new AccessToken(apiKey, apiSecret, {
    identity,
    name: identity,
    ttl: TOKEN_TTL_SECONDS,
    metadata: JSON.stringify({ role }),
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
      url: process.env.NEXT_PUBLIC_LIVEKIT_URL ?? process.env.LIVEKIT_URL ?? null,
      room,
      identity,
      role,
      expiresIn: TOKEN_TTL_SECONDS,
    },
    { headers: { "cache-control": "no-store" } }
  );
}
