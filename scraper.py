"""Pulse — social scraper: Reddit (rdt-cli) + X (headless Chrome + saved login).

Sources:
  - Reddit via rdt-cli: hot per subreddit (parallel) + newest across all subs (one multireddit listing)
  - X via headless Chrome with the login saved by save_x_session.py (parallel tabs)
  - Massive API fallback (if Reddit + X return < 50 posts total)

Credentials come from the environment / x_session.json only — never hardcoded.
  MASSIVE_API_KEY     — Massive news API key (optional, for fallback)

Output: raw_data.json
"""

import asyncio
import json
import os
import sys
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

MASSIVE_API_KEY = os.environ.get("MASSIVE_API_KEY", "")
MASSIVE_URL = (
    "https://api.massive.com/v2/reference/news"
    f"?apiKey={MASSIVE_API_KEY}"
    "&ticker=GME,AMC,NVDA,TSLA,PLTR"
    "&limit=50"
) if MASSIVE_API_KEY else ""

SUBREDDITS = [
    # ── High-signal ticker-mention subs (15) ──────────────────────────────
    # Widened from a 7-sub list that only ever surfaced the same 3 tickers
    # (WEN/VIVO/MSFT) every run. Added breadth; dropped low-signal subs
    # (Shortsqueeze, Daytrading) that rarely carry fresh $TICKER mentions.
    "wallstreetbets",
    "stocks",
    "StockMarket",
    "investing",
    "pennystocks",
    "algotrading",
    "options",
    "Superstonk",
    "Wallstreetbetsnew",
    "smallstreetbets",
    "thetagang",
    "SecurityAnalysis",
    "RobinHoodPennyStocks",
    "StocksAndTrading",
    "weedstocks",
]

# Per-sub 'new' listings added only ~5% unique posts on top of 'hot' (measured 2026-10-19),
# so 'new' is one multireddit listing across all subs instead of 15 separate calls.
REDDIT_WORKERS = 4        # parallel rdt calls; rdt backs off on rate_limited
REDDIT_NEW_PAGES = 1      # safety net: newest 100 across all subs (usually already in hot)
REDDIT_NEW_SKIP = {"Superstonk"}  # floods the combined 'new' feed with GME-only posts
POSTS_PER_SUBREDDIT_SORT = 100  # Reddit listing max; rdt returns 100 posts in ~1.5s
CMD_TIMEOUT = 15  # seconds per CLI call (headroom for rdt/twitter-cli cookie refresh)

# Output path (was referenced but never defined — caused NameError before raw_data.json was written)
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw_data.json")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run_cmd(args, timeout=60, env=None):
    """Run a shell command, return (stdout, stderr, returncode)."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", -1
    except FileNotFoundError:
        return "", "not found", -127
    except Exception as e:
        return "", str(e), -1


# ---------------------------------------------------------------------------
# Reddit scraping via Agent-Reach
# ---------------------------------------------------------------------------

def _parse_rdt_posts(stdout):
    """Parse JSON output from `rdt sub ... --json`.

    rdt-cli wraps results in: {"ok":true,"data":{"kind":"Listing","data":{"children":[...]}}}
    Uses the shared _normalize_reddit_post for each entry.
    """
    posts = []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return posts

    # rdt-cli wraps results:
    listing = data.get("data", {})
    if isinstance(listing, dict):
        children = listing.get("data", {}).get("children", [])
    elif isinstance(listing, list):
        children = listing
    else:
        children = []

    for child in children:
        if not isinstance(child, dict):
            continue
        entry = child.get("data", child)
        if not isinstance(entry, dict):
            continue
        normalized = _normalize_reddit_post(entry)
        if normalized:
            posts.append(normalized)
    return posts


def _rdt(args):
    """Run `rdt ... --json`, backing off once on Reddit's rate limit. Returns stdout or ''."""
    for attempt in range(2):
        stdout, _, rc = run_cmd(["rdt", *args, "--json"], timeout=CMD_TIMEOUT)
        if '"rate_limited"' in stdout and attempt == 0:
            time.sleep(10)
            continue
        return stdout if rc == 0 else ""
    return ""


def _rdt_after(stdout):
    """Pagination cursor from an rdt listing, or None."""
    try:
        return json.loads(stdout)["data"]["data"].get("after")
    except Exception:
        return None


