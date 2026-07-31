package httpserver

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/trytobecomenice/polymarket-copybot/oms/order"
	"github.com/trytobecomenice/polymarket-copybot/oms/store"
)

func newTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	path := filepath.Join(t.TempDir(), "test.db")
	s, err := store.Open(path)
	if err != nil {
		t.Fatalf("store.Open: %v", err)
	}
	t.Cleanup(func() { s.Close() })
	return httptest.NewServer(New(s).Handler())
}

func postJSON(t *testing.T, url string, body any) *http.Response {
	t.Helper()
	b, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("json.Marshal: %v", err)
	}
	resp, err := http.Post(url, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	return resp
}

func decodeOrder(t *testing.T, resp *http.Response) orderResponse {
	t.Helper()
	defer resp.Body.Close()
	var o orderResponse
	if err := json.NewDecoder(resp.Body).Decode(&o); err != nil {
		t.Fatalf("decode orderResponse: %v", err)
	}
	return o
}

func decodeError(t *testing.T, resp *http.Response) errorResponse {
	t.Helper()
	defer resp.Body.Close()
	var e errorResponse
	if err := json.NewDecoder(resp.Body).Decode(&e); err != nil {
		t.Fatalf("decode errorResponse: %v", err)
	}
	return e
}

func TestCreateOrder_NewKeyReturns201WithPendingOrder(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	resp := postJSON(t, srv.URL+"/orders", createOrderRequest{IdempotencyKey: "trade-1"})
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("status = %d, want %d", resp.StatusCode, http.StatusCreated)
	}
	o := decodeOrder(t, resp)
	if o.Status != string(order.Pending) {
		t.Fatalf("Status = %s, want %s", o.Status, order.Pending)
	}
	if o.ID == "" {
		t.Fatal("ID is empty")
	}
}

func TestCreateOrder_RepeatedKeyReturns200WithTheSameOrder(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	first := decodeOrder(t, postJSON(t, srv.URL+"/orders", createOrderRequest{IdempotencyKey: "trade-1"}))

	resp := postJSON(t, srv.URL+"/orders", createOrderRequest{IdempotencyKey: "trade-1"})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want %d (idempotent replay)", resp.StatusCode, http.StatusOK)
	}
	second := decodeOrder(t, resp)
	if second.ID != first.ID {
		t.Fatalf("second call returned a different order id (%s vs %s)", second.ID, first.ID)
	}
}

func TestCreateOrder_MissingIdempotencyKeyReturns400(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	resp := postJSON(t, srv.URL+"/orders", createOrderRequest{IdempotencyKey: ""})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", resp.StatusCode, http.StatusBadRequest)
	}
}

func TestCreateOrder_InvalidJSONReturns400(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	resp, err := http.Post(srv.URL+"/orders", "application/json", bytes.NewReader([]byte("not json")))
	if err != nil {
		t.Fatalf("POST: %v", err)
	}
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", resp.StatusCode, http.StatusBadRequest)
	}
}

func TestGetOrder_ReturnsTheCreatedOrder(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	created := decodeOrder(t, postJSON(t, srv.URL+"/orders", createOrderRequest{IdempotencyKey: "trade-1"}))

	resp, err := http.Get(srv.URL + "/orders/" + created.ID)
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want %d", resp.StatusCode, http.StatusOK)
	}
	got := decodeOrder(t, resp)
	if got.ID != created.ID || got.Status != created.Status {
		t.Fatalf("GET returned %+v, want %+v", got, created)
	}
}

func TestGetOrder_UnknownIDReturns404(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/orders/no-such-id")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status = %d, want %d", resp.StatusCode, http.StatusNotFound)
	}
}

func TestCancelOrder_PendingOrderBecomesInvalidated(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	created := decodeOrder(t, postJSON(t, srv.URL+"/orders", createOrderRequest{IdempotencyKey: "trade-1"}))

	resp := postJSON(t, srv.URL+"/orders/"+created.ID+"/cancel", nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want %d", resp.StatusCode, http.StatusOK)
	}
	cancelled := decodeOrder(t, resp)
	if cancelled.Status != string(order.Invalidated) {
		t.Fatalf("Status after cancel = %s, want %s", cancelled.Status, order.Invalidated)
	}

	// Persisted, not just reflected in the response.
	getResp, err := http.Get(srv.URL + "/orders/" + created.ID)
	if err != nil {
		t.Fatalf("GET after cancel: %v", err)
	}
	got := decodeOrder(t, getResp)
	if got.Status != string(order.Invalidated) {
		t.Fatalf("GET after cancel: Status = %s, want %s", got.Status, order.Invalidated)
	}
}

func TestCancelOrder_AlreadyTerminalOrderReturns409(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	created := decodeOrder(t, postJSON(t, srv.URL+"/orders", createOrderRequest{IdempotencyKey: "trade-1"}))

	// Cancel once -- legal, Pending -> Invalidated.
	firstCancel := postJSON(t, srv.URL+"/orders/"+created.ID+"/cancel", nil)
	if firstCancel.StatusCode != http.StatusOK {
		t.Fatalf("first cancel status = %d, want %d", firstCancel.StatusCode, http.StatusOK)
	}

	// Cancel again -- Invalidated is terminal, illegal.
	secondCancel := postJSON(t, srv.URL+"/orders/"+created.ID+"/cancel", nil)
	if secondCancel.StatusCode != http.StatusConflict {
		t.Fatalf("second cancel status = %d, want %d", secondCancel.StatusCode, http.StatusConflict)
	}
	errBody := decodeError(t, secondCancel)
	if errBody.Error == "" {
		t.Fatal("409 response has an empty error message")
	}

	// And the order's real status must be UNCHANGED by the rejected attempt.
	getResp, err := http.Get(srv.URL + "/orders/" + created.ID)
	if err != nil {
		t.Fatalf("GET after rejected cancel: %v", err)
	}
	got := decodeOrder(t, getResp)
	if got.Status != string(order.Invalidated) {
		t.Fatalf("Status mutated by a rejected cancel: %s, want unchanged %s", got.Status, order.Invalidated)
	}
}

func TestCancelOrder_UnknownIDReturns404(t *testing.T) {
	srv := newTestServer(t)
	defer srv.Close()

	resp := postJSON(t, srv.URL+"/orders/no-such-id/cancel", nil)
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status = %d, want %d", resp.StatusCode, http.StatusNotFound)
	}
}
