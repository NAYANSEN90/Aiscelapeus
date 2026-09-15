"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Room, RoomEvent } from "livekit-client";
import type {
  EscalationEvent,
  Finding,
  RetrievalEvent,
  TranscriptEvent,
  TriageEnvelope,
  TriageState,
} from "./types";

const MAX_TRANSCRIPT = 60;
const MAX_RETRIEVALS = 40;

export interface TriageStream {
  state: TriageState | null;
  findings: Finding[];
  retrievals: RetrievalEvent[];
  escalation: EscalationEvent | null;
  transcript: TranscriptEvent[];
  soap: string | null;
  /** Rolling stats, so latency is visible live rather than only in traces. */
  latency: { last: number | null; best: number | null; worst: number | null; count: number };
}

const EMPTY: TriageStream = {
  state: null,
  findings: [],
  retrievals: [],
  escalation: null,
  transcript: [],
  soap: null,
  latency: { last: null, best: null, worst: null, count: 0 },
};

/**
 * Subscribes to the agent's structured event stream on the LiveKit data channel.
 * One reducer drives both the responder view and the clinician dashboard.
 */
export function useTriageStream(room: Room): TriageStream {
  const [stream, setStream] = useState<TriageStream>(EMPTY);
  const decoder = useRef(new TextDecoder());

  const handle = useCallback((payload: Uint8Array) => {
    let envelope: TriageEnvelope;
    try {
      envelope = JSON.parse(decoder.current.decode(payload)) as TriageEnvelope;
    } catch {
      return;
    }

    setStream((prev) => {
      switch (envelope.topic) {
        case "triage.state":
          return { ...prev, state: envelope.data };

        case "triage.finding":
          return { ...prev, findings: [...prev.findings, envelope.data] };

        case "triage.escalation":
          return { ...prev, escalation: envelope.data };

        case "triage.soap":
          return { ...prev, soap: envelope.data.note };

        case "triage.transcript": {
          const transcript = [...prev.transcript, envelope.data].slice(-MAX_TRANSCRIPT);
          return { ...prev, transcript };
        }

        case "triage.retrieval": {
          const event = envelope.data;
          const retrievals = [event, ...prev.retrievals].slice(0, MAX_RETRIEVALS);
          const ms = event.wall_ms;
          if (ms == null) return { ...prev, retrievals };
          const { best, worst, count } = prev.latency;
          return {
            ...prev,
            retrievals,
            latency: {
              last: ms,
              best: best == null ? ms : Math.min(best, ms),
              worst: worst == null ? ms : Math.max(worst, ms),
              count: count + 1,
            },
          };
        }

        default:
          return prev;
      }
    });
  }, []);

  useEffect(() => {
    const onData = (payload: Uint8Array) => handle(payload);
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, handle]);

  return stream;
}
