"use client";

import { useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";

// Personal, single-viewer dashboard reading a SQLite file bot.py is also
// writing to concurrently -- 5s is the conservative end of the 3-5s ask,
// picked to avoid piling extra read load on that shared file for a page
// nobody's actually watching sub-5s-precision numbers on.
const REFRESH_INTERVAL_MS = 5000;

export default function AutoRefresh() {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  // router.refresh() re-runs OverviewPage's server-side Drizzle queries and
  // streams the new RSC payload down -- reuses getOverviewData() exactly as
  // written, no separate client-fetched JSON API needed for this.
  //
  // Timestamp stamping is deferred into a microtask rather than called
  // directly in the effect body -- react-hooks' set-state-in-effect rule
  // flags a bare synchronous setState() in an effect (cascading-render
  // risk); queueMicrotask makes this an actual callback, same category as
  // a timer/subscription firing, which the rule is fine with. Stamped once
  // immediately on mount (for the already-server-rendered initial data,
  // client-only so it can't cause a server/client hydration mismatch) and
  // again each time a refresh is kicked off.
  useEffect(() => {
    const stamp = () => queueMicrotask(() => setLastUpdated(new Date()));
    stamp();
    const id = setInterval(() => {
      startTransition(() => {
        router.refresh();
        stamp();
      });
    }, REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [router]);

  const timestamp = lastUpdated
    ? [lastUpdated.getHours(), lastUpdated.getMinutes(), lastUpdated.getSeconds()]
        .map((n) => String(n).padStart(2, "0"))
        .join(":")
    : null;

  return (
    <div className="flex items-center gap-2 text-xs text-neutral-500">
      <span
        className={`inline-block h-2 w-2 rounded-full ${
          isPending ? "animate-pulse bg-amber-400" : "bg-emerald-500"
        }`}
        aria-hidden="true"
      />
      <span>{isPending ? "Refreshing…" : timestamp ? `Last updated at ${timestamp}` : "Loading…"}</span>
    </div>
  );
}
