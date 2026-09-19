import { describe, expect, it } from "vitest";
import type { TriageState } from "./types";
import { escalationMessage } from "./escalationPresentation";

function state(
  escalation: TriageState["escalation"],
  escalated = escalation !== "not_needed",
): TriageState {
  return {
    session_id: "inc-test",
    level: 4,
    label: "Severe",
    rationale: "test",
    definition: "test",
    level_provenance: "evidence",
    level_correctable: false,
    escalation,
    escalated,
    clinician_present: escalation === "clinician_joined",
    escalation_reason: "test",
    clinician_requested_at: "now",
    updated_at: "now",
    history: [],
  };
}

describe("escalationMessage", () => {
  it.each([
    ["requested", "Clinician requested — waiting for them to join"],
    ["clinician_joined", "Clinician joined"],
    ["clinician_lost", "Clinician disconnected — the agent remains with you"],
    ["failed_no_response", "No clinician answered — the agent remains with you"],
  ] as const)("renders %s without overstating presence", (status, expected) => {
    expect(escalationMessage(state(status))).toBe(expected);
  });

  it("renders nothing when no escalation exists", () => {
    expect(escalationMessage(state("not_needed", false))).toBeNull();
    expect(escalationMessage(null)).toBeNull();
  });

  it("fails closed on an inconsistent not-needed payload", () => {
    expect(escalationMessage(state("not_needed", true))).toBeNull();
  });
});
