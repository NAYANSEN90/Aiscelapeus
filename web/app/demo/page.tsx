"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { applyDemoFrame, DEMO_SCENARIOS, scenarioById } from "@/lib/demoScenarios";
import { escalationMessage } from "@/lib/escalationPresentation";
import { EMPTY_TRIAGE_STREAM } from "@/lib/triageStream";
import { LEVEL_STYLES } from "@/lib/types";

export default function DemoPage() {
  const [scenarioId, setScenarioId] = useState(DEMO_SCENARIOS[0].id);
  const [cursor, setCursor] = useState(0);
  const [speed, setSpeed] = useState(1);
  const [playing, setPlaying] = useState(true);
  const [stream, setStream] = useState(EMPTY_TRIAGE_STREAM);
  const [replayError, setReplayError] = useState<string | null>(null);
  const scenario = scenarioById(scenarioId);
  const finished = cursor >= scenario.frames.length;

  useEffect(() => {
    if (!playing || finished) return;
    const frame = scenario.frames[cursor];
    const previousAt = cursor === 0 ? 0 : scenario.frames[cursor - 1].atMs;
    const delay = Math.max(80, (frame.atMs - previousAt) / speed);
    const timer = window.setTimeout(() => {
      try {
        setStream((current) => applyDemoFrame(current, frame));
        setCursor((current) => current + 1);
      } catch (error) {
        setReplayError(error instanceof Error ? error.message : "Replay frame rejected");
        setPlaying(false);
      }
    }, delay);
    return () => window.clearTimeout(timer);
  }, [cursor, finished, playing, scenario, speed]);

  const reset = (nextId = scenarioId) => {
    setScenarioId(nextId);
    setCursor(0);
    setStream(EMPTY_TRIAGE_STREAM);
    setReplayError(null);
    setPlaying(true);
  };

  const currentCaption = cursor === 0
    ? "Ready to replay"
    : scenario.frames[Math.min(cursor - 1, scenario.frames.length - 1)].caption;
  const level = stream.state?.level ?? 1;
  const style = LEVEL_STYLES[level] ?? LEVEL_STYLES[1];
  const escalation = escalationMessage(stream.state);
  const findings = useMemo(() => [...stream.findings].reverse(), [stream.findings]);
  const progress = scenario.frames.length === 0 ? 0 : (cursor / scenario.frames.length) * 100;

  return (
    <main className="min-h-screen bg-neutral-950 text-neutral-100">
      <div className="mx-auto max-w-6xl px-4 py-6 md:px-8">
        <header className="flex flex-wrap items-start justify-between gap-4 border-b border-neutral-800 pb-5">
          <div>
            <div className="mb-2 inline-flex rounded-full bg-violet-500/15 px-3 py-1 text-xs font-semibold uppercase tracking-wider text-violet-300 ring-1 ring-violet-500/30">
              Recorded simulation — no live patient
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">Aiscelapeus guided replay</h1>
            <p className="mt-1 max-w-2xl text-sm text-neutral-400">
              The exact event parser and reducer used by the LiveKit room, driven by deterministic fixtures so judges need no microphone, account, or API key.
            </p>
          </div>
          <Link href="/" className="text-sm text-sky-400 hover:underline">Try the live call →</Link>
        </header>

        <section className="mt-6 rounded-xl border border-neutral-800 bg-neutral-900/60 p-5">
          <div className="flex flex-wrap items-end gap-4">
            <label className="min-w-64 flex-1 text-xs uppercase tracking-wide text-neutral-500">
              Scenario
              <select
                value={scenarioId}
                onChange={(event) => reset(event.target.value)}
                className="mt-2 w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm normal-case text-neutral-100"
              >
                {DEMO_SCENARIOS.map((candidate) => (
                  <option key={candidate.id} value={candidate.id}>{candidate.title}</option>
                ))}
              </select>
            </label>
            <button
              onClick={() => finished ? reset() : setPlaying((value) => !value)}
              className="rounded-lg bg-violet-600 px-5 py-2 font-semibold hover:bg-violet-500"
            >
              {finished ? "Replay" : playing ? "Pause" : "Continue"}
            </button>
            <label className="text-xs uppercase tracking-wide text-neutral-500">
              Speed
              <select
                value={speed}
                onChange={(event) => setSpeed(Number(event.target.value))}
                className="ml-2 rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-2 text-sm text-neutral-100"
              >
                <option value={1}>1×</option>
                <option value={2}>2×</option>
                <option value={4}>4×</option>
              </select>
            </label>
          </div>
          <p className="mt-3 text-sm text-neutral-300">{scenario.description}</p>
          <p className="mt-1 text-xs text-neutral-500">Expected: {scenario.expectedOutcome}</p>
          <div
            className="mt-4 h-1.5 overflow-hidden rounded-full bg-neutral-800"
            role="progressbar"
            aria-label="Replay progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(progress)}
          >
            <div className="h-full bg-violet-500 transition-all" style={{ width: `${progress}%` }} />
          </div>
          <p className="mt-2 text-sm text-violet-200" aria-live="polite">{currentCaption}</p>
          {replayError && <p role="alert" className="mt-2 text-sm text-red-300">{replayError}</p>}
        </section>

        <section className="mt-6 grid gap-4 md:grid-cols-3">
          <div className={`rounded-xl p-5 ring-1 md:col-span-2 ${style.bg} ${style.ring}`}>
            <div className="flex items-baseline gap-3">
              <span className={`text-4xl font-bold tabular-nums ${style.text}`}>{level}</span>
              <span className={`text-lg font-semibold ${style.text}`}>
                {stream.state?.label ?? "Awaiting assessment"}
              </span>
            </div>
            <p className="mt-2 text-sm text-neutral-300">{stream.state?.rationale ?? "No assessment yet."}</p>
            {escalation && (
              <p className="mt-3 rounded-md bg-red-950/60 px-3 py-2 text-sm text-red-200 ring-1 ring-red-800">
                {escalation}
              </p>
            )}
          </div>
          <div className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-5">
            <h2 className="text-sm font-semibold text-neutral-300">Moss retrieval</h2>
            <div className="mt-3 text-3xl font-semibold tabular-nums">
              {stream.latency.last == null ? "—" : `${stream.latency.last.toFixed(1)} ms`}
            </div>
            <p className="mt-2 text-xs text-neutral-500">
              {stream.latency.count} replayed lookup{stream.latency.count === 1 ? "" : "s"}; deterministic fixture values, not a live latency claim.
            </p>
          </div>
        </section>

        <section className="mt-6 grid gap-4 lg:grid-cols-2">
          <div className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-5">
            <h2 className="text-sm font-semibold text-neutral-300">Transcript</h2>
            <div className="mt-3 space-y-2">
              {stream.transcript.length === 0 && <p className="text-sm text-neutral-600">Waiting for the first replayed turn.</p>}
              {stream.transcript.map((line, index) => (
                <p key={`${line.at}-${index}`} className="text-sm leading-relaxed text-neutral-200">
                  <span className="mr-2 text-xs uppercase tracking-wide text-neutral-500">{line.speaker}</span>
                  {line.text}
                </p>
              ))}
            </div>
          </div>
          <div className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-5">
            <h2 className="text-sm font-semibold text-neutral-300">Incident timeline</h2>
            <div className="mt-3 space-y-2">
              {findings.length === 0 && <p className="text-sm text-neutral-600">No recorded facts yet.</p>}
              {findings.map((finding) => (
                <div key={finding.id} className="flex gap-3 text-sm">
                  <span className="w-14 shrink-0 font-mono text-xs text-neutral-500">T+{finding.elapsed_s}s</span>
                  <span className="w-24 shrink-0 text-xs uppercase tracking-wide text-neutral-500">{finding.kind}</span>
                  <span>{finding.text.replace(/^\[T\+\d+s\]\s*\w+:\s*/, "")}</span>
                </div>
              ))}
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}
