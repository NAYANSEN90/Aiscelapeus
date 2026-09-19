import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocked = vi.hoisted(() => ({
  instances: [] as Array<{
    apiKey: string;
    apiSecret: string;
    options: Record<string, unknown>;
    grants: Array<Record<string, unknown>>;
  }>,
}));

vi.mock("livekit-server-sdk", () => ({
  AccessToken: class {
    private readonly instance: (typeof mocked.instances)[number];

    constructor(apiKey: string, apiSecret: string, options: Record<string, unknown>) {
      this.instance = { apiKey, apiSecret, options, grants: [] };
      mocked.instances.push(this.instance);
    }

    addGrant(grant: Record<string, unknown>) {
      this.instance.grants.push(grant);
    }

    async toJwt() {
      return "signed-test-token";
    }
  },
}));

import { POST } from "./route";

const ENV_KEYS = ["LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_URL", "NEXT_PUBLIC_LIVEKIT_URL"] as const;
const originalEnv = Object.fromEntries(ENV_KEYS.map((key) => [key, process.env[key]]));

function request(body: unknown, contentType = "application/json") {
  return new NextRequest("http://localhost/api/token", {
    method: "POST",
    headers: { "content-type": contentType },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

beforeEach(() => {
  mocked.instances.length = 0;
  process.env.LIVEKIT_API_KEY = "test-key";
  process.env.LIVEKIT_API_SECRET = "test-secret";
  process.env.LIVEKIT_URL = "wss://livekit.invalid";
  delete process.env.NEXT_PUBLIC_LIVEKIT_URL;
});

afterEach(() => {
  for (const key of ENV_KEYS) {
    const value = originalEnv[key];
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
});

describe("POST /api/token", () => {
  it.each(ENV_KEYS.slice(0, 3))("fails closed when %s is missing", async (key) => {
    delete process.env[key];
    const response = await POST(request({ room: "inc-123", identity: "responder-1", role: "responder" }));
    expect(response.status).toBe(500);
    expect(mocked.instances).toHaveLength(0);
  });

  it("rejects malformed JSON without constructing a token", async () => {
    const response = await POST(request("{"));
    expect(response.status).toBe(400);
    expect(mocked.instances).toHaveLength(0);
  });

  it.each([
    { body: { room: "x", identity: "responder-1", role: "responder" }, field: "room" },
    { body: { room: "inc-123", identity: "bad identity", role: "responder" }, field: "identity" },
    { body: { room: "inc-123", identity: "responder-1", role: "administrator" }, field: "role" },
    { body: { room: "inc-123", identity: "responder-1" }, field: "role" },
  ])("rejects invalid $field input", async ({ body }) => {
    const response = await POST(request(body));
    expect(response.status).toBe(400);
    expect(mocked.instances).toHaveLength(0);
  });

  it.each(["responder", "clinician"] as const)("mints one narrowly scoped %s token", async (role) => {
    const response = await POST(request({ room: "inc-123", identity: `${role}-1`, role }));
    const payload = await response.json();

    expect(response.status).toBe(200);
    expect(payload).toMatchObject({
      token: "signed-test-token",
      url: "wss://livekit.invalid",
      room: "inc-123",
      identity: `${role}-1`,
      role,
      expiresIn: 900,
    });
    expect(mocked.instances).toHaveLength(1);
    expect(mocked.instances[0].options).toMatchObject({
      identity: `${role}-1`,
      metadata: JSON.stringify({ role }),
      ttl: 900,
    });
    expect(mocked.instances[0].grants).toEqual([
      {
        room: "inc-123",
        roomJoin: true,
        canPublish: true,
        canSubscribe: true,
        canPublishData: false,
      },
    ]);
  });
});
