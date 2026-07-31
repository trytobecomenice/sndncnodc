package bullpen

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"
)

// fakeExec builds an execFunc that returns a canned result on every call,
// or (if calls is non-empty) a different result per call in sequence --
// used by the retry tests to simulate "fails once, then succeeds".
func fakeExec(results ...func() (execResult, error)) execFunc {
	i := 0
	return func(ctx context.Context, name string, args []string, timeout time.Duration) (execResult, error) {
		r := results[i]
		if i < len(results)-1 {
			i++
		}
		return r()
	}
}

func jsonResult(exitCode int, body map[string]any) func() (execResult, error) {
	return func() (execResult, error) {
		b, _ := json.Marshal(body)
		return execResult{stdout: b, exitCode: exitCode}, nil
	}
}

func newTestRunner(exec execFunc) *Runner {
	return &Runner{binaryName: "bullpen", exec: exec}
}

func TestRunJSON_SuccessfulCallReturnsParsedBody(t *testing.T) {
	r := newTestRunner(fakeExec(jsonResult(0, map[string]any{"status": "MATCHED", "ok": true})))
	data, err := r.RunJSON(context.Background(), []string{"polymarket", "buy"}, Options{})
	if err != nil {
		t.Fatalf("RunJSON: %v", err)
	}
	if data["status"] != "MATCHED" {
		t.Fatalf("data[status] = %v, want MATCHED", data["status"])
	}
}

func TestRunJSON_TimeoutReturnsTimeoutError(t *testing.T) {
	r := newTestRunner(fakeExec(func() (execResult, error) { return execResult{}, errTimedOut }))
	_, err := r.RunJSON(context.Background(), []string{"polymarket", "buy"}, Options{Timeout: time.Second})
	var timeoutErr *TimeoutError
	if !errors.As(err, &timeoutErr) {
		t.Fatalf("error = %v (%T), want *TimeoutError", err, err)
	}
}

func TestRunJSON_ExitCodeTwoReturnsAuthError(t *testing.T) {
	r := newTestRunner(fakeExec(func() (execResult, error) {
		return execResult{stderr: []byte("session expired"), exitCode: 2}, nil
	}))
	_, err := r.RunJSON(context.Background(), []string{"polymarket", "buy"}, Options{})
	var authErr *AuthError
	if !errors.As(err, &authErr) {
		t.Fatalf("error = %v (%T), want *AuthError", err, err)
	}
}

func TestRunJSON_OtherNonZeroExitReturnsPlainError(t *testing.T) {
	r := newTestRunner(fakeExec(func() (execResult, error) {
		return execResult{stderr: []byte("trade execution failed"), exitCode: 4}, nil
	}))
	_, err := r.RunJSON(context.Background(), []string{"polymarket", "buy"}, Options{})
	if err == nil {
		t.Fatal("expected an error for a non-zero exit code")
	}
	var authErr *AuthError
	if errors.As(err, &authErr) {
		t.Fatal("exit code 4 should NOT be classified as an AuthError")
	}
	var timeoutErr *TimeoutError
	if errors.As(err, &timeoutErr) {
		t.Fatal("exit code 4 should NOT be classified as a TimeoutError")
	}
}

func TestRunJSON_NonZeroExitPrefersJSONErrorFieldOverStderr(t *testing.T) {
	// A trade command can exit non-zero while still printing a JSON error
	// body to stdout -- the structured field must win over raw stderr.
	body, _ := json.Marshal(map[string]any{"error": "insufficient balance"})
	r := newTestRunner(fakeExec(func() (execResult, error) {
		return execResult{stdout: body, stderr: []byte("some generic stderr noise"), exitCode: 4}, nil
	}))
	_, err := r.RunJSON(context.Background(), []string{"polymarket", "sell"}, Options{})
	if err == nil || !strings.Contains(err.Error(), "insufficient balance") {
		t.Fatalf("error = %v, want it to contain the JSON error field", err)
	}
	if strings.Contains(err.Error(), "generic stderr noise") {
		t.Fatalf("error = %v, should NOT fall back to stderr when a JSON error field is present", err)
	}
}

