// Package store persists order/state.go's pure Order type to SQLite —
// Session 2 of the Go OMS. Deliberately a NEW table (oms_order), not a
// rewrite of the existing pending_execution table Python still owns during
// the parallel-run validation period (see the roadmap doc's Phase 2,
// Session 6) — the two coexist in the same data/app.db without touching
// each other.
//
// modernc.org/sqlite (pure Go, no cgo) rather than mattn/go-sqlite3:
// avoids requiring a C toolchain wherever this eventually gets built/
// deployed (EC2 included), matching this project's general preference for
// minimizing deploy-time complexity.
package store

import (
	"database/sql"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"modernc.org/sqlite"

	"github.com/trytobecomenice/polymarket-copybot/oms/order"
)

// sqliteConstraintUnique is SQLITE_CONSTRAINT_UNIQUE's extended result
// code (2067) -- verified directly against modernc.org/sqlite's actual
// error shape (not assumed): a UNIQUE-constraint violation returns
// *sqlite.Error with exactly this Code(), and a message of the form
// "constraint failed: UNIQUE constraint failed: <table>.<column> (2067)".
const sqliteConstraintUnique = 2067

func isUniqueViolation(err error) bool {
	var sqliteErr *sqlite.Error
	if errors.As(err, &sqliteErr) {
		return sqliteErr.Code() == sqliteConstraintUnique
	}
	return false
}

const schema = `
CREATE TABLE IF NOT EXISTS oms_order (
	id TEXT PRIMARY KEY,
	idempotency_key TEXT NOT NULL UNIQUE,
	status TEXT NOT NULL,
	created_at INTEGER NOT NULL DEFAULT (unixepoch()),
	updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);
`

// Store wraps a *sql.DB open against data/app.db (or, in tests, a
// temporary file — never :memory:, since Go's database/sql pool can open
// more than one connection and each :memory: connection is a SEPARATE,
// empty database, a classic footgun for exactly this kind of test).
type Store struct {
	db *sql.DB
}

// Open connects to the SQLite file at path and ensures oms_order exists.
// Safe to call against a database that ALREADY has other tables
// (data/app.db in production) — CREATE TABLE IF NOT EXISTS only, never
// touches anything else.
//
// WAL mode + a single-connection pool (2026-08-01, found by
// TestCreateOrder_ConcurrentSameKeyRaceNeverDuplicates failing with
// "database is locked" before this fix): SQLite only ever allows ONE
// writer at a time, but database/sql's default pool happily opens SEVERAL
// connections and lets them race for the write lock, surfacing as
// SQLITE_BUSY under real concurrent load -- exactly the failure the
// idempotency race test above exists to catch. Mirrors the same "WAL mode
// + busy_timeout" fix this project's own Python/Drizzle side already
// applies to this same data/app.db file (see packages/db/src/migrate.ts).
// SetMaxOpenConns(1) sidesteps the pool-level race entirely rather than
// tuning busy_timeout to merely make it rare — appropriate for this
// service's expected order volume (nowhere near needing concurrent
// writers), and simpler to reason about than a retry-on-busy loop.
func Open(path string) (*Store, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("store: open %s: %w", path, err)
	}
	db.SetMaxOpenConns(1)
	if _, err := db.Exec("PRAGMA journal_mode=WAL;"); err != nil {
		db.Close()
		return nil, fmt.Errorf("store: enable WAL mode: %w", err)
	}
	if _, err := db.Exec("PRAGMA busy_timeout=5000;"); err != nil {
		db.Close()
		return nil, fmt.Errorf("store: set busy_timeout: %w", err)
	}
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, fmt.Errorf("store: ensure schema: %w", err)
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error {
	return s.db.Close()
}

// ErrNotFound is returned by Get when no order matches.
var ErrNotFound = errors.New("store: order not found")

// CreateOrder is the idempotent creation entry point — the single most
// commercially-relevant pattern this whole service exists to demonstrate.
// Given the SAME idempotencyKey twice (e.g. a retried request after a
// timeout whose first attempt actually succeeded), the SECOND call
// returns the order created by the FIRST, never a duplicate row and never
// an error.
//
// Race-safe by construction, not by locking: attempts the INSERT directly
// (new orders start Pending, mirroring order.New()'s own starting state)
// and, on a UNIQUE-constraint violation, re-reads whatever row actually
// won the race and returns THAT — correct even when two goroutines call
// CreateOrder with the same key at the same instant, unlike a
// check-then-insert pattern which has a race window between the two steps.
func (s *Store) CreateOrder(idempotencyKey string) (*order.Order, error) {
	id := uuid.NewString()
	_, err := s.db.Exec(
		"INSERT INTO oms_order (id, idempotency_key, status) VALUES (?, ?, ?)",
		id, idempotencyKey, string(order.Pending),
	)
	if err == nil {
		return &order.Order{ID: id, Status: order.Pending}, nil
	}
	if !isUniqueViolation(err) {
		return nil, fmt.Errorf("store: create order: %w", err)
	}
	// Lost the race (or this is a genuine repeat call) -- whichever row
	// actually exists for this key is the correct answer, not an error.
	existing, getErr := s.getByIdempotencyKey(idempotencyKey)
	if getErr != nil {
		return nil, fmt.Errorf("store: create order: insert conflicted but re-read failed: %w", getErr)
	}
	return existing, nil
}

// Get returns the order with the given internal ID, or ErrNotFound.
func (s *Store) Get(id string) (*order.Order, error) {
	row := s.db.QueryRow("SELECT id, status FROM oms_order WHERE id = ?", id)
	return scanOrder(row)
}

func (s *Store) getByIdempotencyKey(idempotencyKey string) (*order.Order, error) {
	row := s.db.QueryRow("SELECT id, status FROM oms_order WHERE idempotency_key = ?", idempotencyKey)
	return scanOrder(row)
}

func scanOrder(row *sql.Row) (*order.Order, error) {
	var id, status string
	if err := row.Scan(&id, &status); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, fmt.Errorf("store: scan order: %w", err)
	}
	return &order.Order{ID: id, Status: order.Status(status)}, nil
}

// UpdateStatus persists a transition already validated by order.Order's
// own Transition()/Reconcile() methods -- this function does NOT
// re-validate legality itself (that's the state machine's job, not the
// store's); it only refuses to write a status for an id that doesn't
// exist.
func (s *Store) UpdateStatus(id string, newStatus order.Status) error {
	result, err := s.db.Exec(
		"UPDATE oms_order SET status = ?, updated_at = unixepoch() WHERE id = ?",
		string(newStatus), id,
	)
	if err != nil {
		return fmt.Errorf("store: update status: %w", err)
	}
	n, err := result.RowsAffected()
	if err != nil {
		return fmt.Errorf("store: update status: %w", err)
	}
	if n == 0 {
		return ErrNotFound
	}
	return nil
}
