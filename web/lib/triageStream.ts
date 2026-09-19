import { FACT_KINDS } from "./types";
import type {
  Finding,
  SnapshotEvent,
  TriageEnvelope,
  TriageState,
} from "./types";

export const MAX_TRANSCRIPT = 60;
export const MAX_RETRIEVALS = 40;

export interface TriageStream {
  state: TriageState | null;
  findings: Finding[];
  retrievals: Extract<TriageEnvelope, { topic: "triage.retrieval" }>["data"][];
  escalation: Extract<TriageEnvelope, { topic: "triage.escalation" }>["data"] | null;
  transcript: Extract<TriageEnvelope, { topic: "triage.transcript" }>["data"][];
  soap: string | null;
  snapshotTimelineStatus: SnapshotEvent["timeline_status"] | null;
  latency: { last: number | null; best: number | null; worst: number | null; count: number };
}

export const EMPTY_TRIAGE_STREAM: TriageStream = {
  state: null,
  findings: [],
  retrievals: [],
  escalation: null,
  transcript: [],
  soap: null,
  snapshotTimelineStatus: null,
  latency: { last: null, best: null, worst: null, count: 0 },
};

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nullableNumber(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isFinite(value));
}

function nullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isHistoryEntry(value: unknown): boolean {
  return (
    record(value) &&
    finiteNumber(value.level) &&
    typeof value.label === "string" &&
    typeof value.rationale === "string" &&
    typeof value.source === "string" &&
    (value.provenance === "assumption" || value.provenance === "evidence") &&
    typeof value.corrected === "boolean" &&
    typeof value.at === "string"
  );
}

const ESCALATION_STATES = new Set([
  "not_needed",
  "requested",
  "clinician_joined",
  "clinician_lost",
  "failed_no_response",
]);
const FACT_KIND_SET = new Set<string>(FACT_KINDS);

function isState(value: unknown): value is TriageState {
  return (
    record(value) &&
    typeof value.session_id === "string" &&
    typeof value.level === "number" &&
    Number.isInteger(value.level) &&
    value.level >= 1 &&
    value.level <= 5 &&
    typeof value.label === "string" &&
    typeof value.rationale === "string" &&
    typeof value.definition === "string" &&
    (value.level_provenance === "assumption" || value.level_provenance === "evidence") &&
    typeof value.level_correctable === "boolean" &&
    typeof value.escalation === "string" &&
    ESCALATION_STATES.has(value.escalation) &&
    typeof value.escalated === "boolean" &&
    typeof value.clinician_present === "boolean" &&
    nullableString(value.escalation_reason) &&
    nullableString(value.clinician_requested_at) &&
    typeof value.updated_at === "string" &&
    Array.isArray(value.history) &&
    value.history.every(isHistoryEntry)
  );
}

function isFinding(value: unknown): value is Finding {
  return (
    record(value) &&
    typeof value.id === "string" &&
    typeof value.text === "string" &&
    typeof value.kind === "string" &&
    FACT_KIND_SET.has(value.kind) &&
    typeof value.seq === "number" &&
    typeof value.elapsed_s === "number" &&
    typeof value.recorded_at === "string"
  );
}

function isRetrievalHit(value: unknown): boolean {
  return (
    record(value) &&
    typeof value.id === "string" &&
    (value.title === undefined || typeof value.title === "string") &&
    (value.text === undefined || typeof value.text === "string") &&
    (value.score === undefined || finiteNumber(value.score))
  );
}

function isRetrieval(value: unknown): boolean {
  return (
    record(value) &&
    (value.kind === "protocol" || value.kind === "state") &&
    typeof value.query === "string" &&
    (value.category === undefined || nullableString(value.category)) &&
    Array.isArray(value.hits) &&
    value.hits.every(isRetrievalHit) &&
    nullableNumber(value.wall_ms) &&
    nullableNumber(value.moss_ms) &&
    (value.within_budget === null || typeof value.within_budget === "boolean")
  );
}

function isEscalation(value: unknown): boolean {
  return (
    record(value) &&
    typeof value.session_id === "string" &&
    finiteNumber(value.level) &&
    Number.isInteger(value.level) &&
    value.level >= 1 &&
    value.level <= 5 &&
    typeof value.label === "string" &&
    typeof value.status === "string" &&
    ESCALATION_STATES.has(value.status) &&
    value.status !== "not_needed" &&
    typeof value.reason === "string" &&
    nullableString(value.requested_at)
  );
}

function isTranscript(value: unknown): boolean {
  return (
    record(value) &&
    typeof value.speaker === "string" &&
    (value.speaker_index === null || finiteNumber(value.speaker_index)) &&
    typeof value.text === "string" &&
    typeof value.final === "boolean" &&
    typeof value.at === "string"
  );
}

function isSoap(value: unknown): boolean {
  return record(value) && typeof value.session_id === "string" && typeof value.note === "string";
}