func TestRunJSON_ZeroExitWithUnparseableStdoutIsAnError(t *testing.T) {
	r := newTestRunner(fakeExec(func() (execResult, error) {
		return execResult{stdout: []byte("not json at all"), exitCode: 0}, nil
	}))
	_, err := r.RunJSON(context.Background(), []string{"polymarket", "buy"}, Options{})
	if err == nil {
		t.Fatal("expected an error for exit 0 with no parseable JSON")
	}
}

func TestRunJSON_ZeroExitWithOkFalseIsAnError(t *testing.T) {
	r := newTestRunner(fakeExec(jsonResult(0, map[string]any{"ok": false, "error": "market closed"})))
	_, err := r.RunJSON(context.Background(), []string{"polymarket", "buy"}, Options{})
	if err == nil || !strings.Contains(err.Error(), "market closed") {
		t.Fatalf("error = %v, want it to mention the ok:false error detail", err)
	}
}

func TestRunJSON_ZeroExitWithOkTrueIsNotAnError(t *testing.T) {
	r := newTestRunner(fakeExec(jsonResult(0, map[string]any{"ok": true, "status": "MATCHED"})))
	_, err := r.RunJSON(context.Background(), []string{"polymarket", "buy"}, Options{})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestRunJSON_DefaultRetriesIsOne_NoRetryOnFailure(t *testing.T) {
	calls := 0
	r := newTestRunner(func(ctx context.Context, name string, args []string, timeout time.Duration) (execResult, error) {
		calls++
		return execResult{stderr: []byte("boom"), exitCode: 1}, nil
	})
	_, err := r.RunJSON(context.Background(), []string{"polymarket", "buy"}, Options{})
	if err == nil {
		t.Fatal("expected an error")
	}
	if calls != 1 {
		t.Fatalf("calls = %d, want exactly 1 (default Retries=1 means no retry) -- a money-moving call must NEVER retry by default", calls)
	}
}

func TestRunJSON_ExplicitRetriesRetriesOnFailureThenSucceeds(t *testing.T) {
	calls := 0
	r := newTestRunner(func(ctx context.Context, name string, args []string, timeout time.Duration) (execResult, error) {
		calls++
		if calls == 1 {
			return execResult{stderr: []byte("transient"), exitCode: 1}, nil
		}
		body, _ := json.Marshal(map[string]any{"ok": true})
		return execResult{stdout: body, exitCode: 0}, nil
	})
	data, err := r.RunJSON(context.Background(), []string{"tracker", "feed"}, Options{Retries: 3, RetryDelay: time.Millisecond})
	if err != nil {
		t.Fatalf("RunJSON: %v", err)
	}
	if calls != 2 {
		t.Fatalf("calls = %d, want 2 (fail once, succeed on retry)", calls)
	}
	if data["ok"] != true {
		t.Fatalf("data = %v", data)
	}
}

func TestRunJSON_ExhaustsAllRetriesReturnsLastError(t *testing.T) {
	calls := 0
	r := newTestRunner(func(ctx context.Context, name string, args []string, timeout time.Duration) (execResult, error) {
		calls++
		return execResult{stderr: []byte("still broken"), exitCode: 1}, nil
	})
	_, err := r.RunJSON(context.Background(), []string{"tracker", "feed"}, Options{Retries: 3, RetryDelay: time.Millisecond})
	if err == nil {
		t.Fatal("expected an error after exhausting all retries")
	}
	if calls != 3 {
		t.Fatalf("calls = %d, want exactly 3", calls)
	}
}

func TestRequireFilled_MatchedWithTxHashesSucceeds(t *testing.T) {
	response := map[string]any{"status": "MATCHED", "transaction_hashes": []any{"0xabc"}}
	got, err := RequireFilled(response, "test buy")
	if err != nil {
		t.Fatalf("RequireFilled: %v", err)
	}
	if got["status"] != "MATCHED" {
		t.Fatalf("got = %v", got)
	}
}

func TestRequireFilled_RejectsNonMatchedStatus(t *testing.T) {
	response := map[string]any{"status": "UNMATCHED", "transaction_hashes": []any{"0xabc"}}
	_, err := RequireFilled(response, "test buy")
	if err == nil {
		t.Fatal("expected an error for UNMATCHED status")
	}
}

func TestRequireFilled_RejectsMissingTxHashes(t *testing.T) {
	response := map[string]any{"status": "MATCHED"}
	_, err := RequireFilled(response, "test buy")
	if err == nil {
		t.Fatal("expected an error for missing transaction_hashes")
	}
}

func TestRequireFilled_RejectsEmptyTxHashes(t *testing.T) {
	response := map[string]any{"status": "MATCHED", "transaction_hashes": []any{}}
	_, err := RequireFilled(response, "test buy")
	if err == nil {
		t.Fatal("expected an error for empty transaction_hashes")
	}
}

func TestExtractFillPrice(t *testing.T) {
	cases := []struct {
		name     string
		response map[string]any
		want     *float64
	}{
		{"avg_price in valid range", map[string]any{"avg_price": 0.55}, ptr(0.55)},
		{"falls through to price when avg_price absent", map[string]any{"price": 0.42}, ptr(0.42)},
		{"rejects value above 1", map[string]any{"avg_price": 1.5}, nil},
		{"rejects zero", map[string]any{"avg_price": 0.0}, nil},
		{"no plausible field returns nil", map[string]any{"status": "MATCHED"}, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := ExtractFillPrice(c.response)
			assertFloatPtrEqual(t, got, c.want)
		})
	}
}

