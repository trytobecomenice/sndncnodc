import { and, desc, eq, sql } from "drizzle-orm";
import { botMarketEvent, botEventLog, botRiskState, db, paperTrade, walletProfile } from "@/lib/db";

// Always re-read the shared DB per request — bot.py and the copy-trading
// operator loop write to it independently of this Next.js process, so
// nothing here should ever be cached across requests.
export const dynamic = "force-dynamic";

// Mirrors config.py's current risk-parameter values. No shared config
// module exists between the Python bot and this Next.js app (confirmed —
// grepped packages/ and apps/ for these names, nothing came back except
// the unrelated weather package's own constants file), so this is a
// manually-synced duplicate, same category of known drift risk as
// wss_listener.py's own OUTCOME_TOKEN_DECIMALS env var mirroring
// config.py's OUTCOME_TOKEN_DECIMALS_ASSUMPTION. Update these four numbers
// if config.py's values change.
const RISK_CONFIG = {
  paperBankrollUsd: 1125.0,
  maxTotalExposureUsd: 1250.0,
  equityFloorUsd: 900.0,
  maxDrawdownFromPeakUsd: 450.0,
};

const closedTradeFilter = and(eq(paperTrade.status, "closed"), eq(paperTrade.strategy, "bot_filtered"));
const openTradeFilter = and(eq(paperTrade.status, "open"), eq(paperTrade.strategy, "bot_filtered"));

// Same per-dollar-staked blended EV metric as db.compute_live_edge_pct()
// (mean of realized_pnl_usd/cost_basis_usd across closed trades) — one
// definition of "edge," computed the same way on both sides.
const evExpr = sql<number | null>`avg(${paperTrade.realizedPnlUsd} / nullif(${paperTrade.costBasisUsd}, 0))`;
const winsExpr = sql<number>`sum(case when ${paperTrade.realizedPnlUsd} > 0 then 1 else 0 end)`;

