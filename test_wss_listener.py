#!/usr/bin/env python3
"""Unit tests for wss_listener.py: pure address/topic helpers, TransferSingle
decoding, and _handle_log()'s mint/burn filter (2026-07-25 — CTF
splitPosition/mergePositions/redeemPositions can emit the same
TransferSingle shape as a genuine trade, with the zero address as
counterparty; not verified against a real on-chain example, reasoned from
ERC1155 mint/burn convention — see wss_listener.py's own comment).

Run: python3 -m unittest test_wss_listener -v
"""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("POLYGON_WSS_URL", "wss://example.invalid")
os.environ.setdefault("WHALE_WALLET_ADDRESSES", "0xE16D3F2A5807999b358aFfD9445C3a09E45E5e30")

import wss_listener as w  # noqa: E402


class TestAddressTopicHelpers(unittest.TestCase):
    def test_address_to_topic_round_trips(self):
        addr = "0xE16D3F2A5807999b358aFfD9445C3a09E45E5e30"
        topic = w._address_to_topic(addr)
        self.assertEqual(len(topic), 66)  # "0x" + 64 hex chars
        self.assertEqual(w._topic_to_address(topic).lower(), addr.lower())


class TestDecodeTransferSingle(unittest.TestCase):
    def test_decodes_from_to_token_id_and_value(self):
        from eth_abi import encode as abi_encode
        from_addr = "0x0000000000000000000000000000000000000001"
        to_addr = "0x0000000000000000000000000000000000000002"
        log = {
            "topics": [
                bytes.fromhex(w.TRANSFER_SINGLE_TOPIC[2:]),
                bytes.fromhex(w._address_to_topic("0x0000000000000000000000000000000000000099")[2:]),
                bytes.fromhex(w._address_to_topic(from_addr)[2:]),
                bytes.fromhex(w._address_to_topic(to_addr)[2:]),
            ],
            "data": abi_encode(["uint256", "uint256"], [42, 1000]),
        }
        from_out, to_out, token_id, value = w._decode_transfer_single(log)
        self.assertEqual(from_out.lower(), from_addr.lower())
        self.assertEqual(to_out.lower(), to_addr.lower())
        self.assertEqual(token_id, 42)
        self.assertEqual(value, 1000)


