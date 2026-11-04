#!/usr/bin/env python3
"""News monitor for trader-news. Standard library only.

Watches headlines and NSE corporate filings for the stocks you watch or hold
and prints only what is new since the last check, newest first, with how far
each stock has moved today, a flag on anything that touches your symbols and
a warning when a company's results are due within the week.

    python3 news.py                                  # watchlist + plan + portfolio symbols
    python3 news.py --tickers RELIANCE INFY M&M      # or --tickers RELIANCE,INFY,M&M
    python3 news.py --tickers INFY --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-nse                         # headlines only
    python3 news.py --no-moves                       # skip the price line per symbol

Your symbols, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json. Symbols are plain NSE symbols (INFY,
M&M, BAJAJ-AUTO); a trailing .NS is dropped, a BSE .BO symbol is refused.

Sources:
  - Yahoo Finance headline RSS, one request per symbol:
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYMBOL>.NS&region=IN&lang=en-IN
    (some NSE symbols get an empty feed; that is not an error)
  - NSE corporate announcements RSS and board meetings RSS, one request each
    per run, whatever the number of symbols:
    https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml
    https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml
    The feeds name the company, not the symbol, so the symbol is matched to
    its company name through NSE's equity list (EQUITY_L.csv, cached in
    trader/cache/ for a day). The feeds hold about the last day of filings.
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS?range=1d&interval=5m

Results due: a board meeting within the next 7 days that will consider
financial results is flagged, because a day trade must be out before the
close. Meetings are remembered in the state file, so a date announced on an
earlier check still counts.

Times print in India Standard Time (IST). State: trader/state/news.json
remembers what you have seen (30 days), so an item shows once. Offline:
--offline DIR reads yahoo-<SYMBOL>.xml, chart-<SYMBOL>.json,
nse-announcements.xml, nse-board-meetings.xml and EQUITY_L.csv from DIR.
The tests use it.

Exit code 0 when there is something new, 1 when nothing is new, 2 on bad
input or when every source fetch failed (so "nothing new" is never a guess).

Headlines are leads, not facts: open the story or the filing before acting,
and treat nothing here as advice.
"""

import argparse
import csv
import datetime as _dt
import email.utils
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
NSE_ANNOUNCEMENTS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_BOARD = "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml"
EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
UA = "Mozilla/5.0 (compatible; day-trader-skill-nse/1.0)"
KEEP_DAYS = 30
CACHE_HOURS = 24
MOVE_PCT = 3.0      # a stock this far from yesterday's close is flagged as moving
RESULTS_DAYS = 7    # a results board meeting this close is flagged
NSE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")

KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults?\b|\bQ[1-4]\b|\bquarterly\b|\bnet profit\b|\bprofit\b"
                 r"|\bEBITDA\b|\bPAT\b|\bEPS\b|\bguidance\b|\boutlook\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|target price|\boutperform\b"
                r"|\bunderperform\b|\bbuy rating\b|\bsell rating\b"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bcourt\b|\bNCLT\b|\btribunal\b"
              r"|\bpenalty\b|\braid\b|\bEnforcement Directorate\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bdemerger\b|\bamalgamation\b|\bstake\b"
             r"|\btakeover\b|\bdeal\b|\border win\b|\bbags?\b.*\border\b|\bcontract\b"),
    ("capital", r"\bdividend\b|\bbuy-?back\b|\bbonus\b|\bstock split\b|\bsplit\b|\bQIP\b"
                r"|\brights issue\b|\bOFS\b|\boffer for sale\b|\bblock deal\b|\bfund ?rais"),
    ("people", r"\bCEO\b|\bCFO\b|\bMD\b|\bmanaging director\b|\bchairman\b|\bresign|\bsteps down\b"
               r"|\bappoint"),
    ("risk", r"\bdefault\b|\binsolvency\b|\bbankrupt|\bfraud\b|\bpledge\b|\bdelist|\bhack"
             r"|\bbreach\b|\boutage\b|\brecall\b"),
    ("halt", r"\bupper circuit\b|\blower circuit\b|\bhalt(?:s|ed)?\b|\bsuspen(?:d|sion)\b"),
    ("regulatory", r"\bSEBI\b|\bRBI\b|\bCCI\b|\bUSFDA\b|\bFDA\b|\bTRAI\b|\bDGCA\b|\bapproval\b"
                   r"|\bapproves\b|\blicen[cs]e\b"),
    ("macro", r"\brepo rate\b|\bmonetary policy\b|\bCPI\b|\binflation\b|\bGDP\b|\bUnion Budget\b"
              r"|\bFIIs?\b|\bFPIs?\b|\bmonsoon\b|\bcrude\b|\brupee\b|\btariffs?\b|\bNifty\b|\bSensex\b"),
]
# NSE filings: tags from the subject and the text. The ones marked important get a "!".
NSE_TAGS = [
    ("results", r"financial results?|integrated filing- ?financial|outcome of board meeting.*results"
                r"|results?.*outcome of board meeting"),
    ("board meeting", r"board meeting intimation|prior intimation|meeting date"),
    ("dividend", r"\bdividend"),
    ("bonus", r"\bbonus\b"),
    ("split", r"sub-?division|\bsplit\b"),
    ("buyback", r"buy-?back"),
    ("fund raising", r"fund raising|\bqip\b|preferential|rights issue|alteration of capital"),
    ("order win", r"bagging|receiving of orders|\bcontracts?\b"),
    ("merger", r"acquisition|amalgamation|merger|demerger|scheme of arrangement|disposal"),
    ("resignation", r"resignation|cessation"),
    ("people", r"change in directors|\bkmp\b|appointment|change in management|company secretary"),
    ("rating", r"credit rating"),
    ("record date", r"record date"),
    ("legal", r"actions? initiated|orders? passed|penalty|litigation"),
    ("exchange query", r"spurt in volume|news verification|clarification|price movement"),
    ("investor meet", r"analysts?|investor meet|con\. call|investor presentation"),
    ("press release", r"press release"),
]
IMPORTANT_TAGS = {"results", "dividend", "bonus", "split", "buyback", "fund raising", "merger",
                  "resignation", "rating", "legal", "exchange query"}


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time (IST, no tz files)

IST = _dt.timedelta(hours=5, minutes=30)   # India Standard Time, no daylight saving


def to_ist(utc):
    """Aware or naive UTC -> naive IST wall-clock time."""
    if utc.tzinfo is not None:
        utc = utc.astimezone(_dt.timezone.utc)
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    """Naive IST -> aware UTC."""
    return (local.replace(tzinfo=None) - IST).replace(tzinfo=_dt.timezone.utc)


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def normalize_symbol(text):
    """"reliance.ns" -> "RELIANCE". A BSE .BO symbol or anything that is not an NSE symbol raises."""
    sym = (text or "").strip().upper()
    if sym.endswith(".NS"):
        sym = sym[:-3]
    if sym.endswith(".BO"):
        raise NewsError("%s is a BSE symbol: this pack covers NSE only" % text)
    if not NSE_SYMBOL.match(sym):
        raise NewsError("%r is not an NSE symbol (like RELIANCE, M&M, BAJAJ-AUTO)" % text)
    return sym


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{symbol: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[normalize_symbol(sym)] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(normalize_symbol(pl["symbol"]), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            sym = normalize_symbol(sym)
            out[sym] = p.get("name") or out.get(sym)
    return out


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.15):
        self.offline = offline
        self.pause = pause
        self._last = 0.0
        self.attempts = 0   # every fetch tried, and how many of them failed: all failed means exit 2
        self.failures = 0

    def get(self, url, name):
        self.attempts += 1
        try:
            return self._get(url, name)
        except NewsError:
            self.failures += 1
            raise

    def _get(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))


