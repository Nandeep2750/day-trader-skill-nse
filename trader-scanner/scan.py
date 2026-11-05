#!/usr/bin/env python3
"""Chart Scanner for trader-scanner: scan NSE charts for setups. Standard library only.

    python3 scan.py universe                          # NSE equity list -> trader/universe.txt
    python3 scan.py universe --index nifty500         # or just the NIFTY 500
    python3 scan.py daily                             # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --symbols RELIANCE,INFY,M&M # or --watchlist FILE, or --limit 200
    python3 scan.py orb                               # after 9:35 AM IST: opening-range breakouts so far
    python3 scan.py orb --at 09:45 --or-minutes 15    # cut the clock at 9:45 AM IST
    python3 scan.py orb --offline wall5m.json.gz --now "2026-10-29 09:45" --min-price 0
    python3 scan.py volatile --index nifty500 --top 12 # after the close: the most volatile liquid stocks

universe  Downloads NSE's list of listed equities (EQUITY_L.csv from
          nsearchives.nseindia.com) and keeps the EQ series only: BE and BZ
          are trade-to-trade (every trade is for delivery, no intraday), and
          anything that is not a plain NSE symbol is out. --index nifty500
          reads NSE's NIFTY 500 constituents (ind_nifty500list.csv) instead,
          with the same EQ rule. One SYMBOL|Name a line.

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only). Liquidity from the spark meta:
          regularMarketPrice >= --min-price (Rs 50) and regularMarketVolume >=
          --min-volume (5,00,000 shares). Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the 3:30 PM close. (NSE's official
          close, the weighted average of the last 30 minutes, can still move
          Yahoo's bar a little for a few minutes after that.)

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session: 9:15 to 9:30. Stage 1: spark closes
          for every symbol (1 day of 5-minute bars, 20 a request); a chart goes
          on only if a completed bar after the range closed at or above the
          highest close of the range (a necessary condition). Stage 2: each
          candidate's v8 chart (real highs and lows): range high = the highest
          high of the range bars, a hit = the first completed bar to CLOSE above
          it, at that bar's close time. Stage 2 fetches at most --max-confirm
          (200) charts, the most traded candidates first (price x volume so
          far), and says so when it stops short. A v8 chart is usable only when
          every 5-minute bar from 9:15 to the cutoff is there (a missing bar is
          a halt or no trades), up to 3:15 PM: Yahoo's 3:20 PM NSE bar is always
          empty and the 3:15 and 3:25 bars (the closing session) are often thin
          or missing, so those three are never required. Spark leaves out a bar
          whose close did not change, so a closes-only chart only needs its
          9:15 bar; a gap there is the last close carried forward.
          Floors: --min-price 50 (last close, rupees), --min-volume 0.

volatile  The most volatile stocks that are liquid enough to day trade, for
          tomorrow's watchlist. Stage 1: Yahoo spark, 1 month of daily closes,
          20 symbols a request: the average absolute close-to-close move over
          the last 14 completed sessions, the latest session's move, and its
          turnover (price x volume from the spark meta, in crore). Floors:
          --min-price 50 and --min-turnover-cr 100. Stage 2: the --max-confirm
          (30) biggest average movers get a 1-month daily v8 chart (real highs
          and lows): ATR = the SIMPLE average of the last 14 true ranges (not
          Wilder's smoothing), shown as a % of the last completed close; the
          latest session's high-to-low range %; average turnover over the last
          20 completed sessions, floor --min-avg-turnover-cr 150. Ranked by
          ATR %, --top 15 kept and written. A session move of 5 % or more gets
          "check news". --index all|nifty500 scans NSE's list instead of the
          universe file. While the session is open, today's bar is left out of
          the ATR and the averages, and its move and range are labelled "so
          far"; after the 3:30 PM close it counts as a completed session.

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick,
like 10:49:45) is dropped, and a bar counts only once it is complete
(start + 5 minutes <= the clock). Times are India Standard Time (IST, UTC+5:30,
no daylight saving). --now pins the clock ("2026-10-29 09:45", IST; ...Z for UTC).
Symbols are plain NSE symbols (RELIANCE, M&M, BAJAJ-AUTO); a trailing .NS is
dropped and a BSE .BO symbol is refused. Yahoo is asked for SYMBOL.NS.

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on
a 429 or 5xx, one retry after a pause on a 429. A polite pause between
requests (--pause). Partial results are reported as partial.

--offline PATH reads files instead of the network: universe wants a folder
with EQUITY_L.csv (or ind_nifty500list.csv with --index nifty500); daily and
orb take a folder of <SYMBOL>.json v8 chart files or spark responses, or one
file that is a spark response, a v8 chart, or a bundle {"RELIANCE": <v8 json>,
...}. .json.gz is fine.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
"""

