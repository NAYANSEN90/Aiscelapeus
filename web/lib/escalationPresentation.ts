import type { TriageState } from "./types";

/** One honest responder-facing description of the clinician lifecycle. */
export function escalationMessage(state: TriageState | null): string | null {
  if (!state?.escalated) return null;
  switch (state.escalation) {
    case "clinician_joined":
      return "Clinician joined";
    case "clinician_lost":
      return "Clinician disconnected — the agent remains with you";
    case "failed_no_response":
      return "No clinician answered — the agent remains with you";
    case "requested":
      return "Clinician requested — waiting for them to join";
    case "not_needed":
      // An internally inconsistent payload must not claim help exists.
      return null;
  }
}
