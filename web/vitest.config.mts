import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Native resolution of the `@/*` alias the app already uses.
  resolve: { tsconfigPaths: true },
  test: {
    environment: "jsdom",
    include: ["{app,lib}/**/*.test.{ts,tsx}", "__tests__/**/*.test.{ts,tsx}"],
    // The contract suite is a separate CI job: it verifies that every golden
    // fixture written by the Python side parses here. Excluded from the
    // default run so a missing golden fails the contract job by name rather
    // than showing up as an unrelated unit-test failure.
    exclude: ["**/node_modules/**", "**/*.contract.test.{ts,tsx}"],
  },
});
