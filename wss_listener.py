#!/usr/bin/env python3
"""
Standalone Polygon WebSocket listener (the PRODUCER half of a Producer-
Consumer hand-off) — real-time detection of a tracked wallet's Polymarket
trades, replacing polling for this one purpose. Deliberately NOT merged into
bot.py: bot.py's main loop is a plain synchronous `while` loop (no asyncio
anywhere in that file, confirmed by reading it before writing this), and
bolting an asyncio websocket client onto it directly would risk exactly the
instability asyncio-in-a-sync-app is known for. Instead this is a second,
independent OS process that only ever INSERTs into `live_whale_event` (see
packages/db/src/schema.ts) — bot.py's normal sweep loop is meant to poll
that table as an ordinary consumer, the same way it already polls
`pending_execution` (Rule 29). See "WHAT BOT.PY STILL NEEDS TO DO" at the
bottom of this docstring — that consumer side is NOT built yet.

WHAT THIS DETECTS AND WHY (read before trusting this against real capital):

Polymarket trades settle on-chain as CTF (Conditional Tokens Framework,
Gnosis's ERC1155 standard) `TransferSingle` events — NOT as a simple DEX
swap. `TransferSingle(operator, from, to, id, value)` tells you WHO moved
WHICH outcome-token position and HOW MANY shares, but carries no price on
its own. To recover price, this script also fetches the full transaction
receipt for every detected transfer and looks for a paired ERC20 `Transfer`
in the SAME transaction — the collateral (USDC-family) leg of the trade —
and computes price from that pair. If no such transfer is found, price and
usdc_amount are stored as NULL, never guessed.

An alternative, arguably cleaner approach exists: Polymarket's CTF Exchange
contract emits its own `OrderFilled` event (orderHash, maker, taker, side,
tokenId, makerAmountFilled, takerAmountFilled, fee, ...) which — if you have
its exact ABI — hands you price directly without needing the paired-transfer
correlation. This script does NOT use that path: I could not verify the
exact field layout/indexing of that event with full confidence (found via
web search, not independently confirmed against the deployed contract's
actual ABI on PolygonScan or Polymarket's own `ctf-exchange` GitHub repo),
and getting an event decode subtly wrong is a silent-failure risk in a
system feeding real trading decisions. The TransferSingle + paired-transfer
approach only depends on the ERC1155/ERC20 standards themselves (unambiguous,
publicly fixed forever), not on a Polymarket-specific event schema I can't
fully confirm. If you verify OrderFilled's real ABI, that's a legitimate,
probably-better v2 — flagged as a next step, not built here.

CONTRACT ADDRESSES — VERIFY THESE YOURSELF BEFORE RUNNING LIVE:
- CTF (Conditional Tokens): 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
- CTF Exchange V2 (current primary): 0xE111180000d2663C0091e4f400237545B87B996B
- CTF Exchange V1 (legacy, may still see fills): 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
  These three came from a web search + a fetch of docs.polymarket.com/resources/
  contracts, cross-checked against independent PolygonScan listings for the two
  Exchange addresses specifically (both appeared identically across two separate
  lookups). The CTF address itself only came from one source and was not
  independently cross-checked. A "Neg Risk CTF Exchange" address and a "pUSD
  collateral token" address also surfaced during research but are NOT included
  below — their format looked suspicious (unusually repetitive digit patterns,
  and "pUSD" doesn't match Polymarket's well-known USDC collateral) and I could
  not verify them confidently, so they are omitted rather than silently used.
  CONFIRM ALL THREE ADDRESSES ABOVE ON POLYGONSCAN (or docs.polymarket.com)
  YOURSELF before pointing this at anything you intend to act on.

RESILIENCE: the outer loop in main() never lets a connection drop, a decode
error, or an unrecognized payload shape crash the process — every failure is
caught, logged, and followed by a reconnect with exponential backoff
(subscriptions do not survive a dropped connection and are always
re-established from scratch after reconnecting).

NOT LIVE-TESTED end-to-end: I do not have a real Alchemy/QuickNode WSS URL
or a live whale wallet to test against in this environment. Every piece that
could be verified WAS verified directly against the installed web3.py 7.16.0
package (the exact `AsyncWeb3`/`WebSocketProvider`/`eth.subscribe`/
`socket.process_subscriptions`/`keccak` calls used below all exist and
behave as expected — checked by running them, not just reading docs). What
was NOT verified is the exact runtime shape of a live `eth_subscription`
notification over a real socket — `_unwrap_subscription_payload()` below
handles the two shapes documented/observed in web3.py's own examples
defensively, but your first live run is the real test of that part.

WHAT BOT.PY STILL NEEDS TO DO (not built here — brief version; ask if you
want this built too):
1. Poll `SELECT * FROM live_whale_event WHERE consumed_at IS NULL ORDER BY
   block_number, log_index` on a sweep, same cadence as sweep_pending_executions.
2. Resolve `token_id` (a raw CTF position id) to a Polymarket `market_slug`/
   `outcome` — REQUIRED before this can feed process_trade()'s pipeline, and
   NOT solved by this script. Polymarket's own Gamma/CLOB API has a
   token-id lookup for this; bullpen may already expose it.
3. Once resolved, either call process_trade() directly with a synthesized
   trade dict, or (better, given Rule 29 precedent) go straight into
   create_pending_execution()/_execute_buy() for wallets already in
   config.LIMIT_ORDER_TRACKED_WALLETS.
4. UPDATE live_whale_event SET consumed_at = ? WHERE id = ? once processed.
"""

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
import time
import uuid

