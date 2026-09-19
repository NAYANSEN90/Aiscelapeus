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
export function useTriageStream(room: Room): TriageStream {
  const [stream, setStream] = useState<TriageStream>(EMPTY_TRIAGE_STREAM);
  const decoder = useRef(new TextDecoder());

  const handle = useCallback((payload: Uint8Array) => {
    let decoded: unknown;
    try {
      decoded = JSON.parse(decoder.current.decode(payload));
    } catch {
      return;
    }
    const envelope = parseTriageEnvelope(decoded);
    if (envelope) setStream((previous) => reduceTriageStream(previous, envelope));
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