class TestHandleLogMintBurnFilter(unittest.TestCase):
    """_handle_log()'s zero-address skip — the new 2026-07-25 addition."""

    def _log(self, from_addr, to_addr, token_id=42, value=1000, tx_hash=b"\xab" * 32, log_index=0,
              block_number=100):
        from eth_abi import encode as abi_encode

        class _FakeTxHash:
            def hex(self_inner):
                return tx_hash.hex()

        return {
            "topics": [
                bytes.fromhex(w.TRANSFER_SINGLE_TOPIC[2:]),
                bytes.fromhex(w._address_to_topic("0x0000000000000000000000000000000000000099")[2:]),
                bytes.fromhex(w._address_to_topic(from_addr)[2:]),
                bytes.fromhex(w._address_to_topic(to_addr)[2:]),
            ],
            "data": abi_encode(["uint256", "uint256"], [token_id, value]),
            "address": "0xCTFCONTRACT0000000000000000000000000000",
            "transactionHash": _FakeTxHash(),
            "logIndex": log_index,
            "blockNumber": block_number,
        }

    def test_mint_buy_from_zero_address_is_skipped(self):
        wallet = "0x0000000000000000000000000000000000000002"
        zero = "0x" + "0" * 40
        log = self._log(from_addr=zero, to_addr=wallet)
        with patch("wss_listener._find_collateral_transfer", new=AsyncMock(return_value=None)), \
             patch("wss_listener.insert_live_whale_event") as mock_insert:
            asyncio.run(w._handle_log(None, log, "buy"))
        mock_insert.assert_not_called()

    def test_burn_sell_to_zero_address_is_skipped(self):
        wallet = "0x0000000000000000000000000000000000000001"
        zero = "0x" + "0" * 40
        log = self._log(from_addr=wallet, to_addr=zero)
        with patch("wss_listener._find_collateral_transfer", new=AsyncMock(return_value=None)), \
             patch("wss_listener.insert_live_whale_event") as mock_insert:
            asyncio.run(w._handle_log(None, log, "sell"))
        mock_insert.assert_not_called()

    def test_normal_buy_between_two_real_addresses_is_inserted(self):
        from_addr = "0x0000000000000000000000000000000000000001"
        to_addr = "0x0000000000000000000000000000000000000002"
        log = self._log(from_addr=from_addr, to_addr=to_addr)
        with patch("wss_listener._find_collateral_transfer", new=AsyncMock(return_value=None)), \
             patch("wss_listener.insert_live_whale_event") as mock_insert:
            asyncio.run(w._handle_log(None, log, "buy"))
        mock_insert.assert_called_once()
        row = mock_insert.call_args.args[0]
        self.assertEqual(row["wallet_address"], to_addr.lower())
        self.assertEqual(row["direction"], "buy")

    def test_normal_sell_between_two_real_addresses_is_inserted(self):
        from_addr = "0x0000000000000000000000000000000000000001"
        to_addr = "0x0000000000000000000000000000000000000002"
        log = self._log(from_addr=from_addr, to_addr=to_addr)
        with patch("wss_listener._find_collateral_transfer", new=AsyncMock(return_value=None)), \
             patch("wss_listener.insert_live_whale_event") as mock_insert:
            asyncio.run(w._handle_log(None, log, "sell"))
        mock_insert.assert_called_once()
        row = mock_insert.call_args.args[0]
        self.assertEqual(row["wallet_address"], from_addr.lower())
        self.assertEqual(row["direction"], "sell")


class TestWhaleWalletsDefaultsToTrackedTraders(unittest.TestCase):
    def test_defaults_to_config_tracked_traders_when_env_var_unset(self):
        import config
        import importlib

        with patch.dict(os.environ, {"WHALE_WALLET_ADDRESSES": ""}):
            importlib.reload(w)
            expected = {addr.lower() for addr in config.TRACKED_TRADERS}
            self.assertEqual(w.WHALE_WALLETS, expected)
        # Restore the module-level state the other tests in this file rely on.
        os.environ["WHALE_WALLET_ADDRESSES"] = "0xE16D3F2A5807999b358aFfD9445C3a09E45E5e30"
        importlib.reload(w)

    def test_explicit_env_var_overrides_the_default(self):
        import importlib
        with patch.dict(os.environ, {"WHALE_WALLET_ADDRESSES": "0xAAAA,0xBBBB"}):
            importlib.reload(w)
            self.assertEqual(w.WHALE_WALLETS, {"0xaaaa", "0xbbbb"})
        os.environ["WHALE_WALLET_ADDRESSES"] = "0xE16D3F2A5807999b358aFfD9445C3a09E45E5e30"
        importlib.reload(w)


class TestCredentialLeakPrevention(unittest.TestCase):
    """POLYGON_WSS_URL carries an API key inline (e.g. Alchemy's
    wss://.../v2/<KEY> form). web3's WebSocketProvider logs the raw
    endpoint at INFO ("Connecting to: <endpoint>") via its own
    class-attribute logger -- confirmed live, silencing only the base
    class's logger name did NOT stop the leak, only the subclass's own
    name did. This just checks the module import silenced BOTH names, so a
    future web3 version change is at least covered defensively."""

    def test_websocket_provider_logger_silenced_to_warning(self):
        import logging
        logger = logging.getLogger("web3.providers.WebSocketProvider")
        self.assertGreaterEqual(logger.level, logging.WARNING)

    def test_persistent_connection_provider_logger_silenced_to_warning(self):
        import logging
        logger = logging.getLogger("web3.providers.PersistentConnectionProvider")
        self.assertGreaterEqual(logger.level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