def scrape_reddit():
    """Hot per subreddit (parallel) + newest across all subs via one multireddit listing.

    Returns (posts, active_backend).
    """
    all_posts, seen_ids = [], set()

    def add(posts):
        new = [p for p in posts if p["id"] not in seen_ids]
        seen_ids.update(p["id"] for p in new)
        all_posts.extend(new)
        return len(new)

    def hot(sub):
        return sub, _parse_rdt_posts(_rdt(["sub", sub, "--sort", "hot", "--limit", str(POSTS_PER_SUBREDDIT_SORT)]))

    with ThreadPoolExecutor(REDDIT_WORKERS) as ex:
        for sub, posts in ex.map(hot, SUBREDDITS):
            n = add(posts)
            print(f"  {'✓' if n else '·'} r/{sub} [hot]: {n} posts", flush=True)

    multi = "+".join(s for s in SUBREDDITS if s not in REDDIT_NEW_SKIP)
    after = None
    for page in range(REDDIT_NEW_PAGES):
        out = _rdt(["sub", multi, "--sort", "new", "--limit", "100"] + (["--after", after] if after else []))
        n = add(_parse_rdt_posts(out))
        print(f"  {'✓' if n else '·'} all subs [new] page {page + 1}: {n} posts", flush=True)
        after = _rdt_after(out)
        if not after:
            break

    print(f"\n  Reddit total: {len(all_posts)} unique posts")
    return all_posts, ("rdt-cli" if all_posts else "none")


# ---------------------------------------------------------------------------
# X/Twitter cashtag scraping via headless browser + saved login
# ---------------------------------------------------------------------------
# X now rejects scripted API clients even with valid cookies (401 "Could not
# authenticate you" from requests; 404 from twitter-cli, which can't generate
# the x-client-transaction-id header). So we load the login saved by
# save_x_session.py into headless Chromium, open x.com/search (Latest tab), and
# capture the SearchTimeline GraphQL responses the page itself makes.
#
# If X redirects to login, the session expired: we log a clear
# "run save_x_session.py" message and return [] instead of crashing the pipeline.
# ---------------------------------------------------------------------------

X_SESSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_session.json")

# Space-separated terms are AND in X search — alternatives must use OR.
X_CASHTAG_QUERIES = [
    "$AAPL OR $TSLA OR $NVDA OR $MSFT OR $AMZN OR $GOOGL OR $META OR $SPY",
    "$GME OR $AMC OR $PLTR OR $SOFI OR $RIVN OR $MARA OR $COIN OR $MSTR",
    "$SPX OR $VIX OR $QQQ OR $IWM OR $XLF",
    '"earnings beat" OR "buy the dip" OR "short squeeze"',
    '"options flow" OR "unusual options" OR "call sweep"',
]
# The Latest tab is a firehose (~40 tweets = ~10 min); filtering out replies and
# zero-like posts makes the same ~40 tweets span hours of real discussion.
X_FILTER = "lang:en -filter:replies min_faves:3"
X_HOT_TICKERS = 16        # extra cashtag queries built from dd/rankings.json (hot in news)
X_TABS = 4                # queries run in parallel tabs (4 tabs ≈ same wall time as 1)
X_FIRST_WAIT_MS = 10000   # max wait for the first SearchTimeline page
X_SCROLL_WAIT_MS = 5000   # max wait for each scroll's next page
X_SCROLLS_PER_QUERY = 2   # each scroll pulls roughly another 20 tweets
PULSE_RANKINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rankings.json")


def _hot_cashtag_queries():
    """Cashtag OR-queries for the tickers currently hottest in the news (Pulse ranker output),
    so X search isn't limited to the same hard-coded megacaps every run."""
    try:
        rows = json.load(open(PULSE_RANKINGS))["results"]
    except Exception:
        return []
    static = {t.strip("$") for q in X_CASHTAG_QUERIES for t in q.split() if t.startswith("$")}
    rows.sort(key=lambda r: (r.get("discovery") or {}).get("news_count", 0), reverse=True)
    tickers = [r["ticker"] for r in rows if r["ticker"] not in static and r["ticker"].isalpha()][:X_HOT_TICKERS]
    return [" OR ".join(f"${t}" for t in tickers[k:k + 8]) for k in range(0, len(tickers), 8)]


