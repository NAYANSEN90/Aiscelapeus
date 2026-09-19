import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Produces a self-contained server tree for the release image and the
  // downloadable web artifact. Runtime credentials remain environment-only.
  output: "standalone",
  // This repository lives below a user-level package-lock.json. Without an
  // explicit root, Turbopack traces from that parent lockfile and emits an
  // incomplete standalone directory (server.js without `.next/server`).
  // `npm run build` and the container both execute from web/, so cwd is the
  // package boundary on every supported build path.
  outputFileTracingRoot: process.cwd(),
  turbopack: { root: process.cwd() },
};

export default nextConfig;
