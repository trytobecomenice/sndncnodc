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
  `POST /orders/{id}/cancel` (Pending → Invalidated), and
  `POST /orders/{id}/transition` (Session 6: generic
  Filled/Expired/Invalidated, body `{"to": "filled"}` etc — added once a
  real caller needed all three terminal outcomes, not just cancellation).
  Every handler routes through `order.Order`'s own `Transition()` before
  writing to the store, 409 if the order's current state disallows it. No
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

`../oms_client.py` (repo root, not under `oms/` since it's Python) is the
HTTP client — `create_order()`/`get_order()`/`cancel_order()`/
`transition_order()`, unit-tested with a mocked connection and verified
live end-to-end against a real running `omsd`.

**Session 6 (2026-08-01): wired into bot.py, opt-in, off by default.**
`config.ENABLE_OMS_SHADOW_MIRROR` (default `False`) — when on,
`start_shadow_patient_exit()`/`sweep_shadow_patient_exits()`
(`docs/copy-trading/SAFETY.md` §61) additionally mirror their
already-decided outcomes into the Go OMS via `oms_client.py`, purely to
validate the OMS's own correctness under real usage; every call is
wrapped in `try/except` and never influences the real shadow-patient-exit
simulation's own data or decisions. Not the originally-planned call site —
`sweep_pending_exit_orders()`/`start_patient_exit()` (Rule 31 Priority 3)
turned out to be `LIVE_MODE`-only by construction, so it would never
actually run against this bot's paper-only configuration. `omsd` itself
still isn't started by anything in production, so this is fully inert
until Joey deliberately runs it and flips the flag.

## Development

```sh
cd oms
go test ./...
go vet ./...
gofmt -l .   # should print nothing
```
