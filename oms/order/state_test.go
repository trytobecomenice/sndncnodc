package order

import (
	"errors"
	"testing"
)

func TestNewStartsPending(t *testing.T) {
	o := New("order-1")
	if o.Status != Pending {
		t.Fatalf("New() started at %s, want %s", o.Status, Pending)
	}
	if o.ID != "order-1" {
		t.Fatalf("New() ID = %q, want %q", o.ID, "order-1")
	}
}

func TestTransition_LegalCases(t *testing.T) {
	cases := []struct {
		name string
		to   Status
	}{
		{"pending to filled", Filled},
		{"pending to expired", Expired},
		{"pending to invalidated", Invalidated},
		{"pending to unknown fill state", UnknownFillState},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			o := New("order-1")
			if err := o.Transition(c.to); err != nil {
				t.Fatalf("Transition(%s) returned error: %v", c.to, err)
			}
			if o.Status != c.to {
				t.Fatalf("Status = %s, want %s", o.Status, c.to)
			}
		})
	}
}

func TestTransition_TerminalStatesRejectEveryOutgoingTransition(t *testing.T) {
	terminal := []Status{Filled, Expired, Invalidated}
	targets := []Status{Pending, Filled, Expired, Invalidated, UnknownFillState}
	for _, from := range terminal {
		for _, to := range targets {
			t.Run(string(from)+" to "+string(to), func(t *testing.T) {
				o := &Order{ID: "order-1", Status: from}
				err := o.Transition(to)
				if err == nil {
					t.Fatalf("Transition(%s) from terminal state %s succeeded, want error", to, from)
				}
				var transErr *TransitionError
				if !errors.As(err, &transErr) {
					t.Fatalf("error type = %T, want *TransitionError", err)
				}
				if o.Status != from {
					t.Fatalf("Status mutated to %s after a rejected transition, want unchanged %s", o.Status, from)
				}
			})
		}
	}
}

func TestTransition_UnknownFillStateRejectsOrdinaryTransition(t *testing.T) {
	// The whole point of UnknownFillState: nothing automated may leave it
	// via the ordinary Transition() path -- only Reconcile() can.
	for _, to := range []Status{Pending, Filled, Expired, Invalidated, UnknownFillState} {
		t.Run(string(to), func(t *testing.T) {
			o := &Order{ID: "order-1", Status: UnknownFillState}
			if err := o.Transition(to); err == nil {
				t.Fatalf("Transition(%s) from UnknownFillState succeeded via the ordinary path, want error", to)
			}
			if o.Status != UnknownFillState {
				t.Fatalf("Status mutated to %s, want unchanged %s", o.Status, UnknownFillState)
			}
		})
	}
}

func TestTransition_PendingToPendingIsIllegal(t *testing.T) {
	o := New("order-1")
	if err := o.Transition(Pending); err == nil {
		t.Fatal("Transition(Pending) from Pending succeeded, want error (not a real transition)")
	}
}

func TestReconcile_LegalOutcomes(t *testing.T) {
	for _, to := range []Status{Filled, Invalidated} {
		t.Run(string(to), func(t *testing.T) {
			o := &Order{ID: "order-1", Status: UnknownFillState}
			if err := o.Reconcile(to); err != nil {
				t.Fatalf("Reconcile(%s) returned error: %v", to, err)
			}
			if o.Status != to {
				t.Fatalf("Status = %s, want %s", o.Status, to)
			}
		})
	}
}

func TestReconcile_RejectsIllegalOutcomes(t *testing.T) {
	for _, to := range []Status{Pending, Expired, UnknownFillState} {
		t.Run(string(to), func(t *testing.T) {
			o := &Order{ID: "order-1", Status: UnknownFillState}
			if err := o.Reconcile(to); err == nil {
				t.Fatalf("Reconcile(%s) succeeded, want error", to)
			}
			if o.Status != UnknownFillState {
				t.Fatalf("Status mutated to %s after a rejected reconcile, want unchanged", o.Status)
			}
		})
	}
}

func TestReconcile_OnlyAppliesWhenCurrentlyUnknownFillState(t *testing.T) {
	for _, from := range []Status{Pending, Filled, Expired, Invalidated} {
		t.Run(string(from), func(t *testing.T) {
			o := &Order{ID: "order-1", Status: from}
			if err := o.Reconcile(Filled); err == nil {
				t.Fatalf("Reconcile() from %s succeeded, want error (Reconcile only applies to UnknownFillState)", from)
			}
			if o.Status != from {
				t.Fatalf("Status mutated to %s, want unchanged %s", o.Status, from)
			}
		})
	}
}

func TestIsTerminal(t *testing.T) {
	cases := []struct {
		status Status
		want   bool
	}{
		{Pending, false},
		{Filled, true},
		{Expired, true},
		{Invalidated, true},
		{UnknownFillState, false}, // Reconcile() is still available
	}
	for _, c := range cases {
		t.Run(string(c.status), func(t *testing.T) {
			o := &Order{ID: "order-1", Status: c.status}
			if got := o.IsTerminal(); got != c.want {
				t.Fatalf("IsTerminal() for %s = %v, want %v", c.status, got, c.want)
			}
		})
	}
}

func TestTransitionError_MessageIncludesFromAndTo(t *testing.T) {
	err := &TransitionError{From: Filled, To: Pending}
	want := "illegal order transition: filled -> pending"
	if err.Error() != want {
		t.Fatalf("Error() = %q, want %q", err.Error(), want)
	}
}
