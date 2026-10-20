# Pulse

Hunts small and niche companies that are quietly dominating — before everyone knows the name.
Not "MU has crazy revenue"; the point is finding the next one while it's $500M, not $500B.

```
hunt      Yahoo screen: every US-listed $50M–$20B stock with quarterly revenue +25%,
          pre-ranked on trend, extension, size and improving EPS → top 60 researched
heat      names hot in news (Massive, Yahoo), Reddit/X cashtags (scraper.py), small-cap gainers
          → in-band names only (megacaps dropped) → top 50 researched
gate      hard rejects: OTC, < $1, < $1M/day traded, < $10M revenue, shares +30%/yr, reverse split
          (18m), < 4 quarters of cash, pump pattern (+80%/5d on 5x volume with thin fundamentals),
          SEC delisting notice / late filing
score     6 pillars on fixed 0–100 scales → composite − 7 per red flag → BUY / WATCH / AVOID
read      news + social tone (VADER), IWM/SPY/QQQ/VIX backdrop, sector heat, written summary
```

| Pillar | Weight | What it rewards |
|---|---|---|
| Growth | 25% | revenue growth, latest-quarter YoY, acceleration, gross-margin expansion, forward estimates |
| Quality | 18% | margins and their trend, FCF, cash runway, no dilution, low leverage |
| Estimates | 14% | net EPS revisions, estimate drift, surprises, beat rate, target upside |
| Valuation | 10% | EV/sales per point of growth, forward P/E, PEG |
| Momentum | 18% | 3m/6m vs SPY, 1m, off 52w high, above 50/200d |
| Early | 15% | smaller cap, few analysts, institutions near 40% (not crowded), not extended, 1y not parabolic |

Red flags (−7 each, 2+ = AVOID): dilution > 15%, recent offering/shelf (SEC), short float > 25%, +50% in 5d,
press-release-heavy news, social hype with no independent coverage, estimate cuts, broken chart, D/E > 3.

News and social volume decide what gets looked at and feed the pump checks. They never add to the score.

## Files

| File | What it does |
|---|---|
| `ranker.py` | discover → gate → research → score → `rankings.json` |
| `server.py` | dashboard on :8790, re-ranks every 60 min |
| `dashboard.html` | the UI (aero-shards hero with paper-airplane picks, dither background) |
| `scraper.py` | Reddit (rdt-cli) + X (headless Chrome) → `raw_data.json`, ~13s |
| `save_x_session.py` | log in to X once; saves `x_session.json` (gitignored) |
| `valid_tickers.txt` | listed US tickers used to validate cashtags |

Setup: `pip install -r requirements.txt`, install the `rdt` CLI (rdt-cli) for Reddit and have Google Chrome
installed for X. Run `python save_x_session.py` once to log in to X.

## Run

```bash
python ranker.py      # one pass → rankings.json (re-runs scraper.py if its data is > 50 min old)
python server.py      # dashboard at http://127.0.0.1:8790, re-ranks every 60 min
```

Always on: `com.rytty.pulse.plist` → `~/Library/LaunchAgents/`, then `launchctl load` it. Logs: `~/Library/Logs/pulse.log`.

Keys in `.env` (gitignored): `MASSIVE_API_KEY`, `FINNHUB_API_KEY`. Optional `SEC_USER_AGENT="name email"`
turns on SEC EDGAR checks (offerings, delisting notices, late filings, insider filings); set it with
`launchctl setenv` so it never lands in a file. Tunables: `PULSE_MIN_MCAP`, `PULSE_MAX_MCAP`,
`PULSE_MIN_DOLLAR_VOL`, `PULSE_MIN_REVENUE`, `PULSE_HUNT_DEEP`, `PULSE_HEAT_DEEP`, `PULSE_INTERVAL_MIN`, `PULSE_SCRAPE=0`.

A rules-based screen, not a backtested edge and not investment advice.
