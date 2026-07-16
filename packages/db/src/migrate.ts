import { migrate } from "drizzle-orm/libsql/migrator";
import { db, rawClient } from "./client";

async function main() {
  await migrate(db, { migrationsFolder: new URL("../drizzle", import.meta.url).pathname });

  // Journal mode is a property stored in the database file header, not a
  // per-connection setting — set it once, right after the first migration
  // creates the file. busy_timeout IS per-connection and every writer
  // (this client, and Python's db.py) must set it independently.
  await rawClient.execute("PRAGMA journal_mode=WAL;");
  await rawClient.execute("PRAGMA busy_timeout=5000;");

  console.log("Migration complete, WAL mode + busy_timeout applied.");
  rawClient.close();
}

main().catch((err) => {
  console.error("Migration failed:", err);
  process.exit(1);
});