from dotenv import load_dotenv
from eth_abi import decode as abi_decode
from web3 import AsyncWeb3, Web3, WebSocketProvider

import config

# Loads .env into os.environ if present (git-ignored — see .env.example);
# never overrides a var already set in the real shell environment. Only
# this script needs it today: POLYGON_WSS_URL is a credential-bearing URL
# (an Alchemy/Infura/etc. API key embedded in it) that shouldn't be typed
# directly into a shell profile shared with anything else.
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] wss_listener: %(message)s",
)
logger = logging.getLogger("wss_listener")

# web3's own WebSocketProvider (subclass of PersistentConnectionProvider,
# which is where the "Connecting to: <endpoint>" / "Successfully connected
# to: <endpoint>" INFO logging actually lives) resolves self.logger to its
# OWN class-attribute logger, "web3.providers.WebSocketProvider" -- not its
# parent's name (confirmed live: silencing only the parent name did NOT
# stop the leak). Since POLYGON_WSS_URL carries the API key inline (e.g.
# Alchemy's wss://.../v2/<KEY> form), that credential would otherwise land
# in plaintext in this script's own log file via the shared root handler
# above. Both silenced to WARNING, in case a future web3 version logs via
# the parent name instead; genuine connection problems still surface as
# WARNING/ERROR from either.
logging.getLogger("web3.providers.WebSocketProvider").setLevel(logging.WARNING)
logging.getLogger("web3.providers.PersistentConnectionProvider").setLevel(logging.WARNING)

# --- Config (env vars) ------------------------------------------------------

POLYGON_WSS_URL = os.environ.get("POLYGON_WSS_URL")

# Comma-separated, case-insensitive (normalized to lowercase below). 2026-07-25:
# defaults to ALL of config.TRACKED_TRADERS (every wallet the bot copies) rather
# than requiring the operator to hand-copy addresses into this env var — a
# wallet added to TRACKED_TRADERS later is picked up automatically on this
# script's next restart, instead of silently NOT being watched here until
# someone remembers to update a second, separate list. WHALE_WALLET_ADDRESSES
# still works as an override (e.g. for testing against a single wallet).
_WHALE_WALLETS_RAW = os.environ.get("WHALE_WALLET_ADDRESSES", "")
if _WHALE_WALLETS_RAW.strip():
    WHALE_WALLETS = {a.strip().lower() for a in _WHALE_WALLETS_RAW.split(",") if a.strip()}
else:
    WHALE_WALLETS = {addr.lower() for addr in config.TRACKED_TRADERS}

