#!/usr/bin/env python3
"""Soundings — external HTTP endpoint prober with consecutive-failure ntfy alerts.

Probes service URLs from outside Docker to catch port-binding failures,
reverse-proxy misroutes, and DNS issues that internal healthchecks miss.
"""

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread


ENDPOINTS = os.environ.get("ENDPOINTS", "")
NTFY_URL = os.environ.get("NTFY_URL", "http://127.0.0.1:8888")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "vela")
PROBE_INTERVAL = int(os.environ.get("PROBE_INTERVAL", "60"))
FAILURE_THRESHOLD = int(os.environ.get("FAILURE_THRESHOLD", "3"))
PROBE_INTERVAL_OVERRIDES_RAW = os.environ.get("PROBE_INTERVAL_OVERRIDES", "")
RECOVERY_NOTIFY = os.environ.get("RECOVERY_NOTIFY", "true").lower() == "true"
API_PORT = int(os.environ.get("API_PORT", "8591"))
PROBE_TIMEOUT = int(os.environ.get("PROBE_TIMEOUT", "10"))


def parse_endpoints(raw):
    """Parse ENDPOINTS env var into list of endpoint dicts.

    Format: url:label:expected_status,url:label:expected_status,...
    Label and expected_status are optional. Defaults: label=url, expected=200.
    """
    endpoints = []
    if not raw or not raw.strip():
        return endpoints
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) >= 3 and parts[0] in ("http", "https"):
            url = f"{parts[0]}:{parts[1]}"
            remaining = parts[2:]
        else:
            continue

        label = url
        expected = 200

        port_and_rest = remaining[0] if remaining else ""
        if port_and_rest and port_and_rest.split("/")[0].isdigit():
            url = f"{url}:{port_and_rest}"
            remaining = remaining[1:]

        if remaining:
            first = remaining[0]
            if first.isdigit():
                expected = int(first)
                if len(remaining) > 1:
                    label = remaining[1]
            else:
                label = first
                if len(remaining) > 1 and remaining[1].isdigit():
                    expected = int(remaining[1])

        endpoints.append({
            "url": url,
            "label": label,
            "expected_status": expected,
        })
    return endpoints


def parse_interval_overrides(raw):
    """Parse PROBE_INTERVAL_OVERRIDES into label->seconds dict.

    Format: Label:seconds,Label:seconds,...
    """
    overrides = {}
    if not raw or not raw.strip():
        return overrides
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        label, _, seconds = entry.rpartition(":")
        if seconds.isdigit():
            overrides[label.strip()] = int(seconds)
    return overrides


class EndpointState:
    def __init__(self, endpoint, interval=None):
        self.url = endpoint["url"]
        self.label = endpoint["label"]
        self.expected_status = endpoint["expected_status"]
        self.interval = interval or PROBE_INTERVAL
        self.last_probed = 0
        self.consecutive_failures = 0
        self.alerted = False
        self.last_status = None
        self.last_response_time_ms = None
        self.last_check = None
        self.last_error = None
        self.total_checks = 0
        self.total_failures = 0

    def to_dict(self):
        return {
            "url": self.url,
            "label": self.label,
            "expected_status": self.expected_status,
            "interval": self.interval,
            "status": "ok" if self.consecutive_failures == 0 else "failing",
            "consecutive_failures": self.consecutive_failures,
            "last_http_status": self.last_status,
            "last_response_time_ms": self.last_response_time_ms,
            "last_check": self.last_check,
            "last_error": self.last_error,
            "total_checks": self.total_checks,
            "total_failures": self.total_failures,
        }


