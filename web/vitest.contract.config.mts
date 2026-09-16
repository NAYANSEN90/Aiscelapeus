import { defineConfig } from "vitest/config";

// The Python and TypeScript sides of the event contract are hand-maintained
// and have already drifted. Golden JSON fixtures, written by the Python side
// from real payloads, are the shared artifact: this job proves the parser here
// accepts every one of them. It runs as its own CI job so drift is reported as
// drift rather than as an unrelated unit-test failure.
export default defineConfig({
  resolve: { tsconfigPaths: true },
  test: {
    environment: "node",
    include: ["**/*.contract.test.{ts,tsx}"],
    exclude: ["**/node_modules/**"],
  },
});
