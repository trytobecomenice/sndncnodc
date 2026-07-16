import { createClient } from "@libsql/client";
import { drizzle } from "drizzle-orm/libsql";
import { resolveDatabaseUrl, AUTH_TOKEN } from "./env";
import * as schema from "./schema";

// @libsql/client (not better-sqlite3), even for local dev: it works
// identically against a local file and a remote Turso/libSQL database, so
// the eventual Vercel/Turso swap is an env var change, not a code change.
const client = createClient({
  url: resolveDatabaseUrl(),
  authToken: AUTH_TOKEN,
});

export const db = drizzle(client, { schema });
export * from "./schema";
export { client as rawClient };
