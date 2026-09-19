"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Room, RoomEvent } from "livekit-client";
import {
  EMPTY_TRIAGE_STREAM,
  parseTriageEnvelope,
  reduceTriageStream,
  type TriageStream,
} from "./triageStream";

export type { TriageStream } from "./triageStream";

/**
 * Subscribes to the agent's structured event stream on the LiveKit data channel.
 * One reducer drives both the responder view and the clinician dashboard.
 */
export function useTriageStream(room: Room, incidentId: string): TriageStream {
  const [keyedStream, setKeyedStream] = useState<{
    incidentId: string;
    stream: TriageStream;
  }>(() => ({ incidentId, stream: EMPTY_TRIAGE_STREAM }));
  const decoder = useRef(new TextDecoder());

  const handle = useCallback((payload: Uint8Array) => {
    let decoded: unknown;
    try {
      decoded = JSON.parse(decoder.current.decode(payload));
    } catch {
      return;
    }
    const envelope = parseTriageEnvelope(decoded);
    if (envelope) {
      setKeyedStream((previous) => ({
        incidentId,
        stream: reduceTriageStream(
          previous.incidentId === incidentId
            ? previous.stream
            : EMPTY_TRIAGE_STREAM,
          envelope,
        ),
      }));
    }
  }, [incidentId]);

  useEffect(() => {
    const onData = (payload: Uint8Array) => handle(payload);
    room.on(RoomEvent.DataReceived, onData);
    return () => {
      room.off(RoomEvent.DataReceived, onData);
    };
  }, [room, handle, incidentId]);

  // An incident ID is a privacy boundary, not just a room label. Returning an
  // empty stream synchronously on a key change prevents even one render of
  // incident A under incident B's header. The keyed reducer above also starts
  // B from empty if an event arrives before React swaps the room listener.
  return keyedStream.incidentId === incidentId
    ? keyedStream.stream
    : EMPTY_TRIAGE_STREAM;
}