def _load_x_cookies():
    """Load x_session.json cookies into a {name: value} dict, or None."""
    if not os.path.exists(X_SESSION_PATH):
        return None
    try:
        data = json.load(open(X_SESSION_PATH))
        return {c["name"]: c["value"] for c in data.get("cookies", [])}
    except Exception:
        return None


def _parse_x_graphql(data: dict) -> list:
    """Pull tweet posts out of an X GraphQL SearchTimeline JSON response."""
    posts = []
    try:
        search = (data.get("data") or {}).get("search_by_raw_query") or {}
        # Current web client: search_timeline.timeline; older layout: result.timeline
        timeline = ((search.get("search_timeline") or search.get("result") or {})
                    .get("timeline") or {})
        entries = []
        for instr in timeline.get("instructions", []):
            entries.extend(instr.get("entries", []))
        for entry in entries:
            content = entry.get("content", {})
            # Tweets live in content.itemContent; module layouts nest them in content.items
            item_list = content.get("items", []) or []
            tweet_holders = [content, content.get("itemContent", {})] + [
                it.get("item", {}).get("itemContent", {}) for it in item_list]
            for holder in tweet_holders:
                tr = holder.get("tweet_results", {}).get("result")
                if isinstance(tr, dict) and tr.get("__typename") == "TweetWithVisibilityResults":
                    tr = tr.get("tweet")
                if not isinstance(tr, dict):
                    continue
                legacy = tr.get("legacy", {})
                # note_tweet holds the untruncated text of long posts
                text = (tr.get("note_tweet", {}).get("note_tweet_results", {})
                        .get("result", {}).get("text") or
                        legacy.get("full_text") or "")
                if not text:
                    continue
                rest_id = tr.get("rest_id") or legacy.get("id_str") or ""
                if not rest_id:
                    continue
                created_utc = 0
                ca = legacy.get("created_at", "")
                if ca:
                    try:
                        created_utc = datetime.strptime(ca, "%a %b %d %H:%M:%S %z %Y").timestamp()
                    except Exception:
                        created_utc = time.time()
                user = tr.get("core", {}).get("user_results", {}).get("result", {})
                username = (user.get("core", {}).get("screen_name") or
                            user.get("legacy", {}).get("screen_name", ""))
                posts.append({
                    "id": str(rest_id),
                    "title": text,
                    "selftext": text,
                    "score": int(legacy.get("favorite_count") or 0),
                    "num_comments": int(legacy.get("reply_count") or 0),
                    "created_utc": created_utc,
                    "subreddit": None,
                    "permalink": f"https://x.com/{username}/status/{rest_id}" if username else "",
                    "source": "twitter",
                    "scraped_at": now_iso(),
                })
    except Exception:
        return posts
    return posts


async def _x_browser_search(cookies, queries):
    """Run each query on x.com/search (Latest) in headless Chrome, X_TABS at a time, and
    capture the SearchTimeline GraphQL responses. Waits on the responses themselves
    instead of fixed sleeps.

    Stops early on a 429 (X allows ~50 search requests / 15 min; a full run uses ~21).

    Returns ([(query, json_body), ...], login_redirected).
    """
    from urllib.parse import quote
    from scrapling.fetchers import AsyncStealthySession

    is_timeline = lambda r: "SearchTimeline" in r.url
    results, expired, limited = [], asyncio.Event(), {}

    # real_chrome: use the installed Chrome (throwaway profile) — patchright's bundled
    # Chromium build goes missing whenever scrapling/patchright updates.
    # disable_resources: skip images/fonts/media; the GraphQL XHRs still load.
    session = AsyncStealthySession(headless=True, real_chrome=True, disable_resources=True)
    await session.start()
    try:
        keys = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
        await session.context.add_cookies([{k: c[k] for k in keys if k in c} for c in cookies])
        sem = asyncio.Semaphore(X_TABS)

        async def grab(page, action, timeout):
            async with page.expect_response(is_timeline, timeout=timeout) as info:
                await action()
            resp = await info.value
            if resp.status == 429:  # search allows ~50 requests / 15 min per account
                limited["reset"] = resp.headers.get("x-rate-limit-reset")
                raise RuntimeError("rate limited")
            if resp.status == 200:
                results.append((query_of[page], await resp.json()))

        query_of = {}

        async def one(query):
            async with sem:
                if expired.is_set() or limited:
                    return
                page = await session.context.new_page()
                query_of[page] = query
                url = f"https://x.com/search?q={quote(f'{query} {X_FILTER}')}&src=typed_query&f=live"
                try:
                    await grab(page, lambda: page.goto(url, wait_until="domcontentloaded"), X_FIRST_WAIT_MS)
                    for _ in range(X_SCROLLS_PER_QUERY):
                        # the JSON lands before the timeline renders; scrolling an empty page loads nothing
                        await page.wait_for_selector('article[data-testid="tweet"]', timeout=X_SCROLL_WAIT_MS)
                        await page.wait_for_timeout(500)
                        # mouse.wheel no longer triggers X's next page in headless tabs; scrollTo does
                        await grab(page, lambda: page.evaluate("window.scrollTo(0, document.body.scrollHeight)"),
                                   X_SCROLL_WAIT_MS)
                except Exception:
                    if "/login" in page.url or "/i/flow/" in page.url:
                        expired.set()
                finally:
                    await page.close()

        await asyncio.gather(*(one(q) for q in queries))
    finally:
        await session.close()
    if limited:
        reset = limited.get("reset")
        wait = f", resets in {max(0, int(reset) - int(time.time())) // 60 + 1} min" if reset and reset.isdigit() else ""
        print(f"  ⚠ X search rate limit hit{wait} — keeping the {len(results)} pages already fetched")
    return results, expired.is_set()