CTF_CONTRACT_ADDRESS = os.environ.get(
    "CTF_CONTRACT_ADDRESS", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
)
# Used only to help identify which paired ERC20 Transfer in a tx is the
# collateral leg (see _find_collateral_transfer) — a Transfer whose
# counterparty is one of these is treated as the trade's price leg.
_CTF_EXCHANGE_ADDRESSES_RAW = os.environ.get(
    "CTF_EXCHANGE_ADDRESSES",
    "0xE111180000d2663C0091e4f400237545B87B996B,0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
)
CTF_EXCHANGE_ADDRESSES = {
    a.strip().lower() for a in _CTF_EXCHANGE_ADDRESSES_RAW.split(",") if a.strip()
}

# Decimals assumption for the collateral (USDC-family) and outcome (CTF
# share) tokens — both commonly 6 on Polymarket/Polygon, NOT independently
# verified against the deployed token contracts here. Override via env if
# your verification finds otherwise.
COLLATERAL_TOKEN_DECIMALS = int(os.environ.get("COLLATERAL_TOKEN_DECIMALS", "6"))
OUTCOME_TOKEN_DECIMALS = int(os.environ.get("OUTCOME_TOKEN_DECIMALS", "6"))

RECONNECT_BACKOFF_INITIAL_SECONDS = float(os.environ.get("RECONNECT_BACKOFF_INITIAL_SECONDS", "2"))
RECONNECT_BACKOFF_MAX_SECONDS = float(os.environ.get("RECONNECT_BACKOFF_MAX_SECONDS", "60"))

# Computed, not hardcoded — see module docstring on why (transcription-error
# risk on a hand-typed 32-byte hash is real; keccak of the human-readable
# signature is not).
TRANSFER_SINGLE_TOPIC = "0x" + Web3.keccak(
    text="TransferSingle(address,address,address,uint256,uint256)"
).hex()
ERC20_TRANSFER_TOPIC = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex()


def _address_to_topic(address):
    """Left-pads a 20-byte address into a 32-byte log topic, the shape
    `eth_subscribe`'s topics filter (and a raw log's indexed topics) use."""
    return "0x" + "0" * 24 + address.lower().replace("0x", "")


def _topic_to_address(topic):
    """Inverse of _address_to_topic — topic is a 32-byte hex string (or
    HexBytes), the last 20 bytes are the address."""
    hex_str = topic.hex() if hasattr(topic, "hex") else str(topic)
    hex_str = hex_str.replace("0x", "")
    return "0x" + hex_str[-40:]


# --- SQLite hand-off ---------------------------------------------------------
# Plain synchronous sqlite3, called directly from the async loop (a brief
# blocking call, not offloaded to an executor) — acceptable here because
# events are processed one at a time in a single sequential loop, never
# fanned out concurrently, and whale trade frequency is low. A conscious
# simplicity trade-off, not an oversight.


