import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  turbopack: {
    // Explicit monorepo root, not Turbopack's auto-guess. Workspace membership can change what
    // Turbopack infers from the lockfile; this repo's actual root is two levels above the app.
    root: path.join(__dirname, "..", ".."),
  },
};

export default nextConfig;
