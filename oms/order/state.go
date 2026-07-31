// Package order defines the OMS order state machine — the Go counterpart
// to the Python bot's pending_execution table (see packages/db/src/schema.ts's
// pendingExecution: status ∈ pending | filled | expired | invalidated).
//
// unknown_fill_state is new here, but not a new IDEA: bot.py's own
// BullpenTimeoutError handling (see close_position_trailing_tp/
// start_patient_exit callers) already treats a timeout on a money-moving
// call as "log it, never auto-retry, leave for a human to reconcile" —
// this package makes that doctrine a first-class state instead of an
// exception path, so the state machine itself can enforce it: nothing
// automated is allowed to transition OUT of unknown_fill_state, only an
// explicit Reconcile call (standing in for a human checking the real
// order status directly).
//
// Deliberately pure: no I/O, no database, no network. See oms's own
// top-level plan (async-questing-oasis.md, Phase 2, Session 1) for why —
// same "pure function first" discipline the Python side used all night
// for compute_lifespan_fraction_remaining/is_time_decay_loss_cut_eligible.
package order

import "fmt"

// Status mirrors pending_execution.status, plus UnknownFillState.
type Status string

const (
	// Pending: a resting order, not yet resolved either way. The only
	// non-terminal status.
	Pending Status = "pending"
	// Filled: confirmed on-chain fill. Terminal.
	Filled Status = "filled"
	// Expired: timed out unfilled, per the owning mechanism's own max-wait
	// (e.g. Rule 29's dip-and-rebound TTL, Rule 31's ORDER_PEG_MAX_TOTAL_WAIT_SECONDS).
	// Terminal.
	Expired Status = "expired"
	// Invalidated: canceled/superseded before it could fill or expire
	// (e.g. the whale sold out from under it, an anchor-price rule voided
	// it). Terminal.
	Invalidated Status = "invalidated"
	// UnknownFillState: a money-moving call (place/cancel/poll) failed to
	// confirm either way (timeout, ambiguous response) — see
	// BullpenTimeoutError's Python precedent. Never auto-retried and never
	// auto-transitioned out; only Reconcile (a human resolving it against
	// the real order status) may move this forward.
	UnknownFillState Status = "unknown_fill_state"
)

// validTransitions is the single source of truth for what's legal.
// Terminal statuses (Filled, Expired, Invalidated) have no entry, hence no
// outgoing transitions at all. UnknownFillState only accepts Reconcile's
// two possible outcomes, never the ordinary Transition path.
var validTransitions = map[Status]map[Status]bool{
	Pending: {
		Filled:           true,
		Expired:          true,
		Invalidated:      true,
		UnknownFillState: true,
	},
}

// reconcileTransitions is the separate, narrower table Reconcile() checks —
// kept apart from validTransitions so the ordinary Transition() path can
// never accidentally move an order out of UnknownFillState.
var reconcileTransitions = map[Status]bool{
	Filled:      true,
	Invalidated: true,
}

// Order is the pure in-memory representation. No timestamps/IDs beyond
// what identifies it — persistence (Session 2) wraps this, doesn't extend
// it.
type Order struct {
	ID     string
	Status Status
}

// New starts a fresh order in Pending — the only valid starting state,
// mirroring pending_execution's own status default ('pending').
func New(id string) *Order {
	return &Order{ID: id, Status: Pending}
}

// TransitionError reports an illegal state transition attempt — returned,
// never panicked, so a caller (e.g. a sweep loop processing many orders)
// can log-and-skip one bad transition without aborting the others.
type TransitionError struct {
	From, To Status
}

func (e *TransitionError) Error() string {
	return fmt.Sprintf("illegal order transition: %s -> %s", e.From, e.To)
}

// Transition moves the order to `to` if legal, or returns a
// *TransitionError and leaves the order unchanged. UnknownFillState can
// only be left via Reconcile, never this method.
func (o *Order) Transition(to Status) error {
	allowed := validTransitions[o.Status]
	if !allowed[to] {
		return &TransitionError{From: o.Status, To: to}
	}
	o.Status = to
	return nil
}

// Reconcile is the ONLY way out of UnknownFillState — models a human (or
// an explicit reconciliation job) confirming what actually happened by
// checking the real order status directly, never an automated retry.
// Returns a *TransitionError if the order isn't currently
// UnknownFillState, or if `to` isn't one of Reconcile's legal outcomes.
func (o *Order) Reconcile(to Status) error {
	if o.Status != UnknownFillState {
		return &TransitionError{From: o.Status, To: to}
	}
	if !reconcileTransitions[to] {
		return &TransitionError{From: o.Status, To: to}
	}
	o.Status = to
	return nil
}

// IsTerminal reports whether no further transitions are possible at all —
// true for Filled/Expired/Invalidated, false for Pending and
// UnknownFillState (which still has Reconcile available).
func (o *Order) IsTerminal() bool {
	switch o.Status {
	case Filled, Expired, Invalidated:
		return true
	default:
		return false
	}
}
