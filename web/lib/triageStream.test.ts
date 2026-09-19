import { describe, expect, it } from "vitest";
import type { Finding, TriageEnvelope, TriageState } from "./types";
import {
  EMPTY_TRIAGE_STREAM,
  MAX_RETRIEVALS,
  MAX_TRANSCRIPT,
  parseTriageEnvelope,
  reduceTriageStream,
} from "./triageStream";

function state(overrides: Partial<TriageState> = {}): TriageState {
  return {
    session_id: "inc-test",
    level: 4,
    label: "Severe",
    rationale: "Uncontrolled bleeding",
    definition: "Time-critical",
    level_provenance: "evidence",
    level_correctable: false,
    escalation: "requested",
    escalated: true,
    clinician_present: false,
    escalation_reason: "level threshold",
    clinician_requested_at: "2026-09-19T10:00:00Z",
    updated_at: "2026-09-19T10:00:00Z",
    history: [],
    ...overrides,
  };
}

function finding(id: string, seq: number): Finding {
  return {
    id,
    text: `finding ${id}`,
    kind: "observation",
    seq,
    elapsed_s: seq * 1.5,
    recorded_at: "2026-09-19T10:00:00Z",
  };
}

describe("parseTriageEnvelope", () => {
  const valid: TriageEnvelope[] = [
    { topic: "triage.state", data: state() },
    { topic: "triage.finding", data: finding("f-1", 1) },
    {
      topic: "triage.retrieval",
      data: {
        kind: "protocol",
        query: "adult CPR",
        category: "cardiac",
        hits: [{ id: "bls", title: "Adult CPR", score: 0.9 }],
        wall_ms: 4.2,
        moss_ms: 2.1,
        within_budget: true,
      },
    },
    {
      topic: "triage.escalation",
      data: {
        session_id: "inc-test",
        level: 5,
        label: "Critical",
        status: "requested",
        reason: "not breathing",
        requested_at: "2026-09-19T10:00:00Z",
      },
    },
    {
      topic: "triage.transcript",
      data: {
        speaker: "responder",
        speaker_index: 0,
        text: "he is not breathing",
        final: true,
        at: "2026-09-19T10:00:00Z",
      },
    },
    { topic: "triage.soap", data: { session_id: "inc-test", note: "S: collapse" } },
    {
      topic: "triage.snapshot",
      data: { state: state(), findings: [finding("f-1", 1)], timeline_status: "complete" },
    },
  ];

  it.each(valid)("accepts and reduces a valid $topic payload", (wireEnvelope) => {
    const parsed = parseTriageEnvelope(wireEnvelope);
    expect(parsed).toEqual(wireEnvelope);
    expect(() => reduceTriageStream(EMPTY_TRIAGE_STREAM, parsed!)).not.toThrow();
  });

  it.each([
    null,
    {},
    { topic: "unknown", data: {} },
    { topic: "triage.state", data: state({ level: 5.5 as 5 }) },
    { topic: "triage.state", data: state({ escalation: "connected" as TriageState["escalation"] }) },
    { topic: "triage.finding", data: { ...finding("f-1", 1), seq: "1" } },
    { topic: "triage.finding", data: { ...finding("f-1", 1), kind: "breathing" } },
    {
      topic: "triage.snapshot",
      data: { state: state(), findings: [{}], timeline_status: "complete" },
    },
    { topic: "triage.snapshot", data: { state: state(), findings: [], timeline_status: "partial" } },
    {
      topic: "triage.retrieval",
      data: {
        kind: "protocol",
        query: "CPR",
        hits: [null],
        wall_ms: 1,
        moss_ms: 1,
        within_budget: true,
      },
    },
    {
      topic: "triage.retrieval",
      data: {
        kind: "protocol",
        query: "CPR",
        hits: [],
        wall_ms: Number.NaN,
        moss_ms: 1,
        within_budget: true,
      },
    },
    { topic: "triage.escalation", data: { reason: "missing the rest" } },
    {
      topic: "triage.transcript",
      data: { speaker: "responder", speaker_index: 0, text: "help", final: true },
    },
    { topic: "triage.soap", data: { session_id: "inc-test", note: 7 } },
  ])("rejects malformed or unknown channel input %#", (value) => {
    expect(parseTriageEnvelope(value)).toBeNull();
  });
});

