// Package bullpen ports bullpen_client.py's subprocess-call contract into
// Go — Session 4 of the Go OMS. Not a redesign: the exit-code handling,
// timeout-as-unknown-fill-state distinction, and best-effort response-field
// extraction all mirror the Python original field-for-field (see each
// function's doc comment for the exact line it corresponds to), since that
// contract has real production experience behind it (the
// BullpenAuthError/BullpenTimeoutError distinction exists because of a
// real 2026-07-21 incident — see bullpen_client.py's own docstring).
//
// Built and tested here with the actual subprocess call injectable (see
// Runner.exec) so tests never depend on a real `bullpen` binary being
// installed — nothing in this package is wired to a live call site yet.
package bullpen

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

// DefaultCallTimeout mirrors config.BULLPEN_CALL_TIMEOUT_SECONDS (60s) —
// generous ON PURPOSE for buy/sell; see that constant's own comment in
// config.py. Only read-only, high-frequency call sites should pass a
// tighter Options.Timeout.
const DefaultCallTimeout = 60 * time.Second

// TimeoutError mirrors BullpenTimeoutError: the subprocess hit its
// timeout. For money-moving calls this is fundamentally different from a
// clean failure — the order MAY have executed on-chain even though the
// response was never seen. Callers must treat this as
// order.UnknownFillState for manual reconciliation, never retry it
// automatically (see order/state.go's own doc comment on why that state
// exists).
type TimeoutError struct {
	Args    []string
	Timeout time.Duration
}

func (e *TimeoutError) Error() string {
	return fmt.Sprintf(
		"bullpen %s timed out after %s; if this was a trade, the order MAY still have executed",
		strings.Join(e.Args, " "), e.Timeout,
	)
}

// AuthError mirrors BullpenAuthError: the CLI exited 2 ("Authentication
// failure", the CLI's own documented exit code). Distinct from a generic
// error specifically so callers can tell "the session is dead, a human
// needs to re-authenticate" apart from an ordinary transient failure worth
// retrying.
type AuthError struct {
	Msg string
}

func (e *AuthError) Error() string { return e.Msg }

// FilledTradeStatuses mirrors bullpen_client.py's own set: only MATCHED
// means the CLI actually settled shares/cash on-chain.
var FilledTradeStatuses = map[string]bool{"MATCHED": true}

// execResult is what actually running the subprocess produced — kept
// separate from any error type so a non-zero exit code (a normal, expected
// outcome bullpen_client.py's own logic handles explicitly) is never
// conflated with "the subprocess itself failed to run".
type execResult struct {
	stdout   []byte
	stderr   []byte
	exitCode int
}

var errTimedOut = errors.New("bullpen: subprocess timed out")

// execFunc is the injectable seam tests use instead of a real `bullpen`
// binary — see bullpen_test.go.
type execFunc func(ctx context.Context, name string, args []string, timeout time.Duration) (execResult, error)

func realExec(ctx context.Context, name string, args []string, timeout time.Duration) (execResult, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, name, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()

	if ctx.Err() == context.DeadlineExceeded {
		return execResult{}, errTimedOut
	}
	if err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			// A non-zero exit is a normal outcome to hand back to the
			// caller (e.g. "trade execution failed"), not a Go-level
			// error -- exactly the distinction bullpen_client.py's own
			// `result.returncode != 0` check makes explicit.
			return execResult{stdout: stdout.Bytes(), stderr: stderr.Bytes(), exitCode: exitErr.ExitCode()}, nil
		}
		// Couldn't even start the process (binary not found, etc.).
		return execResult{}, err
	}
	return execResult{stdout: stdout.Bytes(), stderr: stderr.Bytes(), exitCode: 0}, nil
}

// Runner wraps one `bullpen` CLI invocation contract. Zero value is not
// usable — construct via NewRunner.
type Runner struct {
	binaryName string
	exec       execFunc
}

// NewRunner returns a Runner that shells out to the real `bullpen` binary
// on PATH.
func NewRunner() *Runner {
	return &Runner{binaryName: "bullpen", exec: realExec}
}

// Options mirrors run_bullpen_json's own parameters and their documented
// defaults.
type Options struct {
	// Retries: 1 (the default, via the zero value) means "try once, no
	// retry" — this MUST stay the default for any call that can move
	// funds (buy/sell). Only read-only calls should pass Retries>1: a
	// retried buy/sell risks double-executing a trade that actually
	// filled but errored on the response leg.
	Retries int
	// RetryDelay: default 500ms (via the zero value).
	RetryDelay time.Duration
	// Timeout: default DefaultCallTimeout (via the zero value). Only
	// read-only, high-frequency call sites should pass a tighter value —
	// never tighten a money-moving call, that manufactures
	// order.UnknownFillState outcomes.
	Timeout time.Duration
}

// RunJSON mirrors run_bullpen_json(args, retries, retry_delay, timeout):
// invokes `bullpen <args...> --output json`, retrying up to opts.Retries
// times (default 1, i.e. no retry) on any error.
func (r *Runner) RunJSON(ctx context.Context, args []string, opts Options) (map[string]any, error) {
	retries := opts.Retries
	if retries <= 0 {
		retries = 1
	}
	retryDelay := opts.RetryDelay
	if retryDelay == 0 {
		retryDelay = 500 * time.Millisecond
	}
	timeout := opts.Timeout
	if timeout == 0 {
		timeout = DefaultCallTimeout
	}

	var lastErr error
	for attempt := 1; attempt <= retries; attempt++ {
		data, err := r.runOnce(ctx, args, timeout)
		if err == nil {
			return data, nil
		}
		lastErr = err
		if attempt < retries {
			time.Sleep(retryDelay)
		}
	}
	return nil, lastErr
}

