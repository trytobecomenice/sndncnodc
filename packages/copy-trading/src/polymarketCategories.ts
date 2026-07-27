// Market -> category resolution for TypeScript scoring code — the TS twin of
// polymarket_simulator.py's resolve_market_category(). Needed by
// scoreWalletCategories.ts to bucket a reconstructed position by category
// before aggregating per-category stats.
//
// CATEGORY_TAG_SLUGS must be kept in sync with config.py's list on the Python
// side (bot.py reads the SAME bucket names back out of wallet_profile.
// category_scores_json's keys) — there is no shared config module between
// the two languages, so this is a manually-maintained duplicate, not a
// drift-proof single source of truth. Each slug verified live against a real
// event's `tags` array before being added (see config.py's comment for the
// specific events each one was confirmed against).
export const CATEGORY_TAG_SLUGS = ["politics", "sports", "crypto", "pop-culture"];

const GAMMA_API_HOST = "https://gamma-api.polymarket.com";
const REQUEST_HEADERS = {
  "User-Agent": "polymarket-copybot/1.0 (+personal research bot)",
  Accept: "application/json",
};

interface GammaMarket {
  events?: Array<{ slug: string }>;
}

interface GammaEvent {
  tags?: Array<{ slug?: string }>;
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: REQUEST_HEADERS });
  if (response.status !== 200) {
    throw new Error(`HTTP ${response.status} fetching ${url}`);
  }
  return (await response.json()) as T;
}

/**
 * Resolves a market's parent event slug via Gamma — the first step
 * resolveMarketCategory() needs. Returns null if the market isn't found or
 * has no parent event (never observed live, but not assumed impossible).
 */
export async function fetchMarketEventSlug(marketSlug: string): Promise<string | null> {
  const data = await getJson<GammaMarket[]>(`${GAMMA_API_HOST}/markets?slug=${encodeURIComponent(marketSlug)}`);
  if (!data || data.length === 0) {
    return null;
  }
  const events = data[0].events ?? [];
  return events.length > 0 ? events[0].slug : null;
}

/**
 * Buckets an event's tags against CATEGORY_TAG_SLUGS (first match wins, in
 * list order). Returns a matched category string, "other" (looked up fine,
 * no configured tag matched), or null (event_slug was falsy, or the lookup
 * failed) — same three-way return shape as the Python twin.
 *
 * MULTI-TAG AMBIGUITY, stated plainly, not hidden: one event can carry
 * several tags spanning both broad topics and narrow one-off labels —
 * confirmed live (Python side): the novelty event
 * "what-will-happen-before-gta-vi" carried BOTH "politics" and
 * "pop-culture" tags at once, with individual sub-markets covering wildly
 * different subjects. This function does not disambiguate per-market
 * within a shared event.
 */
export async function resolveCategoryForEvent(eventSlug: string | null): Promise<string | null> {
  if (!eventSlug) {
    return null;
  }
  let data: GammaEvent[];
  try {
    data = await getJson<GammaEvent[]>(`${GAMMA_API_HOST}/events?slug=${encodeURIComponent(eventSlug)}`);
  } catch {
    return null; // scoring refinement, not a trade-blocking check — degrade quietly
  }
  if (!data || data.length === 0) {
    return null;
  }

  const tagSlugs = new Set((data[0].tags ?? []).map((t) => t.slug).filter((s): s is string => Boolean(s)));
  for (const category of CATEGORY_TAG_SLUGS) {
    if (tagSlugs.has(category)) {
      return category;
    }
  }
  return "other";
}

/**
 * Convenience composition of the two steps above — what
 * scoreWalletCategories.ts actually calls per market_slug. Split into two
 * exported functions (rather than one) so each is independently testable
 * without mocking the other's network call.
 */
export async function resolveMarketCategory(marketSlug: string): Promise<string | null> {
  const eventSlug = await fetchMarketEventSlug(marketSlug).catch(() => null);
  return resolveCategoryForEvent(eventSlug);
}
