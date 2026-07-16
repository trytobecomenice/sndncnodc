// A small "worker pool" helper for calling slow external APIs (like the
// `bullpen` CLI) over a list of things without doing them one at a time
// AND without firing all of them off simultaneously.
//
// WHY THIS EXISTS: imagine you need to look up stats for 160 wallets, and
// each lookup takes ~9 seconds. One at a time, that's 24 minutes. Firing
// all 160 at once would hammer the API (and probably start timing out even
// more than it already does). A concurrency pool is the middle ground: run
// a handful of lookups at the same time (say, 5), and as soon as one
// finishes, start the next one — so you get most of the speed-up from
// parallelism, capped at a safe level.

/**
 * Runs `worker` over every item in `items`, keeping at most `limit` calls to
 * `worker` in flight at once.
 *
 * INPUT:
 *   - items:  the list of things to process (e.g. an array of wallet addresses)
 *   - limit:  the maximum number of `worker` calls allowed to run at the same time
 *   - worker: an async function that processes ONE item and returns a result
 *
 * OUTPUT:
 *   - a Promise that resolves to an array of results, in the SAME ORDER as
 *     `items` — even though the underlying work happens out of order, the
 *     result for items[3] always ends up at results[3].
 */
export async function mapWithConcurrency<T, R>(
  items: T[],
  limit: number,
  worker: (item: T, index: number) => Promise<R>
): Promise<R[]> {
  const results: R[] = new Array(items.length);

  // `nextIndex` is shared state: every "lane" below pulls the next unclaimed
  // item from this shared counter, so no two lanes ever process the same
  // item twice.
  let nextIndex = 0;

  async function runOneLane(): Promise<void> {
    while (true) {
      const currentIndex = nextIndex;
      nextIndex += 1;
      if (currentIndex >= items.length) return; // no more work left
      results[currentIndex] = await worker(items[currentIndex], currentIndex);
    }
  }

  // Start up to `limit` lanes (fewer if there aren't even that many items),
  // and wait for every lane to drain the queue.
  const laneCount = Math.min(limit, items.length);
  const lanes = Array.from({ length: laneCount }, () => runOneLane());
  await Promise.all(lanes);

  return results;
}
