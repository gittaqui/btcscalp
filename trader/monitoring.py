"""Structured logs and non-blocking, bounded Discord alerts."""

import asyncio
import json
import logging
import os
import time
from urllib.parse import urlparse

import httpx


class JsonFormatter(logging.Formatter):
    def format(self, record):
        message = record.getMessage()
        # Redact configured secrets even if an exception or downstream library echoes one.
        for name, value in os.environ.items():
            if value and any(
                marker in name for marker in ("SECRET", "TOKEN", "API_KEY", "GUARD_KEY", "WEBHOOK")
            ):
                message = message.replace(value, "[REDACTED]")
        return json.dumps(
            {"ts_ns": time.time_ns(), "level": record.levelname, "logger": record.name, "message": message}
        )


def configure_logging(level):
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class Alerts:
    def __init__(self, enabled, store):
        self.enabled, self.store = enabled, store
        self.queue = asyncio.Queue(maxsize=100)
        self.last = {}

    def send(self, event, detail=""):
        now = time.monotonic()
        if now - self.last.get(event, -1000) < 30 and event != "TRADE":
            return
        self.last[event] = now
        logging.getLogger("btcscalp").warning("%s %s", event, detail)
        self.store.event("alert", {"event": event, "detail": detail})
        if self.enabled:
            try:
                self.queue.put_nowait(f"BTCScalp | {event} | {detail}")
            except asyncio.QueueFull:
                self.store.event("health", {"reason": "ALERT_QUEUE_FULL"})

    async def run(self):
        url = os.getenv("DISCORD_WEBHOOK_URL", "")
        parsed = urlparse(url)
        if self.enabled and (
            parsed.scheme != "https"
            or parsed.hostname not in {"discord.com", "discordapp.com"}
            or not parsed.path.startswith("/api/webhooks/")
        ):
            raise ValueError("Invalid Discord webhook configuration")
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            while True:
                message = await self.queue.get()
                try:
                    response = await client.post(url, json={"content": message})
                    response.raise_for_status()
                except Exception:
                    self.store.event("health", {"reason": "ALERT_DELIVERY_FAILED"})
                finally:
                    self.queue.task_done()
