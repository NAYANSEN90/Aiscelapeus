import { act, renderHook, waitFor } from "@testing-library/react";
import { RoomEvent, type Room } from "livekit-client";
import { describe, expect, it } from "vitest";
import { EMPTY_TRIAGE_STREAM } from "./triageStream";
import { useTriageStream } from "./useTriageStream";

type DataListener = (payload: Uint8Array) => void;

class FakeRoom {
  private listeners = new Set<DataListener>();

  on(event: RoomEvent, listener: DataListener): void {
    if (event === RoomEvent.DataReceived) this.listeners.add(listener);
  }

  off(event: RoomEvent, listener: DataListener): void {
    if (event === RoomEvent.DataReceived) this.listeners.delete(listener);
  }

  emit(envelope: unknown): void {
    const payload = new TextEncoder().encode(JSON.stringify(envelope));
    for (const listener of this.listeners) listener(payload);
  }
}

const stateA = {
  session_id: "inc-a",
  level: 5,
  label: "Critical",
  rationale: "not breathing",
  definition: "Immediate threat to life",
  level_provenance: "evidence",
  level_correctable: false,
  escalation: "requested",
  escalated: true,
  clinician_present: false,
  escalation_reason: "not breathing",
  clinician_requested_at: "2026-09-19T10:00:00Z",
  updated_at: "2026-09-19T10:00:00Z",
  history: [],
};

const findingA = {
  id: "a-finding",
  text: "incident A private finding",
  kind: "observation",
  seq: 1,
  elapsed_s: 1,
  recorded_at: "2026-09-19T10:00:00Z",
};

describe("useTriageStream incident boundary", () => {
  it("clears every field before incident B receives events", async () => {
    const fakeRoom = new FakeRoom();
    const room = fakeRoom as unknown as Room;
    const { result, rerender } = renderHook(
      ({ incidentId }) => useTriageStream(room, incidentId),
      { initialProps: { incidentId: "inc-a" } },
    );

    act(() => {
      fakeRoom.emit({ topic: "triage.state", data: stateA });
      fakeRoom.emit({ topic: "triage.finding", data: findingA });
      fakeRoom.emit({
        topic: "triage.snapshot",
        data: { state: stateA, findings: [findingA], timeline_status: "complete" },
      });
      fakeRoom.emit({
        topic: "triage.retrieval",
        data: {
          kind: "protocol",
          query: "CPR",
          hits: [],
          wall_ms: 2,
          moss_ms: 1,
          within_budget: true,
        },
      });
      fakeRoom.emit({
        topic: "triage.escalation",
        data: {
          session_id: "inc-a",
          level: 5,
          label: "Critical",
          status: "requested",
          reason: "not breathing",
          requested_at: "2026-09-19T10:00:00Z",
        },
      });
      fakeRoom.emit({
        topic: "triage.transcript",
        data: {
          speaker: "responder",
          speaker_index: 0,
          text: "incident A private transcript",
          final: true,
          at: "2026-09-19T10:00:00Z",
        },
      });
      fakeRoom.emit({
        topic: "triage.soap",
        data: { session_id: "inc-a", note: "incident A private SOAP" },
      });
    });

    expect(result.current).not.toEqual(EMPTY_TRIAGE_STREAM);
    expect(result.current.snapshotTimelineStatus).toBe("complete");

    rerender({ incidentId: "inc-b" });
    await waitFor(() => expect(result.current).toEqual(EMPTY_TRIAGE_STREAM));

    const findingB = { ...findingA, id: "b-finding", text: "incident B finding" };
    act(() => fakeRoom.emit({ topic: "triage.finding", data: findingB }));

    expect(result.current).toEqual({
      ...EMPTY_TRIAGE_STREAM,
      findings: [findingB],
    });
  });
});
