import path from "node:path";
import { defineConfig } from "drizzle-kit";

// drizzle-kit loads this config through its own CJS transpile step, which
// doesn't resolve TS-authored ".js" extension imports the way tsx/Node ESM
// does — so this file inlines the same absolute-path logic as src/env.ts
// rather than importing it. Keep both in sync if the resolution rule changes.
function resolveDatabaseUrl(): string {
  const fromEnv = process.env.DATABASE_URL;
  const repoRoot = path.resolve(__dirname, "../../");
  if (fromEnv && fromEnv.startsWith("file:")) {
    const raw = fromEnv.slice("file:".length);
    const abs = path.isAbsolute(raw) ? raw : path.resolve(repoRoot, raw);
    return `file:${abs}`;
  }
  if (fromEnv) return fromEnv;
  return `file:${path.join(repoRoot, "data", "app.db")}`;
}

const url = resolveDatabaseUrl();

export default defineConfig({
  schema: "./src/schema.ts",
  out: "./drizzle",
  dialect: "sqlite",
  driver: url.startsWith("file:") ? undefined : "turso",
  dbCredentials: url.startsWith("file:")
    ? { url }
    : { url, authToken: process.env.DATABASE_AUTH_TOKEN },
});