func TestExtractFilledShares(t *testing.T) {
	cases := []struct {
		name     string
		response map[string]any
		want     *float64
	}{
		{"filled_shares present", map[string]any{"filled_shares": 12.5}, ptr(12.5)},
		{"genuinely zero shares is NOT nil", map[string]any{"filled_shares": 0.0}, ptr(0.0)},
		{"negative is rejected", map[string]any{"filled_shares": -1.0}, nil},
		{"no plausible field returns nil", map[string]any{}, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := ExtractFilledShares(c.response)
			assertFloatPtrEqual(t, got, c.want)
		})
	}
}

func TestExtractOrderID(t *testing.T) {
	if got := ExtractOrderID(map[string]any{"order_id": "abc-123"}); got != "abc-123" {
		t.Fatalf("got %q, want abc-123", got)
	}
	if got := ExtractOrderID(map[string]any{}); got != "" {
		t.Fatalf("got %q, want empty string for a response with no plausible field", got)
	}
}

// realExec is the one function every other test above bypasses via
// execFunc injection -- exercised directly here against real, always-
// available commands (never the actual `bullpen` binary, which isn't
// installed in this dev/CI environment) so a bug in the real subprocess
// path itself wouldn't slip through unnoticed.
func TestRealExec_CapturesStdoutAndExitCode(t *testing.T) {
	// printf, not echo -n -- some shells' builtin echo doesn't support -n
	// portably, printf's behavior here is standard everywhere.
	result, err := realExec(context.Background(), "sh", []string{"-c", "printf hello; exit 3"}, 5*time.Second)
	if err != nil {
		t.Fatalf("realExec: %v", err)
	}
	if string(result.stdout) != "hello" {
		t.Fatalf("stdout = %q, want %q", result.stdout, "hello")
	}
	if result.exitCode != 3 {
		t.Fatalf("exitCode = %d, want 3", result.exitCode)
	}
}

func TestRealExec_TimeoutReturnsErrTimedOut(t *testing.T) {
	_, err := realExec(context.Background(), "sh", []string{"-c", "sleep 5"}, 50*time.Millisecond)
	if !errors.Is(err, errTimedOut) {
		t.Fatalf("error = %v, want errTimedOut", err)
	}
}

func TestExtractOrderStatus(t *testing.T) {
	if got := ExtractOrderStatus(map[string]any{"status": "open"}); got != "OPEN" {
		t.Fatalf("got %q, want OPEN (uppercased)", got)
	}
	if got := ExtractOrderStatus(map[string]any{}); got != "" {
		t.Fatalf("got %q, want empty string for a response with no plausible field", got)
	}
}

func ptr(f float64) *float64 { return &f }

func assertFloatPtrEqual(t *testing.T, got, want *float64) {
	t.Helper()
	if (got == nil) != (want == nil) {
		t.Fatalf("got %v, want %v", got, want)
	}
	if got != nil && *got != *want {
		t.Fatalf("got %v, want %v", *got, *want)
	}
}
