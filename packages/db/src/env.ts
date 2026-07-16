import path from "node:path";
import { fileURLToPath } from "node:url";

// Resolve the same absolute data/app.db path regardless of which package's
// cwd this is invoked from — a relative path resolved against two different
// working directories is the classic way to accidentally create two separate
// database files (see docs/SAFETY.md).
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "../../../"); // packages/db/src -> repo root
const DEFAULT_DB_PATH = path.join(REPO_ROOT, "data", "app.db");

export function resolveDatabaseUrl(): string {
  const fromEnv = process.env.DATABASE_URL;
  if (fromEnv && fromEnv.startsWith("file:")) {
    const raw = fromEnv.slice("file:".length);
    const abs = path.isAbsolute(raw) ? raw : path.resolve(REPO_ROOT, raw);
    return `file:${abs}`;
  }
  if (fromEnv) return fromEnv; // libsql:// remote URL — used as-is
  return `file:${DEFAULT_DB_PATH}`;
}

export const AUTH_TOKEN = process.env.DATABASE_AUTH_TOKEN || undefined;
export { REPO_ROOT, DEFAULT_DB_PATH };
