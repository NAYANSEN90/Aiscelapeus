import type { TriageEnvelope, TriageState } from "./types";
import {
  parseTriageEnvelope,
  reduceTriageStream,
  type TriageStream,
} from "./triageStream";

export interface DemoFrame {
  atMs: number;
  envelope: TriageEnvelope;
  caption: string;
}

export interface DemoScenario {
  id: string;
  title: string;
  description: string;
  expectedOutcome: string;
  frames: DemoFrame[];
}

function state(
  sessionId: string,
  level: 1 | 2 | 3 | 4 | 5,
  label: string,
  rationale: string,
  escalation: TriageState["escalation"] = "not_needed",
): TriageState {
  const escalated = escalation !== "not_needed";
  const clinicianPresent = escalation === "clinician_joined";
  return {
    session_id: sessionId,
    level,
    label,
    rationale,
    definition: label,
    level_provenance: level === 1 ? "assumption" : "evidence",
    level_correctable: level === 1,
    escalation,
    escalated,
    clinician_present: clinicianPresent,
    escalation_reason: escalated ? rationale : null,
    clinician_requested_at: escalated ? "2026-09-19T12:00:06Z" : null,
    updated_at: "2026-09-19T12:00:06Z",
    history: level === 1 ? [] : [
      {
        level,
        label,
        rationale,
        source: "deterministic transcript edge",
        provenance: "evidence",
        corrected: false,
        at: "2026-09-19T12:00:06Z",
      },
    ],
  };
}

const arrestSession = "demo-poolside-arrest";
const arrestRequested = state(
  arrestSession,
  5,
  "Critical",
  "Reported drowning and not breathing",
  "requested",
);
const arrestJoined = {
  ...arrestRequested,
  escalation: "clinician_joined" as const,
  clinician_present: true,
  updated_at: "2026-09-19T12:00:09Z",
};

const minorSession = "demo-kitchen-cut";

export const DEMO_SCENARIOS: DemoScenario[] = [
  {
    id: "poolside-arrest",
    title: "Poolside arrest",
    description:
      "A drowning report becomes an immediate Level 5 escalation, independent of model tool use.",
    expectedOutcome: "Critical → clinician requested → clinician present",
    frames: [
      {
        atMs: 0,
        caption: "The incident opens at the conservative default.",
        envelope: { topic: "triage.state", data: state(arrestSession, 1, "Minor", "Awaiting evidence") },
      },
      {
        atMs: 900,
        caption: "The caller reports the scene.",
        envelope: {
          topic: "triage.transcript",
          data: {
            speaker: "caller",
            speaker_index: 0,
            text: "We pulled him from the pool and he is not breathing.",
            final: true,
            at: "2026-09-19T12:00:04Z",
          },
        },
      },
      {
        atMs: 1800,
        caption: "The in-process protocol index returns the drowning protocol.",
        envelope: {
          topic: "triage.retrieval",
          data: {
            kind: "protocol",
            query: "drowning not breathing",
            category: "drowning",
            hits: [{ id: "drowning-rescue", title: "Drowning rescue", score: 0.94 }],
            wall_ms: 4.2,
            moss_ms: 2.9,
            within_budget: true,
          },
        },
      },
      {
        atMs: 2700,
        caption: "The observation is written into the incident timeline.",
        envelope: {
          topic: "triage.finding",
          data: {
            id: "fact-breathing",
            text: "[T+6s] breathing: not breathing after drowning",
            kind: "observation",
            seq: 1,
            elapsed_s: 6,
            recorded_at: "2026-09-19T12:00:06Z",
          },
        },
      },
      {
        atMs: 3600,
        caption: "The deterministic edge ratchets to Level 5 and requests a clinician.",
        envelope: { topic: "triage.state", data: arrestRequested },
      },
      {
        atMs: 4500,
        caption: "The request event is broadcast; the UI still says waiting, not joined.",
        envelope: {
          topic: "triage.escalation",
          data: {
            session_id: arrestSession,
            level: 5,
            label: "Critical",
            status: "requested",
            reason: "Reported drowning and not breathing",
            requested_at: "2026-09-19T12:00:06Z",
          },
        },
      },
      {
        atMs: 6000,
        caption: "The clinician joins and receives the current state and timeline.",
        envelope: {
          topic: "triage.snapshot",
          data: {
            state: arrestJoined,
            findings: [
              {
                id: "fact-breathing",
                text: "[T+6s] breathing: not breathing after drowning",
                kind: "observation",
                seq: 1,
                elapsed_s: 6,
                recorded_at: "2026-09-19T12:00:06Z",
              },
            ],
            timeline_status: "complete",
          },
        },
      },
    ],
  },
  {
    id: "minor-cut",
    title: "Minor kitchen cut",
    description:
      "A stable, alert caller receives a protocol lookup and remains below the clinician threshold.",
    expectedOutcome: "Minor → protocol grounded → no escalation",
    frames: [
      {
        atMs: 0,
        caption: "The incident opens.",
        envelope: { topic: "triage.state", data: state(minorSession, 1, "Minor", "Awaiting evidence") },
      },
      {
        atMs: 1000,
        caption: "The caller reports a controlled cut and normal responsiveness.",
        envelope: {
          topic: "triage.transcript",
          data: {
            speaker: "caller",
            speaker_index: 0,
            text: "I cut my palm. I am alert, breathing normally, and the bleeding stops with pressure.",
            final: true,
            at: "2026-09-19T12:10:03Z",
          },
        },
      },
      {
        atMs: 2000,
        caption: "The wound protocol is retrieved inside the latency budget.",
        envelope: {
          topic: "triage.retrieval",
          data: {
            kind: "protocol",
            query: "minor cut bleeding controlled with pressure",
            category: "bleeding",
            hits: [{ id: "minor-cuts", title: "Minor cuts and grazes", score: 0.91 }],
            wall_ms: 3.6,
            moss_ms: 2.4,
            within_budget: true,
          },
        },
      },
      {
        atMs: 3000,
        caption: "The controlled bleed is recorded for the handoff record.",
        envelope: {
          topic: "triage.finding",
          data: {
            id: "fact-bleeding",
            text: "[T+5s] bleeding: palm cut controlled with direct pressure",
            kind: "observation",
            seq: 1,
            elapsed_s: 5,
            recorded_at: "2026-09-19T12:10:05Z",
          },
        },
      },
      {
        atMs: 4000,
        caption: "No evidence crosses the escalation threshold.",
        envelope: {
          topic: "triage.state",
          data: state(minorSession, 1, "Minor", "Bleeding controlled; caller alert and breathing normally"),
        },
      },
    ],
  },
];

export function scenarioById(id: string): DemoScenario {
  const scenario = DEMO_SCENARIOS.find((candidate) => candidate.id === id);
  if (!scenario) throw new Error(`unknown demo scenario: ${id}`);
  return scenario;
}

export function applyDemoFrame(stream: TriageStream, frame: DemoFrame): TriageStream {
  // Exercise the same serialization and untrusted boundary as a LiveKit data
  // message. A bad fixture is an explicit replay failure, never silently fed
  // around the parser into the reducer.
  const wireValue: unknown = JSON.parse(JSON.stringify(frame.envelope));
  const parsed = parseTriageEnvelope(wireValue);
  if (parsed === null) throw new Error(`invalid demo frame: ${frame.caption}`);
  return reduceTriageStream(stream, parsed);
}