def scrape_x_cashtags():
    """Scrape X search (Latest tab) in headless Chromium using the saved login.

    Returns (posts, active_backend) where active_backend is one of:
      'x_browser' — got live posts
      'expired'   — X redirected to login; re-run save_x_session.py
      'none'      — no saved login, browser failure, or no posts returned
    """
    cookies = _load_x_cookies()
    if not cookies or not (cookies.get("auth_token") and cookies.get("ct0")):
        print("  ⚠ No X login in x_session.json\n"
              "     Run `python save_x_session.py` and log in to X to create it.")
        return [], "none"

    print("\n[2b] X search (headless browser + x_session.json login)", flush=True)
    raw_cookies = json.load(open(X_SESSION_PATH)).get("cookies", [])
    queries = X_CASHTAG_QUERIES + _hot_cashtag_queries()
    try:
        results, login_redirected = asyncio.run(_x_browser_search(raw_cookies, queries))
    except Exception as exc:
        print(f"  ⚠ X browser search failed: {exc}")
        return [], "none"
    if login_redirected:
        print("  ✗ X redirected to login — session expired, run `python save_x_session.py`")
        return [], "expired"

    all_posts = []
    seen_ids = set()
    for query in queries:
        new_count = 0
        for q, body in results:
            if q != query:
                continue
            for p in _parse_x_graphql(body):
                if p["id"] not in seen_ids:
                    seen_ids.add(p["id"])
                    all_posts.append(p)
                    new_count += 1
        print(f"  {'✓' if new_count else '·'} X search '{query[:30]}': {new_count} posts")

    print(f"\n  X search total: {len(all_posts)} unique posts")
    return all_posts, ("x_browser" if all_posts else "none")


# ---------------------------------------------------------------------------
# Massive API fallback
# ---------------------------------------------------------------------------

