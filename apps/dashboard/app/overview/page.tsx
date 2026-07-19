import { and, desc, eq } from "drizzle-orm";
import { botEventLog, db, paperTrade, walletProfile } from "@/lib/db";

// Always re-read the shared DB per request — bot.py and the copy-trading
// operator loop write to it independently of this Next.js process, so
// nothing here should ever be cached across requests.
export const dynamic = "force-dynamic";

async function getOverviewData() {
  const openPositions = await db
    .select()
    .from(paperTrade)
    .where(and(eq(paperTrade.status, "open"), eq(paperTrade.strategy, "bot_filtered")));

  const closedTrades = await db
    .select()
    .from(paperTrade)
    .where(and(eq(paperTrade.status, "closed"), eq(paperTrade.strategy, "bot_filtered")));

  const totalRealizedPnl = closedTrades.reduce((sum, t) => sum + (t.realizedPnlUsd ?? 0), 0);
  const wins = closedTrades.filter((t) => (t.realizedPnlUsd ?? 0) > 0).length;
  const losses = closedTrades.length - wins;
  const winRate = closedTrades.length > 0 ? (wins / closedTrades.length) * 100 : null;

  const trackedWallets = await db.select().from(walletProfile).where(eq(walletProfile.status, "track"));

  const recentEvents = await db.select().from(botEventLog).orderBy(desc(botEventLog.timestamp)).limit(25);

  return {
    openPositions,
    totalRealizedPnl,
    winRate,
    wins,
    losses,
    closedCount: closedTrades.length,
    trackedWallets,
    recentEvents,
  };
}

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
      <div className="text-xs uppercase tracking-wide text-neutral-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-neutral-100">{value}</div>
      {sub ? <div className="mt-1 text-xs text-neutral-500">{sub}</div> : null}
    </div>
  );
}

function DemoBadge() {
  return (
    <span className="ml-2 rounded bg-amber-900/40 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-400">
      demo data
    </span>
  );
}

export default async function OverviewPage() {
  const data = await getOverviewData();
  const pnlClass = data.totalRealizedPnl >= 0 ? "text-emerald-400" : "text-red-400";

  return (
    <main className="min-h-screen bg-neutral-950 p-8 text-neutral-100">
      <h1 className="text-2xl font-semibold">Copybot Overview</h1>
      <p className="mt-1 mb-8 text-sm text-neutral-400">
        Reads live from the shared SQLite DB (data/app.db) — the same store bot.py and dashboard.py write to.
      </p>

      <div className="mb-10 grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatCard
          label="Realized paper PnL"
          value={`${data.totalRealizedPnl >= 0 ? "+" : ""}$${data.totalRealizedPnl.toFixed(2)}`}
        />
        <StatCard
          label="Win rate"
          value={data.winRate !== null ? `${data.winRate.toFixed(1)}%` : "—"}
          sub={`${data.wins}W / ${data.losses}L (${data.closedCount} closed)`}
        />
        <StatCard label="Open positions" value={String(data.openPositions.length)} />
        <StatCard label="Tracked wallets" value={String(data.trackedWallets.length)} />
      </div>

      <section className="mb-10">
        <h2 className="mb-3 text-lg font-medium">Open positions</h2>
        <div className="overflow-x-auto rounded-lg border border-neutral-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-neutral-900 text-neutral-400">
              <tr>
                <th className="px-3 py-2 font-medium">Wallet</th>
                <th className="px-3 py-2 font-medium">Market</th>
                <th className="px-3 py-2 font-medium">Outcome</th>
                <th className="px-3 py-2 font-medium">Shares</th>
                <th className="px-3 py-2 font-medium">Avg entry</th>
                <th className="px-3 py-2 font-medium">Cost basis</th>
              </tr>
            </thead>
            <tbody>
              {data.openPositions.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-3 py-4 text-center text-neutral-500">
                    No open positions.
                  </td>
                </tr>
              ) : (
                data.openPositions.map((p) => (
                  <tr key={p.id} className="border-t border-neutral-800">
                    <td className="px-3 py-2 font-mono text-xs text-neutral-300">
                      {p.walletAddress.slice(0, 10)}…
                      {p.isDemoData ? <DemoBadge /> : null}
                    </td>
                    <td className="px-3 py-2 text-neutral-300">{p.marketTitle || p.marketSlug}</td>
                    <td className="px-3 py-2 text-neutral-300">{p.outcome}</td>
                    <td className="px-3 py-2 text-neutral-300">{p.ourShares.toFixed(2)}</td>
                    <td className="px-3 py-2 text-neutral-300">${p.avgEntryPrice.toFixed(3)}</td>
                    <td className="px-3 py-2 text-neutral-300">${p.costBasisUsd.toFixed(2)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-medium">Recent activity</h2>
        <div className="overflow-x-auto rounded-lg border border-neutral-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-neutral-900 text-neutral-400">
              <tr>
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium">Event</th>
                <th className="px-3 py-2 font-medium">Market</th>
                <th className="px-3 py-2 font-medium">Outcome</th>
              </tr>
            </thead>
            <tbody>
              {data.recentEvents.length === 0 ? (
                <tr>
                  <td colSpan={4} className="px-3 py-4 text-center text-neutral-500">
                    No events yet.
                  </td>
                </tr>
              ) : (
                data.recentEvents.map((e) => (
                  <tr key={e.id} className="border-t border-neutral-800">
                    <td className="px-3 py-2 text-neutral-400">{e.timestamp.toISOString()}</td>
                    <td className="px-3 py-2 text-neutral-300">{e.eventType}</td>
                    <td className="px-3 py-2 text-neutral-300">{e.marketSlug ?? "—"}</td>
                    <td className="px-3 py-2 text-neutral-300">{e.outcome ?? "—"}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <p className={`mt-10 text-xs ${pnlClass}`}>
        Paper trading only — no real trades are placed. See docs/copy-trading/SAFETY.md.
      </p>
    </main>
  );
}
