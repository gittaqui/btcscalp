"""Exercise the real HTTP surface with a temporary fake operator token."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import yaml

from trader.storage.db import Store


def test_dashboard_auth_confirmation_origin_and_command_queue(tmp_path, cfg):
    database = tmp_path / "paper.sqlite"
    store = Store(str(database))
    store.set("mode", "paper")
    store.close()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    cfg.database.path = str(database)
    cfg.monitoring.port = port
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg.model_dump(mode="json")))
    token = "unit-test-only-token-" + "a" * 40
    child = subprocess.Popen(
        [sys.executable, "-m", "trader", "dashboard", "--config", str(path)],
        env={**os.environ, "DASHBOARD_TOKEN": token},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    # Explicitly local test traffic; do not route loopback through a network proxy.
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, body=None, auth=True, origin=None):
        headers = {"Authorization": "Bearer " + token} if auth else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers
        )
        try:
            response = client.open(req, timeout=2)
            return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                status, body = request("/", auth=False)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("Dashboard did not start")
                time.sleep(0.05)
        assert status == 200 and b"BTCScalp / Operations" in body
        assert request("/api/status", auth=False)[0] == 401
        assert request("/metrics", auth=False)[0] == 401
        assert request("/api/status")[0] == 200
        assert request("/api/command", {"action": "kill"})[0] == 400
        assert (
            request(
                "/api/command", {"action": "kill", "confirmation": "KILL"}, origin="https://evil.example"
            )[0]
            == 403
        )
        assert request("/api/command", {"action": "kill", "confirmation": "KILL"})[0] == 202
        store = Store(str(database))
        assert store.get("killed")["reason"] == "OPERATOR_KILL"
        assert store.pending_commands()[0]["action"] == "kill"
        store.close()
    finally:
        child.terminate()
        child.wait(timeout=5)