async function getOverviewData() {
  const closedTrades = await db.select().from(paperTrade).where(closedTradeFilter);
  const openPositions = await db.select().from(paperTrade).where(openTradeFilter);

  const totalRealizedPnl = closedTrades.reduce((sum, t) => sum + (t.realizedPnlUsd ?? 0), 0);
  const wins = closedTrades.filter((t) => (t.realizedPnlUsd ?? 0) > 0).length;
  const losses = closedTrades.length - wins;
  const winRate = closedTrades.length > 0 ? (wins / closedTrades.length) * 100 : null;
  const liveEdgePct =
    closedTrades.length > 0
      ? (closedTrades.reduce((sum, t) => sum + (t.costBasisUsd > 0 ? (t.realizedPnlUsd ?? 0) / t.costBasisUsd : 0), 0) /
          closedTrades.filter((t) => t.costBasisUsd > 0).length) *
        100
      : null;

  const currentExposure = openPositions.reduce((sum, p) => sum + p.costBasisUsd, 0);
  // Equity AT COST — bankroll + realized PnL only, deliberately excluding
  // unrealized mark-to-market (that needs a live price fetch per open
  // position, which this server component doesn't do). Labeled honestly
  // below rather than presented as a true current equity figure.
  const equityAtCost = RISK_CONFIG.paperBankrollUsd + totalRealizedPnl;

  const riskStateRows = await db.select().from(botRiskState);
  const riskState = Object.fromEntries(
    riskStateRows.map((r) => [r.key, JSON.parse(r.valueJson) as unknown])
  );
  const equityHwm = (riskState["equity_hwm"] as number | undefined) ?? null;
  const killSwitch = riskState["kill_switch"] as { triggered_at?: string; reasons?: string[] } | null | undefined;
  const drawdownFromPeak = equityHwm !== null ? equityHwm - equityAtCost : null;
  // Published by bot.py's main() at every startup (2026-07-27) -- the real
  // "is this wallet actually being copied right now" answer.
  // wallet_profile.status is a DIFFERENT, TS-scorer-owned field (that
  // pipeline's own recommendation, not config.TRACKED_TRADERS membership)
  // and confirmed live to drift from it (only 2 of 17 real tracked
  // wallets happened to have status='track' when this was found).
  const trackedTraders = (riskState["tracked_traders"] as Record<string, string> | undefined) ?? {};
  const trackedCount = Object.keys(trackedTraders).length;
  // Queried directly against ALL tracked addresses (not filtered through
  // walletEv, which only has wallets with at least one closed trade) --
  // a freshly-tracked wallet with zero closes yet is still "actively
  // copying" and must count here.
  const mutedRows = trackedCount > 0
    ? await db
        .select({ walletAddress: walletProfile.walletAddress })
        .from(walletProfile)
        .where(eq(walletProfile.circuitBreakerMuted, true))
    : [];
  const mutedTrackedCount = mutedRows.filter((r) => r.walletAddress.toLowerCase() in trackedTraders).length;
  const activelyCopyingCount = trackedCount - mutedTrackedCount;

  const walletEv = await db
    .select({
      walletAddress: paperTrade.walletAddress,
      nickname: walletProfile.nickname,
      circuitBreakerMuted: walletProfile.circuitBreakerMuted,
      tradeCount: sql<number>`count(*)`,
      totalPnl: sql<number>`sum(${paperTrade.realizedPnlUsd})`,
      evPct: evExpr,
      wins: winsExpr,
    })
    .from(paperTrade)
    .leftJoin(walletProfile, eq(paperTrade.walletAddress, walletProfile.walletAddress))
    .where(closedTradeFilter)
    .groupBy(paperTrade.walletAddress, walletProfile.nickname, walletProfile.circuitBreakerMuted)
    .orderBy(desc(evExpr));

  const categoryBreakdown = await db
    .select({
      category: botMarketEvent.category,
      tradeCount: sql<number>`count(*)`,
      totalPnl: sql<number>`sum(${paperTrade.realizedPnlUsd})`,
      evPct: evExpr,
      wins: winsExpr,
    })
    .from(paperTrade)
    .leftJoin(botMarketEvent, eq(paperTrade.marketSlug, botMarketEvent.marketSlug))
    .where(closedTradeFilter)
    .groupBy(botMarketEvent.category)
    .orderBy(desc(sql`sum(${paperTrade.realizedPnlUsd})`));

  const recentEvents = await db.select().from(botEventLog).orderBy(desc(botEventLog.timestamp)).limit(15);

  return {
    openPositions,
    totalRealizedPnl,
    winRate,
    wins,
    losses,
    closedCount: closedTrades.length,
    liveEdgePct,
    currentExposure,
    equityAtCost,
    equityHwm,
    drawdownFromPeak,
    killSwitch,
    trackedTraders,
    trackedCount,
    activelyCopyingCount,
    walletEv,
    categoryBreakdown,
    recentEvents,
  };
}

function pctColor(pct: number | null) {
  if (pct === null) return "text-neutral-500";
  return pct >= 0 ? "text-emerald-400" : "text-red-400";
}

function fmtPct(pct: number | null, digits = 1) {
  if (pct === null) return "—";
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(digits)}%`;
}

function fmtUsd(usd: number | null) {
  if (usd === null) return "—";
  return `${usd >= 0 ? "+" : ""}$${usd.toFixed(2)}`;
}

function KpiCard({ label, value, valueClass, sub }: { label: string; value: string; valueClass?: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 p-4">
      <div className="text-xs uppercase tracking-wide text-neutral-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${valueClass ?? "text-neutral-100"}`}>{value}</div>
      {sub ? <div className="mt-1 text-xs text-neutral-500">{sub}</div> : null}
    </div>
  );
}

function LimitBar({ label, current, limit, dangerAbove = 0.85 }: { label: string; current: number; limit: number; dangerAbove?: number }) {
  const ratio = limit > 0 ? Math.min(1, current / limit) : 0;
  const color = ratio >= dangerAbove ? "bg-red-500" : ratio >= 0.6 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-xs">
        <span className="text-neutral-400">{label}</span>
        <span className="tabular-nums text-neutral-300">
          ${current.toFixed(2)} / ${limit.toFixed(2)}
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-neutral-800">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${ratio * 100}%` }} />
      </div>
    </div>
  );
}