def _connect():
    conn = sqlite3.connect(config.SQLITE_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def insert_live_whale_event(row):
    """INSERT OR IGNORE keyed on (tx_hash, log_index) — see schema.ts's
    live_whale_event_tx_log_idx unique index. A re-delivered log (e.g. from
    a reconnect that re-processes a block it already saw) is a silent no-op,
    not a duplicate row or a crash.
    """
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO live_whale_event "
            "(id, wallet_address, contract_address, event_type, direction, token_id, "
            "share_amount, usdc_amount, price, tx_hash, log_index, block_number, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()), row["wallet_address"], row["contract_address"],
                row["event_type"], row["direction"], row["token_id"], row["share_amount"],
                row["usdc_amount"], row["price"], row["tx_hash"], row["log_index"],
                row["block_number"], int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# --- Log decoding -------------------------------------------------------------


def _decode_transfer_single(log):
    """log's topics: [event_sig, operator, from, to] (all indexed);
    data: abi-encoded (id, value), both non-indexed. Returns
    (from_address, to_address, token_id, share_amount_raw)."""
    topics = log["topics"]
    from_address = _topic_to_address(topics[2])
    to_address = _topic_to_address(topics[3])
    token_id, value = abi_decode(["uint256", "uint256"], bytes(log["data"]))
    return from_address, to_address, token_id, value


async def _find_collateral_transfer(w3, tx_hash):
    """Fetches the full transaction receipt and looks for an ERC20 Transfer
    log in the SAME transaction whose counterparty (from or to) is one of
    CTF_EXCHANGE_ADDRESSES — treated as the trade's collateral leg. Returns
    the raw uint256 value if found, else None. Best-effort: a tx with no
    such transfer (or a decode failure) returns None rather than raising —
    losing one price measurement must never block ingesting the trade
    itself (same philosophy as bot.py's measure_paper_shortfall).
    """
    try:
        receipt = await w3.eth.get_transaction_receipt(tx_hash)
    except Exception as e:
        logger.warning(f"could not fetch receipt for {tx_hash.hex() if hasattr(tx_hash, 'hex') else tx_hash}: {e}")
        return None

    for receipt_log in receipt["logs"]:
        topics = receipt_log["topics"]
        if len(topics) < 3:
            continue
        # HexBytes.hex() includes "0x" on some versions, not others —
        # normalize both sides to a bare hex string before comparing.
        topic0 = topics[0].hex().replace("0x", "")
        if topic0 != ERC20_TRANSFER_TOPIC.replace("0x", ""):
            continue
        from_addr = _topic_to_address(topics[1])
        to_addr = _topic_to_address(topics[2])
        if from_addr not in CTF_EXCHANGE_ADDRESSES and to_addr not in CTF_EXCHANGE_ADDRESSES:
            continue
        try:
            (value,) = abi_decode(["uint256"], bytes(receipt_log["data"]))
            return value
        except Exception as e:
            logger.warning(f"failed to decode candidate collateral transfer: {e}")
            continue
    return None


def _unwrap_subscription_payload(payload):
    """Defensive against the two shapes web3.py's own docs/examples show for
    a delivered eth_subscription notification — a dict with a "result" key,
    or (less commonly, older/alternate code paths) the log object itself.
    NOT independently live-verified against a real socket (see module
    docstring) — if this raises on your first real run, the payload shape
    just needs one more branch here, not a redesign.
    """
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    if hasattr(payload, "result"):
        return payload.result
    if isinstance(payload, dict) and "topics" in payload:
        return payload  # already a bare log object
    raise ValueError(f"unrecognized subscription payload shape: {type(payload)}: {payload!r}")


async def _handle_log(w3, log, direction):
    from_address, to_address, token_id, share_amount_raw = _decode_transfer_single(log)
    wallet = to_address.lower() if direction == "buy" else from_address.lower()

    # Not every CTF TransferSingle is a market trade (2026-07-25 addition —
    # found while reasoning about whether this feed is complete enough to
    # ever be trusted as the SOLE trade-detection mechanism; not verified
    # against a real example, this is CTF/ERC1155 mint-and-burn convention,
    # not something confirmed live). Gnosis Conditional Tokens' own
    # splitPosition (locks collateral, MINTS outcome tokens) and
    # mergePositions/redeemPositions (BURN outcome tokens, return
    # collateral) are typically represented as TransferSingle events too —
    # a mint has from=0x0..0, a burn has to=0x0..0. Neither is a genuine
    # order-book trade; skip both rather than risk mistaking a wallet's own
    # direct position-management for a signal worth copying.
    zero_address = "0x" + "0" * 40
    counterparty = from_address.lower() if direction == "buy" else to_address.lower()
    if counterparty == zero_address:
        logger.info(
            f"skipping likely mint/burn (not a trade): wallet={wallet} token_id={token_id} "
            f"from={from_address} to={to_address} tx={log['transactionHash'].hex()}"
        )
        return

    usdc_amount = None
    price = None
    collateral_raw = await _find_collateral_transfer(w3, log["transactionHash"])
    if collateral_raw is not None and share_amount_raw > 0:
        usdc_amount = collateral_raw / (10 ** COLLATERAL_TOKEN_DECIMALS)
        shares_human = share_amount_raw / (10 ** OUTCOME_TOKEN_DECIMALS)
        price = usdc_amount / shares_human if shares_human else None

    row = {
        "wallet_address": wallet,
        "contract_address": log["address"].lower(),
        "event_type": "TransferSingle",
        "direction": direction,
        "token_id": str(token_id),
        "share_amount": str(share_amount_raw),
        "usdc_amount": usdc_amount,
        "price": price,
        "tx_hash": log["transactionHash"].hex(),
        "log_index": log["logIndex"],
        "block_number": log["blockNumber"],
    }
    insert_live_whale_event(row)
    logger.info(
        f"{direction.upper()} wallet={wallet} token_id={token_id} "
        f"shares_raw={share_amount_raw} price={price} tx={row['tx_hash']}"
    )


async def _run_once():
    """One connect-subscribe-listen session. Raises on ANY connection-level
    or unexpected failure so the caller's reconnect loop handles it — this
    function itself never retries, by design (single responsibility)."""
    if not WHALE_WALLETS:
        raise RuntimeError("WHALE_WALLET_ADDRESSES is empty — nothing to subscribe to")

    async with AsyncWeb3(WebSocketProvider(POLYGON_WSS_URL)) as w3:
        subscriptions = {}  # subscription_id -> "buy" | "sell", used only for logging
        for wallet in WHALE_WALLETS:
            wallet_topic = _address_to_topic(wallet)
            sell_sub = await w3.eth.subscribe(
                "logs",
                {"address": CTF_CONTRACT_ADDRESS,
                 "topics": [TRANSFER_SINGLE_TOPIC, None, wallet_topic, None]},
            )
            buy_sub = await w3.eth.subscribe(
                "logs",
                {"address": CTF_CONTRACT_ADDRESS,
                 "topics": [TRANSFER_SINGLE_TOPIC, None, None, wallet_topic]},
            )
            subscriptions[sell_sub] = "sell"
            subscriptions[buy_sub] = "buy"
            logger.info(f"subscribed: wallet={wallet} sell_sub={sell_sub} buy_sub={buy_sub}")

        async for payload in w3.socket.process_subscriptions():
            try:
                log = _unwrap_subscription_payload(payload)
                # direction is re-derived from the log's own from/to below
                # (_handle_log), not trusted purely off which subscription
                # fired — the subscription id is only used for the log line
                # here, defense-in-depth against a payload-shape surprise
                # where the id<->direction mapping in `subscriptions` didn't
                # line up the way expected.
                sub_id = payload.get("subscription") if isinstance(payload, dict) else getattr(payload, "subscription", None)
                direction_hint = subscriptions.get(sub_id, "buy")
                topics = log["topics"]
                to_addr = _topic_to_address(topics[3]).lower()
                direction = "buy" if to_addr in WHALE_WALLETS else "sell"
                if direction != direction_hint:
                    logger.debug(
                        f"direction re-derived from log ({direction}) differs from "
                        f"subscription hint ({direction_hint}) — using the log-derived value"
                    )
                await _handle_log(w3, log, direction)
            except Exception as e:
                # A single bad/unexpected log must never take down the whole
                # session — log it and keep listening. Connection-level
                # failures surface as this async-for loop ending/raising,
                # which propagates out to _run_once()'s caller instead.
                logger.error(f"failed to process one subscription payload: {e}", exc_info=True)


async def main():
    if not POLYGON_WSS_URL:
        logger.critical("POLYGON_WSS_URL is not set — nothing to connect to. Exiting.")
        sys.exit(1)
    if not WHALE_WALLETS:
        logger.critical("WHALE_WALLET_ADDRESSES is not set (comma-separated) — nothing to watch. Exiting.")
        sys.exit(1)

    logger.info(f"CTF contract: {CTF_CONTRACT_ADDRESS}")
    logger.info(f"CTF Exchange addresses (collateral-leg matching): {sorted(CTF_EXCHANGE_ADDRESSES)}")
    logger.info(f"Watching {len(WHALE_WALLETS)} wallet(s): {sorted(WHALE_WALLETS)}")
    logger.warning(
        "Contract addresses above were web-search-verified, not independently "
        "confirmed by you against PolygonScan — see this file's module docstring "
        "before trusting this against real capital."
    )

    backoff = RECONNECT_BACKOFF_INITIAL_SECONDS
    while True:
        try:
            await _run_once()
            # process_subscriptions() returning normally (rather than
            # raising) still means the connection ended — treat it the same
            # as an exception for reconnect purposes.
            logger.warning("subscription stream ended without an exception — reconnecting")
        except asyncio.CancelledError:
            raise  # real shutdown (Ctrl+C/SIGTERM) — never swallow this
        except Exception as e:
            logger.error(f"connection lost or session failed: {e}", exc_info=True)

        logger.info(f"reconnecting in {backoff:.1f}s")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()  # only used for --help right now; flags may be added later
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("wss_listener stopped.")