def scrape_massive():
    """Fetch news articles from Massive API as fallback posts.

    Requires MASSIVE_API_KEY env var. Skips silently if not configured.
    The API key must be set in the environment; no hardcoded fallback.
    """
    if not MASSIVE_API_KEY:
        print("  -- MASSIVE_API_KEY not set — skipping Massive API fallback")
        return []

    posts = []
    try:
        req = urllib.request.Request(
            MASSIVE_URL,
            headers={"User-Agent": "ProjectSentiment/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                print(f"  ✗ Massive API returned HTTP {resp.status}")
                return posts
            data = json.loads(resp.read().decode())

        entries = data.get("data", data.get("articles", []))
        if not isinstance(entries, list):
            return posts

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            post_id = entry.get("id") or entry.get("article_id") or entry.get("url", "")
            if not post_id:
                continue

            posts.append({
                "id": str(post_id),
                "title": entry.get("title", "") or entry.get("headline", ""),
                "selftext": entry.get("body", "") or entry.get("summary", "") or entry.get("description", ""),
                "score": int(entry.get("score") or 0),
                "num_comments": int(entry.get("comments") or 0),
                "created_utc": float(entry.get("published_at") or entry.get("timestamp") or 0),
                "subreddit": None,
                "permalink": entry.get("url", "") or entry.get("source_url", ""),
                "source": "massive_news",
                "scraped_at": now_iso(),
            })

        print(f"\n  Massive API: {len(posts)} articles")
    except Exception as e:
        print(f"  ✗ Massive API failed: {e}")
    return posts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _normalize_reddit_post(entry: dict) -> dict:
    """Normalize a single Reddit post dict from any backend into the canonical shape.

    Handles field name variance between opencli and rdt-cli output formats.
    """
    post_id = (entry.get("id") or entry.get("postId") or
               entry.get("name") or entry.get("fullname", ""))
    if not post_id:
        return None

    permalink = entry.get("url") or entry.get("permalink", "")
    if permalink and not permalink.startswith("http"):
        permalink = "https://reddit.com" + permalink

    return {
        "id": str(post_id),
        "title": entry.get("title", ""),
        "selftext": entry.get("selftext", "") or entry.get("body", "") or "",
        "score": int(entry.get("score") or entry.get("upvotes") or entry.get("ups") or 0),
        "num_comments": int(entry.get("comments") or entry.get("num_comments") or 0),
        "created_utc": float(entry.get("created_utc") or entry.get("created") or 0),
        "subreddit": entry.get("subreddit", ""),
        "permalink": permalink,
        "source": "reddit",
        "scraped_at": now_iso(),
    }


def main():
    print("=" * 56, flush=True)
    print("  PULSE — SCRAPER (Reddit + X)", flush=True)
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print("=" * 56, flush=True)

    # Reddit (subprocesses) and X (headless Chrome) are independent — run them side by side.
    # X posts keep source = "twitter" so the extractor counts them as before.
    print("\n[1/2] Reddit + X in parallel")
    print("-" * 40)

    def guarded(name, fn):
        try:
            return fn()
        except Exception as e:
            print(f"⚠ {name} failed: {e}")
            return [], "error"

    with ThreadPoolExecutor(2) as ex:
        reddit_job = ex.submit(guarded, "Reddit", scrape_reddit)
        x_job = ex.submit(guarded, "X", scrape_x_cashtags)
        reddit_posts, reddit_backend = reddit_job.result()
        twitter_posts, twitter_backend = x_job.result()

    # --- Massive fallback ---
    agent_reach_total = len(reddit_posts) + len(twitter_posts)
    massive_posts = []
    massive_backend = "none"
    if agent_reach_total < 50:
        print(f"\n[2/2] Massive API fallback ({agent_reach_total} Reddit + X posts < 50)")
        print("-" * 40)
        try:
            massive_posts = scrape_massive()
            if massive_posts:
                massive_backend = "massive_api"
        except Exception as e:
            print(f"⚠ Massive API failed: {e}")
            massive_posts = []

    # --- Combine & deduplicate ---
    all_posts = reddit_posts + twitter_posts + massive_posts
    seen = set()
    deduped = []
    for post in all_posts:
        if post["id"] not in seen:
            seen.add(post["id"])
            deduped.append(post)
    all_posts = deduped

    # --- Check for total failure ---
    reddit_count = len(reddit_posts)
    twitter_count = len(twitter_posts)
    massive_count = len(massive_posts)

    if reddit_count == 0 and twitter_count == 0 and massive_count == 0:
        print("\n✗ All sources failed — no data collected.")
        sys.exit(1)

    # --- Build output ---
    sources = {}
    if reddit_count:
        sources["reddit"] = reddit_count
    if twitter_count:
        sources["twitter"] = twitter_count
    if massive_count:
        sources["massive_news"] = massive_count

    output = {
        "scraped_at": now_iso(),
        "total_posts": len(all_posts),
        "sources": sources,
        "posts": all_posts,
        "active_backend": {
            "reddit": reddit_backend,
            "twitter": twitter_backend,
            "massive": massive_backend,
        },
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # --- Summary ---
    print("\n" + "=" * 56)
    print("  SCRAPE SUMMARY")
    print("=" * 56)
    print(f"  Reddit       : {reddit_count}")
    print(f"  Twitter      : {twitter_count}")
    print(f"  Massive News : {massive_count}")
    print(f"  Total        : {len(all_posts)}")
    print("=" * 56)
    print(f"\n  ✓ Saved → {OUTPUT_FILE}")
    print("\n✅ Scraper complete.")
    print("   Next step: python ticker_extractor.py\n")


if __name__ == "__main__":
    main()
