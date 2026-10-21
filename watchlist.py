"""Watchlists: folders of tickers, each remembering the day, price and Pulse rating it was added at,
so the dashboard can show what it has done since — the one stat only a watchlist can have."""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import re
import threading

import ranker

FILE = ranker.ROOT / "watchlist.json"
DEFAULT_FOLDER = "Watching"
TICKER = re.compile(r"[A-Z0-9.\-]{1,10}")
QUOTE_TTL_S = 60

_lock = threading.Lock()
_warming: set[str] = set()
_ranked = {"mtime": None, "rows": {}}


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text())
    except (OSError, ValueError):
        d = {}
    d.setdefault("folders", [DEFAULT_FOLDER])
    d.setdefault("items", [])
    if not d["folders"]:
        d["folders"] = [DEFAULT_FOLDER]
    return d


def _save(d: dict) -> None:
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(FILE)


def folder_name(v) -> str:
    v = re.sub(r"[\x00-\x1f]", "", str(v or "")).strip()[:40]
    if not v:
        raise ValueError("folder name required")
    return v


def ticker(v) -> str:
    v = str(v or "").upper().strip()
    if not TICKER.fullmatch(v):
        raise ValueError("bad ticker")
    return v


def ranked() -> dict:
    """Latest ranking rows by ticker, re-read only when the file changes."""
    try:
        m = ranker.OUT.stat().st_mtime
    except OSError:
        return {}
    if m != _ranked["mtime"]:
        try:
            rows = json.loads(ranker.OUT.read_text()).get("results", [])
            _ranked.update(mtime=m, rows={r["ticker"]: r for r in rows})
        except ValueError:
            pass
    return _ranked["rows"]


def quote(sym: str) -> dict:
    def go():
        try:
            fi = ranker.yf.Ticker(sym).fast_info
            last, prev = ranker.num(fi.get("lastPrice")), ranker.num(fi.get("previousClose"))
            return {"price": last, "prev_close": prev} if last else {"error": "no quote"}
        except Exception as e:
            return {"error": f"{type(e).__name__}"}

    return ranker._memo(("quote", sym), QUOTE_TTL_S, go)


def _warm(sym: str) -> None:
    """Score an off-list ticker in the background; the next refresh picks it up from memory."""
    if sym in _warming:
        return
    _warming.add(sym)

    def go():
        try:
            ranker.stock(sym)
        finally:
            _warming.discard(sym)

    threading.Thread(target=go, daemon=True).start()


def rating(sym: str, warm: bool = True) -> dict | None:
    """Current Pulse rating: from the latest run, else a cached on-demand score."""
    r = ranked().get(sym)
    if r:
        return {"score": r["score"], "tier": r["tier"], "name": r["name"], "sector": r.get("sector"), "ranked": True}
    hit = ranker._mem.get(("stock", sym))
    if hit and not hit[1].get("error"):
        x = hit[1]
        return {"score": x["score"], "tier": x["tier"], "name": x["name"], "sector": x.get("sector"), "ranked": False}
    if warm:
        _warm(sym)
    return None


def view() -> dict:
    with _lock:
        d = _load()
    items = d["items"]
    with cf.ThreadPoolExecutor(8) as ex:
        quotes = dict(zip([i["ticker"] for i in items], ex.map(lambda i: quote(i["ticker"]), items)))
    today, dirty, out = dt.date.today(), False, []
    for it in items:
        q, rt = quotes.get(it["ticker"]) or {}, rating(it["ticker"])
        if it.get("added_score") is None and rt:  # rating wasn't known yet when it was added: backfill once
            it.update(added_score=rt["score"], added_tier=rt["tier"])
            dirty = True
        if not it.get("name") and rt:
            it["name"] = rt["name"]
            dirty = True
        px, prev, base = q.get("price"), q.get("prev_close"), it.get("added_price")
        added = dt.date.fromisoformat(it["added_at"][:10])
        out.append(it | {
            "price": px,
            "day_pct": px / prev - 1 if px and prev else None,
            "since_pct": px / base - 1 if px and base else None,
            "days": (today - added).days,
            "score": rt and rt["score"], "tier": rt and rt["tier"], "sector": rt and rt["sector"],
            "ranked": bool(rt and rt["ranked"]),
            "score_delta": rt["score"] - it["added_score"] if rt and it.get("added_score") is not None else None,
        })
    if dirty:
        with _lock:
            cur = _load()
            by = {i["ticker"]: i for i in items}
            for i in cur["items"]:
                if i["ticker"] in by:
                    for k in ("added_score", "added_tier", "name"):
                        if i.get(k) is None:
                            i[k] = by[i["ticker"]].get(k)
            _save(cur)
    return {"folders": d["folders"], "items": out}


def act(body: dict) -> dict:
    op = body.get("op")
    with _lock:
        d = _load()
        items = d["items"]
        if op == "add":
            sym = ticker(body.get("ticker"))
            folder = folder_name(body.get("folder") or DEFAULT_FOLDER)
            if folder not in d["folders"]:
                d["folders"].append(folder)
            cur = next((i for i in items if i["ticker"] == sym), None)
            if cur:  # already watched: adding again just files it elsewhere, the since-added clock keeps running
                cur["folder"] = folder
            else:
                rt, q = rating(sym), quote(sym)
                items.append({"ticker": sym, "folder": folder, "name": rt and rt["name"],
                              "added_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                              "added_price": q.get("price"),
                              "added_score": rt and rt["score"], "added_tier": rt and rt["tier"]})
        elif op == "remove":
            sym = ticker(body.get("ticker"))
            d["items"] = [i for i in items if i["ticker"] != sym]
        elif op == "move":
            sym, folder = ticker(body.get("ticker")), folder_name(body.get("folder"))
            if folder not in d["folders"]:
                d["folders"].append(folder)
            for i in items:
                if i["ticker"] == sym:
                    i["folder"] = folder
        elif op == "folder_add":
            name = folder_name(body.get("name"))
            if name not in d["folders"]:
                d["folders"].append(name)
        elif op == "folder_rename":
            name, to = folder_name(body.get("name")), folder_name(body.get("to"))
            if name not in d["folders"]:
                raise ValueError("no such folder")
            if to in d["folders"] and to != name:
                raise ValueError("folder already exists")
            d["folders"] = [to if f == name else f for f in d["folders"]]
            for i in items:
                if i["folder"] == name:
                    i["folder"] = to
        elif op == "folder_remove":  # its stocks fall back into the first remaining folder
            name = folder_name(body.get("name"))
            d["folders"] = [f for f in d["folders"] if f != name] or [DEFAULT_FOLDER]
            for i in d["items"]:
                if i["folder"] == name:
                    i["folder"] = d["folders"][0]
        else:
            raise ValueError("unknown op")
        _save(d)
    return view()


def tickers() -> list[str]:
    with _lock:
        return [i["ticker"] for i in _load()["items"]]
