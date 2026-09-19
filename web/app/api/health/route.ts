import { NextResponse } from "next/server";
import { liveAccessPolicy } from "@/lib/liveAccessPolicy";

export const dynamic = "force-dynamic";

export function GET() {
  const missing: string[] = [];
  if (!process.env.LIVEKIT_API_KEY) missing.push("LIVEKIT_API_KEY");
  if (!process.env.LIVEKIT_API_SECRET) missing.push("LIVEKIT_API_SECRET");
  if (!(process.env.NEXT_PUBLIC_LIVEKIT_URL ?? process.env.LIVEKIT_URL)) {
    missing.push("LIVEKIT_URL");
  }
  missing.push(...liveAccessPolicy(process.env).problems);

  if (missing.length > 0) {
    return NextResponse.json(
      { status: "not_ready", missing },
      { status: 503, headers: { "cache-control": "no-store" } }
    );
  }

  return NextResponse.json(
    { status: "ok" },
    { headers: { "cache-control": "no-store" } }
  );
}
