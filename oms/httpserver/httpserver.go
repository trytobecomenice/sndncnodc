// Package httpserver exposes the OMS's order store over plain HTTP —
// Session 3 of the Go OMS. Deliberately stdlib net/http, no framework, no
// gRPC: matches this project's existing "stdlib only, add complexity only
// once proven necessary" convention (telegram_alerts.py,
// polymarket_simulator.py), and there is no second Go-to-Go service yet
// that would justify gRPC's codegen/proto overhead.
//
// Not wired into bot.py or LIVE_MODE — this package is a standalone,
// independently-testable HTTP surface. See the roadmap doc's Phase 2,
// Session 5 for how bot.py eventually becomes a client of it.
package httpserver

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"

	"github.com/trytobecomenice/polymarket-copybot/oms/order"
	"github.com/trytobecomenice/polymarket-copybot/oms/store"
)

// Server wraps a *store.Store and exposes it over HTTP. Holds no other
// state — every request is independently handled against the store, same
// "no I/O the store itself doesn't already own" discipline store.go's own
// UpdateStatus docstring established.
type Server struct {
	store *store.Store
}

// New wraps s. Does not take ownership of s's lifecycle (Close() is the
// caller's responsibility, same as store.Open()'s own contract).
func New(s *store.Store) *Server {
	return &Server{store: s}
}

// Handler returns the routed http.Handler — net/http's method+pattern
// routing (Go 1.22+, e.g. "POST /orders") needs no third-party router.
func (srv *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /orders", srv.handleCreateOrder)
	mux.HandleFunc("GET /orders/{id}", srv.handleGetOrder)
	mux.HandleFunc("POST /orders/{id}/cancel", srv.handleCancelOrder)
	return mux
}

type orderResponse struct {
	ID     string `json:"id"`
	Status string `json:"status"`
}

func orderToResponse(o *order.Order) orderResponse {
	return orderResponse{ID: o.ID, Status: string(o.Status)}
}

type errorResponse struct {
	Error string `json:"error"`
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		// The response status/headers are already sent at this point --
		// nothing left to do but log it, same "a notification failure
		// must never take down the caller" spirit as telegram_alerts.py's
		// own fails-silently design, just for a response-encoding edge
		// case here rather than an outbound notification.
		log.Printf("httpserver: failed to encode JSON response: %v", err)
	}
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, errorResponse{Error: message})
}

type createOrderRequest struct {
	IdempotencyKey string `json:"idempotency_key"`
}

// handleCreateOrder: POST /orders. Idempotent per store.CreateOrder's own
// contract -- 201 Created only when THIS request actually created the
// order, 200 OK when it returned an existing one (the standard way an
// idempotent-creation endpoint distinguishes the two, so a retried client
// can tell "my first attempt already went through" from "this just
// happened").
func (srv *Server) handleCreateOrder(w http.ResponseWriter, r *http.Request) {
	var req createOrderRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if req.IdempotencyKey == "" {
		writeError(w, http.StatusBadRequest, "idempotency_key is required")
		return
	}

	o, created, err := srv.store.CreateOrder(req.IdempotencyKey)
	if err != nil {
		log.Printf("httpserver: CreateOrder(%s): %v", req.IdempotencyKey, err)
		writeError(w, http.StatusInternalServerError, "failed to create order")
		return
	}

	status := http.StatusOK
	if created {
		status = http.StatusCreated
	}
	writeJSON(w, status, orderToResponse(o))
}

// handleGetOrder: GET /orders/{id}.
func (srv *Server) handleGetOrder(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	o, err := srv.store.Get(id)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "order not found")
			return
		}
		log.Printf("httpserver: Get(%s): %v", id, err)
		writeError(w, http.StatusInternalServerError, "failed to fetch order")
		return
	}
	writeJSON(w, http.StatusOK, orderToResponse(o))
}

// handleCancelOrder: POST /orders/{id}/cancel. Cancellation is modeled as
// the SAME Pending -> Invalidated transition order.Order already defines
// (see order/state.go) — this handler fetches the current order,
// constructs the pure Order type, and asks IT whether the transition is
// legal before ever writing to the store. That ordering matters: the
// state machine is the single source of truth for what's legal, the store
// is deliberately dumb persistence (see store.UpdateStatus's own
// docstring) — this handler is where the two get composed.
func (srv *Server) handleCancelOrder(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	current, err := srv.store.Get(id)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "order not found")
			return
		}
		log.Printf("httpserver: Get(%s) for cancel: %v", id, err)
		writeError(w, http.StatusInternalServerError, "failed to fetch order")
		return
	}

	if err := current.Transition(order.Invalidated); err != nil {
		// Not a server error -- the caller asked for an illegal
		// transition (e.g. canceling an already-filled order). 409
		// Conflict, not 400: the request was well-formed, the resource's
		// current STATE is what disallows it.
		writeError(w, http.StatusConflict, err.Error())
		return
	}

	if err := srv.store.UpdateStatus(id, order.Invalidated); err != nil {
		log.Printf("httpserver: UpdateStatus(%s, invalidated): %v", id, err)
		writeError(w, http.StatusInternalServerError, "failed to cancel order")
		return
	}
	writeJSON(w, http.StatusOK, orderToResponse(current))
}
