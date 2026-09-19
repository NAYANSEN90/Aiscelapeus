import { describe, expect, it } from "vitest";
import { newIncidentId, responderIdentityFor } from "./incidentIdentity";

const FIRST_UUID = "123e4567-e89b-42d3-a456-426614174000";
const SECOND_UUID = "123e4567-e89b-42d3-b456-426614174001";

describe("incident identity lifecycle", () => {
  it("keeps one responder identity stable for retries in the same incident", () => {
    const incidentId = newIncidentId(() => FIRST_UUID);

    expect(responderIdentityFor(incidentId)).toBe(
      "responder-inc-123e4567-e89b-42d3-a456-426614174000",
    );
    expect(responderIdentityFor(incidentId)).toBe(responderIdentityFor(incidentId));
  });

  it("creates a different room and media identity after an explicit restart", () => {
    const first = newIncidentId(() => FIRST_UUID);
    const second = newIncidentId(() => SECOND_UUID);

    expect(second).not.toBe(first);
    expect(responderIdentityFor(second)).not.toBe(responderIdentityFor(first));
  });

  it("rejects malformed incident identifiers instead of pinning media ambiguously", () => {
    expect(() => responderIdentityFor("inc-not-a-uuid")).toThrow(
      "valid incident ID",
    );
    expect(() => newIncidentId(() => "not-a-uuid")).toThrow(
      "invalid incident ID",
    );
  });
});
