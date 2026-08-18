#!/usr/bin/env python3
"""Tests for Soundings — external HTTP endpoint prober."""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import soundings


class TestParseEndpoints(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(soundings.parse_endpoints(""), [])
        self.assertEqual(soundings.parse_endpoints(None), [])

    def test_single_url(self):
        result = soundings.parse_endpoints("http://localhost:8080")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "http://localhost:8080")
        self.assertEqual(result[0]["expected_status"], 200)

    def test_url_with_label(self):
        result = soundings.parse_endpoints("http://localhost:8080:My Service")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "http://localhost:8080")
        self.assertEqual(result[0]["label"], "My Service")

    def test_https_url(self):
        result = soundings.parse_endpoints("https://example.com:443:Example")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "https://example.com:443")
        self.assertEqual(result[0]["label"], "Example")

    def test_multiple_endpoints(self):
        raw = "http://localhost:8080:Svc1,http://localhost:9090:Svc2"
        result = soundings.parse_endpoints(raw)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["label"], "Svc1")
        self.assertEqual(result[1]["label"], "Svc2")

    def test_url_without_port(self):
        result = soundings.parse_endpoints("http://example.com/health:Health")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["label"], "Health")

    def test_whitespace_handling(self):
        raw = " http://localhost:8080:Svc1 , http://localhost:9090:Svc2 "
        result = soundings.parse_endpoints(raw)
        self.assertEqual(len(result), 2)


class TestEndpointState(unittest.TestCase):
    def test_initial_state(self):
        state = soundings.EndpointState({
            "url": "http://localhost:8080",
            "label": "Test",
            "expected_status": 200,
        })
        self.assertEqual(state.consecutive_failures, 0)
        self.assertFalse(state.alerted)
        self.assertIsNone(state.last_status)

    def test_to_dict(self):
        state = soundings.EndpointState({
            "url": "http://localhost:8080",
            "label": "Test",
            "expected_status": 200,
        })
        d = state.to_dict()
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["url"], "http://localhost:8080")
        self.assertEqual(d["label"], "Test")
        self.assertEqual(d["consecutive_failures"], 0)

    def test_failing_state(self):
        state = soundings.EndpointState({
            "url": "http://localhost:8080",
            "label": "Test",
            "expected_status": 200,
        })
        state.consecutive_failures = 3
        d = state.to_dict()
        self.assertEqual(d["status"], "failing")


class MockServer:
    """A small HTTP server for testing probes."""

    def __init__(self, status_code=200, delay=0):
        self.status_code = status_code
        self.delay = delay
        self.request_count = 0

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parent.request_count += 1
                if parent.delay:
                    time.sleep(parent.delay)
                self.send_response(parent.status_code)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, format, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


class TestProbe(unittest.TestCase):
    def setUp(self):
        self.mock = MockServer(200).start()

    def tearDown(self):
        self.mock.stop()

    def test_successful_probe(self):
        state = soundings.EndpointState({
            "url": self.mock.url,
            "label": "Test",
            "expected_status": 200,
        })
        result = soundings.probe(state)
        self.assertEqual(result, "ok")
        self.assertEqual(state.consecutive_failures, 0)
        self.assertEqual(state.last_status, 200)
        self.assertIsNotNone(state.last_response_time_ms)
        self.assertEqual(state.total_checks, 1)

    def test_wrong_status_code(self):
        self.mock.status_code = 503
        state = soundings.EndpointState({
            "url": self.mock.url,
            "label": "Test",
            "expected_status": 200,
        })
        result = soundings.probe(state)
        self.assertEqual(result, "failing")
        self.assertEqual(state.consecutive_failures, 1)

    def test_connection_refused(self):
        state = soundings.EndpointState({
            "url": "http://127.0.0.1:1",
            "label": "Dead",
            "expected_status": 200,
        })
        result = soundings.probe(state)
        self.assertEqual(result, "failing")
        self.assertEqual(state.consecutive_failures, 1)
        self.assertIsNotNone(state.last_error)

    @patch.object(soundings, "FAILURE_THRESHOLD", 2)
    def test_alert_after_threshold(self):
        state = soundings.EndpointState({
            "url": "http://127.0.0.1:1",
            "label": "Dead",
            "expected_status": 200,
        })
        soundings.probe(state)
        self.assertEqual(state.consecutive_failures, 1)
        result = soundings.probe(state)
        self.assertEqual(result, "alert")
        self.assertTrue(state.alerted)
        self.assertEqual(state.consecutive_failures, 2)

    @patch.object(soundings, "FAILURE_THRESHOLD", 2)
    def test_no_duplicate_alert(self):
        state = soundings.EndpointState({
            "url": "http://127.0.0.1:1",
            "label": "Dead",
            "expected_status": 200,
        })
        soundings.probe(state)
        soundings.probe(state)
        result = soundings.probe(state)
        self.assertEqual(result, "failing")
        self.assertTrue(state.alerted)

    @patch.object(soundings, "FAILURE_THRESHOLD", 1)
    @patch.object(soundings, "RECOVERY_NOTIFY", True)
    def test_recovery_notification(self):
        state = soundings.EndpointState({
            "url": "http://127.0.0.1:1",
            "label": "Test",
            "expected_status": 200,
        })
        soundings.probe(state)
        state.url = self.mock.url
        result = soundings.probe(state)
        self.assertEqual(result, "recovered")
        self.assertEqual(state.consecutive_failures, 0)

    def test_expected_non_200(self):
        self.mock.status_code = 301
        state = soundings.EndpointState({
            "url": self.mock.url,
            "label": "Redirect",
            "expected_status": 301,
        })
        result = soundings.probe(state)
        self.assertEqual(result, "ok")


class TestNotify(unittest.TestCase):
    def setUp(self):
        self.received = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                parent.received.append({
                    "title": self.headers.get("Title"),
                    "priority": self.headers.get("Priority"),
                    "body": body.decode(),
                })
                self.send_response(200)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()

    @patch.object(soundings, "NTFY_URL", None)
    def test_notify_sends_message(self):
        soundings.NTFY_URL = f"http://127.0.0.1:{self.port}"
        result = soundings.notify("Test Title", "Test Body", priority="high", tags=["warning"])
        self.assertTrue(result)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0]["title"], "Test Title")
        self.assertEqual(self.received[0]["priority"], "high")


if __name__ == "__main__":
    unittest.main()
