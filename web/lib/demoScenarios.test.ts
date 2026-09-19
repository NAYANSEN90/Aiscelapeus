import { describe, expect, it } from "vitest";
import { applyDemoFrame, DEMO_SCENARIOS, scenarioById } from "./demoScenarios";
import { EMPTY_TRIAGE_STREAM, parseTriageEnvelope } from "./triageStream";

describe("judge demo scenarios", () => {
  it("uses unique IDs, non-empty frames, and a monotonic replay clock", () => {
    const ids = DEMO_SCENARIOS.map((scenario) => scenario.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const scenario of DEMO_SCENARIOS) {
      expect(scenario.frames.length).toBeGreaterThan(0);
      expect(scenario.frames[0].atMs).toBe(0);
      expect(scenario.frames.map((frame) => frame.atMs)).toEqual(
        [...scenario.frames.map((frame) => frame.atMs)].sort((a, b) => a - b),
      );
    }
  });

  it("re-enters every fixture through the same untrusted JSON parser as LiveKit", () => {
    for (const scenario of DEMO_SCENARIOS) {
      for (const frame of scenario.frames) {
        const wireValue = JSON.parse(JSON.stringify(frame.envelope));
        expect(parseTriageEnvelope(wireValue), `${scenario.id}: ${frame.caption}`).toEqual(
          frame.envelope,
        );
      }
    }
  });

  it("shows requested before joined in the arrest counterfactual", () => {
    const scenario = scenarioById("poolside-arrest");
    const states = scenario.frames.flatMap((frame) => {
      if (frame.envelope.topic === "triage.state") return [frame.envelope.data.escalation];
      if (frame.envelope.topic === "triage.snapshot") {
        return [frame.envelope.data.state.escalation];
      }
      return [];
    });

    expect(states).toContain("requested");
    expect(states.at(-1)).toBe("clinician_joined");
  });

  it("replays the arrest into a Level 5 joined state with one recorded fact", () => {
    const final = scenarioById("poolside-arrest").frames.reduce(
      (stream, frame) => applyDemoFrame(stream, frame),
      EMPTY_TRIAGE_STREAM,
    );

    expect(final.state).toMatchObject({
      level: 5,
      escalation: "clinician_joined",
      clinician_present: true,
    });
    expect(final.findings).toHaveLength(1);
    expect(final.latency).toMatchObject({ last: 4.2, best: 4.2, worst: 4.2, count: 1 });
  });

  it("keeps the minor scenario un-escalated", () => {
    const final = scenarioById("minor-cut").frames.reduce(
      (stream, frame) => applyDemoFrame(stream, frame),
      EMPTY_TRIAGE_STREAM,
    );

    expect(final.state).toMatchObject({ level: 1, escalation: "not_needed" });
    expect(final.escalation).toBeNull();
  });

  it("rejects an unknown scenario instead of silently playing another one", () => {
    expect(() => scenarioById("missing")).toThrow("unknown demo scenario");
  });

  it("refuses an invalid runtime frame before it reaches the reducer", () => {
    const frame = {
      ...DEMO_SCENARIOS[0].frames[0],
      envelope: { topic: "triage.state", data: {} },
    } as unknown as (typeof DEMO_SCENARIOS)[number]["frames"][number];
    expect(() => applyDemoFrame(EMPTY_TRIAGE_STREAM, frame)).toThrow("invalid demo frame");
  });
});
