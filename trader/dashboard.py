"""Authenticated operator UI; command queue isolates HTTP handlers from exchange credentials."""

import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from trader.reporting import report
from trader.storage.db import Store


def serve(config):
    token = os.getenv("DASHBOARD_TOKEN", "")
    if len(token) < 32:
        raise ValueError("DASHBOARD_TOKEN must contain at least 32 random characters")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log HTTP authorization headers.

        def respond(self, status, data, kind="application/json"):
            payload = data.encode() if isinstance(data, str) else json.dumps(data, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(payload)

        def authenticated(self):
            value = self.headers.get("Authorization", "")
            return hmac.compare_digest(value, "Bearer " + token)

        def do_GET(self):
            if self.path == "/":
                return self.respond(
                    200, Path(__file__).with_name("dashboard.html").read_text(), "text/html; charset=utf-8"
                )
            if self.path == "/dashboard.js":
                return self.respond(
                    200, Path(__file__).with_name("dashboard.js").read_text(), "text/javascript"
                )
            if not self.authenticated():
                return self.respond(401, {"error": "Authentication required"})
            store = Store(config.database.path)
            try:
                if self.path == "/api/status":
                    metrics = report(store)
                    result = {
                        "snapshot": store.get("snapshot", {}),
                        "metrics": metrics,
                        "mode": store.get("mode"),
                        "paused": store.get("paused", False),
                        "kill_switch": store.get("killed"),
                        "websocket": store.get("websocket", "offline"),
                        "exchange_status": store.get(
                            "exchange_status", "paper" if store.get("mode") == "paper" else "unknown"
                        ),
                        "heartbeat_age_seconds": (time.time_ns() - store.get("runtime_heartbeat_ns", 0))
                        / 1e9,
                        "last_trade": store.fills()[-1].model_dump(mode="json") if store.fills() else None,
                        "commands": [
                            dict(row)
                            for row in store.db.execute("SELECT * FROM commands ORDER BY id DESC LIMIT 10")
                        ],
                    }
                    self.respond(200, result)
                elif self.path == "/metrics":
                    snap = store.get("snapshot", {})
                    values = {
                        "btcscalp_equity_usd": snap.get("equity", 0),
                        "btcscalp_position_btc": snap.get("position_btc", 0),
                        "btcscalp_killed": int(bool(store.get("killed"))),
                        "btcscalp_heartbeat_age_seconds": (
                            time.time_ns() - store.get("runtime_heartbeat_ns", 0)
                        )
                        / 1e9,
                    }
                    self.respond(
                        200, "\n".join(f"{key} {value}" for key, value in values.items()) + "\n", "text/plain"
                    )
                else:
                    self.respond(404, {"error": "Not found"})
            finally:
                store.close()

        def do_POST(self):
            if not self.authenticated():
                return self.respond(401, {"error": "Authentication required"})
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self.respond(403, {"error": "Origin rejected"})
            if self.path != "/api/command" or self.headers.get("Content-Type") != "application/json":
                return self.respond(400, {"error": "Invalid request"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError("Invalid body size")
                body = json.loads(self.rfile.read(length))
                action = body["action"]
                if (
                    action in {"resume", "flatten", "kill", "reset-kill"}
                    and body.get("confirmation") != action.upper()
                ):
                    raise ValueError("Explicit confirmation required")
                store = Store(config.database.path)
                try:
                    command_id = store.command(action)
                finally:
                    store.close()
                self.respond(
                    202, {"command_id": command_id, "status": "queued; check runtime acknowledgement"}
                )
            except (ValueError, KeyError):
                self.respond(400, {"error": "Invalid command or confirmation"})

    server = ThreadingHTTPServer((config.monitoring.host, config.monitoring.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        server.server_close()
