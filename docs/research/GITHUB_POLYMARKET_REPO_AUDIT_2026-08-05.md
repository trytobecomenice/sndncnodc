# GitHub Polymarket Repository Audit — 2026-08-05

**Status:** research complete; **no third-party source code imported**.

**Scope:** source-level review of the three repositories Joey named for Copy Bot and prediction-
market arbitrage research. This is an engineering/trading-risk review, not an endorsement of the
README claims.

## Executive decision

| Repository | Audited commit | What is actually present | License | Decision |
|---|---|---|---|---|
| [Drakkar-Software/OctoBot-Prediction-Market](https://github.com/Drakkar-Software/OctoBot-Prediction-Market) | `08e2c835578e536a2e2228da67352ca3405177d9` (2026-03-30) | Thin Python launcher/configuration package around the external `OctoBot[full]` dependency; no copy-sizing, wallet-polling, order or arbitrage engine is implemented in this repository | GPL-3.0-or-later | **Do not copy.** Potentially run separately later as a benchmark, after dependency and profile/tentacle review. |
| [Anmoldureha/polymarket-trading-bot-strategies](https://github.com/Anmoldureha/polymarket-trading-bot-strategies) (PolyHFT-style strategy repo) | `139639278ac514a172d959d5151100cf29674fbb` (2025-12-09) | Ten-strategy Python project, including a basic binary complete-set detector and a keyword-similarity “combinatorial” signal | **No license file found** | **Do not copy source.** No license means there is no permission to reuse its code. The general maths may be clean-room reimplemented. |
| [llogiq33/Polymarket-Copy-Trade](https://github.com/llogiq33/Polymarket-Copy-Trade) | `e26349f226ccf0b6f6d0dd0bdca777b4da60c534` (2026-04-10) | One `README.md`; no Python/TypeScript source, tests, package manifest or bot implementation | **No license file found** | **Reject as a code source.** There is nothing technical to integrate or test. |

The README descriptions are materially stronger than the source that is available in these
repositories. None of the three should be connected to our AWS process, production database,
`.env`, wallet credentials, or live execution path.

## 1. OctoBot Prediction Market

### What the repository really does

- `octobot_prediction_market/cli.py` delegates startup to `octobot.cli.main`.
- `requirements.txt` pins `OctoBot[full]==2.1.1`; the substantive trading system therefore lives
  in an external package and its profiles/tentacles, not in the audited repository.
- `default_config.json` selects `profile_copy_trading` and the `prediction_market` distribution.
- The only repository test checks that the CLI passes its default configuration to OctoBot. It
  does not test leader selection, wallet mirroring, sizing, fill handling, PnL, or arbitrage.
- The project's own README marks **Follow a profile**, follow-budget controls and market whitelist
  as work in progress. It also says the arbitrage bot is under development.

### Trader assessment

This is not a ready-made copy-trading module that we can lift into the current bot. At best, it is
an entrypoint into the much larger OctoBot ecosystem. Before even benchmarking it, we would need
to audit the exact installed `OctoBot` package plus the prediction-market profile/tentacles and
prove:

1. where leaderboard/profile discovery is implemented;
2. whether copy quantity means shares, notional, portfolio percentage or risk-adjusted exposure;
3. how partial fills, stale order books, duplicate trades, exits and restarts behave;
4. whether market/token identity and resolution rules are validated;
5. whether any private key or API credential is transmitted outside Polymarket;
6. how GPL obligations would apply to our repository and deployment.

**Conclusion:** useful as a product/UX reference only. Direct source reuse offers almost no value
from this wrapper and introduces GPL/dependency-supply-chain questions.

## 2. PolyHFT-style strategy repository

### Single-market arbitrage: the useful idea

The detector looks for a binary complete set where:

```text
executable YES ask + executable NO ask < $1.00
```

The economic idea is valid only if both legs acquire the **same number of redeemable shares**, the
two outcomes really form one complete set, and the all-in cost remains below $1 after fees,
slippage, depth and failure costs.

### Why its implementation is not production-safe

1. **The two legs are sequential, not atomic.** The implementation submits YES first and NO
   second. If NO fails, it tries to cancel YES. Cancellation cannot undo any YES quantity that has
   already filled, leaving naked directional exposure.
2. **It checks only top-of-book price.** There is no proof that enough size exists at the quoted
   asks, so the apparent edge can disappear across order-book depth.
3. **No fee/fill/slippage gate is included in the opportunity calculation.** A displayed
   `YES + NO < 1` is not the same as executable positive PnL.
4. **A post-order runtime bug exists.** `single_arbitrage.py` calls `trade_logger` after placing
   both orders and `error_logger` in the exception handler, but imports/defines neither name. The
   live path can therefore raise `NameError` after orders have already been submitted, and the
   error handler can itself raise another `NameError`.
5. **Paper mode does not simulate execution.** It returns a synthetic order with status `pending`;
   there is no fill probability, partial-fill, queue, depth, latency or mark-to-market model.
6. **The included unit test tests detection only.** It does not call `execute_trade()` and does not
   test one-leg failure, partial fills, cancellation races, fee-adjusted PnL or recovery.

The audited detector test passes, but that is a much narrower claim than safe arbitrage execution:

```text
python3 -m pytest tests/test_single_arbitrage.py test_new_strategies.py -q
3 passed, 4 warnings
```

### “Combinatorial arbitrage” is not an arbitrage engine

- Markets are grouped by a fixed keyword list, then question text is compared using word-set
  similarity.
- A price difference between similarly worded questions is treated as divergence. Similar wording
  does **not** establish logical implication, mutual exclusivity, exhaustiveness, identical expiry,
  or compatible resolution criteria.
- `execute_trade()` explicitly logs `Signal Only` and returns `None`.
- The test uses two mocked Bitcoin questions and checks only that a divergence is emitted. It does
  not verify logical constraints, settlement compatibility, an executable hedge or guaranteed
  payoff.

This may be a rough research screener, but describing it as executable combinatorial arbitrage
would be misleading.

### License and credential findings

- No `LICENSE`, `LICENSE.md` or equivalent license grant was found at the audited commit. We must
  not paste or adapt the source into this repository without permission from the copyright owner.
- Static review found no obvious hard-coded webhook/paste site or simple credential-exfiltration
  pattern. That is **not** a security guarantee.
- The project is designed to load Polymarket and Hyperliquid private keys. It must never be run
  using our `.env`, keys, AWS instance role or production database.

**Conclusion:** retain the complete-set maths as a research concept; do not reuse the code.

## 3. llogiq33/Polymarket-Copy-Trade

The audited commit contains only a promotional README. There is no implementation behind the
claims: no wallet watcher, leaderboard integration, order copier, database, risk manager, tests,
dependency lockfile or reproducible run command. It also has no license grant.

**Conclusion:** nothing can be technically validated or copied. Do not install, pay for, or give
credentials to anything based solely on this repository.

## Safe material we can carry forward

We can independently implement general trading concepts without copying expression/source code:

1. **Binary complete-set research detector** — calculate fee-adjusted, depth-aware cost for equal
   YES/NO share quantities and require a safety buffer below the $1 payout.
2. **Execution-risk model** — explicitly model two-leg latency, first-leg fill, second-leg failure,
   unwind price, partial fills and maximum tolerable leg exposure.
3. **Logical-market graph** — combinatorial relationships must be based on explicit event/outcome
   constraints, resolution source, expiry and implication rules; never keyword similarity alone.
4. **Shadow-only observation** — record hypothetical opportunities, attainable depth, latency and
   realized theoretical edge without submitting orders.
5. **Promotion gate** — only consider paper position simulation after sufficient shadow samples,
   false-positive review and deterministic tests. Live execution remains out of scope under this
   project's paper-only rule.

Recommended v1 acceptance rule for one binary market:

```text
guaranteed_edge = 1.00
                  - depth_weighted_yes_cost
                  - depth_weighted_no_cost
                  - all_fees
                  - leg_risk_reserve

signal only when guaranteed_edge >= configured_safety_buffer
```

This formula is intentionally a specification, not copied implementation.

## Import and security manifest

- Third-party source files copied into this repository: **0**.
- Third-party packages installed: **0**.
- Third-party code executed with project credentials: **0**.
- `.env`, API keys, SSH keys or private keys read/copied/staged: **0**.
- AWS bot process or production database changed: **no**.
- External repositories were cloned only into a temporary audit directory for read-only review.

## Proposed next step — not performed in this audit

Create an isolated, paper-only `research/arbitrage` specification and shadow detector written from
scratch against current official Polymarket market/order-book interfaces. It should have no import
path into Copy Bot execution, no key access and no order-submission method. First prove how often
the displayed edge survives depth, fees and latency; then decide whether the idea deserves a paper
portfolio module.

## Source permalinks

- OctoBot: [README feature status](https://github.com/Drakkar-Software/OctoBot-Prediction-Market/blob/08e2c835578e536a2e2228da67352ca3405177d9/README.md#L23-L43), [CLI delegation](https://github.com/Drakkar-Software/OctoBot-Prediction-Market/blob/08e2c835578e536a2e2228da67352ca3405177d9/octobot_prediction_market/cli.py#L16-L28), [default profile](https://github.com/Drakkar-Software/OctoBot-Prediction-Market/blob/08e2c835578e536a2e2228da67352ca3405177d9/octobot_prediction_market/config/default_config.json#L21-L23), [dependency pin](https://github.com/Drakkar-Software/OctoBot-Prediction-Market/blob/08e2c835578e536a2e2228da67352ca3405177d9/requirements.txt), and [GPL license](https://github.com/Drakkar-Software/OctoBot-Prediction-Market/blob/08e2c835578e536a2e2228da67352ca3405177d9/LICENSE).
- PolyHFT-style repo: [single-market detector/execution](https://github.com/Anmoldureha/polymarket-trading-bot-strategies/blob/139639278ac514a172d959d5151100cf29674fbb/src/strategies/single_arbitrage.py#L162-L277), [paper order stub](https://github.com/Anmoldureha/polymarket-trading-bot-strategies/blob/139639278ac514a172d959d5151100cf29674fbb/src/api/polymarket_client.py#L295-L345), [detection-only test](https://github.com/Anmoldureha/polymarket-trading-bot-strategies/blob/139639278ac514a172d959d5151100cf29674fbb/tests/test_single_arbitrage.py#L48-L70), and [signal-only combinatorial strategy](https://github.com/Anmoldureha/polymarket-trading-bot-strategies/blob/139639278ac514a172d959d5151100cf29674fbb/src/strategies/combinatorial_arbitrage.py#L138-L157).
- llogiq33: [audited README-only repository](https://github.com/llogiq33/Polymarket-Copy-Trade/tree/e26349f226ccf0b6f6d0dd0bdca777b4da60c534).

## Change record for Claude

1. **When:** 2026-08-05 HKT.
2. **Files adjusted:**
   - `docs/research/GITHUB_POLYMARKET_REPO_AUDIT_2026-08-05.md` — created this audit.
   - `CLAUDE_HANDOFF.md` — added a matching handoff entry.
3. **What changed:**
   - Audited the exact commits listed above at source level.
   - Recorded the difference between README claims and implemented behavior.
   - Rejected direct code copying on technical and/or licensing grounds.
   - Recorded a clean-room, shadow-only path for later arbitrage research.
   - Made no bot logic, strategy parameter, roster, AWS, database, `.env` or credential change.
