import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  turbopack: {
    // Explicit monorepo root, not Turbopack's auto-guess. Without this, adding/removing a pnpm
    // workspace member (e.g. packages/weather) can change what Turbopack infers as the project
    // root from the lockfile, and it starts erroring "couldn't find the Next.js package from
    // the project directory" instead of just picking the same root it used before — happened
    // 2026-07-19 right after packages/weather was added. This repo's actual root is two levels
    // up from apps/dashboard.
    root: path.join(__dirname, "..", ".."),
  },
};

export default nextConfig;
