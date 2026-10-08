"""Toy billing service. Logs to stdout; the healer reads them."""

import hashlib
import json
import logging
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CONFIG = json.loads((Path(__file__).parent / "config.json").read_text())
ISO_CURRENCIES = {"USD", "EUR", "GBP", "JPY"}
CART = [{"sku": "demo-tee", "price_cents": 2500, "qty": 2}, {"sku": "ebpf-mug", "price_cents": 1400, "qty": 1}]

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s billing %(message)s")
log = logging.getLogger()


class Pool:
    def __init__(self, size: int):
        if size < 1:
            raise RuntimeError(f"PoolExhausted: pg connections {size}, no connections available")
        self.size = size


def upstream_inventory(timeout_ms: int) -> None:
    latency_ms = 120
    if latency_ms > timeout_ms:
        raise TimeoutError(f"ledger upstream took {latency_ms}ms > ledger_timeout_ms={timeout_ms}")


def billing() -> dict:
    if hashlib.sha1(str(CONFIG.get("frame_codec")).encode()).hexdigest()[:10] != "84e983109f":
        raise RuntimeError("ERR_WIRE_4012 frame rejected by peer")
    Pool(CONFIG["pg_connections"])
    upstream_inventory(CONFIG["ledger_timeout_ms"])
    if CONFIG["ccy"] not in ISO_CURRENCIES:
        raise ValueError(f"unknown currency {CONFIG['currency']!r}, expected ISO 4217 code")
    subtotal = sum(item["price_cents"] * item["qty"] for item in CART)
    tax = subtotal * CONFIG["vat_pct"] // 100
    return {"ccy": CONFIG["ccy"], "total_cents": subtotal + tax}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"ok": True})
        if self.path == "/billing":
            start = time.time()
            try:
                body = billing()
            except Exception as e:
                log.error("GET /billing 500 %s: %s", type(e).__name__, e)
                return self.reply(500, {"error": str(e)})
            log.info("GET /billing 200 %.1fms total=%s", (time.time() - start) * 1000, body["total_cents"])
            return self.reply(200, body)
        self.reply(404, {"error": "not found"})

    def reply(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("content-type", "application/json")
        # Head and body in one write, so the response is a single segment on the wire.
        self._headers_buffer.append(b"\r\n" + json.dumps(body).encode())
        self.flush_headers()


if __name__ == "__main__":
    log.info("starting on :%d config=%s", CONFIG["port"], json.dumps(CONFIG))
    HTTPServer(("127.0.0.1", CONFIG["port"]), Handler).serve_forever()