// runOnce mirrors _run_bullpen_json_once field-for-field: parse stdout as
// JSON best-effort BEFORE checking the exit code (a trade command can
// exit non-zero while still printing a JSON error body), prefer a
// structured error/error_code/message field over raw stderr when present,
// and treat exit code 2 as an AuthError distinct from every other
// non-zero exit.
func (r *Runner) runOnce(ctx context.Context, args []string, timeout time.Duration) (map[string]any, error) {
	fullArgs := append(append([]string{}, args...), "--output", "json")
	result, execErr := r.exec(ctx, r.binaryName, fullArgs, timeout)
	if errors.Is(execErr, errTimedOut) {
		return nil, &TimeoutError{Args: args, Timeout: timeout}
	}
	if execErr != nil {
		return nil, fmt.Errorf("bullpen %s: %w", strings.Join(args, " "), execErr)
	}

	var data map[string]any
	if trimmed := bytes.TrimSpace(result.stdout); len(trimmed) > 0 {
		_ = json.Unmarshal(trimmed, &data) // best-effort; a parse failure just leaves data nil, same as Python's except json.JSONDecodeError: data = None
	}

	if result.exitCode != 0 {
		detail := strings.TrimSpace(string(result.stderr))
		if d := stringField(data, "error"); d != "" {
			detail = d
		} else if d := stringField(data, "error_code"); d != "" {
			detail = d
		} else if d := stringField(data, "message"); d != "" {
			detail = d
		}
		if detail == "" {
			detail = "no error detail"
		}
		message := fmt.Sprintf("bullpen %s exited %d: %s", strings.Join(args, " "), result.exitCode, detail)
		if result.exitCode == 2 {
			return nil, &AuthError{Msg: message}
		}
		return nil, errors.New(message)
	}
	if data == nil {
		return nil, fmt.Errorf("bullpen %s produced no parseable JSON output: %q", strings.Join(args, " "), string(result.stdout))
	}
	if okVal, exists := data["ok"]; exists {
		if b, isBool := okVal.(bool); isBool && !b {
			return nil, fmt.Errorf("bullpen %s error: %v", strings.Join(args, " "), data["error"])
		}
	}
	return data, nil
}

func stringField(m map[string]any, key string) string {
	if m == nil {
		return ""
	}
	if v, ok := m[key].(string); ok {
		return v
	}
	return ""
}

func numericField(m map[string]any, key string) (float64, bool) {
	// encoding/json decodes every JSON number into a Go float64 when the
	// target is `any` -- this is exactly that decoding, not a numeric
	// coercion of our own.
	v, ok := m[key]
	if !ok {
		return 0, false
	}
	f, ok := v.(float64)
	return f, ok
}

// RequireFilled mirrors require_filled(response, action_desc): only a
// MATCHED status with at least one transaction hash counts as a confirmed
// on-chain fill.
func RequireFilled(response map[string]any, actionDesc string) (map[string]any, error) {
	status := strings.ToUpper(stringField(response, "status"))
	var txHashes []any
	if v, ok := response["transaction_hashes"].([]any); ok {
		txHashes = v
	}
	if !FilledTradeStatuses[status] || len(txHashes) == 0 {
		statusDisplay := status
		if statusDisplay == "" {
			statusDisplay = "missing"
		}
		return nil, fmt.Errorf(
			"%s did not confirm an on-chain fill (status=%s, transaction_hashes=%v)",
			actionDesc, statusDisplay, txHashes,
		)
	}
	return response, nil
}

// ExtractFillPrice mirrors extract_fill_price(): best-effort read of the
// ACTUAL average fill price. UNVERIFIED against a real response (same
// status the Python original carries) — tries the plausible candidate
// field names and returns nil if none is present, in which case the
// caller falls back to the source trade's price. A *float64, not a plain
// float64, specifically to distinguish "no plausible field present" (nil)
// from "the field really is present and equals a valid price" (never 0,
// since prices here are always in (0, 1]).
func ExtractFillPrice(response map[string]any) *float64 {
	for _, key := range []string{"avg_price", "average_price", "fill_price", "executed_price", "price"} {
		if v, ok := numericField(response, key); ok && v > 0 && v <= 1 {
			return &v
		}
	}
	return nil
}

// ExtractFilledShares mirrors extract_filled_shares(): best-effort read
// of the ACTUAL number of shares filled, specifically to avoid overstating
// a position on a partial fill. Returns nil (not 0) when no plausible
// field is present, so the caller can distinguish "genuinely filled zero
// shares" (a real, present numeric 0) from "response shape doesn't tell
// us" (nil).
func ExtractFilledShares(response map[string]any) *float64 {
	for _, key := range []string{"filled_shares", "shares_filled", "matched_shares", "shares", "size"} {
		if v, ok := numericField(response, key); ok && v >= 0 {
			return &v
		}
	}
	return nil
}

// ExtractOrderID mirrors extract_order_id(): best-effort read of a
// resting order's id from a limit-buy/limit-sell response. Returns ""
// (Python: None) if no plausible field is present — the caller must treat
// the order as unable-to-track, never assume a successful placement.
func ExtractOrderID(response map[string]any) string {
	for _, key := range []string{"order_id", "orderId", "id"} {
		if v, ok := response[key].(string); ok && v != "" {
			return v
		}
	}
	return ""
}

// ExtractOrderStatus mirrors extract_order_status(): best-effort read of
// a resting order's status field, uppercased. Returns "" (Python: None) if
// no plausible field is present.
func ExtractOrderStatus(response map[string]any) string {
	for _, key := range []string{"status", "order_status", "state"} {
		if v, ok := response[key].(string); ok && v != "" {
			return strings.ToUpper(v)
		}
	}
	return ""
}
