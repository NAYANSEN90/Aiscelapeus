import { afterEach, describe, expect, it } from "vitest";
import { GET } from "./route";

const KEYS = [
  "LIVEKIT_API_KEY",
  "LIVEKIT_API_SECRET",
  "LIVEKIT_URL",
  "NEXT_PUBLIC_LIVEKIT_URL",
  "RESPONDER_ACCESS_CODE",
  "CLINICIAN_ACCESS_CODE",
  "NODE_ENV",
] as const;
const ORIGINAL = Object.fromEntries(KEYS.map((key) => [key, process.env[key]]));
const mutableEnv = process.env as Record<string, string | undefined>;

afterEach(() => {
  for (const key of KEYS) {
    const value = ORIGINAL[key];
    if (value === undefined) delete mutableEnv[key];
    else mutableEnv[key] = value;
  }
});

describe("GET /api/health", () => {
  it("fails closed and names missing configuration without returning values", async () => {
    for (const key of KEYS) delete process.env[key];

    const response = GET();

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      status: "not_ready",
      missing: ["LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_URL"],
    });
  });

  it("reports ready when the server can mint a usable LiveKit token", async () => {
    process.env.LIVEKIT_API_KEY = "configured";
    process.env.LIVEKIT_API_SECRET = "configured";
    process.env.LIVEKIT_URL = "wss://livekit.invalid";
    delete process.env.NEXT_PUBLIC_LIVEKIT_URL;

    const response = GET();

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(await response.json()).toEqual({ status: "ok" });
  });

  it("accepts the public LiveKit URL used by the token response", async () => {
    process.env.LIVEKIT_API_KEY = "configured";
    process.env.LIVEKIT_API_SECRET = "configured";
    delete process.env.LIVEKIT_URL;
    process.env.NEXT_PUBLIC_LIVEKIT_URL = "wss://livekit.invalid";

    expect(GET().status).toBe(200);
  });

  it("fails closed when a production live role has no access policy", async () => {
    mutableEnv.NODE_ENV = "production";
    process.env.LIVEKIT_API_KEY = "configured";
    process.env.LIVEKIT_API_SECRET = "configured";
    process.env.LIVEKIT_URL = "wss://livekit.invalid";
    process.env.RESPONDER_ACCESS_CODE = "responder-code-2026";
    delete process.env.CLINICIAN_ACCESS_CODE;

    const response = GET();
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      status: "not_ready",
      missing: ["CLINICIAN_ACCESS_CODE"],
    });
  });

  it.each([
    ["equal", "same-access-code-2026", "same-access-code-2026"],
    ["blank", "                    ", "clinician-code-2026"],
    ["weak", "short", "clinician-code-2026"],
  ])("reports an invalid %s production access policy", async (_case, responder, clinician) => {
    mutableEnv.NODE_ENV = "production";
    process.env.LIVEKIT_API_KEY = "configured";
    process.env.LIVEKIT_API_SECRET = "configured";
    process.env.LIVEKIT_URL = "wss://livekit.invalid";
    process.env.RESPONDER_ACCESS_CODE = responder;
    process.env.CLINICIAN_ACCESS_CODE = clinician;

    const response = GET();
    expect(response.status).toBe(503);
    expect((await response.json()).status).toBe("not_ready");
  });
});
