/**
 * Shapes of the events the agent publishes over the LiveKit data channel.
 * Both the responder view and the doctor dashboard render from this one stream,
 * so triage state never diverges between them.
 */

export type TriageTopic =
  | "triage.state"
  | "triage.finding"
  | "triage.retrieval"
  | "triage.escalation"
  | "triage.transcript"
  | "triage.soap"
  | "triage.snapshot";

export interface TriageState {
  session_id: string;
  level: 1 | 2 | 3 | 4 | 5;
  label: string;
  rationale: string;
  definition: string;
  level_provenance: "assumption" | "evidence";
  level_correctable: boolean;
  escalation:
    | "not_needed"
    | "requested"
    | "clinician_joined"
    | "clinician_lost"
    | "failed_no_response";
  escalated: boolean;
  clinician_present: boolean;
  escalation_reason: string | null;
  clinician_requested_at: string | null;
  updated_at: string;
  history: Array<{
    level: number;
    label: string;
    rationale: string;
    source: string;
    provenance: "assumption" | "evidence";
    corrected: boolean;
    at: string;
  }>;
}

export interface Finding {
  id: string;
  text: string;
  kind: FactKind;
  seq: number;
  elapsed_s: number;
  recorded_at: string;
}

export const FACT_KINDS = [
  "vital",
  "intervention",
  "observation",
  "symptom",
  "escalation",
] as const;
export type FactKind = (typeof FACT_KINDS)[number];

export interface RetrievalEvent {
  kind: "protocol" | "state";
  query: string;
  category?: string | null;
  hits: Array<{ id: string; title?: string; text?: string; score?: number }>;
  wall_ms: number | null;
  moss_ms: number | null;
  within_budget: boolean | null;
}

export interface EscalationEvent {
  session_id: string;
  level: number;
  label: string;
  status:
    | "requested"
    | "clinician_joined"
    | "clinician_lost"
    | "failed_no_response";
  reason: string;
  requested_at: string | null;
}

export interface TranscriptEvent {
  speaker: string;
  speaker_index: number | null;
  text: string;
  final: boolean;
  at: string;
}

export interface SoapEvent {
  session_id: string;
  note: string;
}

export interface SnapshotEvent {
  state: TriageState;
  findings: Finding[];
  timeline_status: "complete" | "unavailable";
}

export type TriageEnvelope =
  | { topic: "triage.state"; data: TriageState }
  | { topic: "triage.finding"; data: Finding }
  | { topic: "triage.retrieval"; data: RetrievalEvent }
  | { topic: "triage.escalation"; data: EscalationEvent }
  | { topic: "triage.transcript"; data: TranscriptEvent }
  | { topic: "triage.soap"; data: SoapEvent }
  | { topic: "triage.snapshot"; data: SnapshotEvent };

export const LEVEL_STYLES: Record<number, { bg: string; ring: string; text: string }> = {
  1: { bg: "bg-emerald-500/10", ring: "ring-emerald-500/40", text: "text-emerald-300" },
  2: { bg: "bg-sky-500/10", ring: "ring-sky-500/40", text: "text-sky-300" },
  3: { bg: "bg-amber-500/10", ring: "ring-amber-500/40", text: "text-amber-300" },
  4: { bg: "bg-orange-500/10", ring: "ring-orange-500/40", text: "text-orange-300" },
  5: { bg: "bg-red-500/10", ring: "ring-red-500/50", text: "text-red-300" },
};
