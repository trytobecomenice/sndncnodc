#!/usr/bin/env python3
"""Unit tests for oms_client.py (2026-08-01, Phase 2 Session 5) — the
Python HTTP client for the Go OMS service. Mocks
http.client.HTTPConnection the same way test_telegram_alerts.py mocks
http.client.HTTPSConnection; never touches a real running OMS process.

Run: python3 -m unittest test_oms_client -v
"""

import json
import unittest
from unittest.mock import MagicMock, patch

import oms_client


def _mock_conn(status, body):
    conn = MagicMock()
    response = MagicMock(status=status)
    response.read.return_value = json.dumps(body).encode("utf-8") if body is not None else b""
    conn.getresponse.return_value = response
    return conn


class TestCreateOrder(unittest.TestCase):
    def test_success_returns_parsed_body_and_sends_correct_request(self):
        conn = _mock_conn(201, {"id": "order-1", "status": "pending"})
        with patch("oms_client.http.client.HTTPConnection", return_value=conn) as mock_conn_cls:
            result = oms_client.create_order("trade-123")
        mock_conn_cls.assert_called_once_with(
            oms_client.config.OMS_HOST, oms_client.config.OMS_PORT, timeout=oms_client.DEFAULT_TIMEOUT_SECONDS
        )
        conn.request.assert_called_once()
        method, path = conn.request.call_args.args[:2]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/orders")
        sent_body = json.loads(conn.request.call_args.kwargs["body"])
        self.assertEqual(sent_body, {"idempotency_key": "trade-123"})
        self.assertEqual(result, {"id": "order-1", "status": "pending"})
        conn.close.assert_called_once()

    def test_idempotent_replay_200_also_succeeds(self):
        conn = _mock_conn(200, {"id": "order-1", "status": "pending"})
        with patch("oms_client.http.client.HTTPConnection", return_value=conn):
            result = oms_client.create_order("trade-123")
        self.assertEqual(result["id"], "order-1")

    def test_error_response_raises_with_server_detail(self):
        conn = _mock_conn(400, {"error": "idempotency_key is required"})
        with patch("oms_client.http.client.HTTPConnection", return_value=conn):
            with self.assertRaises(oms_client.OmsClientError) as ctx:
                oms_client.create_order("")
        self.assertIn("idempotency_key is required", str(ctx.exception))

    def test_connection_failure_raises_oms_client_error_not_raw_oserror(self):
        with patch("oms_client.http.client.HTTPConnection", side_effect=OSError("no route to host")):
            with self.assertRaises(oms_client.OmsClientError) as ctx:
                oms_client.create_order("trade-123")
        self.assertIn("no route to host", str(ctx.exception))


class TestGetOrder(unittest.TestCase):
    def test_success_returns_parsed_body(self):
        conn = _mock_conn(200, {"id": "order-1", "status": "filled"})
        with patch("oms_client.http.client.HTTPConnection", return_value=conn):
            result = oms_client.get_order("order-1")
        self.assertEqual(result, {"id": "order-1", "status": "filled"})

    def test_sends_the_id_in_the_path(self):
        conn = _mock_conn(200, {"id": "order-1", "status": "pending"})
        with patch("oms_client.http.client.HTTPConnection", return_value=conn):
            oms_client.get_order("order-1")
        method, path = conn.request.call_args.args[:2]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/orders/order-1")

    def test_not_found_raises_oms_client_error(self):
        conn = _mock_conn(404, {"error": "order not found"})
        with patch("oms_client.http.client.HTTPConnection", return_value=conn):
            with self.assertRaises(oms_client.OmsClientError) as ctx:
                oms_client.get_order("no-such-id")
        self.assertIn("order not found", str(ctx.exception))


class TestCancelOrder(unittest.TestCase):
    def test_success_returns_the_cancelled_order(self):
        conn = _mock_conn(200, {"id": "order-1", "status": "invalidated"})
        with patch("oms_client.http.client.HTTPConnection", return_value=conn):
            result = oms_client.cancel_order("order-1")
        self.assertEqual(result["status"], "invalidated")
        method, path = conn.request.call_args.args[:2]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/orders/order-1/cancel")

    def test_conflict_on_already_terminal_order_raises_oms_client_error(self):
        conn = _mock_conn(409, {"error": "illegal order transition: invalidated -> invalidated"})
        with patch("oms_client.http.client.HTTPConnection", return_value=conn):
            with self.assertRaises(oms_client.OmsClientError) as ctx:
                oms_client.cancel_order("order-1")
        self.assertIn("illegal order transition", str(ctx.exception))


class TestTransitionOrder(unittest.TestCase):
    def test_success_returns_the_transitioned_order(self):
        conn = _mock_conn(200, {"id": "order-1", "status": "filled"})
        with patch("oms_client.http.client.HTTPConnection", return_value=conn):
            result = oms_client.transition_order("order-1", "filled")
        self.assertEqual(result["status"], "filled")
        method, path = conn.request.call_args.args[:2]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/orders/order-1/transition")
        sent_body = json.loads(conn.request.call_args.kwargs["body"])
        self.assertEqual(sent_body, {"to": "filled"})

    def test_rejects_an_unrecognized_target_before_making_any_request(self):
        with patch("oms_client.http.client.HTTPConnection") as mock_conn_cls:
            with self.assertRaises(ValueError):
                oms_client.transition_order("order-1", "banana")
        mock_conn_cls.assert_not_called()

    def test_conflict_on_already_terminal_order_raises_oms_client_error(self):
        conn = _mock_conn(409, {"error": "illegal order transition: filled -> expired"})
        with patch("oms_client.http.client.HTTPConnection", return_value=conn):
            with self.assertRaises(oms_client.OmsClientError) as ctx:
                oms_client.transition_order("order-1", "expired")
        self.assertIn("illegal order transition", str(ctx.exception))


class TestRequestRobustness(unittest.TestCase):
    def test_non_json_body_on_a_success_status_does_not_crash(self):
        conn = MagicMock()
        response = MagicMock(status=200)
        response.read.return_value = b"not json"
        conn.getresponse.return_value = response
        with patch("oms_client.http.client.HTTPConnection", return_value=conn):
            result = oms_client.get_order("order-1")
        self.assertEqual(result, {})

    def test_empty_body_does_not_crash(self):
        conn = _mock_conn(200, None)
        with patch("oms_client.http.client.HTTPConnection", return_value=conn):
            result = oms_client.get_order("order-1")
        self.assertEqual(result, {})

    def test_connection_is_closed_even_when_the_call_raises(self):
        conn = _mock_conn(500, {"error": "internal"})
        with patch("oms_client.http.client.HTTPConnection", return_value=conn):
            with self.assertRaises(oms_client.OmsClientError):
                oms_client.get_order("order-1")
        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