// "Actively copying" is determined from bot.py's real config.TRACKED_TRADERS
// membership (published to bot_risk_state, see getOverviewData) plus
// circuit_breaker_muted -- NOT wallet_profile.status, which answers a
// different question entirely (see that field's own comment above).
function StatusPill({ tracked, muted }: { tracked: boolean; muted: boolean | null }) {
  if (!tracked) {
    return <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-neutral-400">not tracked</span>;
  }
  if (muted) {
    return <span className="rounded bg-red-900/40 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-red-400">muted</span>;
  }
  return <span className="rounded bg-emerald-900/40 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-emerald-400">copying</span>;
}

export default async function OverviewPage() {
  const data = await getOverviewData();

  return (
    <main className="min-h-screen bg-neutral-950 p-8 text-neutral-100">
      <h1 className="text-2xl font-semibold">Copybot Overview</h1>
      <p className="mt-1 mb-8 text-sm text-neutral-400">
        Reads live from the shared SQLite DB (data/app.db) — the same store bot.py writes to.
      </p>

      {/* --- Top KPI strip ------------------------------------------------ */}
      <div className="mb-8 grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-7">
        <KpiCard
          label="Live edge (per $ staked)"
          value={fmtPct(data.liveEdgePct, 1)}
          valueClass={pctColor(data.liveEdgePct)}
          sub="mean(pnl / cost basis)"
        />
        <KpiCard label="Realized PnL" value={fmtUsd(data.totalRealizedPnl)} valueClass={pctColor(data.totalRealizedPnl)} />
        <KpiCard
          label="Win rate"
          value={data.winRate !== null ? `${data.winRate.toFixed(1)}%` : "—"}
          sub={`${data.wins}W / ${data.losses}L (${data.closedCount})`}
        />
        <KpiCard
          label="Tracked wallets"
          value={String(data.trackedCount)}
          sub={`${data.activelyCopyingCount} actively copying`}
        />
        <KpiCard label="Open positions" value={String(data.openPositions.length)} sub={`$${data.currentExposure.toFixed(2)} deployed`} />
        <KpiCard
          label="Equity (at cost)"
          value={`$${data.equityAtCost.toFixed(2)}`}
          sub={`bankroll $${RISK_CONFIG.paperBankrollUsd.toFixed(2)} · excl. unrealized`}
        />
        <KpiCard
          label="Kill switch"
          value={data.killSwitch ? "LATCHED" : "clear"}
          valueClass={data.killSwitch ? "text-red-400" : "text-emerald-400"}
          sub={data.killSwitch?.triggered_at}
        />
      </div>

      {/* --- Risk limits --------------------------------------------------- */}
      <section className="mb-8 rounded-lg border border-neutral-800 bg-neutral-900 p-4">
        <h2 className="mb-3 text-sm font-medium uppercase tracking-wide text-neutral-400">Risk limits</h2>
        <div className="grid gap-4 md:grid-cols-2">
          <LimitBar label="Total exposure" current={data.currentExposure} limit={RISK_CONFIG.maxTotalExposureUsd} />
          <LimitBar
            label="Drawdown from peak"
            current={data.drawdownFromPeak ?? 0}
            limit={RISK_CONFIG.maxDrawdownFromPeakUsd}
          />
        </div>
        <p className="mt-3 text-xs text-neutral-500">
          Equity floor ${RISK_CONFIG.equityFloorUsd.toFixed(2)} · peak equity{" "}
          {data.equityHwm !== null ? `$${data.equityHwm.toFixed(2)}` : "—"}
        </p>
      </section>

      {/* --- Per-wallet EV --------------------------------------------------- */}
      <section className="mb-8">
        <h2 className="mb-3 text-lg font-medium">Wallet performance (by live edge)</h2>
        <div className="overflow-x-auto rounded-lg border border-neutral-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-neutral-900 text-neutral-400">
              <tr>
                <th className="px-3 py-2 font-medium">Wallet</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Trades</th>
                <th className="px-3 py-2 font-medium">Win rate</th>
                <th className="px-3 py-2 font-medium">Edge (per $)</th>
                <th className="px-3 py-2 font-medium">Total PnL</th>
              </tr>
            </thead>
            <tbody>
              {data.walletEv.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-3 py-4 text-center text-neutral-500">
                    No closed trades yet.
                  </td>
                </tr>
              ) : (
                data.walletEv.map((w) => {
                  const winRate = w.tradeCount > 0 ? (w.wins / w.tradeCount) * 100 : null;
                  return (
                    <tr key={w.walletAddress} className="border-t border-neutral-800">
                      <td className="px-3 py-2 text-neutral-300">
                        <span className="font-medium">{w.nickname ?? "—"}</span>{" "}
                        <span className="font-mono text-xs text-neutral-500">{w.walletAddress.slice(0, 8)}…</span>
                      </td>
                      <td className="px-3 py-2">
                        <StatusPill
                          tracked={w.walletAddress.toLowerCase() in data.trackedTraders}
                          muted={w.circuitBreakerMuted}
                        />
                      </td>
                      <td className="px-3 py-2 tabular-nums text-neutral-300">{w.tradeCount}</td>
                      <td className="px-3 py-2 tabular-nums text-neutral-300">{winRate !== null ? `${winRate.toFixed(0)}%` : "—"}</td>
                      <td className={`px-3 py-2 tabular-nums font-medium ${pctColor(w.evPct !== null ? w.evPct * 100 : null)}`}>
                        {fmtPct(w.evPct !== null ? w.evPct * 100 : null)}
                      </td>
                      <td className={`px-3 py-2 tabular-nums ${pctColor(w.totalPnl)}`}>{fmtUsd(w.totalPnl)}</td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* --- Category breakdown --------------------------------------------- */}
      <section className="mb-8">
        <h2 className="mb-3 text-lg font-medium">By category</h2>
        <div className="overflow-x-auto rounded-lg border border-neutral-800">
          <table className="w-full text-left text-sm">
            <thead className="bg-neutral-900 text-neutral-400">
              <tr>
                <th className="px-3 py-2 font-medium">Category</th>
                <th className="px-3 py-2 font-medium">Trades</th>
                <th className="px-3 py-2 font-medium">Win rate</th>
                <th className="px-3 py-2 font-medium">Edge (per $)</th>
                <th className="px-3 py-2 font-medium">Total PnL</th>
              </tr>
            </thead>
            <tbody>
              {data.categoryBreakdown.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-3 py-4 text-center text-neutral-500">
                    No closed trades yet.
                  </td>
                </tr>
              ) : (
                data.categoryBreakdown.map((c) => {
                  const winRate = c.tradeCount > 0 ? (c.wins / c.tradeCount) * 100 : null;
                  return (
                    <tr key={c.category ?? "uncategorized"} className="border-t border-neutral-800">
                      <td className="px-3 py-2 capitalize text-neutral-300">{c.category ?? "uncategorized"}</td>
                      <td className="px-3 py-2 tabular-nums text-neutral-300">{c.tradeCount}</td>
                      <td className="px-3 py-2 tabular-nums text-neutral-300">{winRate !== null ? `${winRate.toFixed(0)}%` : "—"}</td>
                      <td className={`px-3 py-2 tabular-nums font-medium ${pctColor(c.evPct !== null ? c.evPct * 100 : null)}`}>
                        {fmtPct(c.evPct !== null ? c.evPct * 100 : null)}
                      </td>
                      <td className={`px-3 py-2 tabular-nums ${pctColor(c.totalPnl)}`}>{fmtUsd(c.totalPnl)}</td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* --- Open positions --------------------------------------------- */}
      <section className="mb-8">
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
                    <td className="px-3 py-2 font-mono text-xs text-neutral-300">{p.walletAddress.slice(0, 10)}…</td>
                    <td className="px-3 py-2 text-neutral-300">{p.marketTitle || p.marketSlug}</td>
                    <td className="px-3 py-2 text-neutral-300">{p.outcome}</td>
                    <td className="px-3 py-2 tabular-nums text-neutral-300">{p.ourShares.toFixed(2)}</td>
                    <td className="px-3 py-2 tabular-nums text-neutral-300">${p.avgEntryPrice.toFixed(3)}</td>
                    <td className="px-3 py-2 tabular-nums text-neutral-300">${p.costBasisUsd.toFixed(2)}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* --- Recent activity ---------------------------------------------- */}
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

      <p className="mt-10 text-xs text-neutral-500">
        Paper trading only — no real trades are placed. See docs/copy-trading/SAFETY.md.
      </p>
    </main>
  );
}
