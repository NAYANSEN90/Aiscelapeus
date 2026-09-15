"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Room, RoomEvent, Track } from "livekit-client";
import { useTriageStream } from "@/lib/useTriageStream";
import { LEVEL_STYLES } from "@/lib/types";

type Phase = "idle" | "connecting" | "live" | "error";

function newSessionId() {
  const stamp = new Date().toISOString().slice(5, 16).replace(/[-:T]/g, "");
  return `inc-${stamp}-${Math.random().toString(36).slice(2, 6)}`;
}

export default function ResponderPage() {
  const [room] = useState(() => new Room({ adaptiveStream: true, dynacast: true }));
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState("");
  const [micLive, setMicLive] = useState(false);
  const [agentSpeaking, setAgentSpeaking] = useState(false);

  const stream = useTriageStream(room);

  useEffect(() => setSessionId(newSessionId()), []);

  // Play the agent's audio as soon as it publishes.
  useEffect(() => {
    const onTrack = (track: Track) => {
      if (track.kind === Track.Kind.Audio) {
        const el = track.attach();
        el.autoplay = true;
        el.style.display = "none";
        document.body.appendChild(el);
      }
    };
    const onSpeakers = (speakers: { isLocal: boolean }[]) =>
      setAgentSpeaking(speakers.some((s) => !s.isLocal));
    const onDisconnect = () => {
      setPhase("idle");
      setMicLive(false);
    };

    room.on(RoomEvent.TrackSubscribed, onTrack);
    room.on(RoomEvent.ActiveSpeakersChanged, onSpeakers);
    room.on(RoomEvent.Disconnected, onDisconnect);
    return () => {
      room.off(RoomEvent.TrackSubscribed, onTrack);
      room.off(RoomEvent.ActiveSpeakersChanged, onSpeakers);
      room.off(RoomEvent.Disconnected, onDisconnect);
    };
  }, [room]);

  const connect = useCallback(async () => {
    setPhase("connecting");
    setError(null);
    try {
      const res = await fetch("/api/token", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          room: sessionId,
          identity: `responder-${Math.random().toString(36).slice(2, 8)}`,
          role: "responder",
        }),
      });
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.error ?? "Could not get a token");
      if (!payload.url) throw new Error("NEXT_PUBLIC_LIVEKIT_URL is not set");

      await room.connect(payload.url, payload.token);
      await room.localParticipant.setMicrophoneEnabled(true);
      setMicLive(true);
      setPhase("live");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("error");
    }
  }, [room, sessionId]);

  const disconnect = useCallback(async () => {
    await room.disconnect();
    setPhase("idle");
    setMicLive(false);
  }, [room]);

  const toggleMic = useCallback(async () => {
    const next = !micLive;
    await room.localParticipant.setMicrophoneEnabled(next);
    setMicLive(next);
  }, [room, micLive]);

  const level = stream.state?.level ?? 1;
  const style = LEVEL_STYLES[level] ?? LEVEL_STYLES[1];
  const budgetOk = stream.latency.worst == null || stream.latency.worst <= 10;

  const timeline = useMemo(() => [...stream.findings].reverse(), [stream.findings]);

  return (
    <main className="min-h-screen bg-neutral-950 text-neutral-100">
      <div className="mx-auto max-w-6xl px-4 py-6 md:px-8">
        <header className="flex flex-wrap items-center justify-between gap-4 border-b border-neutral-800 pb-5">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Aiscelapeus</h1>
            <p className="text-sm text-neutral-400">
              Hands-free field triage. Speak normally; the assistant is listening.
            </p>
          </div>
          <div className="text-right text-xs text-neutral-500">
            <div className="font-mono">{sessionId || "—"}</div>
            <a href="/doctor" className="text-sky-400 hover:underline">
              Clinician dashboard →
            </a>
          </div>
        </header>

        {/* Connection */}
        <section className="mt-6 flex flex-wrap items-center gap-3">
          {phase !== "live" ? (
            <button
              onClick={connect}
              disabled={phase === "connecting" || !sessionId}
              className="rounded-lg bg-red-600 px-6 py-3 text-base font-semibold text-white transition hover:bg-red-500 disabled:opacity-50"
            >
              {phase === "connecting" ? "Connecting…" : "Start emergency call"}
            </button>
          ) : (
            <>
              <button
                onClick={toggleMic}
                className={`rounded-lg px-6 py-3 text-base font-semibold transition ${
                  micLive
                    ? "bg-emerald-600 text-white hover:bg-emerald-500"
                    : "bg-neutral-700 text-neutral-200 hover:bg-neutral-600"
                }`}
              >
                {micLive ? "Microphone live" : "Microphone muted"}
              </button>
              <button
                onClick={disconnect}
                className="rounded-lg border border-neutral-700 px-6 py-3 text-base font-medium text-neutral-300 hover:bg-neutral-900"
              >
                End call
              </button>
              <span
                className={`ml-1 text-sm ${
                  agentSpeaking ? "text-sky-300" : "text-neutral-500"
                }`}
              >
                {agentSpeaking ? "● Assistant speaking" : "○ Listening"}
              </span>
            </>
          )}
          {error && <span className="text-sm text-red-400">{error}</span>}
        </section>

        {/* Criticality + escalation */}
        <section className="mt-6 grid gap-4 md:grid-cols-3">
          <div className={`rounded-xl p-5 ring-1 md:col-span-2 ${style.bg} ${style.ring}`}>
            <div className="flex items-baseline gap-3">
              <span className={`text-4xl font-bold tabular-nums ${style.text}`}>{level}</span>
              <span className={`text-lg font-semibold ${style.text}`}>
                {stream.state?.label ?? "Awaiting assessment"}
              </span>
            </div>
            <p className="mt-2 text-sm text-neutral-300">
              {stream.state?.rationale ?? "No assessment yet."}
            </p>
            {stream.escalation && (
              <p className="mt-3 rounded-md bg-red-950/60 px-3 py-2 text-sm text-red-200 ring-1 ring-red-800">
                Clinician bridged — {stream.escalation.reason}
              </p>
            )}
          </div>

          {/* The latency story, live. */}
          <div className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-5">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-neutral-300">Moss retrieval</h2>
              <span
                className={`text-xs font-medium ${
                  budgetOk ? "text-emerald-400" : "text-amber-400"
                }`}
              >
                {budgetOk ? "within 10ms budget" : "over budget"}
              </span>
            </div>
            <div className="mt-3 grid grid-cols-3 gap-2 text-center">
              {[
                ["last", stream.latency.last],
                ["best", stream.latency.best],
                ["worst", stream.latency.worst],
              ].map(([label, value]) => (
                <div key={label as string}>
                  <div className="text-xl font-semibold tabular-nums text-neutral-100">
                    {value == null ? "—" : `${(value as number).toFixed(1)}`}
                  </div>
                  <div className="text-[11px] uppercase tracking-wide text-neutral-500">
                    {label as string} ms
                  </div>
                </div>
              ))}
            </div>
            <p className="mt-3 text-xs text-neutral-500">
              {stream.latency.count} in-process lookups, no vector database round trip.
            </p>
          </div>
        </section>

        {/* Transcript + timeline */}
        <section className="mt-6 grid gap-4 lg:grid-cols-2">
          <div className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-5">
            <h2 className="text-sm font-semibold text-neutral-300">Live transcript</h2>
            <div className="mt-3 max-h-80 space-y-2 overflow-y-auto pr-1">
              {stream.transcript.length === 0 && (
                <p className="text-sm text-neutral-600">Nothing heard yet.</p>
              )}
              {stream.transcript.map((line, i) => (
                <p key={i} className="text-sm leading-relaxed text-neutral-200">
                  <span className="mr-2 text-xs uppercase tracking-wide text-neutral-500">
                    {line.speaker}
                  </span>
                  {line.text}
                </p>
              ))}
            </div>
          </div>

          <div className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-5">
            <h2 className="text-sm font-semibold text-neutral-300">Incident timeline</h2>
            <div className="mt-3 max-h-80 space-y-2 overflow-y-auto pr-1">
              {timeline.length === 0 && (
                <p className="text-sm text-neutral-600">Nothing recorded yet.</p>
              )}
              {timeline.map((fact) => (
                <div key={fact.id} className="flex gap-3 text-sm">
                  <span className="w-14 shrink-0 font-mono text-xs text-neutral-500">
                    T+{Number(fact.elapsed_s || 0).toFixed(0)}s
                  </span>
                  <span className="w-24 shrink-0 text-xs uppercase tracking-wide text-neutral-500">
                    {fact.kind}
                  </span>
                  <span className="text-neutral-200">
                    {fact.text.replace(/^\[T\+\d+s\]\s*\w+:\s*/, "")}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </section>

        {stream.soap && (
          <section className="mt-6 rounded-xl border border-neutral-800 bg-neutral-900/50 p-5">
            <h2 className="text-sm font-semibold text-neutral-300">SOAP note</h2>
            <pre className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-neutral-200">
              {stream.soap}
            </pre>
          </section>
        )}
      </div>
    </main>
  );
}