import argparse
import csv
import datetime
import gzip
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill-nse/1.0)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
NSE_URL = "https://nsearchives.nseindia.com/content/"
NSE_LISTS = {  # --index -> (file name, path under NSE_URL)
    "all": ("EQUITY_L.csv", "equities/EQUITY_L.csv"),
    "nifty500": ("ind_nifty500list.csv", "indices/ind_nifty500list.csv"),
}
# The F&O underlyings, one row a symbol. Needed because a scrip's close and its broker
# square-off now depend on whether it has F&O contracts, and because SEBI's short selling
# framework (Annexure-1, clause 7) makes only F&O scrips eligible for short selling.
FO_LOTS = ("fo_mktlots.csv", "fo/fo_mktlots.csv")
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
VOL_SESSIONS = 14       # volatile: average move and ATR over the last 14 completed sessions
TURNOVER_SESSIONS = 20  # volatile: average daily turnover over the last 20 completed sessions
VOL_CONFIRM = 30        # volatile: stage-2 v8 charts, the biggest average movers first
BIG_MOVE = 5.0          # volatile: a session move this size (%) gets a "check news" note
SPLIT_RATIOS = (1.5, 2, 2.5, 3, 4, 5, 10)  # volatile: previous close / close of a split or bonus
SPLIT_TOL = 0.03        # volatile: within 3 % of one of those ratios is an unadjusted split / bonus
SESSION_MIN = 375       # NSE regular session, 9:15 to 15:30, in minutes
MIN_TURNOVER_SESSIONS = 10  # volatile: sessions with a volume needed for the average turnover
VOL_CHART_RANGE = "2mo"     # volatile stage 2: enough daily bars for 20 sessions of turnover
DEFAULT_UNIVERSE = "trader/universe.txt"
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

