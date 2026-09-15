"use client";

import { useCallback, useEffect, useState } from "react";
import { Room, RoomEvent, Track } from "livekit-client";
import { useTriageStream } from "@/lib/useTriageStream";
import { LEVEL_STYLES } from "@/lib/types";

/**
 * Clinician view. Joins an in-progress incident room, hears the live audio, and
 * reads the same triage state the responder sees -- so a doctor arriving
 * mid-incident is caught up by the Moss-recorded timeline rather than by asking
 * the responder to recap while they are busy.
 */
export default function DoctorPage() {
  const [room] = useState(() => new Room({ adaptiveStream: true }));
  const [sessionId, setSessionId] = useState("");
  const [joined, setJoined] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [talking, setTalking] = useState(false);

  const stream = useTriageStream(room);

  useEffect(() => {
    const onTrack = (track: Track) => {
      if (track.kind === Track.Kind.Audio) {
        const el = track.attach();
        el.autoplay = true;
        el.style.display = "none";
        document.body.appendChild(el);
      }
    };
    room.on(RoomEvent.TrackSubscribed, onTrack);
    return () => {
      room.off(RoomEvent.TrackSubscribed, onTrack);
    };
  }, [room]);

  const join = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch("/api/token", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          room: sessionId.trim(),
          identity: `clinician-${Math.random().toString(36).slice(2, 8)}`,
          role: "clinician",
        }),
      });
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.error ?? "Could not get a token");
      await room.connect(payload.url, payload.token);
      // Join muted. The clinician opens their mic deliberately.
      await room.localParticipant.setMicrophoneEnabled(false);
      setJoined(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [room, sessionId]);

  const toggleTalk = useCallback(async () => {
    const next = !talking;
    await room.localParticipant.setMicrophoneEnabled(next);
    setTalking(next);
  }, [room, talking]);

  const level = stream.state?.level ?? 1;
  const style = LEVEL_STYLES[level] ?? LEVEL_STYLES[1];

  return (
    <main className="min-h-screen bg-neutral-950 text-neutral-100">
      <div className="mx-auto max-w-5xl px-4 py-6 md:px-8">
        <header className="border-b border-neutral-800 pb-5">
          <h1 className="text-2xl font-semibold tracking-tight">Clinician dashboard</h1>
          <p className="text-sm text-neutral-400">
            Join an active incident. You arrive with the full recorded timeline.
          </p>
        </header>

        {!joined ? (
          <section className="mt-6 flex flex-wrap items-center gap-3">
            <input
              value={sessionId}
              onChange={(e) => setSessionId(e.target.value)}
              placeholder="incident id, e.g. inc-0914-1832-a4f2"
              className="w-80 rounded-lg border border-neutral-700 bg-neutral-900 px-4 py-3 font-mono text-sm outline-none focus:border-sky-600"
            />
            <button
              onClick={join}
              disabled={sessionId.trim().length < 3}
              className="rounded-lg bg-sky-600 px-6 py-3 font-semibold text-white hover:bg-sky-500 disabled:opacity-50"
            >
              Join incident
            </button>
            {error && <span className="text-sm text-red-400">{error}</span>}
          </section>
        ) : (
          <>
            <section className="mt-6 flex flex-wrap items-center gap-3">
              <button
                onClick={toggleTalk}
                className={`rounded-lg px-6 py-3 font-semibold transition ${
                  talking
                    ? "bg-emerald-600 text-white hover:bg-emerald-500"
                    : "bg-neutral-700 text-neutral-200 hover:bg-neutral-600"
                }`}
              >
                {talking ? "You are live on the call" : "Open microphone"}
              </button>
              <span className="font-mono text-xs text-neutral-500">{sessionId}</span>
            </section>

            <section className={`mt-6 rounded-xl p-5 ring-1 ${style.bg} ${style.ring}`}>
              <div className="flex items-baseline gap-3">
                <span className={`text-4xl font-bold tabular-nums ${style.text}`}>{level}</span>
                <span className={`text-lg font-semibold ${style.text}`}>
                  {stream.state?.label ?? "Awaiting assessment"}
                </span>
              </div>
              <p className="mt-2 text-sm text-neutral-300">
                {stream.state?.definition ?? ""}
              </p>
              <p className="mt-1 text-sm text-neutral-400">{stream.state?.rationale ?? ""}</p>
            </section>

            <section className="mt-6 grid gap-4 lg:grid-cols-2">
              <div className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-5">
                <h2 className="text-sm font-semibold text-neutral-300">
                  Incident timeline (Moss)
                </h2>
                <div className="mt-3 max-h-96 space-y-2 overflow-y-auto pr-1">
                  {stream.findings.length === 0 && (
                    <p className="text-sm text-neutral-600">Nothing recorded yet.</p>
                  )}
                  {[...stream.findings].reverse().map((fact) => (
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

              <div className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-5">
                <h2 className="text-sm font-semibold text-neutral-300">
                  Retrieval log
                  <span className="ml-2 font-normal text-neutral-500">
                    what the assistant looked up, and how fast
                  </span>
                </h2>
                <div className="mt-3 max-h-96 space-y-2 overflow-y-auto pr-1">
                  {stream.retrievals.length === 0 && (
                    <p className="text-sm text-neutral-600">No lookups yet.</p>
                  )}
                  {stream.retrievals.map((r, i) => (
                    <div key={i} className="text-sm">
                      <div className="flex items-baseline gap-2">
                        <span
                          className={`font-mono text-xs ${
                            r.within_budget ? "text-emerald-400" : "text-amber-400"
                          }`}
                        >
                          {r.wall_ms == null ? "—" : `${r.wall_ms.toFixed(1)}ms`}
                        </span>
                        <span className="text-xs uppercase tracking-wide text-neutral-500">
                          {r.kind}
                        </span>
                        <span className="text-neutral-300">{r.query}</span>
                      </div>
                      {r.hits.length > 0 && (
                        <div className="ml-14 text-xs text-neutral-500">
                          {r.hits.map((h) => h.title ?? h.text ?? h.id).join(" · ")}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            </section>

            <section className="mt-6 rounded-xl border border-neutral-800 bg-neutral-900/50 p-5">
              <h2 className="text-sm font-semibold text-neutral-300">Transcript</h2>
              <div className="mt-3 max-h-64 space-y-2 overflow-y-auto pr-1">
                {stream.transcript.map((line, i) => (
                  <p key={i} className="text-sm text-neutral-200">
                    <span className="mr-2 text-xs uppercase tracking-wide text-neutral-500">
                      {line.speaker}
                    </span>
                    {line.text}
                  </p>
                ))}
              </div>
            </section>
          </>
        )}
      </div>
    </main>
  );
}
