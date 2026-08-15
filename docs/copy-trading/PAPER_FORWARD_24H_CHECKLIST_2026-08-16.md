# Paper forward observation — 24-hour checklist (2026-08-16)

## Scope and frozen controls

- Observation starts only after exactly one `bot.py` process and one Phase-0 recorder are healthy.
- `config.LIVE_MODE` stays `False` for the entire window.
- `data/autodeploy.lock` stays present for the entire window. No pushed commit may enter the run.
- `data/watchdog_paused` is removed only after the initial bot start succeeds; watchdog then protects
  evidence uptime.
- Bullpen auth may remain absent only under the explicit paper-mode suppression ending
  `2026-08-23T00:00:00Z`. Suppression is forbidden in live mode. Before any live-readiness review,
  AWS must complete `bullpen login` and the authenticated read-only canary must pass.
- Flat `$3` `unprovenanced` sizing is intentional experimental isolation, not the long-term sizing
  policy. Rebuilding the global scorer happens after this forward test, not during it.

Record the UTC start epoch once on AWS:

```bash
date +%s | tee data/paper_forward_24h_start_epoch
```

Run the read-only gate at +10 minutes, +1 hour, +6 hours and +24 hours:

```bash
python3 scripts/check_paper_forward_health.py \
  --since-epoch "$(cat data/paper_forward_24h_start_epoch)"
```

The command prints one JSON object and exits non-zero on a stop condition.

## Expected evidence

1. Process health
   - Exactly one `bot.py` process.
   - Exactly one `phase0_soak_recorder.py` process.
   - `watchdog_paused` absent; `autodeploy.lock` present.
2. Roster
   - `wallet_profile.status='track' AND circuit_breaker_muted=0` remains at least 3.
   - Starting baseline on 2026-08-16 was 12 active/unmuted wallets out of 17 tracked.
3. Sizing
   - New no-evidence copies are journalled as `sizing_tier='unprovenanced'`, never `base`.
   - Every new `unprovenanced` decision has `trade_size_usd=3.0`.
   - Category rows may remain a minority. No target percentage is preregistered for only 24 hours;
     report the observed distribution without tuning it.
4. Phase-0
   - Journal mtime stays within 180 seconds.
   - The most recent 500 rows contain `poll_cycle`, `wallet_signal*` or
     `delayed_book_observation`; a tail containing only `ws_disconnected` is not healthy evidence.
5. Safety
   - Zero `live_*` events.
   - No persisted `kill_switch` or active `entry_interlock`; a run blocked at the BUY gate is not
     forward PnL evidence.
   - Bot continues writing evidence after the ten-minute startup grace.
   - Bullpen canary emits `state='suppressed_paper_mode'`, `execution_ready=false` until login; it
     must not claim healthy execution.

## Immediate stop conditions

Recreate `data/watchdog_paused`, stop the single bot process gracefully and keep autodeploy locked if
any of these occurs:

- zero or multiple bot processes;
- active/unmuted roster below `MIN_TRACKED_TRADERS`;
- no new bot evidence after the ten-minute grace;
- any `unprovenanced` size other than `$3`;
- any `live_*` event while `LIVE_MODE=False`;
- a latched portfolio kill switch or active/malformed entry interlock;
- Phase-0 journal stale for more than 180 seconds, malformed, or its last 500 rows contain only
  disconnects;
- SQLite integrity/locking errors, disk exhaustion, kill switch, panic protocol or duplicate-order
  evidence.

Do not change trader selection, sizing, candidate universe or scorer logic inside the observation
window. A stopped/invalid run is restarted as a new preregistered window rather than spliced onto the
old one.

## Completion decision at +24 hours

- Save the four JSON health outputs and the sizing-tier distribution.
- If every gate stayed green, remove `data/autodeploy.lock` only after checking that local uncommitted
  ledger-v2 work has not been pushed to `main`.
- If any gate failed, keep autodeploy locked and treat the collected interval as operational debugging
  evidence, not strategy evidence.