NSE_HOLIDAYS = {  # NSE equity segment trading holidays, 2026 (weekdays only)
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
# Special sessions (Muhurat trading on Sunday 2026-11-08, budget-day Sundays) are not
# modelled: weekends stay closed.
CALENDAR_YEARS = (2026,)   # NSE publishes next year's list in December: add 2027 then
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
REQUIRED_UNTIL = datetime.time(15, 15)  # the 3:15 / 3:20 / 3:25 bars are never required (see orb)

# SEBI's Closing Auction Session (CAS) moved the end of continuous trading, and moves it
# again. From 2026-08-03 an F&O-eligible scrip stops continuous trading at 15:15 and the
# auction runs after it, while every other scrip still runs to 15:30. From 2026-12-07 (the
# SEBI circular of 2026-10-15) continuous trading runs to 15:30 for every scrip and the
# auction moves to 15:30 to 15:40. An order is only ever placed in continuous trading, so
# that is the close session() reports.
CAS_FNO_FROM = datetime.date(2026, 8, 3)       # F&O scrips' continuous close moved to 15:15
CAS_ALL_DAY_FROM = datetime.date(2026, 12, 7)  # continuous close back to 15:30 for every scrip
CAS_CLOSE = datetime.time(15, 15)

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time

IST = datetime.timedelta(hours=5, minutes=30)   # India Standard Time, no daylight saving


def to_ist(utc):
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST


def ts_to_ist(ts):
    """Unix seconds -> naive IST. Never uses the machine's time zone."""
    return to_ist(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts)))


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ScanError('--now wants a time like "2026-10-29 09:45" (IST) or ...Z (UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants an IST time like 09:45" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def continuous_close(d, fno=False):
    """When continuous trading ends for a scrip on date d (IST). An F&O-eligible scrip inside
    the first CAS window closes at 15:15; everything else, and every scrip once the revised
    framework starts, closes at 15:30."""
    if fno and CAS_FNO_FROM <= d < CAS_ALL_DAY_FROM:
        return CAS_CLOSE
    return CLOSE


def session(d, fno=False):
    """(open_time, close_time) of continuous trading in IST, or None if closed all day.
    Pass fno=True for a scrip with F&O contracts: its close can be earlier (see CAS above)."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, continuous_close(d, fno)


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


NSE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")


def nse_symbol(text):
    """'reliance.ns' -> 'RELIANCE'. A BSE .BO symbol or anything that is not an NSE symbol is refused."""
    s = (text or "").strip().upper()
    if s.endswith(".BO"):
        raise ScanError("%s is a BSE symbol: this pack trades NSE only (use %s)" % (s, s[:-3]))
    if s.endswith(".NS"):
        s = s[:-3]
    if not NSE_SYMBOL.match(s):
        raise ScanError("%r is not an NSE symbol (like RELIANCE, M&M or BAJAJ-AUTO)" % text)
    return s


def to_yahoo(sym):
    """NSE symbol -> Yahoo's, unquoted: M&M -> M&M.NS."""
    return sym.upper() + ".NS"


def from_yahoo(ysym):
    """Yahoo's symbol -> ours: M&M.NS -> M&M."""
    s = (ysym or "").upper()
    return s[:-3] if s.endswith(".NS") else s


def _q(ysym):
    return urllib.parse.quote(ysym, safe="")


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(_q(to_yahoo(s)) for s in symbols), rng, interval)


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (_q(to_yahoo(sym)), rng, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def parse_spark(text_or_doc, wanted=None):
    """Spark JSON -> {SYM: series}. Symbols Yahoo does not know are simply missing
    from the answer. `wanted` maps Yahoo symbols back to ours (M&M.NS -> M&M)."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        sym = (wanted or {}).get(ysym.upper(), from_yahoo(ysym))
        out[sym] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = name_hint
        if not sym:
            try:
                sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <SYMBOL>.json")
        sym = from_yahoo(sym)
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path):
    """A folder of <T>.json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc):
                _absorb(doc, "" if "spark" in doc else _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

def parse_nse_list(text):
    """NSE's EQUITY_L.csv or ind_nifty500list.csv -> rows as dicts with clean keys:
    symbol, name, series. NSE's EQUITY_L header has spaces after the commas."""
    text = text.lstrip("\ufeff")
    rows = list(csv.reader(text.replace("\r", "").split("\n")))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ScanError("not an NSE equity list (the file is empty)")
    hdr = [h.strip().upper() for h in rows[0]]
    names = {"SYMBOL": "symbol", "NAME OF COMPANY": "name", "COMPANY NAME": "name", "SERIES": "series"}
    if not {"SYMBOL", "SERIES"} <= set(hdr) or not ({"NAME OF COMPANY", "COMPANY NAME"} & set(hdr)):
        raise ScanError("not an NSE equity list (header %s)" % ",".join(rows[0])[:60])
    out = []
    for r in rows[1:]:
        d = {}
        for h, v in zip(hdr, r):
            if h in names:
                d[names[h]] = " ".join(v.split())
        out.append(d)
    return out


def classify(row):
    """One NSE list row -> 'keep' or the rule that drops it."""
    sym = (row.get("symbol") or "").strip().upper()
    if not NSE_SYMBOL.match(sym):
        return "not a plain NSE symbol"
    series = (row.get("series") or "").strip().upper()
    if series in ("BE", "BZ"):
        return "series %s (trade-to-trade, no intraday)" % series
    if series != "EQ":
        return "series %s" % (series or "missing")
    return "keep"


def build_universe(text):
    """CSV text -> (kept [(SYM, name)], counts)."""
    kept, counts = {}, {}
    rows = parse_nse_list(text)
    counts["rows"] = len(rows)
    for r in rows:
        why = classify(r)
        counts[why] = counts.get(why, 0) + 1
        if why == "keep":
            kept[r["symbol"].upper()] = r.get("name") or ""
    return sorted(kept.items()), counts


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes (the same math as the reel's refs/scan.py)."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes, anything dated after the clock dropped."""
    now = to_ist(now_utc)
    today = now.date()
    s = session(today)
    today_done = s is None or now.time() >= s[1]
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = ts_to_ist(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = float(c)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_price, min_volume):
    """Pure. data = {SYM: series}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_closes(s, now_utc)
        meta = s.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        vol = meta.get("regularMarketVolume")
        if price is None or price < min_price or (min_volume > 0 and (vol is None or vol < min_volume)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "setups": tags, "price": round(closes[-1], 2),
                         "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                         "volume": int(vol) if vol is not None else None})
    hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))  # most rupees traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- volatility

def _pos(x):
    """A price that can be used: a finite positive number, else None (Yahoo sends 0, NaN or junk at times)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def _vol(x):
    """A volume that can be used: a finite number >= 0, else None."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x >= 0 else None


def split_like(prev, close):
    """Does prev -> close look like an unadjusted split or bonus? True when prev / close
    is within SPLIT_TOL of a SPLIT_RATIOS ratio (2 = 1:1 bonus or 2-for-1 split, 1.5 =
    1:2 bonus, 10 = 10-for-1 split ...). A genuine 25 % move (1.33) is not."""
    r = prev / close
    return any(abs(r / k - 1) <= SPLIT_TOL for k in SPLIT_RATIOS)


def day_bars(series, now_utc, ohlc=False):
    """Daily bars through the clock, one per NSE trading day (a late duplicate wins),
    as dicts d/h/l/c/v. Dropped: a bar whose close is missing or not positive, a bar
    dated on a weekend or NSE holiday (a Muhurat or other special session is not a
    regular session), and with ohlc a bar whose high or low is missing or not
    positive. A missing volume stays None. Returns (completed, live): live is today's
    bar while the session is still open (its numbers are so far), else None."""
    now = to_ist(now_utc)
    today = now.date()
    by_day = {}
    hs, ls, vs = series.get("h"), series.get("l"), series.get("v")
    for i, (ts, c) in enumerate(zip(series["t"], series["c"])):
        c = _pos(c)
        if c is None:
            continue
        d = ts_to_ist(ts).date()
        if d > today or not trading_day(d)[0]:
            continue
        h = _pos(hs[i]) if hs else None
        lo = _pos(ls[i]) if ls else None
        if ohlc and (h is None or lo is None):
            continue
        by_day[d] = {"d": d, "c": c, "h": h, "l": lo, "v": _vol(vs[i]) if vs else None}
    s = session(today)
    if s is not None and now.time() < s[0]:
        by_day.pop(today, None)   # before the open there is no today's bar yet (a replay must not see one)
    bars = [by_day[d] for d in sorted(by_day)]
    if bars and bars[-1]["d"] == today and s is not None and now.time() < s[1]:
        return bars[:-1], bars[-1]
    return bars, None


def move_stats(series, now_utc):
    """Stage 1, closes only -> (dict, None) or (None, why). avg_move_pct is the mean
    absolute close-to-close move of the last VOL_SESSIONS completed sessions; day_pct
    is the latest session's move (so far, while it is live). The meta price and volume
    are used only when the meta's regularMarketTime is on that same IST day (a --now
    replay, or a meta from a special session, would mix days): otherwise price is the
    day's close and its turnover is unknown (None). jump: a step in the window that
    looks like an unadjusted split or bonus (split_like)."""
    done, live = day_bars(series, now_utc)
    closes = [b["c"] for b in done]
    if len(closes) < VOL_SESSIONS + 1:
        return None, "short"
    day = (live or done[-1])["d"]
    meta = series.get("meta") or {}
    mt = meta.get("regularMarketTime")
    same_day = isinstance(mt, (int, float)) and math.isfinite(mt) and ts_to_ist(mt).date() == day
    price = (_pos(meta.get("regularMarketPrice")) if same_day else None) or (live or done[-1])["c"]
    vol = _vol(meta.get("regularMarketVolume")) if same_day else None
    if live is not None:
        day_pct = (price / closes[-1] - 1) * 100
        steps = [(closes[-1], price)]
    else:
        day_pct = (closes[-1] / closes[-2] - 1) * 100
        steps = []
    idx = range(len(closes) - VOL_SESSIONS, len(closes))
    moves = [abs(closes[i] / closes[i - 1] - 1) * 100 for i in idx]
    steps += [(closes[i - 1], closes[i]) for i in idx]
    return {"price": price, "day": day, "day_complete": live is None, "day_pct": day_pct,
            "avg_move_pct": sum(moves) / len(moves), "jump": any(split_like(p, c) for p, c in steps),
            "turnover_cr": (price * vol / 1e7) if vol is not None else None}, None


def atr_stats(series, now_utc, day=None):
    """Stage 2, a v8 daily chart -> (dict, None) or (None, why). ATR is the SIMPLE
    average of the last VOL_SESSIONS true ranges of completed sessions (not Wilder's
    smoothing), as a % of the last completed close. day_range_pct is the high to low
    of the session dated `day` (stage 1's latest session; so far while it is live),
    None when the chart has no usable bar for that day. avg_turnover_cr is close x
    volume over the last TURNOVER_SESSIONS completed sessions that have a volume
    (at least MIN_TURNOVER_SESSIONS of them). jump: a close-to-close step in the ATR
    window that looks like an unadjusted split or bonus (split_like); a real crash
    (CARTRADE 2026-10-28: -12.6 %, true range 21.7 %) is not one."""
    done, live = day_bars(series, now_utc, ohlc=True)
    if len(done) < VOL_SESSIONS + 1:
        return None, "under %d sessions with highs and lows" % (VOL_SESSIONS + 1)
    win = list(zip(done[-VOL_SESSIONS - 1:-1], done[-VOL_SESSIONS:]))
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in win]
    jump = any(split_like(p["c"], b["c"]) for p, b in win)
    atr = sum(trs) / len(trs)
    close = done[-1]["c"]
    last = live or done[-1]
    rng = (last["h"] - last["l"]) / last["c"] * 100 if day is None or last["d"] == day else None
    tv = [b["c"] * b["v"] for b in done[-TURNOVER_SESSIONS:] if b["v"] is not None]
    if len(tv) < MIN_TURNOVER_SESSIONS:
        return None, "under %d sessions with a volume" % MIN_TURNOVER_SESSIONS
    return {"atr": atr, "atr_pct": atr / close * 100, "close": close, "day_range_pct": rng, "jump": jump,
            "avg_turnover_cr": sum(tv) / len(tv) / 1e7, "turnover_sessions": len(tv)}, None


def scan_volatile(data, symbols, now_utc, min_price, min_turnover_cr, min_avg_turnover_cr, max_confirm,
                  confirm=None, top=15):
    """Pure. data = {SYM: series} (spark or v8). confirm(sym) -> a v8 daily series for
    stage 2; None means use data[sym] when it has highs and lows. max_confirm and top:
    0 means all. While the latest session is still open the stage-1 turnover floor is
    pro-rated by the elapsed part of the session (at least a tenth); a symbol whose day
    turnover is unknown skips it. The stage-2 20-session average floor always applies.
    Stage 2 confirms in batches of max_confirm, biggest close-to-close movers first,
    until `top` liquid hits are found, the candidates run out, or 3 x max_confirm
    charts were sent. Returns (hits, counts, failed)."""
    if max_confirm < 0 or top < 0:
        raise ScanError("max_confirm and top take 0 (all) or a positive number")
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "stale": 0, "jump": 0,
              "scanned": 0, "confirmed": 0, "thin": 0, "hits": 0, "floor_fraction": 1.0,
              "turnover_unknown": 0}
    now = to_ist(now_utc)
    elapsed = (now - datetime.datetime.combine(now.date(), OPEN)).total_seconds() / 60.0
    frac = max(0.1, min(1.0, elapsed / SESSION_MIN))
    failed, stats, jumps = {}, [], []
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        st, why = move_stats(s, now_utc)
        if st is None:
            counts["short"] += 1
            continue
        stats.append(dict(st, symbol=sym))
    latest = max((st["day"] for st in stats), default=None)
    rows = []
    for st in stats:
        if st["day"] < latest:  # no bar for the latest session: suspended, delisted or stale data
            counts["stale"] += 1
            continue
        if st["price"] < min_price:
            counts["illiquid"] += 1
            continue
        if st["turnover_cr"] is None:  # meta from another day: the stage-2 average floor decides
            counts["turnover_unknown"] += 1
        else:
            floor = min_turnover_cr
            if not st["day_complete"]:
                floor *= frac
                counts["floor_fraction"] = frac
            if st["turnover_cr"] < floor:
                counts["illiquid"] += 1
                continue
        if st["jump"]:
            counts["jump"] += 1
            jumps.append(st["symbol"])
            continue
        counts["scanned"] += 1
        rows.append(st)
    rows.sort(key=lambda r: (-r["avg_move_pct"], r["symbol"]))
    batch = max_confirm or len(rows)
    cap = min(len(rows), 3 * max_confirm) if max_confirm else len(rows)
    hits, sent, batches = [], 0, 0
    while sent < cap and (not top or len(hits) < top):
        chunk = rows[sent:min(sent + batch, cap)]
        sent += len(chunk)
        batches += 1
        hits.extend(_confirm_batch(chunk, data, now_utc, confirm, min_avg_turnover_cr, counts, failed, jumps))
    hits.sort(key=lambda h: (-h["atr_pct"], h["symbol"]))
    counts["sent"], counts["batches"] = sent, batches
    counts["unconfirmed"] = len(rows) - sent
    counts["ranked"] = len(hits)
    counts["jumps"] = sorted(jumps)
    counts["latest"] = latest.isoformat() if latest else None
    hits = hits[:top] if top else hits
    counts["hits"] = len(hits)
    return hits, counts, failed


def _confirm_batch(rows, data, now_utc, confirm, min_avg_turnover_cr, counts, failed, jumps):
    """Stage 2 for one batch of stage-1 rows -> hits (unsorted). Updates counts, failed, jumps."""
    hits = []
    for r in rows:
        sym = r["symbol"]
        try:
            ser = confirm(sym) if confirm else data.get(sym)
        except ScanError as e:
            failed[sym] = str(e)
            continue
        if not ser or not ser.get("h"):
            failed[sym] = "no v8 chart with highs and lows"
            continue
        a, why = atr_stats(ser, now_utc, r["day"])
        if a is None:
            failed[sym] = why
            continue
        if a["jump"]:
            counts["jump"] += 1
            jumps.append(sym)
            continue
        counts["confirmed"] += 1
        if a["avg_turnover_cr"] < min_avg_turnover_cr:
            counts["thin"] += 1
            continue
        hits.append({"symbol": sym, "price": round(r["price"], 2), "atr": round(a["atr"], 2),
                     "atr_pct": round(a["atr_pct"], 2), "avg_move_pct": round(r["avg_move_pct"], 2),
                     "day": r["day"].isoformat(), "day_complete": r["day_complete"],
                     "day_pct": round(r["day_pct"], 2),
                     "day_range_pct": None if a["day_range_pct"] is None else round(a["day_range_pct"], 2),
                     "turnover_cr": None if r["turnover_cr"] is None else round(r["turnover_cr"], 1),
                     "avg_turnover_cr": round(a["avg_turnover_cr"], 1), "turnover_sessions": a["turnover_sessions"],
                     "note": "check news" if abs(r["day_pct"]) >= BIG_MOVE else ""})
    return hits


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    return datetime.datetime.combine(day, OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff):
    """5-minute bars of `day` that are complete at `cutoff` (naive IST),
    on the grid, with a close. Sorted, one per slot."""
    s = session(day)
    close_t = s[1] if s else CLOSE
    out = {}
    hs, ls = series.get("h"), series.get("l")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        it = ts_to_ist(ts)
        if it.date() != day or it.second or it.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if it.time() < OPEN or it.time() >= close_t:
            continue  # the pre-open, or after the 3:30 PM close
        if it + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        v = series.get("v")
        out[it] = {"start": it, "c": float(c),
                   "h": None if not hs or hs[i] is None else float(hs[i]),
                   "l": None if not ls or ls[i] is None else float(ls[i]),
                   "v": (v[i] or 0) if v else 0}
    return [out[k] for k in sorted(out)]


def complete_slots(day, cutoff):
    """How many 5-minute slots from 9:15 are complete at `cutoff` (no more than the session has)."""
    want = int((cutoff - slot(day, 0)).total_seconds() // (BAR_MIN * 60))
    s = session(day)
    if s:
        want = min(want, int((datetime.datetime.combine(day, s[1]) - slot(day, 0)).total_seconds()
                             // (BAR_MIN * 60)))
    return max(want, 0)


def fill_closes(bars, day, cutoff):
    """Closes-only (spark) bars -> one bar per complete slot from 9:15. Spark leaves out a
    bar whose close did not change, so a gap is the last close carried forward. Without the
    9:15 bar there is nothing to carry: the bars come back as they were (and are unusable)."""
    if not bars or bars[0]["start"] != slot(day, 0):
        return bars
    by = dict((b["start"], b) for b in bars)
    out = []
    for k in range(complete_slots(day, cutoff)):
        b = by.get(slot(day, k))
        if b is None:
            b = {"start": slot(day, k), "c": out[-1]["c"], "h": None, "l": None, "v": 0}
        out.append(b)
    return out


def usable(bars, day, cutoff, n_range):
    """Every slot from 9:15 to the cutoff is there, and at least the range. Slots from
    3:15 PM on are never required: on NSE days Yahoo's 3:20 bar is always empty and the
    3:15 and 3:25 bars (the closing session) are often thin or missing."""
    want = complete_slots(day, cutoff)
    if want < n_range:
        return False
    need = min(want, int((datetime.datetime.combine(day, REQUIRED_UNTIL) - slot(day, 0)).total_seconds()
                         // (BAR_MIN * 60)))
    have = set(b["start"] for b in bars)
    return all(slot(day, k) in have for k in range(need))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": clock_label(closes_at),
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    now = to_ist(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = trading_day(day)
    if not ok:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    s = session(day)
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, s[1])
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s IST on %s. Replay the session with --at 09:45."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s IST: the %d-minute opening range runs 9:15 to %s and the first bar after it "
                        "closes at %s. Run orb again after %s IST."
                        % (clock_label(cutoff), or_minutes, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s IST, or replay "
                        "today with --at 09:45." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                      clock_label(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0, confirm=None,
             max_confirm=None):
    """stage1 = {SYM: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (price x volume so far) are confirmed; the rest are counted as unconfirmed.
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff)
        if s.get("h") is None:
            bars = fill_closes(bars, day, cutoff)  # spark: closes only, unchanged closes left out
        if not usable(bars, day, cutoff, n_range):
            counts["unusable"] += 1
            continue
        vol = sum(b["v"] for b in bars) if s.get("v") else (s.get("meta") or {}).get("regularMarketVolume") or 0
        if bars[-1]["c"] < min_price or vol < min_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            cands.append((sym, bars, bars[-1]["c"] * vol))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            full = session_bars(s2, day, cutoff)
            if not usable(full, day, cutoff, n_range):
                failed[sym] = "the v8 chart has a missing bar"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            hits.append(dict({"symbol": sym, "setups": ["orb"]}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None):
    """Batches of 20. -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        wanted = {to_yahoo(s): s for s in batch}
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)), wanted)
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [nse_symbol(s) for s in re.split(r"[,\s]+", a.symbols) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [nse_symbol(str(x.get("symbol") if isinstance(x, dict) else x)) for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(nse_symbol(ln.split("|")[0]))
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def group_in(n):
    """Indian digit grouping: 1234567 -> '12,34,567'."""
    s = str(int(n))
    neg, s = (s[0] == "-"), s.lstrip("-")
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ("-" if neg else "") + ",".join(parts + [tail])


def inr(x, paise=True):
    """1174.3 -> '\u20b91,174.30'; 100000 -> '\u20b91,00,000.00' (Indian grouping)."""
    x = round(float(x), 2)
    whole = int(abs(x))
    p = int(round((abs(x) - whole) * 100))
    if p == 100:
        whole, p = whole + 1, 0
    return "%s\u20b9%s%s" % ("-" if x < 0 else "", group_in(whole), (".%02d" % p) if paise else "")


# ---------------------------------------------------------------- commands

def nse_list_text(index, offline_dir=None):
    """The NSE list for --index (all or nifty500): from offline_dir if given, else NSE's archive."""
    fname, rel = NSE_LISTS[index]
    if offline_dir:
        p = os.path.join(offline_dir, fname)
        if not os.path.exists(p):
            raise ScanError("no %s in %s" % (fname, offline_dir))
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    try:
        return http_get(NSE_URL + rel, timeout=60)
    except urllib.error.HTTPError as e:
        raise ScanError("nsearchives.nseindia.com answered HTTP %d for %s" % (e.code, fname))
    except (urllib.error.URLError, OSError) as e:
        raise ScanError("could not reach nsearchives.nseindia.com: %s" % getattr(e, "reason", e))


def fno_symbols(offline_dir=None):
    """The NSE symbols that have F&O contracts, as a frozenset, from fo_mktlots.csv.

    The file is a CSV whose second column is the symbol. Index underlyings (NIFTY,
    BANKNIFTY) come along harmlessly: no equity scrip shares their name."""
    fname, rel = FO_LOTS
    if offline_dir:
        path = os.path.join(offline_dir, fname)
        if not os.path.exists(path):
            raise ScanError("no %s in %s" % (fname, offline_dir))
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    else:
        try:
            text = http_get(NSE_URL + rel, timeout=60)
        except urllib.error.HTTPError as e:
            raise ScanError("nsearchives.nseindia.com answered HTTP %d for %s" % (e.code, fname))
        except (urllib.error.URLError, OSError) as e:
            raise ScanError("could not reach nsearchives.nseindia.com: %s" % getattr(e, "reason", e))
    return fno_symbols_from(text, fname)


def fno_symbols_from(text, fname=FO_LOTS[0]):
    """Parse fo_mktlots.csv text into a frozenset of symbols."""
    out = set()
    for line in text.splitlines()[1:]:
        cols = line.split(",")
        if len(cols) < 2:
            continue
        sym = cols[1].strip().upper()
        if sym and sym != "SYMBOL":
            out.add(sym)
    if not out:
        raise ScanError("%s held no symbols: the file's shape changed" % fname)
    return frozenset(out)


def cmd_universe(a):
    t0 = time.time()
    fname = NSE_LISTS[a.index][0]
    text = nse_list_text(a.index, a.offline)
    kept, counts = build_universe(text)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, name in kept:
                fh.write("%s|%s\n" % (sym, name))
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
    what = "NIFTY 500 constituents" if a.index == "nifty500" else "NSE equity list"
    if a.json:
        print(json.dumps({"count": len(kept), "rows": counts.get("rows", 0), "index": a.index, "file": fname,
                          "dropped": dropped, "out": None if a.no_write else a.out,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("%s (%s): %s rows." % (what, fname, "{:,}".format(counts.get("rows", 0))))
        print("Kept %s NSE stocks in the EQ series." % "{:,}".format(len(kept)))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k)
                                          for k, v in sorted(dropped.items(), key=lambda kv: (-kv[1], kv[0]))) + ".")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name a line)." % a.out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching daily closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_volume)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "source": "yahoo", "filters": {"min_price": a.min_price, "min_volume": a.min_volume},
           "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    wrote = not a.no_write and counts["fetched"] > 0  # nothing fetched: keep the last watchlist
    if wrote:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    local = to_ist(now)
    print("Daily scan, %s IST, closes through %s (Yahoo spark, daily closes only)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (\u20b9%s+ and %s+ shares)%s; "
          "%s scanned." % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
                           (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
                           "{:,}".format(counts["fetched"] - counts["illiquid"]), _n(a.min_price),
                           group_in(a.min_volume),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           "{:,}".format(counts["scanned"])))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (price x volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-10s %12s  RSI %5.1f  %s" % (h["symbol"], inr(h["price"]), h["rsi"],
                                              ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if wrote:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    elif not a.no_write:
        print("Nothing was fetched, so %s was left as it was." % a.out)
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL.")
    return 0 if hits else 1


def _yahoo_doc(doc):
    """Is this one Yahoo answer (spark or v8 chart), by its shape, not just its key?"""
    if not isinstance(doc, dict):
        return False
    for key in ("spark", "chart"):
        inner = doc.get(key)
        if isinstance(inner, dict) and ("result" in inner or "error" in inner):
            return True
    return False


def _merge_ohlc(data, errors, got, errs):
    """Merge one loaded doc into data: a v8 chart (highs and lows) wins over a spark entry."""
    errors.update(errs)
    for sym, ser in got.items():
        if sym not in data or (ser.get("h") and not data[sym].get("h")):
            data[sym] = ser


def load_offline_ohlc(path):
    """Like load_offline, but each file (and each top-level item of a bundle file) is
    read on its own and, when a symbol is in both a spark answer (closes only) and a
    v8 chart, the v8 chart wins."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        items = []
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            items.append((_stem(f) if isinstance(doc, dict) and "chart" in doc else "", doc))
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        if _yahoo_doc(doc):
            items = [("", doc)]
        elif isinstance(doc, dict):  # a bundle, even one with an item named "spark" or "chart"
            items = [(k if isinstance(v, dict) and "chart" in v else "", v) for k, v in doc.items()]
        else:
            raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % path)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    for hint, doc in items:
        got, errs = {}, {}
        _absorb(doc, hint, got, errs)
        _merge_ohlc(data, errors, got, errs)
    for sym in data:
        errors.pop(sym, None)
    return data, errors


def cmd_volatile(a):
    t0 = time.time()
    if a.show is not None:
        raise ScanError("volatile keeps the --top N most volatile: use --top, not --show")
    if a.max_confirm < 0 or a.top < 0:
        raise ScanError("--max-confirm and --top take 0 (all) or a positive number")
    now = parse_now(a.now)
    data = failed = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline_ohlc(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    if a.index:
        if a.symbols or a.watchlist or a.universe:
            raise ScanError("--index replaces the symbol list: drop --symbols / --watchlist / --universe")
        list_dir = None
        if a.offline:
            list_dir = a.offline if os.path.isdir(a.offline) else os.path.dirname(os.path.abspath(a.offline))
            if not os.path.exists(os.path.join(list_dir, NSE_LISTS[a.index][0])):
                raise ScanError("--index needs an offline folder with %s in it" % NSE_LISTS[a.index][0])
        kept, _ = build_universe(nse_list_text(a.index, list_dir))
        symbols = [s for s, _ in kept]
        if a.limit:
            symbols = symbols[:a.limit]
        if not symbols:
            raise ScanError("no symbols in the %s list" % a.index)
    else:
        symbols = read_symbols(a, offline_syms)
    confirm = None
    if not a.offline:
        progress(a, "Stage 1: daily closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "1mo", "1d", a.pause, a)

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            return parse_chart(yahoo_get(chart_path(sym, VOL_CHART_RANGE, "1d")), sym)
    hits, counts, failed2 = scan_volatile(data, symbols, now, a.min_price, a.min_turnover_cr,
                                          a.min_avg_turnover_cr, a.max_confirm, confirm, a.top)
    failed = dict(failed or {})
    nfailed = len([s for s in symbols if s not in data])  # stage 1: no closes at all (offline: not in the files)
    failed.update(failed2)
    day = max((h["day"] for h in hits), default=None)
    complete = all(h["day_complete"] for h in hits)
    as_of = ("%s %s" % (day, "close" if complete else "so far")) if day else None
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "volatile",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "failed_stage2": len(failed2), "unconfirmed": counts["unconfirmed"], "source": "yahoo",
           "interval": "1d", "corporate_action": counts["jumps"],
           "filters": {"min_price": a.min_price, "min_turnover_cr": a.min_turnover_cr,
                       "min_turnover_cr_fraction": round(counts["floor_fraction"], 2),
                       "min_avg_turnover_cr": a.min_avg_turnover_cr, "sessions": VOL_SESSIONS,
                       "atr": "simple average of %d true ranges" % VOL_SESSIONS,
                       "stage2": "the biggest close-to-close movers, %s a batch, at most %s"
                                 % (a.max_confirm or "all", (3 * a.max_confirm) or "all")},
           "note": NOTE, "hits": hits}
    if a.index:
        doc["index"] = a.index
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    stage2_failed = counts["confirmed"] == 0 and bool(failed2)  # e.g. Yahoo refused every v8 chart
    wrote = not a.no_write and counts["fetched"] > 0 and not stage2_failed
    if wrote:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Volatility scan, %s IST, daily bars%s (Yahoo)." % (
        to_ist(now).strftime("%a %Y-%m-%d %H:%M"),
        (" through %s" % as_of) if as_of else ""))
    if counts["floor_fraction"] < 1:
        floor = "₹%s cr+ traded so far: ₹%s cr pro-rated to %d%% of the session" % (
            _n(round(a.min_turnover_cr * counts["floor_fraction"], 1)), _n(a.min_turnover_cr),
            round(counts["floor_fraction"] * 100))
    else:
        floor = "₹%s cr+ traded on the day" % _n(a.min_turnover_cr)
    extra = []
    if counts["turnover_unknown"]:
        extra.append("%d with no turnover for that day skipped that floor (the average floor still applies)"
                     % counts["turnover_unknown"])
    if counts["short"]:
        extra.append("%d had under %d sessions" % (counts["short"], VOL_SESSIONS + 1))
    if counts["stale"]:
        extra.append("%d had no bar for %s (suspended or stale)" % (counts["stale"], counts["latest"]))
    print("Fetched %s of %s charts%s. %s passed the floors (₹%s+, %s)%s."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]), _n(a.min_price), floor,
             ("; " + "; ".join(extra)) if extra else ""))
    print("Stage 2: the %s biggest close-to-close movers sent for real highs and lows%s; %s confirmed%s, %s under "
          "₹%s cr average daily turnover dropped." % (
              "{:,}".format(counts["sent"]),
              (" (%d batches of %d)" % (counts["batches"], a.max_confirm)) if counts["batches"] > 1 else "",
              "{:,}".format(counts["confirmed"]),
              (" (%d had no usable daily chart)" % len(failed2)) if failed2 else "",
              "{:,}".format(counts["thin"]), _n(a.min_avg_turnover_cr)))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more liquid charts were not checked (stage 2 stops at %s found or %s sent; "
              "--max-confirm 0 checks all)." % ("{:,}".format(counts["unconfirmed"]), a.top or "all",
                                               3 * a.max_confirm))
    if counts["jumps"]:
        print("Left out, a price step that matches a split or bonus ratio: %s. Check corporate action "
              "(split/bonus?)." % ", ".join(counts["jumps"]))
    if hits:
        live = "" if complete else " (the latest session is still open: its move and range are so far)"
        print("Most volatile first, by ATR%% (%d-day average true range / close)%s:" % (VOL_SESSIONS, live))
        print("  %-11s %11s %7s %8s %8s %9s %11s  %s" % ("symbol", "price", "ATR%", "avg|d|%", "day%",
                                                       "range%", "avg turnover", "note"))
        for h in hits:
            notes = [h["note"]] if h["note"] else []
            if h["day_range_pct"] is None:
                notes.append("no range for %s" % h["day"])
            if h["turnover_sessions"] < TURNOVER_SESSIONS:
                notes.append("turnover over %d sessions" % h["turnover_sessions"])
            rng = "%8.2f%%" % h["day_range_pct"] if h["day_range_pct"] is not None else "%9s" % "-"
            print(("  %-11s %11s %6.2f%% %7.2f%% %+7.2f%% %s %8s cr  %s" % (
                h["symbol"], inr(h["price"]), h["atr_pct"], h["avg_move_pct"], h["day_pct"], rng,
                "{:,}".format(int(round(h["avg_turnover_cr"]))), "; ".join(notes))).rstrip())
        if counts["ranked"] > len(hits):
            print("  ... %d more passed (--top N for more)." % (counts["ranked"] - len(hits)))
    else:
        print("No chart passed every floor.")
    if wrote:
        print("Wrote %s (mode volatile, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    elif not a.no_write and stage2_failed:
        print("Stage 2 could not confirm any chart (%d failed), so %s was left as it was." % (len(failed2), a.out))
    elif not a.no_write:
        print("Nothing was fetched, so %s was left as it was." % a.out)
    print("Volatile cuts both ways: a wide day means a wide opening range, a far stop and fewer shares. "
          + NOTE + " Next: /trader-news on the list before the open.")
    return 0 if hits else 1


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or "14:45", "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_ist(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day)
    symbols = read_symbols(a, offline_syms)
    confirm = None
    if not a.offline:
        today = to_ist(parse_now(None)).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_path(sym, rng, "%dm" % BAR_MIN)), sym)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_volume, confirm,
                                     a.max_confirm)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    as_of = "%s %s IST" % (day.isoformat(), cutoff.strftime("%H:%M"))
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"], "source": "yahoo", "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (9:15 to %s), 5-minute bars, Yahoo."
          % (day_label(day), clock_label(cutoff), a.or_minutes, clock_label(slot(day, a.or_minutes // BAR_MIN))))
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the price / volume floor)" % "{:,}".format(counts["illiquid"])) if counts["illiquid"] else "",
             "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), ("the %s checked" % "{:,}".format(counts["confirmed"])) if counts["unconfirmed"]
                 else "{:,}".format(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %8s  %-10s %12s  range %s to %s  %+.2f%% over" % (
                h["at"], h["symbol"], inr(h["price"]), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL and /trader-entry SYMBOL.")
    return 0 if hits else 1


def _p(x):
    return inr(x)[1:]


def _count(text):
    """argparse type: 0 or a positive whole number."""
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a whole number" % text)
    if n < 0:
        raise argparse.ArgumentTypeError("%d: use 0 (all) or a positive number" % n)
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (trader-scanner): daily setups and opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download NSE's equity list -> trader/universe.txt")
    p.add_argument("--index", choices=sorted(NSE_LISTS), default="all",
                   help="all (EQUITY_L.csv, the default) or nifty500 (ind_nifty500list.csv)")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--offline", metavar="DIR", help="a folder with EQUITY_L.csv / ind_nifty500list.csv")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")

    def common(p, min_price, min_volume, show=50):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated NSE symbols instead of the universe")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of symbols")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price, help="rupees (%s)" % _n(min_price))
        if min_volume is not None:
            p.add_argument("--min-volume", type=int, default=min_volume)
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-29 09:45" IST, or ...Z)')
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=show, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p, 50.0, 500000)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 50.0, 0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM IST (default: config no_entries_after, 14:45)")
    p.add_argument("--config", default="trader/config.json")
    p = sub.add_parser("volatile", help="the most volatile liquid stocks (ATR%%), for tomorrow's watchlist")
    common(p, 50.0, None, show=None)
    p.add_argument("--index", choices=sorted(NSE_LISTS),
                   help="scan NSE's list instead of the universe file: all (EQ series) or nifty500")
    p.add_argument("--min-turnover-cr", type=float, default=100.0,
                   help="rupees crore traded on the latest session, stage 1 (100)")
    p.add_argument("--min-avg-turnover-cr", type=float, default=150.0,
                   help="average rupees crore a day over %d sessions, stage 2 (150)" % TURNOVER_SESSIONS)
    p.add_argument("--max-confirm", type=_count, default=VOL_CONFIRM,
                   help="v8 charts to fetch in stage 2, the biggest close-to-close movers first "
                        "(%d; 0 = all): stage 2 never sees the rest" % VOL_CONFIRM)
    p.add_argument("--top", type=_count, default=15, help="hits to keep, most volatile first (15; 0 = all)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb, "volatile": cmd_volatile}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
