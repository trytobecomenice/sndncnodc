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
- `store/` — SQLite-backed order persistence + idempotent creation
  (Session 2). A NEW `oms_order` table alongside `pending_execution` (not
  a replacement) — the two coexist in `data/app.db` during the
  parallel-run validation period. Uses `modernc.org/sqlite` (pure Go, no
  cgo) rather than `mattn/go-sqlite3`, and `SetMaxOpenConns(1)` + WAL mode
  to avoid `SQLITE_BUSY` under concurrent writers (caught live by this
  package's own race test before the fix).

- `httpserver/` — plain `net/http` service exposing the store (Session 3):
  `POST /orders` (idempotent create, 201/200), `GET /orders/{id}`,
  `POST /orders/{id}/cancel` (Pending → Invalidated via `order.Order`'s own
  `Transition()`, 409 if the order's current state disallows it). No
  framework, no gRPC — see the package doc for why.
- `cmd/omsd/` — the runnable binary (`go run ./cmd/omsd`, env
  `OMS_DB_PATH`/`OMS_ADDR`). Not started by anything in production yet —
  no systemd unit, no `watchdog.py`/`autodeploy.py` awareness.

- `bullpen/` — ports `bullpen_client.py`'s subprocess-call contract
  field-for-field (Session 4): `RunJSON` (retry policy, exit-code
  handling, `TimeoutError`/`AuthError`), `RequireFilled`,
  `ExtractFillPrice`, `ExtractFilledShares`, `ExtractOrderID`,
  `ExtractOrderStatus`. The actual `bullpen` subprocess call is
  never invoked by tests — `Runner.exec` is an injectable seam
  (`realExec` for production, a fake for tests), so nothing here
  depends on the real `bullpen` binary being installed. Still not
  wired to a live call site.

Session 5 wires `bot.py` in as a client (paper path only). None of this is
wired into `bot.py` or `LIVE_MODE` yet — see the roadmap doc's Phase 2
section for the full session-by-session plan and its paper-mode-first
validation discipline.

## Development

```sh
cd oms
go test ./...
go vet ./...
gofmt -l .   # should print nothing
```
