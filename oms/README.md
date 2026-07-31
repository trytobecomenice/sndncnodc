# oms

Go order-management service — Phase 2 of the 4-layer architecture roadmap
(see `.claude/plans/async-questing-oasis.md` if present locally, or ask for
the roadmap doc). Reframed around order-state-lifecycle, not custody: this
service owns order *state* (pending/filled/expired/invalidated/
unknown_fill_state) and *decides* what to send, but actual signing/
submission stays on Bullpen CLI — no private-key custody moves into this
codebase (see `docs/copy-trading/SAFETY.md` §6).

Mirrors `packages/db/src/schema.ts`'s `pendingExecution` table state shape,
plus a new `unknown_fill_state` making bot.py's existing
`BullpenTimeoutError` doctrine ("log it, never auto-retry, leave for a
human to reconcile") a first-class state instead of an exception path.

## Layout

- `order/` — the pure order state machine (Session 1). No I/O, no
  database, no network — see its own package doc for why.

Later sessions add a SQLite-backed order store (Session 2), an HTTP
service (Session 3), and a Bullpen subprocess integration ported from
`bullpen_client.py` (Session 4). None of this is wired into `bot.py` or
`LIVE_MODE` yet — see the roadmap doc's Phase 2 section for the full
session-by-session plan and its paper-mode-first validation discipline.

## Development

```sh
cd oms
go test ./...
go vet ./...
gofmt -l .   # should print nothing
```
