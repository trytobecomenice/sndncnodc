package store

import (
	"errors"
	"path/filepath"
	"sync"
	"testing"

	"github.com/trytobecomenice/polymarket-copybot/oms/order"
)

// openTemp opens a Store against a fresh temp-file SQLite database, never
// :memory: -- Go's database/sql pool can open more than one connection,
// and each :memory: connection is a SEPARATE, empty database (a classic
// footgun especially for the concurrency test below, which needs every
// goroutine to see the SAME data).
func openTemp(t *testing.T) *Store {
	t.Helper()
	path := filepath.Join(t.TempDir(), "test.db")
	s, err := Open(path)
	if err != nil {
		t.Fatalf("Open(%s): %v", path, err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

func TestCreateOrder_NewKeyStartsPending(t *testing.T) {
	s := openTemp(t)
	o, created, err := s.CreateOrder("trade-1")
	if err != nil {
		t.Fatalf("CreateOrder: %v", err)
	}
	if !created {
		t.Fatal("created = false for a genuinely new idempotency key, want true")
	}
	if o.Status != order.Pending {
		t.Fatalf("Status = %s, want %s", o.Status, order.Pending)
	}
	if o.ID == "" {
		t.Fatal("ID is empty")
	}
}

func TestCreateOrder_RepeatedKeyReturnsTheSameOrderNeverADuplicate(t *testing.T) {
	s := openTemp(t)
	first, firstCreated, err := s.CreateOrder("trade-1")
	if err != nil {
		t.Fatalf("first CreateOrder: %v", err)
	}
	if !firstCreated {
		t.Fatal("first call: created = false, want true")
	}
	second, secondCreated, err := s.CreateOrder("trade-1")
	if err != nil {
		t.Fatalf("second CreateOrder: %v", err)
	}
	if secondCreated {
		t.Fatal("second call (repeated key): created = true, want false -- it returned an existing order")
	}
	if second.ID != first.ID {
		t.Fatalf("second call returned a DIFFERENT order id (%s vs %s) -- idempotency broken", second.ID, first.ID)
	}

	var count int
	if err := s.db.QueryRow("SELECT count(*) FROM oms_order WHERE idempotency_key = ?", "trade-1").Scan(&count); err != nil {
		t.Fatalf("count query: %v", err)
	}
	if count != 1 {
		t.Fatalf("row count for idempotency_key='trade-1' = %d, want exactly 1", count)
	}
}

func TestCreateOrder_DifferentKeysCreateDifferentOrders(t *testing.T) {
	s := openTemp(t)
	a, _, err := s.CreateOrder("trade-1")
	if err != nil {
		t.Fatalf("CreateOrder(trade-1): %v", err)
	}
	b, _, err := s.CreateOrder("trade-2")
	if err != nil {
		t.Fatalf("CreateOrder(trade-2): %v", err)
	}
	if a.ID == b.ID {
		t.Fatal("different idempotency keys produced the same order id")
	}
}

func TestCreateOrder_ConcurrentSameKeyRaceNeverDuplicates(t *testing.T) {
	// The property this whole package exists to guarantee: many goroutines
	// racing to create an order with the SAME idempotency key must all
	// end up agreeing on exactly one underlying order, and exactly ONE of
	// them should observe created=true.
	s := openTemp(t)
	const n = 20
	ids := make([]string, n)
	createdFlags := make([]bool, n)
	errs := make([]error, n)
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func(i int) {
			defer wg.Done()
			o, created, err := s.CreateOrder("race-key")
			errs[i] = err
			createdFlags[i] = created
			if o != nil {
				ids[i] = o.ID
			}
		}(i)
	}
	wg.Wait()

	for i, err := range errs {
		if err != nil {
			t.Fatalf("goroutine %d: CreateOrder returned error: %v", i, err)
		}
	}
	first := ids[0]
	for i, id := range ids {
		if id != first {
			t.Fatalf("goroutine %d got order id %s, goroutine 0 got %s -- duplicate orders under race", i, id, first)
		}
	}
	createdCount := 0
	for _, c := range createdFlags {
		if c {
			createdCount++
		}
	}
	if createdCount != 1 {
		t.Fatalf("created=true count = %d across %d racing goroutines, want exactly 1", createdCount, n)
	}

	var count int
	if err := s.db.QueryRow("SELECT count(*) FROM oms_order WHERE idempotency_key = ?", "race-key").Scan(&count); err != nil {
		t.Fatalf("count query: %v", err)
	}
	if count != 1 {
		t.Fatalf("row count for idempotency_key='race-key' = %d, want exactly 1 despite %d concurrent creators", count, n)
	}
}

func TestGet_ReturnsErrNotFoundForUnknownID(t *testing.T) {
	s := openTemp(t)
	_, err := s.Get("no-such-id")
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("Get(unknown) error = %v, want ErrNotFound", err)
	}
}

func TestGet_ReturnsTheCreatedOrder(t *testing.T) {
	s := openTemp(t)
	created, _, err := s.CreateOrder("trade-1")
	if err != nil {
		t.Fatalf("CreateOrder: %v", err)
	}
	got, err := s.Get(created.ID)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.ID != created.ID || got.Status != created.Status {
		t.Fatalf("Get returned %+v, want %+v", got, created)
	}
}

func TestUpdateStatus_PersistsAndIsReadableViaGet(t *testing.T) {
	s := openTemp(t)
	created, _, err := s.CreateOrder("trade-1")
	if err != nil {
		t.Fatalf("CreateOrder: %v", err)
	}
	if err := s.UpdateStatus(created.ID, order.Filled); err != nil {
		t.Fatalf("UpdateStatus: %v", err)
	}
	got, err := s.Get(created.ID)
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if got.Status != order.Filled {
		t.Fatalf("Status after update = %s, want %s", got.Status, order.Filled)
	}
}

func TestUpdateStatus_ReturnsErrNotFoundForUnknownID(t *testing.T) {
	s := openTemp(t)
	err := s.UpdateStatus("no-such-id", order.Filled)
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("UpdateStatus(unknown) error = %v, want ErrNotFound", err)
	}
}

func TestOpen_ReusesAnExistingDatabaseFileWithOtherTables(t *testing.T) {
	// data/app.db in production already has dozens of unrelated tables --
	// Open() must never assume it owns the whole file.
	path := filepath.Join(t.TempDir(), "shared.db")
	s1, err := Open(path)
	if err != nil {
		t.Fatalf("first Open: %v", err)
	}
	if _, err := s1.db.Exec("CREATE TABLE some_other_table (id TEXT PRIMARY KEY)"); err != nil {
		t.Fatalf("creating unrelated table: %v", err)
	}
	if _, _, err := s1.CreateOrder("trade-1"); err != nil {
		t.Fatalf("CreateOrder before reopen: %v", err)
	}
	s1.Close()

	s2, err := Open(path)
	if err != nil {
		t.Fatalf("second Open (reopening an existing file): %v", err)
	}
	defer s2.Close()
	if _, err := s2.getByIdempotencyKey("trade-1"); err != nil {
		t.Fatalf("order created before reopen is unreadable after reopen: %v", err)
	}
	var exists int
	if err := s2.db.QueryRow(
		"SELECT count(*) FROM sqlite_master WHERE type='table' AND name='some_other_table'",
	).Scan(&exists); err != nil {
		t.Fatalf("checking unrelated table survived: %v", err)
	}
	if exists != 1 {
		t.Fatal("unrelated pre-existing table was lost/dropped by Open()")
	}
}