function isSnapshot(value: unknown): value is SnapshotEvent {
  return (
    record(value) &&
    isState(value.state) &&
    Array.isArray(value.findings) &&
    value.findings.every(isFinding) &&
    (value.timeline_status === "complete" || value.timeline_status === "unavailable")
  );
}

function highResolutionUtcKey(value: string): string | null {
  // Python emits UTC ISO-8601 with up to six fractional digits. Date.parse
  // discards digits 4-6, which can reverse two state changes inside one ms.
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(?:Z|\+00:00)$/.exec(value);
  if (!match) return null;
  return `${match[1]}.${(match[2] ?? "").padEnd(6, "0")}`;
}

function upsertFinding(findings: Finding[], incoming: Finding): Finding[] {
  const byId = new Map(findings.map((finding) => [finding.id, finding]));
  byId.set(incoming.id, incoming);
  return [...byId.values()].sort((left, right) => left.seq - right.seq);
}

/** Runtime boundary for untrusted LiveKit data-channel JSON. */
export function parseTriageEnvelope(value: unknown): TriageEnvelope | null {
  if (!record(value) || typeof value.topic !== "string" || !("data" in value)) return null;
  const data = value.data;

  switch (value.topic) {
    case "triage.state":
      return isState(data) ? { topic: value.topic, data } : null;
    case "triage.finding":
      return isFinding(data) ? { topic: value.topic, data } : null;
    case "triage.snapshot":
      return isSnapshot(data) ? { topic: value.topic, data } : null;
    case "triage.retrieval":
      return isRetrieval(data)
        ? { topic: value.topic, data: data as unknown as Extract<TriageEnvelope, { topic: "triage.retrieval" }>["data"] }
        : null;
    case "triage.escalation":
      return isEscalation(data)
        ? { topic: value.topic, data: data as unknown as Extract<TriageEnvelope, { topic: "triage.escalation" }>["data"] }
        : null;
    case "triage.transcript":
      return isTranscript(data)
        ? { topic: value.topic, data: data as unknown as Extract<TriageEnvelope, { topic: "triage.transcript" }>["data"] }
        : null;
    case "triage.soap":
      return isSoap(data)
        ? { topic: value.topic, data: data as unknown as Extract<TriageEnvelope, { topic: "triage.soap" }>["data"] }
        : null;
    default:
      return null;
  }
}

export function reduceTriageStream(
  previous: TriageStream,
  envelope: TriageEnvelope,
): TriageStream {
  switch (envelope.topic) {
    case "triage.state":
      return { ...previous, state: envelope.data };
    case "triage.finding":
      return { ...previous, findings: upsertFinding(previous.findings, envelope.data) };
    case "triage.snapshot": {
      const sameSession = previous.state?.session_id === envelope.data.state.session_id;
      const previousTime = previous.state
        ? highResolutionUtcKey(previous.state.updated_at)
        : null;
      const snapshotTime = highResolutionUtcKey(envelope.data.state.updated_at);
      // A delayed catch-up must never roll back state that arrived live while
      // the agent awaited the timeline read. Unparseable clocks fail closed in
      // favour of the already-rendered live state.
      const snapshotIsNewer =
        !sameSession ||
        previous.state == null ||
        (previousTime !== null && snapshotTime !== null && snapshotTime > previousTime);

      const byId = new Map(envelope.data.findings.map((finding) => [finding.id, finding]));
      if (sameSession) {
        // Live findings win on ID and survive a snapshot captured at an older
        // timeline watermark. Sort by seq to restore the durable order.
        for (const finding of previous.findings) byId.set(finding.id, finding);
      }
      const findings = [...byId.values()].sort((left, right) => left.seq - right.seq);

      return {
        ...previous,
        state: snapshotIsNewer ? envelope.data.state : previous.state,
        findings,
        snapshotTimelineStatus: envelope.data.timeline_status,
      };
    }
    case "triage.escalation":
      return { ...previous, escalation: envelope.data };
    case "triage.soap":
      return { ...previous, soap: envelope.data.note };
    case "triage.transcript":
      return {
        ...previous,
        transcript: [...previous.transcript, envelope.data].slice(-MAX_TRANSCRIPT),
      };
    case "triage.retrieval": {
      const event = envelope.data;
      const retrievals = [event, ...previous.retrievals].slice(0, MAX_RETRIEVALS);
      if (event.wall_ms == null) return { ...previous, retrievals };
      return {
        ...previous,
        retrievals,
        latency: {
          last: event.wall_ms,
          best:
            previous.latency.best == null
              ? event.wall_ms
              : Math.min(previous.latency.best, event.wall_ms),
          worst:
            previous.latency.worst == null
              ? event.wall_ms
              : Math.max(previous.latency.worst, event.wall_ms),
          count: previous.latency.count + 1,
        },
      };
    }
  }
}
