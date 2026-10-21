#!/usr/bin/env python3
"""Pulse server — serves the dashboard and re-runs scrape + ranking on a loop.

  python server.py                 # http://127.0.0.1:8790, re-rank every 60 min
  PULSE_INTERVAL_MIN=30 python server.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import resource
from urllib.parse import parse_qs, urlparse
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ranker
import watchlist

PORT = int(os.environ.get("PULSE_PORT", 8790))
INTERVAL_MIN = float(os.environ.get("PULSE_INTERVAL_MIN", 60))
DASH = ranker.ROOT / "dashboard.html"

state = {"running": False, "last_run": None, "last_error": None, "next_run": None, "took_s": None}
lock = threading.Lock()
wake = threading.Event()
warm_lock = threading.Lock()


def prewarm() -> None:
    """Background: warm every ranked stock's competitors so comp clicks open instantly."""
    if not warm_lock.acquire(blocking=False):
        return
    try:
        syms = [r["ticker"] for r in json.loads(ranker.OUT.read_text()).get("results", [])]
        ranker.prewarm(syms)
        ranked = set(syms)
        for s in watchlist.tickers():  # watched names that fell out of the ranking still get a fresh rating
            if s not in ranked:
                ranker.stock(s, force=True)
    except Exception:
        traceback.print_exc()
    finally:
        warm_lock.release()


def prewarm_bg() -> None:
    threading.Thread(target=prewarm, daemon=True).start()


def iso(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if ts else None


def run_once() -> None:
    if not lock.acquire(blocking=False):
        return
    state["running"] = True
    try:
        out = ranker.run()
        state.update(last_run=out["generated_at"], took_s=out["took_s"], last_error=None)
        prewarm_bg()
    except Exception as e:
        traceback.print_exc()
        state["last_error"] = f"{type(e).__name__}: {e}"
    finally:
        state["running"] = False
        lock.release()


def loop() -> None:
    # skip the immediate run if the last file is still fresh
    if ranker.OUT.exists() and time.time() - ranker.OUT.stat().st_mtime < INTERVAL_MIN * 60:
        state["last_run"] = iso(ranker.OUT.stat().st_mtime)
        nxt = ranker.OUT.stat().st_mtime + INTERVAL_MIN * 60
        prewarm_bg()  # memory is empty after a restart
    else:
        nxt = time.time()
    while True:
        state["next_run"] = iso(nxt)
        wake.wait(max(0, nxt - time.time()))
        wake.clear()
        run_once()
        nxt = time.time() + INTERVAL_MIN * 60


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, DASH.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/rankings":
            body = ranker.OUT.read_bytes() if ranker.OUT.exists() else b'{"results":[]}'
            self._send(200, body)
        elif path in ("/api/chart", "/api/compare", "/api/stock"):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            sym, rng = (q.get("t") or "").upper(), q.get("r", "1M")
            if not re.fullmatch(r"[A-Z0-9.\-]{1,10}", sym) or (path == "/api/chart" and rng not in ranker.CHART_RANGES):
                return self._send(400, b'{"error":"bad ticker or range"}')
            try:
                data = (ranker.chart(sym, rng) if path == "/api/chart" else
                        ranker.compare(sym) if path == "/api/compare" else ranker.stock(sym))
                self._send(200, json.dumps(ranker.clean(data)).encode())
            except Exception as e:
                self._send(502, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode())
        elif path == "/api/watchlist":
            self._send(200, json.dumps(ranker.clean(watchlist.view())).encode())
        elif path == "/api/status":
            self._send(200, json.dumps({**state, "interval_min": INTERVAL_MIN}).encode())
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/api/run":
            started = not state["running"]
            if started:
                wake.set()
            self._send(202, json.dumps({"started": started, "running": True}).encode())
        elif self.path.split("?", 1)[0] == "/api/watchlist":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= 4096:
                    raise ValueError("bad body")
                body = json.loads(self.rfile.read(n))
                if not isinstance(body, dict):
                    raise ValueError("bad body")
                self._send(200, json.dumps(ranker.clean(watchlist.act(body))).encode())
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)}).encode())
        else:
            self._send(404, b'{"error":"not found"}')

    def log_message(self, *a):  # keep the log to the ranker's own output
        pass


if __name__ == "__main__":
    # launchd starts agents with a 256-file soft limit; yfinance's parallel download + sqlite cache blow past it
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(8192, hard), hard))
    threading.Thread(target=loop, daemon=True).start()
    print(f"Pulse on http://127.0.0.1:{PORT} — re-rank every {INTERVAL_MIN:g} min", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