def probe(state):
    state.total_checks += 1
    state.last_check = datetime.now(timezone.utc).isoformat()

    try:
        req = urllib.request.Request(state.url, method="GET")
        req.add_header("User-Agent", "Soundings/1.0")
        start = time.monotonic()
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            elapsed = (time.monotonic() - start) * 1000
            state.last_status = resp.status
            state.last_response_time_ms = round(elapsed, 1)
            state.last_error = None

            if resp.status == state.expected_status:
                was_failing = state.consecutive_failures >= FAILURE_THRESHOLD
                state.consecutive_failures = 0
                state.alerted = False
                if was_failing and RECOVERY_NOTIFY:
                    return "recovered"
                return "ok"
            else:
                state.total_failures += 1
                state.consecutive_failures += 1
                state.last_error = f"Expected {state.expected_status}, got {resp.status}"
                if state.consecutive_failures >= FAILURE_THRESHOLD and not state.alerted:
                    state.alerted = True
                    return "alert"
                return "failing"

    except urllib.error.HTTPError as e:
        elapsed = (time.monotonic() - start) * 1000
        state.last_status = e.code
        state.last_response_time_ms = round(elapsed, 1)

        if e.code == state.expected_status:
            was_failing = state.consecutive_failures >= FAILURE_THRESHOLD
            state.consecutive_failures = 0
            state.alerted = False
            state.last_error = None
            if was_failing and RECOVERY_NOTIFY:
                return "recovered"
            return "ok"

        state.last_error = f"HTTP {e.code}: {e.reason}"
        state.total_failures += 1
        state.consecutive_failures += 1
        if state.consecutive_failures >= FAILURE_THRESHOLD and not state.alerted:
            state.alerted = True
            return "alert"
        return "failing"

    except Exception as e:
        state.last_status = None
        state.last_response_time_ms = None
        state.last_error = str(e)[:200]
        state.total_failures += 1
        state.consecutive_failures += 1
        if state.consecutive_failures >= FAILURE_THRESHOLD and not state.alerted:
            state.alerted = True
            return "alert"
        return "failing"


def notify(title, message, priority="default", tags=None):
    url = f"{NTFY_URL}/{NTFY_TOPIC}"
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    req = urllib.request.Request(url, data=message.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[ntfy error] {e}", file=sys.stderr)
        return False


def make_api_handler(states):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/status":
                data = {
                    "endpoints": [s.to_dict() for s in states],
                    "summary": {
                        "total": len(states),
                        "ok": sum(1 for s in states if s.consecutive_failures == 0),
                        "failing": sum(1 for s in states if s.consecutive_failures > 0),
                    },
                    "config": {
                        "probe_interval": PROBE_INTERVAL,
                        "failure_threshold": FAILURE_THRESHOLD,
                    },
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data, indent=2).encode())
            elif self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    return Handler


def main():
    endpoints = parse_endpoints(ENDPOINTS)
    if not endpoints:
        print("No endpoints configured. Set ENDPOINTS env var.", file=sys.stderr)
        print("Format: url:label,url:label,...", file=sys.stderr)
        sys.exit(1)

    overrides = parse_interval_overrides(PROBE_INTERVAL_OVERRIDES_RAW)
    states = [EndpointState(ep, overrides.get(ep["label"])) for ep in endpoints]

    print(f"Soundings starting — probing {len(states)} endpoints every {PROBE_INTERVAL}s")
    print(f"  Alert after {FAILURE_THRESHOLD} consecutive failures")
    print(f"  ntfy: {NTFY_URL}/{NTFY_TOPIC}")
    for s in states:
        interval_note = f" (every {s.interval}s)" if s.interval != PROBE_INTERVAL else ""
        print(f"  - {s.label}: {s.url} (expect {s.expected_status}){interval_note}")

    server = HTTPServer(("0.0.0.0", API_PORT), make_api_handler(states))
    api_thread = Thread(target=server.serve_forever, daemon=True)
    api_thread.start()
    print(f"  API: http://0.0.0.0:{API_PORT}/api/status")

    running = True

    def handle_signal(signum, frame):
        nonlocal running
        print(f"\nReceived signal {signum}, shutting down")
        running = False
        server.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while running:
        now = time.monotonic()
        for state in states:
            if not running:
                break
            if now - state.last_probed < state.interval:
                continue
            state.last_probed = now
            result = probe(state)

            if result == "alert":
                title = f"Soundings: {state.label} DOWN"
                body = (
                    f"{state.url} failed {state.consecutive_failures} consecutive probes.\n"
                    f"Error: {state.last_error}"
                )
                print(f"[ALERT] {state.label}: {state.last_error}")
                notify(title, body, priority="high", tags=["red_circle", "warning"])

            elif result == "recovered":
                title = f"Soundings: {state.label} recovered"
                body = f"{state.url} is responding again ({state.last_response_time_ms}ms)"
                print(f"[RECOVERED] {state.label}")
                notify(title, body, priority="default", tags=["green_circle"])

            elif result == "failing":
                print(
                    f"[FAILING] {state.label}: {state.last_error} "
                    f"({state.consecutive_failures}/{FAILURE_THRESHOLD})"
                )

        time.sleep(PROBE_INTERVAL)


if __name__ == "__main__":
    main()