describe("reduceTriageStream", () => {
  it("appends an individual finding", () => {
    const next = reduceTriageStream(EMPTY_TRIAGE_STREAM, {
      topic: "triage.finding",
      data: finding("f-1", 1),
    });
    expect(next.findings.map((item) => item.id)).toEqual(["f-1"]);
  });

  it("upserts a live finding already delivered in the snapshot", () => {
    const snapshot = reduceTriageStream(EMPTY_TRIAGE_STREAM, {
      topic: "triage.snapshot",
      data: {
        state: state(),
        findings: [finding("same-fact", 1)],
        timeline_status: "complete",
      },
    });
    const corrected = { ...finding("same-fact", 1), text: "corrected live payload" };

    const next = reduceTriageStream(snapshot, {
      topic: "triage.finding",
      data: corrected,
    });

    expect(next.findings).toEqual([corrected]);
  });

  it("takes newer snapshot state while retaining live findings from the same incident", () => {
    const previous = {
      ...EMPTY_TRIAGE_STREAM,
      state: state({
        level: 2,
        escalated: false,
        escalation: "not_needed",
        updated_at: "2026-09-19T09:00:00Z",
      }),
      findings: [finding("live", 1)],
    };
    const currentState = state({ clinician_present: true, escalation: "clinician_joined" });
    const next = reduceTriageStream(previous, {
      topic: "triage.snapshot",
      data: {
        state: currentState,
        findings: [finding("snapshot", 2)],
        timeline_status: "complete",
      },
    });

    expect(next.state).toEqual(currentState);
    expect(next.findings.map((item) => item.id)).toEqual(["live", "snapshot"]);
  });

  it("does not let a delayed snapshot erase newer state or findings", () => {
    const liveState = state({
      level: 5,
      label: "Critical",
      escalation: "clinician_joined",
      clinician_present: true,
      updated_at: "2026-09-19T10:00:03Z",
    });
    const previous = {
      ...EMPTY_TRIAGE_STREAM,
      state: liveState,
      findings: [finding("new-live-fact", 3)],
    };
    const delayedSnapshotState = state({ updated_at: "2026-09-19T10:00:01Z" });

    const next = reduceTriageStream(previous, {
      topic: "triage.snapshot",
      data: {
        state: delayedSnapshotState,
        findings: [finding("older-snapshot-fact", 2)],
        timeline_status: "complete",
      },
    });

    expect(next.state).toEqual(liveState);
    expect(next.findings.map((item) => item.id)).toEqual([
      "older-snapshot-fact",
      "new-live-fact",
    ]);
  });

  it("preserves Python microsecond ordering inside one JavaScript millisecond", () => {
    const previousState = state({ updated_at: "2026-09-19T10:00:00.000100+00:00" });
    const newerSnapshot = state({
      level: 5,
      label: "Critical",
      updated_at: "2026-09-19T10:00:00.000900+00:00",
    });

    const next = reduceTriageStream(
      { ...EMPTY_TRIAGE_STREAM, state: previousState },
      {
        topic: "triage.snapshot",
        data: { state: newerSnapshot, findings: [], timeline_status: "complete" },
      },
    );

    expect(next.state).toEqual(newerSnapshot);
  });

  it("never shares the snapshot array with caller-owned data", () => {
    const findings = [finding("f-1", 1)];
    const next = reduceTriageStream(EMPTY_TRIAGE_STREAM, {
      topic: "triage.snapshot",
      data: { state: state(), findings, timeline_status: "complete" },
    });
    findings.push(finding("mutated", 2));
    expect(next.findings.map((item) => item.id)).toEqual(["f-1"]);
  });

  it("bounds transcripts to the newest sixty turns", () => {
    let stream = EMPTY_TRIAGE_STREAM;
    for (let index = 0; index < MAX_TRANSCRIPT + 5; index += 1) {
      stream = reduceTriageStream(stream, {
        topic: "triage.transcript",
        data: {
          speaker: "responder",
          speaker_index: 0,
          text: String(index),
          final: true,
          at: "now",
        },
      });
    }
    expect(stream.transcript).toHaveLength(MAX_TRANSCRIPT);
    expect(stream.transcript[0].text).toBe("5");
  });

  it("bounds retrievals newest-first and computes latency only from measurements", () => {
    let stream = EMPTY_TRIAGE_STREAM;
    const event = (index: number, wall_ms: number | null): TriageEnvelope => ({
      topic: "triage.retrieval",
      data: {
        kind: "protocol",
        query: String(index),
        hits: [],
        wall_ms,
        moss_ms: null,
        within_budget: wall_ms == null ? null : wall_ms <= 10,
      },
    });

    stream = reduceTriageStream(stream, event(-1, null));
    for (let index = 0; index < MAX_RETRIEVALS + 3; index += 1) {
      stream = reduceTriageStream(stream, event(index, index + 1));
    }

    expect(stream.retrievals).toHaveLength(MAX_RETRIEVALS);
    expect(stream.retrievals[0].query).toBe(String(MAX_RETRIEVALS + 2));
    expect(stream.latency).toEqual({
      last: MAX_RETRIEVALS + 3,
      best: 1,
      worst: MAX_RETRIEVALS + 3,
      count: MAX_RETRIEVALS + 3,
    });
  });

  it("updates each non-collection topic without erasing independent state", () => {
    const withState = reduceTriageStream(EMPTY_TRIAGE_STREAM, {
      topic: "triage.state",
      data: state(),
    });
    const withEscalation = reduceTriageStream(withState, {
      topic: "triage.escalation",
      data: {
        session_id: "inc-test",
        level: 4,
        label: "Severe",
        status: "requested",
        reason: "threshold",
        requested_at: "now",
      },
    });
    const withSoap = reduceTriageStream(withEscalation, {
      topic: "triage.soap",
      data: { session_id: "inc-test", note: "S: recorded" },
    });

    expect(withSoap.state?.session_id).toBe("inc-test");
    expect(withSoap.escalation?.reason).toBe("threshold");
    expect(withSoap.soap).toBe("S: recorded");
  });
});