def yahoo_symbol(sym):
    """"M&M" -> "M%26M.NS": the Yahoo symbol, quoted for a URL."""
    return urllib.parse.quote(sym + ".NS")


def norm_name(name):
    """"Mahindra &amp; Mahindra Limited" -> "mahindra and mahindra". Case, punctuation and
    the Limited / Ltd suffix do not count."""
    s = (name or "").lower().replace("&amp;", "&").replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = s.split()
    while words and words[-1] in ("limited", "ltd"):
        words.pop()
    if words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def parse_equity_list(data):
    """NSE's EQUITY_L.csv -> {symbol: company name}. Every series, so a BE stock still has a name."""
    text = data.decode("utf-8-sig", "replace") if isinstance(data, bytes) else data
    out = {}
    rows = csv.reader(io.StringIO(text))
    header = [h.strip().upper() for h in next(rows, [])]
    if "SYMBOL" not in header or "NAME OF COMPANY" not in header:
        raise NewsError("NSE equity list has no SYMBOL / NAME OF COMPANY columns")
    si, ni = header.index("SYMBOL"), header.index("NAME OF COMPANY")
    for r in rows:
        if len(r) > max(si, ni) and r[si].strip():
            out[r[si].strip().upper()] = r[ni].strip()
    return out


def company_names(fetcher, cache="trader/cache/EQUITY_L.csv", now=None):
    """{symbol: name}. Offline: EQUITY_L.csv in the offline folder. Online: the cached copy if it
    is under a day old, else NSE's file (and the cache is refreshed)."""
    if fetcher.offline:
        return parse_equity_list(fetcher.get(EQUITY_LIST, "EQUITY_L.csv"))
    now_ts = time.time() if now is None else now
    if cache and os.path.isfile(cache) and now_ts - os.path.getmtime(cache) < CACHE_HOURS * 3600:
        with open(cache, "rb") as fh:
            return parse_equity_list(fh.read())
    data = fetcher.get(EQUITY_LIST, "EQUITY_L.csv")
    names = parse_equity_list(data)
    if cache:
        folder = os.path.dirname(cache)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(cache, "wb") as fh:
            fh.write(data)
    return names


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (Yahoo RSS), ISO 8601, or NSE's "03-Nov-2026 16:19:41" (IST) -> aware UTC, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return ist_to_utc(_dt.datetime.strptime(value, "%d-%b-%Y %H:%M:%S"))
    except ValueError:
        pass
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            return ist_to_utc(d)  # a bare ISO time is IST, like --now
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_rss(data, sym):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (sym, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": sym, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_nse_rss(data, feed):
    """NSE announcements or board meetings RSS -> raw entries
    {company, link, text, subject, meeting, time}. feed is "announcements" or "board"."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("NSE %s feed is not valid XML: %s" % (feed, exc))
    out = []
    for it in root.iter("item"):
        desc = " ".join(_text(it, "description").split())
        subject, meeting = "", None
        m = re.search(r"\|\s*SUBJECT:\s*(.*)$", desc)
        if m:
            subject = " ".join(m.group(1).split())
            desc = desc[:m.start()].strip()
        m = re.search(r"\|\s*Meeting Date:\s*(\d{2}-[A-Za-z]{3}-\d{4})", desc)
        if m:
            meeting = _meeting_date(m.group(1))
            desc = desc[:m.start()].strip()
            subject = subject or desc
        if meeting is None:
            m = re.search(r"Board Meeting to be held on (\d{2}-[A-Za-z]{3}-\d{4})", desc, re.I)
            if m:
                meeting = _meeting_date(m.group(1))
        out.append({"company": _text(it, "title"), "link": _text(it, "link"), "text": desc,
                    "subject": subject, "meeting": meeting, "feed": feed,
                    "time": parse_time(_text(it, "pubDate"))})
    return out


def _meeting_date(text):
    try:
        return _dt.datetime.strptime(text, "%d-%b-%Y").date()
    except ValueError:
        return None


def nse_tags(entry):
    hay = "%s %s" % (entry["subject"], entry["text"])
    return [label for label, rx in NSE_TAGS if re.search(rx, hay, re.I)]


def nse_items(entries, by_name):
    """Raw NSE entries -> items for the symbols you watch. by_name: {normalized name: symbol}."""
    out = []
    for e in entries:
        sym = by_name.get(norm_name(e["company"]))
        if not sym:
            continue
        key = e["link"] or "%s|%s|%s" % (e["company"], e["text"], e["time"])
        tags = nse_tags(e)
        if e["feed"] == "board":
            title = "%s: board meeting on %s" % (e["company"], e["meeting"].isoformat() if e["meeting"]
                                                 else "a date not given")
            if e["text"]:
                title += " (%s)" % e["text"]
        else:
            title = "%s: %s" % (e["company"], e["subject"] or "announcement")
        out.append({"id": "nse-%s:%s" % (e["feed"], hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]),
                    "source": "NSE", "kind": "filing", "ticker": sym, "title": title,
                    "summary": e["text"], "link": e["link"], "time": e["time"], "nse_tags": tags,
                    "important": bool(set(tags) & IMPORTANT_TAGS), "meeting": e["meeting"]})
    return out


def parse_chart(data, sym):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % sym)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % sym)
    when = meta.get("regularMarketTime")
    return {"ticker": sym, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_symbol(t)), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_symbol(t)), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def short_name(name):
    """"Infosys Limited" -> "Infosys": the name as headlines write it."""
    words = (name or "").replace("&amp;", "&").split()
    while words and words[-1].lower().rstrip(".") in ("limited", "ltd"):
        words.pop()
    return " ".join(words)


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\bNSE\s*:\s*%s\b" % re.escape(t)]
        name = short_name(name)
        if name and len(name) >= 4:
            alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your symbols each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        if it["kind"] == "headline":
            for t, rx in pats.items():
                if rx.search(hay):
                    touches.add(t)
        it["touches"] = sorted(touches)
        if it["kind"] == "filing":
            it["tags"] = list(it.get("nse_tags") or [])
        else:
            it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s|%s" % (it["kind"], re.sub(r"\W+", " ", it["title"].lower()).strip(),
                              it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, nse=True, names=None, errors=None):
    """Headlines per symbol, then the two NSE feeds once. Returns the items (NSE ones carry
    "meeting" when they announce a board meeting)."""
    errors = [] if errors is None else errors
    items = []
    for t in sorted(watch):
        try:
            items += parse_rss(fetcher.get(YAHOO % yahoo_symbol(t), "yahoo-%s.xml" % t), t)
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("headlines for %s: %s" % (t, exc))
    if not nse:
        return items
    if not names:
        errors.append("NSE filings skipped: no NSE equity list to match symbols to company names")
        return items
    by_name = {}
    for t in sorted(watch):
        if t in names:
            by_name[norm_name(names[t])] = t
        else:
            errors.append("%s is not on NSE's equity list: no NSE filings for it" % t)
    if not by_name:
        return items
    for url, name, feed in ((NSE_ANNOUNCEMENTS, "nse-announcements.xml", "announcements"),
                            (NSE_BOARD, "nse-board-meetings.xml", "board")):
        try:
            items += nse_items(parse_nse_rss(fetcher.get(url, name), feed), by_name)
        except NewsError as exc:
            errors.append("NSE %s feed: %s" % (feed, exc))
    return items


def remember_meetings(items, state, today):
    """Board meetings announced in these items -> state["meetings"][symbol]; past ones are dropped."""
    meetings = state.setdefault("meetings", {})
    for it in items:
        if it.get("source") != "NSE" or not it.get("meeting"):
            continue
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        rec = {"date": it["meeting"].isoformat(), "results": bool(re.search(r"\bresults?\b", hay, re.I)),
               "about": it.get("summary") or "", "link": it["link"]}
        lst = meetings.setdefault(it["ticker"], [])
        same = next((m for m in lst if m["date"] == rec["date"]), None)
        if same is None:
            lst.append(rec)
        elif rec["results"] and not same["results"]:
            same.update(rec)
    for sym in list(meetings):
        meetings[sym] = sorted((m for m in meetings[sym] if m["date"] >= today.isoformat()),
                               key=lambda m: m["date"])
        if not meetings[sym]:
            del meetings[sym]


def results_due(state, watch, today, days=RESULTS_DAYS):
    """[{symbol, date, link}] for watched symbols with a results board meeting from today to
    today + days."""
    last = (today + _dt.timedelta(days=days)).isoformat()
    out = []
    for sym in sorted(watch):
        for m in state.get("meetings", {}).get(sym, []):
            if m["results"] and today.isoformat() <= m["date"] <= last:
                out.append({"symbol": sym, "date": m["date"], "link": m["link"]})
                break
    return out


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-10s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            m["ticker"], "{:,.2f}".format(m["price"]), m["pct"], "{:,.2f}".format(m["prev_close"]),
            when, flag))
    return out


def render_due(due):
    return ["RESULTS DUE  %s on %s: a day trade is out before the close." % (
        d["symbol"], _dt.date.fromisoformat(d["date"]).strftime("%a %Y-%m-%d")) for d in due]


def render(items, errors, now, moves=None, move_pct=MOVE_PCT, due=None):
    out = render_due(due or []) + render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["kind"] == "filing" and it.get("summary") and it["summary"] not in it["title"]:
            s = it["summary"]
            extra.append(s if len(s) <= 200 else s[:197].rstrip() + "...")
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        if it["link"]:
            extra.append(it["link"])
        if extra:
            out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % to_ist(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        if d.get("meeting"):
            d["meeting"] = d["meeting"].isoformat()
        d.pop("nse_tags", None)
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="NSE symbols; default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--names-cache", default="trader/cache/EQUITY_L.csv",
                    help="NSE equity list cache (refreshed after a day)")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-nse", action="store_true", help="headlines only")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per symbol")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time (IST unless it carries Z or an offset), for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.tickers:
            watch = {normalize_symbol(t): None for arg in args.tickers for t in arg.split(",") if t.strip()}
            known = watch_from_files(args.portfolio, args.watchlist, args.plan)
            watch.update({t: known[t] for t in watch if known.get(t)})
        else:
            watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    except NewsError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2
    if not watch:
        print("news: no symbols. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    if args.now:
        now = parse_time(args.now)
        if now is None:
            print('news: --now wants a time like "2026-11-03 10:15" (IST) or ...Z (UTC)', file=sys.stderr)
            return 2
    else:
        now = _dt.datetime.now(_dt.timezone.utc)
    today = to_ist(now).date()
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    names = None
    if not args.no_nse:
        try:
            names = company_names(fetcher, args.names_cache)
        except NewsError as exc:
            errors.append("NSE equity list: %s" % exc)
    for t in watch:
        if names and t in names and not watch[t]:
            watch[t] = names[t]
    items = collect(fetcher, watch, nse=not args.no_nse, names=names, errors=errors)
    items = tag(items, watch)
    # before dedupe: a board-meetings entry can share its link with the announcement
    remember_meetings([i for i in items if i["time"] is None or i["time"] <= now + _dt.timedelta(minutes=10)],
                      state, today)
    items = dedupe(items)
    due = results_due(state, watch, today)
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "results_due": due, "moves": mv,
                          "items": _json_ready(shown), "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct, due))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    if fetcher.attempts and fetcher.failures == fetcher.attempts:
        return 2  # every source failed: "nothing new" would be a lie
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
