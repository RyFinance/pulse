#!/usr/bin/env python3
"""Pulse — hunts small and niche stocks that are quietly dominating, before everyone knows the name.

Pipeline (one call to run()):
  1. discover  two funnels
               hunt: Yahoo screen of every US-listed $50M–$500B stock growing revenue 25%+ (fundamentals first)
               heat: names hot in news (Massive, Yahoo), on Reddit/X (scraper.py), small-cap gainers/actives
  2. gate      info for every candidate, then hard filters against pumps, dilution machines,
               pre-revenue story stocks, OTC/illiquid names and reverse splits (rejects are kept, with reasons)
  3. research  quarterly revenue/margins/share count, estimates, revisions, surprises, SEC filings (optional)
  4. score     6 pillars on fixed 0–100 scales: growth, quality, estimates, valuation, momentum, early
  5. read      news + social sentiment, market backdrop, sector heat, a written summary → rankings.json

News and social volume decide what gets looked at and feed the pump checks. They never add to the score.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf
from yfinance import EquityQuery as EQ

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT  # scraper.py, raw_data.json and valid_tickers.txt live alongside
OUT = ROOT / "rankings.json"
HIST = ROOT / "history"
CACHE = ROOT / "cache"

env = os.environ.get
MIN_MCAP = float(env("PULSE_MIN_MCAP", 5e7))
MAX_MCAP = float(env("PULSE_MAX_MCAP", 5e11))
MIN_PRICE = float(env("PULSE_MIN_PRICE", 1))
MIN_DOLLAR_VOL = float(env("PULSE_MIN_DOLLAR_VOL", 1e6))
MIN_REVENUE = float(env("PULSE_MIN_REVENUE", 1e7))
HUNT_PAGES = int(env("PULSE_HUNT_PAGES", 5))       # 250 screen rows per page, smallest caps first
HUNT_DEEP = int(env("PULSE_HUNT_DEEP", 70))        # screen names that get full research
HUNT_BIG = int(env("PULSE_HUNT_BIG", 20))          # of those, slots kept for proven names over $10B
HEAT_DEEP = int(env("PULSE_HEAT_DEEP", 50))        # hot names that get full research
NEWS_HOURS = int(env("PULSE_NEWS_HOURS", 48))
SOCIAL_MAX_AGE_MIN = float(env("PULSE_SOCIAL_MAX_AGE_MIN", 50))
FUND_TTL_H = float(env("PULSE_FUND_TTL_H", 12))
WORKERS = int(env("PULSE_WORKERS", 6))
KEEP_HISTORY = 300
UA = {"User-Agent": "Mozilla/5.0"}
MAJOR_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS", "NAS"}
SCREEN_EXCHANGES = ["NMS", "NGM", "NCM", "NYQ", "ASE"]
PR_WIRES = ("globenewswire", "globe newswire", "pr newswire", "prnewswire", "business wire", "businesswire",
            "accesswire", "access newswire", "newsfile", "newsdirect", "issuer direct", "einpresswire", "pressat")

WEIGHTS = {"growth": 0.25, "quality": 0.18, "estimates": 0.12, "valuation": 0.08, "momentum": 0.17, "early": 0.20}
BUY_AT, AVOID_BELOW, FLAG_COST = 70, 42, 7
BIG_CAP, BIG_BUY_AT = 5e10, 75   # names everyone already knows have to earn a BUY
# diamond in the rough: small, barely covered, growing fast, financially sound, nothing wrong with it
DIAMOND_MAX_MCAP, DIAMOND_MAX_ANALYSTS, DIAMOND_MIN_SCORE = 3e9, 6, 60
EARNINGS_BLACKOUT_D = 2

INFO_KEYS = [
    "shortName", "longName", "quoteType", "exchange", "sector", "industry", "marketCap", "currentPrice",
    "regularMarketPrice", "totalRevenue", "revenueGrowth", "earningsGrowth", "grossMargins", "operatingMargins",
    "profitMargins", "returnOnEquity", "freeCashflow", "operatingCashflow", "totalCash", "totalDebt",
    "debtToEquity", "currentRatio", "forwardPE", "trailingPegRatio", "enterpriseToRevenue",
    "priceToSalesTrailing12Months", "targetMeanPrice", "recommendationMean", "recommendationKey",
    "numberOfAnalystOpinions", "shortPercentOfFloat", "heldPercentInstitutions", "heldPercentInsiders",
    "floatShares", "sharesOutstanding", "averageVolume", "fiftyTwoWeekHigh", "longBusinessSummary", "website",
]


# ---------------------------------------------------------------- helpers
def load_env() -> None:
    for p in (ROOT / ".env",):
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def clean(o):
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [clean(v) for v in o]
    if isinstance(o, (dt.date, dt.datetime, pd.Timestamp)):
        return o.isoformat()
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if hasattr(o, "item"):
        return clean(o.item())
    return o


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def to_yahoo(sym: str) -> str | None:
    sym = (sym or "").upper().strip()
    if not sym or len(sym) > 6 or any(c in sym for c in ":^="):
        return None
    return sym.replace(".", "-")


def cached(name: str, ttl_h: float, fn):
    CACHE.mkdir(exist_ok=True)
    p = CACHE / name
    if p.exists() and time.time() - p.stat().st_mtime < ttl_h * 3600:
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    val = fn()
    if val is not None:
        p.write_text(json.dumps(clean(val)))
    return val


_vader = None


def tone(text: str) -> float:
    global _vader
    if _vader is None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        _vader = SentimentIntensityAnalyzer()
    return _vader.polarity_scores(text or "")["compound"]


def is_wire(publisher: str | None) -> bool:
    p = (publisher or "").lower()
    return any(w in p for w in PR_WIRES)


# ---------------------------------------------------------------- 1. discover
def massive_news(heads) -> Counter:
    key, heat = env("MASSIVE_API_KEY"), Counter()
    if not key:
        return heat
    since = (now_utc() - dt.timedelta(hours=NEWS_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = "https://api.massive.com/v2/reference/news"
    params = {"published_utc.gte": since, "limit": 1000, "order": "desc", "sort": "published_utc", "apiKey": key}
    for _ in range(2):
        try:
            d = requests.get(url, params=params, timeout=25).json()
        except Exception as e:
            print(f"[discover] massive news failed: {e}")
            break
        for a in d.get("results", []):
            tks = a.get("tickers") or []
            if not tks or len(tks) > 6:  # roundups name everything
                continue
            insight = {i.get("ticker"): i.get("sentiment") for i in a.get("insights") or []}
            for t in tks:
                y = to_yahoo(t)
                if not y:
                    continue
                heat[y] += 1
                add_head(heads, y, a.get("title"), a.get("article_url"), (a.get("publisher") or {}).get("name"),
                         a.get("published_utc"), insight.get(t))
        if not d.get("next_url"):
            break
        url, params = d["next_url"], {"apiKey": key}
    return heat


def add_head(heads, sym, title, url, publisher, published, label=None):
    if not title or len(heads[sym]) >= 8 or title in {h["title"] for h in heads[sym]}:
        return
    heads[sym].append({"title": title, "url": url, "publisher": publisher, "published": published,
                       "label": label, "tone": round(tone(title), 3), "wire": is_wire(publisher)})


def _yahoo_items(query: str, n: int = 20) -> list[dict]:
    try:
        return yf.Search(query, news_count=n, max_results=0).news or []
    except Exception:
        return []


def _yahoo_add(heads, sym, a):
    add_head(heads, sym, a.get("title"), a.get("link"), a.get("publisher"),
             dt.datetime.fromtimestamp(a["providerPublishTime"], dt.timezone.utc).isoformat())


YAHOO_QUERIES = ["small cap stocks", "stocks to watch", "earnings beat", "raises guidance", "analyst upgrade",
                 "record revenue", "contract award", "stock surges", "acquisition", "initiates coverage"]


def yahoo_topic_news(heads) -> Counter:
    heat, cutoff = Counter(), time.time() - NEWS_HOURS * 3600
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for items in ex.map(_yahoo_items, YAHOO_QUERIES):
            for a in items:
                rel = [t for t in a.get("relatedTickers") or [] if to_yahoo(t) and "." not in t]
                if (a.get("providerPublishTime") or 0) < cutoff or not rel or len(rel) > 6:
                    continue
                for t in rel:
                    heat[t] += 1
                    _yahoo_add(heads, t, a)
    return heat


def yahoo_ticker_news(syms, heads) -> Counter:
    heat, cutoff = Counter(), time.time() - NEWS_HOURS * 3600
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for sym, items in zip(syms, ex.map(_yahoo_items, syms)):
            for a in items:
                rel = a.get("relatedTickers") or []
                if (a.get("providerPublishTime") or 0) >= cutoff and sym in rel and len(rel) <= 6:
                    heat[sym] += 1
                    _yahoo_add(heads, sym, a)
    return heat


def finnhub_news(syms, heads, heat) -> None:
    """Top up tickers still short of headlines (free tier 60/min → ~1/s)."""
    key = env("FINNHUB_API_KEY")
    if not key:
        return
    today, cutoff = dt.date.today(), time.time() - NEWS_HOURS * 3600
    for sym in syms:
        if len(heads.get(sym, [])) >= 3:
            continue
        try:
            r = requests.get("https://finnhub.io/api/v1/company-news", timeout=10, params={
                "symbol": sym, "from": str(today - dt.timedelta(days=NEWS_HOURS // 24 + 1)), "to": str(today),
                "token": key})
            if r.status_code == 429:
                time.sleep(5)
                continue
            items = [n for n in r.json() if (n.get("datetime") or 0) >= cutoff]
        except Exception:
            continue
        heat[sym] = max(heat.get(sym, 0), len(items))
        for n in items:
            add_head(heads, sym, n.get("headline"), n.get("url"), n.get("source"),
                     dt.datetime.fromtimestamp(n["datetime"], dt.timezone.utc).isoformat())
        time.sleep(1.05)


def refresh_social() -> None:
    """Re-run the Reddit/X scraper if its output is stale (X allows ~2 full runs / 15 min)."""
    raw = PROJECT / "raw_data.json"
    if env("PULSE_SCRAPE", "1") != "1":
        return
    if raw.exists() and time.time() - raw.stat().st_mtime < SOCIAL_MAX_AGE_MIN * 60:
        return
    print("[social] refreshing Reddit + X scrape", flush=True)
    try:
        subprocess.run([sys.executable, "scraper.py"], cwd=PROJECT, timeout=240,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[social] scraper failed: {e}")


def social_mentions() -> tuple[dict, dict]:
    """Cashtag mentions in the scraper's last pull, validated against listed tickers.

    Returns (per-ticker stats, overall stats)."""
    raw, valid = PROJECT / "raw_data.json", PROJECT / "valid_tickers.txt"
    if not raw.exists():
        return {}, {}
    try:
        d = json.loads(raw.read_text())
    except Exception:
        return {}, {}
    ok = set(valid.read_text().split()) if valid.exists() else None
    cutoff = time.time() - NEWS_HOURS * 3600
    per = defaultdict(lambda: {"mentions": 0, "reddit": 0, "x": 0, "tones": [], "likes": 0, "posts": []})
    tones, n_posts = [], 0
    for p in d.get("posts", []):
        if (p.get("created_utc") or 0) < cutoff:
            continue
        text = f"{p.get('title') or ''} {(p.get('selftext') or '')[:600]}"
        n_posts += 1
        t = tone(text)
        tones.append(t)
        tags = {m for m in re.findall(r"\$([A-Z]{1,5})\b", text) if ok is None or m in ok}
        if len(tags) > 6:  # cashtag spam / watchlist dumps
            continue
        for m in tags:
            s = per[m]
            s["mentions"] += 1
            s["reddit" if p.get("source") == "reddit" else "x"] += 1
            s["tones"].append(t)
            s["likes"] += int(p.get("score") or 0)
            if len(s["posts"]) < 5:
                s["posts"].append({"text": (p.get("title") or "")[:200], "url": p.get("permalink"),
                                   "source": p.get("source"), "sub": p.get("subreddit"), "tone": round(t, 3)})
    out = {}
    for k, s in per.items():
        out[k] = {**{x: s[x] for x in ("mentions", "reddit", "x", "likes", "posts")},
                  "tone": round(sum(s["tones"]) / len(s["tones"]), 3) if s["tones"] else None}
    overall = {"posts": n_posts, "tone": round(sum(tones) / len(tones), 3) if tones else None,
               "scraped_at": d.get("scraped_at"), "sources": d.get("sources")}
    return out, overall


def hunt_screen() -> tuple[list[dict], int]:
    """Every US-listed $50M–$500B stock growing quarterly revenue 25%+ with real liquidity."""
    q = EQ("and", [
        EQ("btwn", ["intradaymarketcap", MIN_MCAP, MAX_MCAP]),
        EQ("is-in", ["exchange", *SCREEN_EXCHANGES]),
        EQ("gt", ["avgdailyvol3m", 100000]),
        EQ("gt", ["intradayprice", MIN_PRICE]),
        EQ("gt", ["quarterlyrevenuegrowth.quarterly", 25]),
    ])
    rows, total = [], 0
    for page in range(HUNT_PAGES):
        try:
            r = yf.screen(q, size=250, offset=page * 250, sortField="intradaymarketcap", sortAsc=True)
        except Exception as e:
            print(f"[hunt] screen page {page} failed: {e}")
            break
        total = r.get("total") or total
        rows += r.get("quotes", [])
        if len(rows) >= total:
            break
    return rows, total


def prerank(q: dict) -> float:
    """Cheap score from screen fields: uptrend, not parabolic, liquid, the smaller the better."""
    px, d50, d200 = num(q.get("regularMarketPrice")), num(q.get("fiftyDayAverage")), num(q.get("twoHundredDayAverage"))
    chg52 = num(q.get("fiftyTwoWeekChangePercent"))
    mcap = num(q.get("marketCap")) or 0
    dv = (px or 0) * (num(q.get("averageDailyVolume3Month")) or 0)
    if not px or not d200 or dv < MIN_DOLLAR_VOL or mcap <= 0:
        return -1
    s = 0.0
    s += 25 if px > d200 else 0
    s += 15 if d50 and d50 > d200 else 0
    ext = px / d200
    s += 20 * max(0.0, min(1.0, (2.2 - ext) / 1.1))            # 1.1x → full, 2.2x+ → none
    if chg52 is not None:
        s += 15 if -10 <= chg52 <= 250 else 0                    # in a trend, not already a moonshot
    s += 15 * max(0.0, min(1.0, (math.log10(MAX_MCAP) - math.log10(mcap)) / 2.3))  # smaller = more room
    eps_t, eps_f = num(q.get("epsTrailingTwelveMonths")), num(q.get("epsForward"))
    s += 10 if eps_t is not None and eps_f is not None and eps_f > eps_t else 0  # earnings improving
    return s


def yahoo_lists() -> dict[str, set]:
    out = {"trending": set()}
    try:
        r = requests.get("https://query1.finance.yahoo.com/v1/finance/trending/US?count=30", headers=UA, timeout=10)
        out["trending"] = {q["symbol"] for q in r.json()["finance"]["result"][0]["quotes"]}
    except Exception as e:
        print(f"[discover] yahoo trending failed: {e}")
    for name in ("small_cap_gainers", "aggressive_small_caps", "most_actives", "day_gainers"):
        try:
            out[name] = {q["symbol"] for q in yf.screen(name, count=25)["quotes"]}
        except Exception as e:
            out[name] = set()
            print(f"[discover] yahoo {name} failed: {e}")
    return out


# ---------------------------------------------------------------- 2/3. fetch
def _frame(t, attr) -> list[dict]:
    try:
        df = getattr(t, attr)
        if df is None or getattr(df, "empty", True):
            return []
        df = df.reset_index()
        df.columns = [str(c) for c in df.columns]
        return clean(df.to_dict("records"))
    except Exception:
        return []


def _rows(df, names, n=5) -> dict:
    out = {}
    if df is None or getattr(df, "empty", True):
        return out
    for name in names:
        if name in df.index:
            out[name] = [num(v) for v in df.loc[name].tolist()[:n]]
    out["_dates"] = [str(c)[:10] for c in list(df.columns)[:n]]
    return out


def fetch_info(sym: str) -> dict | None:
    def go():
        try:
            i = yf.Ticker(sym).info or {}
        except Exception:
            return None
        return {k: i.get(k) for k in INFO_KEYS} if i.get("quoteType") else None
    return cached(f"{sym}.info.json", FUND_TTL_H, go)


def fetch_deep(sym: str) -> dict:
    def go():
        t = yf.Ticker(sym)
        d = {a: _frame(t, a) for a in ("eps_trend", "eps_revisions", "growth_estimates", "earnings_history",
                                       "revenue_estimate")}
        try:
            d["q_income"] = _rows(t.quarterly_income_stmt, ["Total Revenue", "Gross Profit", "Operating Income"])
        except Exception:
            d["q_income"] = {}
        try:
            d["q_balance"] = _rows(t.quarterly_balance_sheet, ["Ordinary Shares Number", "Share Issued"])
        except Exception:
            d["q_balance"] = {}
        try:
            ed = (t.calendar or {}).get("Earnings Date") or []
            d["next_earnings"] = clean(ed[0]) if ed else None
        except Exception:
            d["next_earnings"] = None
        return d
    return cached(f"{sym}.deep.json", FUND_TTL_H, go) or {}


# ---- SEC EDGAR (optional: needs SEC_USER_AGENT="name email" in the environment, sec.gov only)
def sec_headers():
    ua = env("SEC_USER_AGENT")
    return {"User-Agent": ua} if ua else None


def sec_cik_map() -> dict:
    h = sec_headers()
    if not h:
        return {}
    def go():
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=h, timeout=20)
        r.raise_for_status()
        return {v["ticker"].replace(".", "-"): v["cik_str"] for v in r.json().values()}
    try:
        return cached("_sec_cik.json", 24 * 7, go) or {}
    except Exception as e:
        print(f"[sec] ticker map failed: {e}")
        return {}


OFFERING_FORMS = ("S-1", "S-1/A", "S-3", "S-3/A", "F-1", "F-3", "424B1", "424B2", "424B3", "424B4", "424B5")


def sec_filings(sym: str, cik: int | None) -> dict | None:
    h = sec_headers()
    if not h or not cik:
        return None
    def go():
        time.sleep(0.25)  # stay well under SEC's 10 req/s
        r = requests.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers=h, timeout=20)
        r.raise_for_status()
        rec = r.json()["filings"]["recent"]
        today = dt.date.today()
        out = {"offerings_120d": [], "late_filings_180d": 0, "delisting_notice_180d": False, "form4_90d": 0}
        for form, date, items in zip(rec["form"], rec["filingDate"], rec.get("items", [""] * len(rec["form"]))):
            age = (today - dt.date.fromisoformat(date)).days
            if age > 180:
                break
            if form in OFFERING_FORMS and age <= 120:
                out["offerings_120d"].append(f"{form} {date}")
            if form.startswith("NT 10"):
                out["late_filings_180d"] += 1
            if form == "8-K" and "3.01" in (items or ""):
                out["delisting_notice_180d"] = True
            if form == "4" and age <= 90:
                out["form4_90d"] += 1
        return out
    try:
        return cached(f"{sym}.sec.json", FUND_TTL_H, go)
    except Exception:
        return None


# ---------------------------------------------------------------- prices
def performance(syms: list[str]) -> dict[str, dict]:
    try:
        px = yf.download(syms, period="2y", auto_adjust=True, actions=True, progress=False, threads=8)
    except Exception as e:
        print(f"[perform] download failed: {e}")
        return {}
    close, vol, splits = px["Close"], px["Volume"], px.get("Stock Splits")

    def ret(s, n):
        s = s.dropna()
        return num(s.iloc[-1] / s.iloc[-1 - n] - 1) if len(s) > n else None

    spy = close.get("SPY")
    spy3, spy6 = (ret(spy, 63), ret(spy, 126)) if spy is not None else (None, None)
    out = {}
    for s in syms:
        if s not in close:
            continue
        c = close[s].dropna()
        if len(c) < 30:
            continue
        v = vol[s].reindex(c.index).fillna(0)
        last = float(c.iloc[-1])
        r3, r6 = ret(c, 63), ret(c, 126)
        rs = None
        if splits is not None and s in splits:
            sp = splits[s].dropna()
            sp = sp[(sp > 0) & (sp != 1) & (sp.index >= sp.index.max() - pd.Timedelta(days=548))] if len(sp) else sp
            rs = [f"{d.date()} {r:g}:1" if r >= 1 else f"{d.date()} 1:{1 / r:g}" for d, r in sp.items() if r < 1]
        d200 = c.tail(200).mean() if len(c) >= 200 else None
        out[s] = {
            "price": last, "r1d": ret(c, 1), "r5d": ret(c, 5), "r1m": ret(c, 21), "r3m": r3, "r6m": r6,
            "r1y": ret(c, 252),
            "rel3m": r3 - spy3 if None not in (r3, spy3) else None,
            "rel6m": r6 - spy6 if None not in (r6, spy6) else None,
            "off_high": num(last / c.tail(252).max() - 1),
            "above50": last > c.tail(50).mean() if len(c) >= 50 else None,
            "above200": last > d200 if d200 else None,
            "ext200": num(last / d200) if d200 else None,
            "dollar_vol": num((c.tail(20) * v.tail(20)).mean()),
            "vol_spike": num(v.tail(5).mean() / v.tail(60).mean()) if v.tail(60).mean() > 0 else None,
            "reverse_splits": rs or [],
        }
    return out


# ---------------------------------------------------------------- 4. score
def lin(x, lo, hi):
    if x is None:
        return None
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100))


def _row(rows, period):
    return next((r for r in rows if r.get("period") == period), {})


def _series(d, *names):
    for n in names:
        v = d.get(n)
        if v and any(x is not None for x in v):
            return v
    return []


def features(info: dict, deep: dict, p: dict) -> dict:
    g = lambda k: num(info.get(k))
    price = p.get("price") or g("currentPrice") or g("regularMarketPrice")
    rev = g("totalRevenue")
    x = {
        "mcap": g("marketCap"), "revenue": rev, "gross_margin": g("grossMargins"), "op_margin": g("operatingMargins"),
        "fcf_margin": g("freeCashflow") / rev if g("freeCashflow") is not None and rev else None,
        "debt_eq": g("debtToEquity"), "rev_growth": g("revenueGrowth"), "eps_growth": g("earningsGrowth"),
        "fwd_pe": g("forwardPE"), "peg": g("trailingPegRatio"), "ev_sales": g("enterpriseToRevenue"),
        "target_upside": g("targetMeanPrice") / price - 1 if g("targetMeanPrice") and price else None,
        "analysts": g("numberOfAnalystOpinions") or 0, "short_float": g("shortPercentOfFloat"),
        "inst": g("heldPercentInstitutions"), "insiders": g("heldPercentInsiders"), "float": g("floatShares"),
        "price": price,
    }
    # quarterly revenue / margins / shares (newest first)
    qi, qb = deep.get("q_income", {}), deep.get("q_balance", {})
    rv = _series(qi, "Total Revenue")
    gp, oi = _series(qi, "Gross Profit"), _series(qi, "Operating Income")
    x["q_yoy"] = rv[0] / rv[4] - 1 if len(rv) >= 5 and rv[0] and rv[4] and rv[4] > 0 else None
    x["q_seq"] = rv[0] / rv[1] - 1 if len(rv) >= 2 and rv[0] and rv[1] and rv[1] > 0 else None
    # acceleration: latest quarter's YoY vs the trailing-year growth Yahoo reports
    x["rev_accel"] = x["q_yoy"] - x["rev_growth"] if None not in (x["q_yoy"], x["rev_growth"]) else None
    gm = [a / b if a is not None and b else None for a, b in zip(gp, rv)]
    om = [a / b if a is not None and b else None for a, b in zip(oi, rv)]
    x["gm_trend"] = gm[0] - gm[4] if len(gm) >= 5 and None not in (gm[0], gm[4]) else None
    x["om_trend"] = om[0] - om[4] if len(om) >= 5 and None not in (om[0], om[4]) else None
    sh = _series(qb, "Ordinary Shares Number", "Share Issued")
    x["dilution"] = sh[0] / sh[4] - 1 if len(sh) >= 5 and sh[0] and sh[4] else (
        sh[0] / sh[-1] - 1 if len(sh) >= 3 and sh[0] and sh[-1] else None)
    # revenue per share: growth funded by share issuance (bank M&A) is fine, pure dilution isn't
    rg = x["q_yoy"] if x["q_yoy"] is not None else x["rev_growth"]
    x["rps_growth"] = (1 + rg) / (1 + x["dilution"]) - 1 if None not in (rg, x["dilution"]) else None
    # cash runway only matters when operations burn cash; capex-heavy but profitable (E&P, shipping) is fine
    fcf, ocf, cash = g("freeCashflow"), g("operatingCashflow"), g("totalCash")
    if info.get("sector") in FINANCIAL_SECTORS:
        x["runway_q"] = None
    elif ocf is not None and ocf >= 0:
        x["runway_q"] = 99.0
    elif cash is not None and (ocf is not None or fcf is not None):
        burn = -(ocf if ocf is not None else fcf)
        x["runway_q"] = 99.0 if burn <= 0 else cash / (burn / 4)
    else:
        x["runway_q"] = None

    ge = deep.get("growth_estimates", [])
    x["growth_ny"] = num(_row(ge, "+1y").get("stockTrend"))
    x["rev_est_ny"] = num(_row(deep.get("revenue_estimate", []), "+1y").get("growth"))
    up = dn = 0
    for r in deep.get("eps_revisions", []):
        up += num(r.get("upLast30days")) or 0
        dn += num(r.get("downLast30days")) or 0
    x["rev_up"], x["rev_dn"] = int(up), int(dn)
    x["revision_ratio"] = (up - dn) / (up + dn) if up + dn else None
    chg = []
    for per in ("0y", "+1y"):
        r = _row(deep.get("eps_trend", []), per)
        cur, ago = num(r.get("current")), num(r.get("90daysAgo"))
        if cur is not None and ago:
            chg.append((cur - ago) / abs(ago))
    x["eps_trend_90d"] = sum(chg) / len(chg) if chg else None
    hist = [r for r in deep.get("earnings_history", []) if num(r.get("surprisePercent")) is not None][-4:]
    x["surprise_avg"] = sum(num(r["surprisePercent"]) for r in hist) / len(hist) if hist else None
    x["beat_rate"] = sum(num(r["surprisePercent"]) > 0 for r in hist) / len(hist) if hist else None
    x["growth_adj_ps"] = x["ev_sales"] / (max(x["rev_growth"], 0.01) * 100) \
        if x["ev_sales"] and x["rev_growth"] is not None and x["rev_growth"] > 0 else None

    for k in ("r1d", "r5d", "r1m", "r3m", "r6m", "r1y", "rel3m", "rel6m", "off_high", "ext200",
              "dollar_vol", "vol_spike"):
        x[k] = p.get(k)
    x["above50"] = None if p.get("above50") is None else (100.0 if p["above50"] else 0.0)
    x["above200"] = None if p.get("above200") is None else (100.0 if p["above200"] else 0.0)
    x["log_mcap"] = math.log10(x["mcap"]) if x["mcap"] else None
    x["inst_fit"] = abs(x["inst"] - 0.4) if x["inst"] is not None else None
    return x


pct = lambda v: f"{v * 100:+.0f}%"
pct0 = lambda v: f"{v * 100:.0f}%"
pp = lambda v: f"{v * 100:+.1f}pt"
mult = lambda v: f"{v:.1f}x"
money = lambda v: f"${v / 1e9:.2f}B" if v >= 1e9 else f"${v / 1e6:.0f}M"

# (feature, pillar, label, formatter, scaler)
METRICS = [
    ("rev_growth", "growth", "Revenue growth (TTM)", pct, lambda v: lin(v, 0.0, 0.60)),
    ("q_yoy", "growth", "Latest quarter revenue YoY", pct, lambda v: lin(v, 0.0, 0.70)),
    ("rev_accel", "growth", "Revenue acceleration", pp, lambda v: lin(v, -0.15, 0.15)),
    ("gm_trend", "growth", "Gross margin change YoY", pp, lambda v: lin(v, -0.05, 0.05)),
    ("growth_ny", "growth", "Est. EPS growth next yr", pct, lambda v: lin(v, 0.0, 0.40)),
    ("rev_est_ny", "growth", "Est. revenue growth next yr", pct, lambda v: lin(v, 0.0, 0.35)),
    ("gross_margin", "quality", "Gross margin", pct0, lambda v: lin(v, 0.15, 0.60)),
    ("op_margin", "quality", "Operating margin", pct0, lambda v: lin(v, -0.20, 0.20)),
    ("om_trend", "quality", "Operating margin change YoY", pp, lambda v: lin(v, -0.05, 0.10)),
    ("fcf_margin", "quality", "FCF margin", pct0, lambda v: lin(v, -0.20, 0.15)),
    ("runway_q", "quality", "Cash runway (quarters)", lambda v: "cash positive" if v >= 99 else f"{v:.0f}q",
     lambda v: lin(v, 2, 12)),
    ("dilution", "quality", "Share count change YoY", pct, lambda v: lin(v, 0.20, 0.0)),
    ("rps_growth", "quality", "Revenue per share growth", pct, lambda v: lin(v, 0.0, 0.40)),
    ("debt_eq", "quality", "Debt/equity", lambda v: f"{v / 100:.2f}", lambda v: lin(v, 200, 0)),
    ("revision_ratio", "estimates", "EPS revisions 30d (net)", lambda v: f"{v:+.2f}", lambda v: lin(v, -0.6, 0.6)),
    ("eps_trend_90d", "estimates", "EPS estimate change 90d", pct, lambda v: lin(v, -0.10, 0.15)),
    ("surprise_avg", "estimates", "Avg EPS surprise (4q)", pct, lambda v: lin(v, -0.05, 0.15)),
    ("beat_rate", "estimates", "Beat rate (4q)", pct0, lambda v: lin(v, 0.25, 1.0)),
    ("target_upside", "estimates", "Analyst target upside", pct, lambda v: lin(v, -0.10, 0.40)),
    ("growth_adj_ps", "valuation", "EV/sales per point of growth", lambda v: f"{v:.2f}", lambda v: lin(v, 0.5, 0.05)),
    ("fwd_pe", "valuation", "Forward P/E", mult, lambda v: 0.0 if v <= 0 else lin(v, 80, 12)),
    ("peg", "valuation", "PEG", lambda v: f"{v:.2f}", lambda v: None if v <= 0 else lin(v, 3.0, 0.7)),
    ("rel3m", "momentum", "3m vs SPY", pct, lambda v: lin(v, -0.20, 0.30)),
    ("rel6m", "momentum", "6m vs SPY", pct, lambda v: lin(v, -0.30, 0.50)),
    ("r1m", "momentum", "1m return", pct, lambda v: lin(v, -0.20, 0.20)),
    ("off_high", "momentum", "From 52w high", pct, lambda v: lin(v, -0.50, 0.0)),
    ("above50", "momentum", "Above 50-day avg", lambda v: "yes" if v else "no", lambda v: v),
    ("above200", "momentum", "Above 200-day avg", lambda v: "yes" if v else "no", lambda v: v),
    ("log_mcap", "early", "Market cap", lambda v: money(10 ** v), lambda v: lin(v, math.log10(MAX_MCAP), 8.0)),
    ("analysts", "early", "Analyst coverage", lambda v: f"{v:.0f}", lambda v: lin(v, 20, 2)),
    ("inst_fit", "early", "Institutional ownership (sweet spot ~40%)", lambda v: f"±{v * 100:.0f}pt from 40%",
     lambda v: lin(v, 0.5, 0.0)),
    ("ext200", "early", "Price vs 200-day avg", lambda v: f"{v:.2f}x", lambda v: lin(v, 2.0, 1.05)),
    ("r1y", "early", "1y return (lower = earlier)", pct, lambda v: lin(v, 4.0, 0.6)),
]
PILLAR_N = Counter(m[1] for m in METRICS)
PILLAR_OF = {m[2]: m[1] for m in METRICS}
FINANCIAL_SECTORS = {"Financial Services", "Real Estate"}


def gate(sym, info, deep, p, sec, x, social, heads) -> list[str]:
    """Hard rejects. Anything here never gets ranked."""
    r = []
    fin = info.get("sector") in FINANCIAL_SECTORS  # banks: stock-funded M&A, deposits make cash-flow "burn" meaningless
    if (info.get("exchange") or "") not in MAJOR_EXCHANGES:
        r.append(f"Not on a major US exchange ({info.get('exchange') or '?'})")
    if (x["price"] or 0) < MIN_PRICE:
        r.append("Price under $1")
    if x["mcap"] and not MIN_MCAP <= x["mcap"] <= MAX_MCAP:
        r.append(f"Market cap {money(x['mcap'])} outside {money(MIN_MCAP)}–{money(MAX_MCAP)}")
    if (x["dollar_vol"] or 0) < MIN_DOLLAR_VOL:
        r.append(f"Illiquid (<{money(MIN_DOLLAR_VOL)}/day traded)")
    if (x["revenue"] or 0) < MIN_REVENUE and info.get("sector") not in FINANCIAL_SECTORS:
        r.append("Pre-revenue / story stock (<$10M sales)")
    if not fin and (x["dilution"] or 0) > 0.30 and (x["rps_growth"] is None or x["rps_growth"] < 0.05):
        r.append(f"Dilution machine (shares {pct(x['dilution'])} YoY, revenue/share not keeping up)")
    if p.get("reverse_splits"):
        r.append(f"Reverse split {p['reverse_splits'][-1]}")
    if not fin and x["runway_q"] is not None and x["runway_q"] < 4:
        r.append(f"Cash runway {x['runway_q']:.1f} quarters")
    wires = sum(h["wire"] for h in heads)
    wire_heavy = len(heads) >= 2 and wires / len(heads) > 0.6
    if (x["r5d"] or 0) > 0.8 and (x["vol_spike"] or 0) > 5 and ((x["rev_growth"] or 0) < 0.2 or wire_heavy):
        r.append(f"Pump pattern ({pct(x['r5d'])} in 5d on {x['vol_spike']:.0f}x volume, thin fundamentals)")
    if sec:
        if sec.get("delisting_notice_180d"):
            r.append("Exchange delisting notice (8-K 3.01)")
        if sec.get("late_filings_180d"):
            r.append("Late SEC filing (NT 10-K/Q)")
    return r


def red_flags(x, sec, social, heads, sector) -> list[str]:
    fl = []
    if (x["dilution"] or 0) > 0.15 and (x["rps_growth"] is None or x["rps_growth"] < 0.10):
        fl.append(f"Diluting ({pct(x['dilution'])} shares YoY)")
    if sec and sec.get("offerings_120d"):
        fl.append(f"Offering/shelf filed ({sec['offerings_120d'][0]})")
    if (x["short_float"] or 0) > 0.25:
        fl.append(f"Heavily shorted ({x['short_float']:.0%} of float)")
    if (x["r5d"] or 0) > 0.5:
        fl.append(f"Vertical move ({pct(x['r5d'])} in 5d)")
    wires = sum(h["wire"] for h in heads)
    if len(heads) >= 3 and wires / len(heads) > 0.6:
        fl.append("News is mostly company press releases")
    if social and social.get("mentions", 0) >= 5 and x["analysts"] <= 1 and len(heads) - wires <= 1:
        fl.append("Social hype with no independent coverage")
    if x["revision_ratio"] is not None and x["revision_ratio"] <= -0.5 and (x["eps_trend_90d"] or 0) < -0.05:
        fl.append("Analysts cutting estimates")
    if x["above200"] == 0.0 and (x["off_high"] or 0) < -0.45:
        fl.append("Broken chart (<200d, >45% off high)")
    if (x["debt_eq"] or 0) > 300 and sector not in FINANCIAL_SECTORS:
        fl.append("Heavily levered (D/E > 3)")
    return fl


def score(sym, info, deep, p, sec, social, heads) -> dict:
    x = features(info, deep, p)
    sector = info.get("sector")
    pillars, contrib, seen = defaultdict(list), [], 0
    for key, pillar, label, fmt, scaler in METRICS:
        v = x.get(key)
        s = scaler(v) if v is not None else None
        if s is None:
            continue
        seen += 1
        pillars[pillar].append(s)
        contrib.append({"label": label, "pillar": pillar, "value": fmt(v), "score": round(s),
                        "impact": round((s - 50) * WEIGHTS[pillar] / PILLAR_N[pillar], 2)})
    pill = {k: round(sum(pillars[k]) / len(pillars[k]), 1) if pillars.get(k) else None for k in WEIGHTS}
    # one or two analysts can't make a consensus: shrink the estimates pillar toward neutral
    if pill["estimates"] is not None:
        trust = min(1.0, x["analysts"] / 4)
        pill["estimates"] = round(50 + (pill["estimates"] - 50) * trust, 1)
    composite = sum(WEIGHTS[k] * (pill[k] if pill[k] is not None else 50) for k in WEIGHTS)
    flags = red_flags(x, sec, social, heads, sector)
    composite -= FLAG_COST * len(flags)
    coverage = seen / len(METRICS)

    cautions, days_to_er, ne = [], None, deep.get("next_earnings")
    if ne:
        try:
            days_to_er = (dt.date.fromisoformat(ne[:10]) - dt.date.today()).days
            if 0 <= days_to_er <= 7:
                cautions.append(f"Earnings in {days_to_er}d")
        except ValueError:
            pass
    if coverage < 0.6:
        cautions.append(f"Thin data ({coverage:.0%} of metrics)")
    if x["analysts"] <= 2:
        cautions.append("Barely covered — do your own DD")
    if (x["float"] or 1e12) < 15e6:
        cautions.append(f"Tiny float ({x['float'] / 1e6:.1f}M shares)")
    if sector in FINANCIAL_SECTORS:
        cautions.append("Financials: margin metrics less meaningful")

    if len(flags) >= 2 or composite < AVOID_BELOW:
        tier = "AVOID"
    elif composite >= (BIG_BUY_AT if (x["mcap"] or 0) > BIG_CAP else BUY_AT) and not flags and coverage >= 0.6:
        tier = "BUY"
        if days_to_er is not None and 0 <= days_to_er <= EARNINGS_BLACKOUT_D:
            tier = "WATCH"
            cautions.insert(0, "BUY-grade, held to WATCH until earnings print")
    else:
        tier = "WATCH"
    if tier == "WATCH" and (x["mcap"] or 0) > BIG_CAP and composite >= BUY_AT and not flags:
        cautions.insert(0, f"Big name: needs {BIG_BUY_AT} to be a BUY")

    diamond = (tier != "AVOID" and not flags and composite >= DIAMOND_MIN_SCORE
               and (x["mcap"] or 1e15) <= DIAMOND_MAX_MCAP and x["analysts"] <= DIAMOND_MAX_ANALYSTS
               and (pill["growth"] or 0) >= 65 and (pill["quality"] or 0) >= 50)

    contrib.sort(key=lambda c: c["impact"])
    news_tones = [h["tone"] for h in heads]
    labels = [h["label"] for h in heads if h.get("label")]
    return {
        "ticker": sym, "name": info.get("shortName") or info.get("longName") or sym,
        "sector": sector, "industry": info.get("industry"), "market_cap": x["mcap"], "price": x["price"],
        "about": (info.get("longBusinessSummary") or "")[:420], "website": info.get("website"),
        "score": round(composite, 1), "tier": tier, "diamond": diamond, "pillars": pill, "coverage": round(coverage, 2),
        "flags": flags, "cautions": cautions,
        "strengths": [c for c in reversed(contrib) if c["impact"] > 0][:4],
        "weaknesses": [c for c in contrib if c["impact"] < 0][:4], "metrics": contrib,
        "next_earnings": ne, "days_to_earnings": days_to_er, "recommendation": info.get("recommendationKey"),
        "analysts": x["analysts"], "rev_up": x["rev_up"], "rev_dn": x["rev_dn"],
        "perf": {k: x[k] for k in ("r1d", "r5d", "r1m", "r3m", "r6m", "r1y", "rel3m", "off_high")},
        "key": {k: x[k] for k in ("revenue", "rev_growth", "q_yoy", "rev_accel", "gross_margin", "op_margin",
                                  "dilution", "runway_q", "rps_growth", "inst", "insiders", "short_float", "ev_sales",
                                  "fwd_pe", "target_upside", "dollar_vol")},
        "sentiment": {
            "news_tone": round(sum(news_tones) / len(news_tones), 3) if news_tones else None,
            "news_pos": labels.count("positive"), "news_neg": labels.count("negative"),
            "wire_share": round(sum(h["wire"] for h in heads) / len(heads), 2) if heads else None,
            "social": social or None,
        },
        "sec": sec,
        "estimates": {"eps_trend": deep.get("eps_trend", []), "earnings_history": deep.get("earnings_history", [])[-4:]},
        "quarters": deep.get("q_income", {}),
    }


# ---------------------------------------------------------------- 5. read
def tone_label(t):
    if t is None:
        return "—"
    return "bullish" if t >= 0.15 else "upbeat" if t >= 0.05 else "bearish" if t <= -0.15 else "cautious" \
        if t <= -0.05 else "neutral"


def write_read(rows, rejected, market, news_overall, social_overall, hunt_total):
    buys = [r for r in rows if r["tier"] == "BUY"]
    iwm, spy = market.get("IWM", {}), market.get("SPY", {})
    parts = []
    if iwm.get("r1m") is not None and spy.get("r1m") is not None:
        lead = iwm["r1m"] - spy["r1m"]
        parts.append(f"Small caps are {'leading' if lead > 0.01 else 'lagging' if lead < -0.01 else 'tracking'} "
                     f"the market (IWM {iwm['r1m'] * 100:+.1f}% vs SPY {spy['r1m'] * 100:+.1f}% over a month).")
    parts.append(f"News reads {tone_label(news_overall)}"
                 + (f", social {tone_label(social_overall.get('tone'))} across {social_overall.get('posts', 0):,} "
                    f"Reddit/X posts." if social_overall.get("posts") else "."))
    parts.append(f"Screened {hunt_total:,} growers, researched {len(rows) + len(rejected)}, "
                 f"{len(rejected)} thrown out as pump, dilution or liquidity risk.")
    gems = [r for r in rows if r.get("diamond")]
    if gems:
        g = gems[0]
        parts.append(f"{len(gems)} diamond{'s' if len(gems) != 1 else ''} in the rough — best: {g['ticker']} "
                     f"({money(g['market_cap'])}, {g['analysts']:.0f} analysts, {g['industry'] or g['sector']}).")
    others = [r for r in buys if not r.get("diamond")]
    if buys:
        parts.append(f"{len(buys)} clear every bar.")
    if others:
        b = others[0]
        why = ", ".join(f"{s['label'].lower()} {s['value']}" for s in b["strengths"][:2])
        parts.append(f"Best setup beyond the diamonds: {b['ticker']} ({money(b['market_cap'])}, "
                     f"{b['industry'] or b['sector']}) — {why}.")
    if not buys:
        parts.append("Nothing clears every bar this run — patience is a position.")
    sect = Counter(r["sector"] for r in rows if r["tier"] == "BUY" and r["sector"])
    if sect:
        parts.append("Strength is concentrated in " + ", ".join(f"{s} ({n})" for s, n in sect.most_common(3)) + ".")
    return " ".join(parts)


def run(verbose: bool = True) -> dict:
    load_env()
    t0 = time.time()
    log = print if verbose else (lambda *a, **k: None)

    refresh_social()
    heads = defaultdict(list)
    social, social_overall = social_mentions()
    mheat, yheat = massive_news(heads), yahoo_topic_news(heads)
    lists = yahoo_lists()
    hunt_rows, hunt_total = hunt_screen()
    hunt = sorted(((prerank(q), q["symbol"], num(q.get("marketCap")) or 0) for q in hunt_rows
                   if to_yahoo(q["symbol"])), reverse=True)
    big = [s for v, s, m in hunt if v >= 0 and m > 1e10][:HUNT_BIG]
    small = [s for v, s, m in hunt if v >= 0 and m <= 1e10][:HUNT_DEEP - len(big)]
    hunt_syms = small + big
    log(f"[hunt] {hunt_total} growers screened → {len(hunt_syms)} shortlisted")

    heat = Counter()
    for s, n in (mheat + yheat).items():
        heat[s] += n
    for s, st in social.items():
        heat[s] += st["mentions"]
    for name, bonus in (("trending", 4), ("small_cap_gainers", 4), ("aggressive_small_caps", 3),
                        ("most_actives", 2), ("day_gainers", 2)):
        for s in lists.get(name, ()):
            if to_yahoo(s):
                heat[to_yahoo(s)] += bonus
    for w in ("SPY", "QQQ", "DIA", "IWM", "VIX"):
        heat.pop(w, None)
    heat_syms = [s for s, _ in heat.most_common(HEAT_DEEP * 3) if s not in hunt_syms]

    # stage 1: cheap info for everything, size/exchange gate before the expensive research
    cands = list(dict.fromkeys(hunt_syms + heat_syms))
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        infos = dict(zip(cands, ex.map(fetch_info, cands)))
    in_band = lambda i: i and i.get("quoteType") == "EQUITY" and MIN_MCAP <= (num(i.get("marketCap")) or 0) <= MAX_MCAP
    heat_keep = [s for s in heat_syms if in_band(infos.get(s))][:HEAT_DEEP]
    universe = list(dict.fromkeys([s for s in hunt_syms if in_band(infos.get(s))] + heat_keep))
    log(f"[gate] {len(universe)} in the ${MIN_MCAP / 1e6:.0f}M–${MAX_MCAP / 1e9:.0f}B band "
        f"({len(heat_syms)} hot names checked, megacaps dropped)")

    # stage 2: research
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        deeps = dict(zip(universe, ex.map(fetch_deep, universe)))
    ciks = sec_cik_map()
    secs = {s: sec_filings(s, ciks.get(s)) for s in universe} if ciks else {}
    ynews = yahoo_ticker_news(universe, heads)
    news_count = Counter({s: max(mheat.get(s, 0), yheat.get(s, 0), ynews.get(s, 0)) for s in universe})
    finnhub_news(universe, heads, news_count)
    perf = performance(universe + ["SPY", "QQQ", "IWM", "^VIX"])

    rows, rejected = [], []
    hunt_set = set(hunt_syms)
    for sym in universe:
        info, deep, p = infos[sym], deeps.get(sym, {}), perf.get(sym)
        if not p:
            continue
        x = features(info, deep, p)
        why = gate(sym, info, deep, p, secs.get(sym), x, social.get(sym), heads.get(sym, []))
        src = {"hunt": sym in hunt_set, "news": news_count[sym], "social": (social.get(sym) or {}).get("mentions", 0),
               **{k: sym in {to_yahoo(v) for v in vals} for k, vals in lists.items()}}
        if why:
            rejected.append({"ticker": sym, "name": info.get("shortName") or sym, "reasons": why,
                             "market_cap": x["mcap"], "price": x["price"], "r5d": x["r5d"], "discovery": src})
            continue
        r = score(sym, info, deep, p, secs.get(sym), social.get(sym), heads.get(sym, []))
        r["discovery"], r["headlines"] = src, heads.get(sym, [])
        rows.append(r)
    rows.sort(key=lambda r: r["score"], reverse=True)

    prev = {}
    if OUT.exists():
        try:
            prev = {r["ticker"]: r["rank"] for r in json.loads(OUT.read_text()).get("results", [])}
        except Exception:
            pass
    for n, r in enumerate(rows, 1):
        r["rank"] = n
        r["rank_change"] = (prev[r["ticker"]] - n) if r["ticker"] in prev else None
        r["new"] = bool(prev) and r["ticker"] not in prev

    market = {k: {m: perf.get(k, {}).get(m) for m in ("price", "r1d", "r5d", "r1m", "r3m")}
              for k in ("SPY", "QQQ", "IWM", "^VIX")}
    all_news = [h["tone"] for hs in heads.values() for h in hs]
    news_overall = round(sum(all_news) / len(all_news), 3) if all_news else None
    sectors = defaultdict(list)
    for r in rows:
        sectors[r["sector"] or "Other"].append(r)
    sector_heat = sorted(({"sector": k, "count": len(v), "buys": sum(r["tier"] == "BUY" for r in v),
                           "avg": round(sum(r["score"] for r in v) / len(v), 1), "best": v[0]["ticker"]}
                          for k, v in sectors.items()), key=lambda s: (-s["buys"], -s["avg"]))
    reject_reasons = Counter(re.split(r" \(| \d| \$", w)[0] for r in rejected for w in r["reasons"])

    out = clean({
        "generated_at": now_utc().isoformat(), "took_s": round(time.time() - t0, 1),
        "universe": len(rows), "counts": Counter(r["tier"] for r in rows), "diamonds": sum(bool(r.get("diamond")) for r in rows),
        "read": write_read(rows, rejected, market, news_overall, social_overall, hunt_total),
        "summary": {"hunt_total": hunt_total, "researched": len(rows) + len(rejected), "rejected": len(rejected),
                    "reject_reasons": reject_reasons.most_common(6), "market": market,
                    "news_tone": news_overall, "news_items": len(all_news), "social": social_overall,
                    "sector_heat": sector_heat, "sec_enabled": bool(ciks)},
        "config": {"weights": WEIGHTS, "buy_at": BUY_AT, "avoid_below": AVOID_BELOW, "flag_cost": FLAG_COST,
                   "min_mcap": MIN_MCAP, "max_mcap": MAX_MCAP, "min_dollar_vol": MIN_DOLLAR_VOL,
                   "min_revenue": MIN_REVENUE, "news_hours": NEWS_HOURS},
        "results": rows, "rejected": rejected,
    })
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out))
    tmp.replace(OUT)

    HIST.mkdir(exist_ok=True)
    slim = {"generated_at": out["generated_at"],
            "results": [{k: r[k] for k in ("ticker", "rank", "score", "tier", "price")} for r in rows]}
    (HIST / f"{now_utc():%Y-%m-%d_%H%M}.json").write_text(json.dumps(slim))
    for old in sorted(HIST.glob("*.json"))[:-KEEP_HISTORY]:
        old.unlink()

    log(f"[done] {len(rows)} ranked, {len(rejected)} rejected in {out['took_s']}s — {dict(out['counts'])}")
    return out


if __name__ == "__main__":
    res = run()
    print("\n" + res["read"] + "\n")
    for r in res["results"][:20]:
        print(f"{r['rank']:>3} {r['ticker']:<6} {r['tier']:<5} {r['score']:>5}  {money(r['market_cap']):>8}  "
              f"{(r['industry'] or '')[:28]:<28} {r['name'][:26]}")
    print("\nrejected:", ", ".join(f"{r['ticker']} ({r['reasons'][0]})" for r in res["rejected"][:12]))


# ---------------------------------------------------------------- on-demand: chart + competitors
_mem: dict = {}


def _memo(key, ttl_s, fn):
    hit = _mem.get(key)
    if hit and time.time() - hit[0] < ttl_s:
        return hit[1]
    val = fn()
    _mem[key] = (time.time(), val)
    return val


CHART_RANGES = {  # range -> (yfinance period or days back, interval, cache seconds)
    "1D": ("1d", "5m", 120), "1W": ("5d", "30m", 600), "1M": ("1mo", "60m", 1800), "3M": ("3mo", "1d", 3600),
    "1Y": ("1y", "1d", 3600), "3Y": (3 * 365, "1wk", 6 * 3600), "5Y": ("5y", "1wk", 6 * 3600),
    "10Y": ("10y", "1mo", 6 * 3600),
}


def chart(sym: str, rng: str) -> dict:
    """Close prices for one range, Apple-Stocks style: line only, previous close for 1D."""
    period, interval, ttl = CHART_RANGES[rng]

    def go():
        t = yf.Ticker(sym)
        if isinstance(period, int):
            h = t.history(start=str(dt.date.today() - dt.timedelta(days=period)), interval=interval, auto_adjust=True)
        else:
            h = t.history(period=period, interval=interval, auto_adjust=True)
        if rng == "1D" and len(h) < 2:  # pre-market: show the last full session
            h = t.history(period="5d", interval="5m", auto_adjust=True)
            if len(h):
                h = h[h.index.date == h.index[-1].date()]
        h = h["Close"].dropna()
        if h.empty:
            return {"ticker": sym, "range": rng, "points": []}
        pts = [[int(ts.timestamp()), round(float(v), 4)] for ts, v in h.items()]
        out = {"ticker": sym, "range": rng, "points": pts, "last": pts[-1][1], "high": max(v for _, v in pts),
               "low": min(v for _, v in pts)}
        if rng == "1D":
            try:
                prev = num(t.fast_info.get("previousClose"))
            except Exception:
                prev = None
            out["prev_close"] = prev
            day = h.index[0]
            out["session"] = [int(day.replace(hour=9, minute=30).timestamp()), int(day.replace(hour=16, minute=0).timestamp())]
        base = out.get("prev_close") or pts[0][1]
        out["base"] = base
        out["change"] = round(pts[-1][1] - base, 4)
        out["change_pct"] = round(pts[-1][1] / base - 1, 5) if base else None
        return out

    return _memo(("chart", sym, rng), ttl, go)


def peers(sym: str) -> list[str]:
    key = env("FINNHUB_API_KEY")
    if not key:
        return []

    def go():
        try:
            r = requests.get("https://finnhub.io/api/v1/stock/peers", params={"symbol": sym, "token": key}, timeout=10)
            return [p for p in r.json() if isinstance(p, str) and p != sym and to_yahoo(p) and "." not in p]
        except Exception:
            return []

    return cached(f"{sym}.peers.json", 24 * 7, go) or []


def compare(sym: str) -> dict:
    """The stock plus its 3 closest direct competitors, scored the same way, with a verdict."""
    def go():
        load_env()
        info = fetch_info(sym)
        if not info:
            return {"ticker": sym, "error": "no data"}
        cands = peers(sym)[:12]
        with cf.ThreadPoolExecutor(WORKERS) as ex:
            pinfo = dict(zip(cands, ex.map(fetch_info, cands)))
        mcap = num(info.get("marketCap")) or 1
        ok = [s for s in cands if pinfo.get(s) and pinfo[s].get("quoteType") == "EQUITY"
              and (pinfo[s].get("exchange") or "") in MAJOR_EXCHANGES and num(pinfo[s].get("marketCap"))]
        ok.sort(key=lambda s: abs(math.log10(num(pinfo[s]["marketCap"])) - math.log10(mcap)))  # closest in size
        group = [sym] + ok[:3]
        infos = {sym: info, **{s: pinfo[s] for s in ok[:3]}}
        with cf.ThreadPoolExecutor(WORKERS) as ex:
            deeps = dict(zip(group, ex.map(fetch_deep, group)))
        perf = performance(group + ["SPY"])
        rows = []
        for s in group:
            p = perf.get(s)
            if not p:
                continue
            x = features(infos[s], deeps[s], p)
            why = gate(s, infos[s], deeps[s], p, None, x, None, [])
            r = score(s, infos[s], deeps[s], p, None, None, [])
            rows.append({k: r[k] for k in ("ticker", "name", "industry", "market_cap", "price", "score", "tier",
                                           "pillars", "flags", "strengths", "weaknesses", "key", "perf", "analysts")}
                        | {"rejected": why, "self": s == sym})
        live = [r for r in rows if not r["rejected"]]
        best = max(live, key=lambda r: r["score"]) if live else None
        verdict = None
        if best and len(rows) > 1:
            avg = {k: sum((r["pillars"].get(k) or 50) for r in rows) / len(rows) for k in WEIGHTS}
            edge = sorted(WEIGHTS, key=lambda k: (best["pillars"].get(k) or 50) - avg[k], reverse=True)[:2]
            lead = " and ".join(f"{k} ({best['pillars'].get(k) or 0:.0f} vs {avg[k]:.0f} avg)" for k in edge)
            if best["ticker"] == sym:
                verdict = f"{sym} holds up — it's the strongest of its peer group, leading on {lead}."
            else:
                gap = best["score"] - next((r["score"] for r in rows if r["self"]), 0)
                verdict = (f"{best['ticker']} looks better than {sym}: {gap:+.1f} points overall, "
                           f"leading on {lead}.")
        return {"ticker": sym, "industry": info.get("industry"), "rows": rows,
                "best": best["ticker"] if best else None, "verdict": verdict}

    return _memo(("compare", sym), 3600, go)
