// Re-exports the shared Drizzle client/schema so dashboard pages import from
// a stable local path (@/lib/db) rather than reaching into the package
// directly everywhere — packages/db remains the single source of truth for
// the schema itself.
export * from "@copybot/db";
