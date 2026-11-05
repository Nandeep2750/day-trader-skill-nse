#!/usr/bin/env python3
"""A live, read-only dashboard for trader-autopilot (trader-autopilot).

    python3 dashboard.py --project ~/paper-test            # then open http://127.0.0.1:8765
    python3 dashboard.py --project ~/paper-test --port 9000 --open

Serves one page on 127.0.0.1 only. The page refreshes itself every 15 seconds from the
files the autopilot writes (trader/state/autopilot-DATE.json, the journal, today's log,
trader/config.json) and a 5-minute Yahoo chart per symbol (cached 60 seconds). A past
day opens at /?date=YYYY-MM-DD from the "Past days" list: its trades, events, log and that
day's charts, read from the same dated files (kept, never overwritten). It never
writes a file, never calls ticket.py, and never talks to a broker. Ctrl+C stops it; the
autopilot keeps running.

Not financial advice.
"""

import argparse
import datetime
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

IST = datetime.timedelta(hours=5, minutes=30)   # India Standard Time, no daylight saving
UA = "Mozilla/5.0 (compatible; day-trader-skill-nse/1.0)"
NSE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
DAY = re.compile(r"^\d{4}-\d\d-\d\d$")
DAY_FILE = re.compile(r"^(?:journal|autopilot)-(\d{4}-\d\d-\d\d)\.json$")
QUOTE_TTL = 60
LOG_LINES = 120


def now_ist():
    return datetime.datetime.utcnow() + IST


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def tail(path, n):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-n:]
    except OSError:
        return []


def symbols_of(project, cfg):
    ap = (cfg or {}).get("autopilot") or {}
    syms = ap.get("symbols")
    if not syms:
        try:
            with open(os.path.join(project, "trader", "universe.txt"), encoding="utf-8") as fh:
                syms = [ln.split("#")[0].strip() for ln in fh]
        except OSError:
            syms = []
    out = []
    for s in syms or []:
        s = str(s).strip().upper()
        if s.endswith(".NS"):
            s = s[:-3]
        if NSE_SYMBOL.match(s) and s not in out:
            out.append(s)
    return out


_quotes = {}
_quotes_lock = threading.Lock()


def get_chart_json(sym, day=None):
    """query1, then query2, then query1 again: a cold first request sometimes times out.
    day (YYYY-MM-DD) asks for that IST session instead of today's; Yahoo keeps 5-minute bars about 60 days."""
    span = "range=1d"
    if day:
        start = int((datetime.datetime.strptime(day, "%Y-%m-%d") - datetime.datetime(1970, 1, 1) - IST).total_seconds())
        span = "period1=%d&period2=%d" % (start, start + 86400)
    err = None
    for host in ("query1", "query2", "query1"):
        url = ("https://%s.finance.yahoo.com/v8/finance/chart/%s?%s&interval=5m"
               % (host, urllib.parse.quote(sym + ".NS"), span))
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                return json.load(r)
        except Exception as e:
            err = e
            time.sleep(0.5)
    raise err


def fetch_chart(sym, day=None):
    doc = get_chart_json(sym, day)
    res = (doc.get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("no chart")
    res = res[0]
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    col = lambda name: q.get(name) or [None] * len(ts)
    for i, t in enumerate(ts):
        c = col("close")[i]
        if c is None:
            continue
        hi, lo, op, vol = col("high")[i], col("low")[i], col("open")[i], col("volume")[i]
        clock = (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(t)) + IST).strftime("%H:%M")
        # [IST clock, close, high, low, open, volume, unix seconds shifted to IST so the chart's UTC axis reads IST]
        bars.append([clock, round(c, 2), round(hi if hi is not None else c, 2), round(lo if lo is not None else c, 2),
                     round(op if op is not None else c, 2), int(vol or 0), int(t) + int(IST.total_seconds())])
    if day:   # a past session: its own last bar is the price, not today's market price
        return {"price": bars[-1][1] if bars else None, "prev": meta.get("chartPreviousClose"),
                "as_of": bars[-1][0] if bars else None, "bars": bars}
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    stamp = meta.get("regularMarketTime")
    as_of = ((datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(stamp)) + IST).strftime("%H:%M")
             if stamp else None)
    return {"price": price, "prev": prev, "as_of": as_of, "bars": bars}


def quote(sym, day=None):
    key = (sym, day)
    with _quotes_lock:
        hit = _quotes.get(key)
    if hit and (time.time() - hit[0] < QUOTE_TTL or (day and "error" not in hit[1])):   # a past day never changes
        return hit[1]
    try:
        data = fetch_chart(sym, day)
        stamp = time.time()
    except Exception as e:  # network, rate limit, bad JSON: show the last good one, marked stale
        if hit and "error" not in hit[1]:
            return dict(hit[1], stale=True)
        data = {"error": str(e)[:120]}
        stamp = time.time() - QUOTE_TTL + 15   # retry a failed symbol after 15 seconds
    with _quotes_lock:
        _quotes[key] = (stamp, data)
    return data


def quotes(syms, day=None):
    """All symbols at once, so one slow fetch does not hold up the rest."""
    out = {}

    def one(s):
        out[s] = quote(s, day)
    threads = [threading.Thread(target=one, args=(s,)) for s in syms]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return {s: out.get(s, {"error": "no quote"}) for s in syms}


def days_of(trader):
    """Every day the autopilot left a journal or a state file for, newest first, with the journal's totals."""
    dates = set()
    for sub in ("autopilot", "state"):
        try:
            names = os.listdir(os.path.join(trader, sub))
        except OSError:
            continue
        for name in names:
            m = DAY_FILE.match(name)
            if m:
                dates.add(m.group(1))
    out = []
    for d in sorted(dates, reverse=True):
        tot = (read_json(os.path.join(trader, "autopilot", "journal-%s.json" % d)) or {}).get("totals")
        if not isinstance(tot, dict):
            st = read_json(os.path.join(trader, "state", "autopilot-%s.json" % d)) or {}
            tot = {"trades": len(st.get("trades") or {})}
        out.append({"date": d, "trades": tot.get("trades"), "closed": tot.get("closed"), "net": tot.get("net")})
    return out


def snapshot(project, day=None):
    now = now_ist()
    today = now.date().isoformat()
    date = day if day and DAY.match(day) and day <= today else today
    live = date == today
    trader = os.path.join(project, "trader")
    cfg = read_json(os.path.join(trader, "config.json")) or {}
    state = read_json(os.path.join(trader, "state", "autopilot-%s.json" % date))
    journal = read_json(os.path.join(trader, "autopilot", "journal-%s.json" % date))
    ticks = [ln for ln in tail(os.path.join(trader, "autopilot", "logs", "launchd.log"), 40) if ln.strip()]
    syms = symbols_of(project, cfg) if live else []   # a past day shows what it picked and traded, not today's list
    pick = (state or {}).get("pick")
    if isinstance(pick, dict) and isinstance(pick.get("symbols"), list):   # today's pick (or hand list) wins
        syms = symbols_of(project, {"autopilot": {"symbols": pick["symbols"]}}) if pick["symbols"] else []
    for sym in sorted(((state or {}).get("trades") or {})):                 # every trade today stays on the board
        if sym not in syms:
            syms.append(sym)
    limits = {k: cfg.get(k) for k in ("budget", "max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day",
                                      "no_entries_before", "no_entries_after", "flat_by", "broker", "product")}
    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"), "date": date, "today": today, "live": live,
        "paper": cfg.get("paper", True) is not False,
        "auto_paper": ((cfg.get("autopilot") or {}).get("auto_paper") is True),
        "limits": limits, "symbols": syms,
        "state": state, "journal": journal,
        "last_tick": ticks[-1] if ticks else None,
        "log": tail(os.path.join(trader, "autopilot", "logs", date + ".log"), LOG_LINES),
        "quotes": quotes(syms, None if live else date),
        "days": days_of(trader),
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Autopilot Dashboard</title>
<!-- A rising saffron line on indigo, the same ink as the page, so the tab is easy to pick out.
     Inline as a data URI: no extra request and no route to serve. -->
<link rel="icon" href="data:image/svg+xml,<svg%20xmlns=%22http://www.w3.org/2000/svg%22%20viewBox=%220%200%2032%2032%22><rect%20width=%2232%22%20height=%2232%22%20rx=%227%22%20fill=%22%2316204A%22/><path%20d=%22M6%2022L13%2015L18%2019L26%209%22%20fill=%22none%22%20stroke=%22%23E39B12%22%20stroke-width=%223.4%22%20stroke-linecap=%22round%22%20stroke-linejoin=%22round%22/><circle%20cx=%2226%22%20cy=%229%22%20r=%223.4%22%20fill=%22%2345C99A%22/></svg>">
<meta name="description" content="Live board for the paper-trading autopilot: the session timeline, open trades, stop and target levels, P&amp;L, events, the log and past days.">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;800&display=swap" rel="stylesheet">
<style>
/* One NSE session, 09:15 to 15:30, read as a timeline: indigo ink on cool paper, saffron for now, teal up, red down. */
:root {
  color-scheme: light;
  --paper: #F3F5FA; --surface: #FFFFFF; --ink: #16204A; --ink-2: #3A4570; --muted: #5F688C;
  --rule: #D9DEEC; --rule-strong: #16204A;
  --accent: #2F4BD8; --accent-ink: #FFFFFF; --now: #E39B12; --now-ink: #16204A;
  --up: #0E7C5A; --down: #C33B3B; --up-soft: #D5EFE5; --down-soft: #F7DCDC;
  --range: #D4DBF6; --entries: #E9EDF9; --track: #F3F5FA;
  --font: "Hanken Grotesk", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --t-xs: 0.75rem; --t-sm: 0.875rem; --t-md: 1rem; --t-lg: 1.25rem; --t-xl: 1.5625rem; --t-2xl: 1.953rem; --t-3xl: 2.441rem; --t-4xl: 3.052rem;
  --s1: 4px; --s2: 8px; --s3: 12px; --s4: 16px; --s5: 24px; --s6: 32px; --s7: 48px; --s8: 64px;
  --r-lg: 18px; --r-md: 8px; --r-pill: 999px;
  --container: 1240px; --gutter: 32px; --dur: 140ms;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  color-scheme: dark;
  --paper: #0E1430; --surface: #151D3D; --ink: #E9EDF9; --ink-2: #C3CAE6; --muted: #98A1C3;
  --rule: #283258; --rule-strong: #E9EDF9;
  --accent: #8DA0FF; --accent-ink: #0E1430; --now: #F2B33D; --now-ink: #0E1430;
  --up: #45C99A; --down: #F27B7B; --up-soft: #123D33; --down-soft: #47222A;
  --range: #2A3570; --entries: #1D2650; --track: #0E1430;
} }
:root[data-theme="dark"] {
  color-scheme: dark;
  --paper: #0E1430; --surface: #151D3D; --ink: #E9EDF9; --ink-2: #C3CAE6; --muted: #98A1C3;
  --rule: #283258; --rule-strong: #E9EDF9;
  --accent: #8DA0FF; --accent-ink: #0E1430; --now: #F2B33D; --now-ink: #0E1430;
  --up: #45C99A; --down: #F27B7B; --up-soft: #123D33; --down-soft: #47222A;
  --range: #2A3570; --entries: #1D2650; --track: #0E1430;
}
*, *::before, *::after { box-sizing: border-box; }
html, body { overflow-x: clip; }
body { margin: 0; background: var(--paper); color: var(--ink); font-family: var(--font); font-size: var(--t-md); line-height: 1.5; font-variant-numeric: tabular-nums; -webkit-font-smoothing: antialiased; }
::selection { background: var(--now); color: var(--now-ink); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
h1, h2, h3 { margin: 0; line-height: 1.1; overflow-wrap: break-word; min-width: 0; }
p { margin: 0; }
a { color: inherit; }
.container { width: 100%; max-width: var(--container); margin-inline: auto; padding-inline: var(--gutter); }
.up { color: var(--up); } .down { color: var(--down); }
.quiet { color: var(--muted); }

/* Top bar */
.bar__in { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--s3) var(--s5); padding-block: var(--s4); }
.mark { font-size: var(--t-lg); font-weight: 800; letter-spacing: -0.01em; }
.bar__right { display: flex; flex-wrap: wrap; align-items: center; gap: var(--s2); }
.cell { display: inline-flex; align-items: center; min-height: 32px; padding: 0 var(--s3); border-radius: var(--r-pill); font-size: var(--t-sm); font-weight: 600; border: 1px solid var(--rule); color: var(--ink-2); }
.cell:empty { display: none; }
.cell--paper { background: var(--ink); border-color: var(--ink); color: var(--paper); }
.cell--live { background: var(--up-soft); border-color: transparent; color: var(--up); }
.cell--warn { background: var(--now); border-color: var(--now); color: var(--now-ink); }
.cell--bad { background: var(--down); border-color: var(--down); color: var(--surface); }
.cell--quiet { color: var(--muted); }
.btn { display: inline-flex; align-items: center; min-height: 32px; padding: 0 var(--s3); border-radius: var(--r-pill); border: 1px solid var(--rule-strong); background: transparent; color: var(--ink); font: 600 var(--t-sm)/1 var(--font); text-decoration: none; cursor: pointer; transition: background-color var(--dur) ease, color var(--dur) ease; }
.btn:hover { background: var(--ink); color: var(--paper); }
.btn:active { background: var(--ink-2); color: var(--paper); }
.btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; transition: none; }
.btn[hidden] { display: none; }
.btn--icon { width: 32px; padding: 0; justify-content: center; }
.btn--icon svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }

/* The day: date, figures, session rail */
.day { padding-block: var(--s5) var(--s7); }
.day__title { font-size: var(--t-3xl); font-weight: 800; letter-spacing: -0.025em; }
.sub { margin-top: var(--s2); font-size: var(--t-sm); color: var(--muted); max-width: 70ch; }
.figs { display: grid; grid-template-columns: minmax(0, 1.5fr) repeat(3, minmax(0, 1fr)); gap: var(--s5); margin: var(--s6) 0; }
.figs > div { min-width: 0; }
.figs dt { font-size: var(--t-sm); color: var(--muted); }
.figs dd { margin: var(--s1) 0 0; font-size: var(--t-xl); font-weight: 700; letter-spacing: -0.015em; overflow-wrap: anywhere; }
.figs .lead dd { font-size: var(--t-4xl); font-weight: 800; letter-spacing: -0.035em; line-height: 1; }
.figs .sm { display: block; margin-top: var(--s1); font-size: var(--t-sm); font-weight: 500; letter-spacing: 0; color: var(--muted); }

.rail { background: var(--surface); border: 1px solid var(--rule); border-radius: var(--r-lg); padding: var(--s5) var(--s5) var(--s4); }
.rail__plot { position: relative; padding-top: 22px; }
.rail__track { position: relative; height: 34px; border-radius: var(--r-md); background: var(--track); overflow: hidden; }
.rail__seg { position: absolute; top: 0; bottom: 0; }
.rail__seg.is-entries { background: var(--entries); }
.rail__seg.is-range { background: var(--range); }
.rail__tag { position: absolute; top: 0; font-size: var(--t-xs); color: var(--muted); white-space: nowrap; }
.rail__tag.is-end { transform: translateX(-100%); }
.rail__flat { position: absolute; top: 18px; bottom: 22px; border-left: 2px dashed var(--ink-2); }
.rail__now { position: absolute; top: 18px; bottom: 22px; border-left: 2px solid var(--now); z-index: 2; }
.rail__now span { position: absolute; top: -20px; left: 0; transform: translateX(-50%); padding: 0 var(--s2); border-radius: var(--r-pill); background: var(--now); color: var(--now-ink); font-size: var(--t-xs); font-weight: 800; line-height: 18px; white-space: nowrap; }
.lanes { position: relative; }
.lane { position: relative; height: 30px; margin-top: var(--s2); }
.lane__bar { position: absolute; top: 0; bottom: 0; min-width: 4px; border-radius: 6px; background: var(--entries); border-left: 3px solid var(--muted); }
.lane.is-up .lane__bar { background: var(--up-soft); border-left-color: var(--up); }
.lane.is-down .lane__bar { background: var(--down-soft); border-left-color: var(--down); }
.lane.is-open .lane__bar { border-right: 2px dashed var(--ink-2); }
.lane__label { position: absolute; top: 0; padding-left: var(--s3); font-size: var(--t-sm); font-weight: 600; line-height: 30px; white-space: nowrap; color: var(--ink); }
.lane__label.is-flip { transform: translateX(-100%); padding-left: 0; padding-right: var(--s3); }
.lane__label b { font-weight: 800; }
.lane__label .up, .lane__label .down { font-weight: 700; }
.lanes__empty { margin-top: var(--s3); font-size: var(--t-sm); color: var(--muted); }
.axis { position: relative; height: 22px; margin-top: var(--s2); }
.axis span { position: absolute; top: 4px; font-size: var(--t-xs); color: var(--muted); transform: translateX(-50%); white-space: nowrap; }
.axis span:first-child { transform: none; }
.axis span:last-child { transform: translateX(-100%); }

/* Sections */
.sec { position: relative; padding-block: var(--s6); }
.sec::before { content: ""; position: absolute; top: 0; left: var(--gutter); right: var(--gutter); border-top: 1px solid var(--rule); }
.sec__head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: var(--s2) var(--s5); margin-bottom: var(--s4); }
.sec h2 { font-size: var(--t-xl); font-weight: 800; letter-spacing: -0.015em; }
.sec__note { font-size: var(--t-sm); color: var(--muted); }

.tw { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: var(--t-sm); }
th, td { text-align: left; padding: var(--s3) var(--s4) var(--s3) 0; white-space: nowrap; vertical-align: baseline; }
th { font-weight: 500; color: var(--muted); border-bottom: 2px solid var(--rule-strong); }
td { border-bottom: 1px solid var(--rule); }
th.r, td.r { text-align: right; }
td.sym { font-weight: 800; font-size: var(--t-md); position: sticky; left: 0; background: var(--paper); }
td.now { font-weight: 800; font-size: var(--t-md); }
td.st { color: var(--muted); }
.empty { padding: var(--s4) 0; color: var(--muted); }

.limits { display: flex; flex-wrap: wrap; gap: var(--s2) var(--s6); margin: 0 0 var(--s4); font-size: var(--t-sm); }
.limits > div { display: flex; gap: var(--s2); }
.limits dt { color: var(--muted); }
.limits dd { margin: 0; font-weight: 700; }

.rows { list-style: none; margin: 0; padding: 0; border-top: 2px solid var(--rule-strong); }
.row { display: grid; grid-template-columns: minmax(0, 12rem) minmax(0, 9rem) minmax(0, 1fr) minmax(0, 15rem); gap: var(--s5); align-items: center; padding: var(--s4) 0; border-bottom: 1px solid var(--rule); }
.row__name { font-weight: 800; font-size: var(--t-lg); letter-spacing: -0.01em; }
.row__state { display: block; font-size: var(--t-sm); color: var(--muted); }
.row__state.is-live { color: var(--accent); font-weight: 600; }
.row__px { font-weight: 800; font-size: var(--t-lg); }
.row__chg { display: block; font-size: var(--t-sm); }
.row__meta { font-size: var(--t-sm); color: var(--muted); }
.row__meta span { display: block; }
.row svg { width: 100%; height: 56px; display: block; }
.row__chart { height: 150px; min-width: 0; }
.row.is-big .row__chart { grid-column: 1 / -1; grid-row: 2; height: 420px; }
.row__meta .link { margin-top: var(--s1); padding: var(--s1) 0; min-height: 32px; border: 0; background: none; color: var(--ink); font: 600 var(--t-sm)/1 var(--font); text-decoration: underline; text-underline-offset: 0.18em; cursor: pointer; transition: color var(--dur) ease; }
.row__meta .link:hover { color: var(--accent); }
.row__meta .link:active { color: var(--ink-2); }
.row__meta .link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; transition: none; }
.ch-band { fill: var(--range); }
.ch-px { fill: none; stroke: var(--accent); stroke-width: 1.6; }
.ch-stop { stroke: var(--down); stroke-dasharray: 4 3; }
.ch-target { stroke: var(--up); stroke-dasharray: 4 3; }
.ch-entry { stroke: var(--muted); stroke-dasharray: 2 3; }

.two { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: var(--s6); }
.notes { list-style: none; margin: 0; padding: 0; border-top: 2px solid var(--rule-strong); font-size: var(--t-sm); }
.notes li { padding: var(--s3) 0; border-bottom: 1px solid var(--rule); }
.notes li.quiet { color: var(--muted); }
pre { margin: 0; max-height: 420px; overflow: auto; padding: var(--s4); border-radius: var(--r-md); background: var(--surface); border: 1px solid var(--rule); font: 400 var(--t-xs)/1.6 ui-monospace, "SF Mono", Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--ink-2); }

.days { max-width: 44rem; }
.days a { font-weight: 600; text-underline-offset: 0.18em; }
.days a:hover { color: var(--accent); }
.days tr.is-current td { background: var(--entries); }
.days tr.is-current a { text-decoration: none; font-weight: 800; }
.days td:first-child { padding-left: var(--s2); }
.days th:first-child { padding-left: var(--s2); }
.foot { padding-block: var(--s5); font-size: var(--t-sm); color: var(--muted); }

@media (max-width: 60rem) {
  :root { --gutter: 24px; }
  .figs { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .figs .lead { grid-column: 1 / -1; }
  .row { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: var(--s2) var(--s4); }
  .row svg, .row__chart, .row__meta { grid-column: 1 / -1; }
  .row.is-big .row__chart { grid-row: auto; height: 320px; }
  .two { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 40rem) {
  :root { --gutter: 16px; }
  .day__title { font-size: var(--t-2xl); }
  .figs .lead dd { font-size: var(--t-3xl); }
  .rail { padding: var(--s4) var(--s3) var(--s3); }
  .axis .is-minor, .rail__tag.is-minor { display: none; }
  .lane__time { display: none; }
}
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; } }
</style></head><body>
<header class="container bar__in">
  <p class="mark">Autopilot</p>
  <div class="bar__right"><a id="back" class="btn" href="./" hidden>Back to today</a><span id="mode" class="cell"></span><span id="live" class="cell"></span><button id="theme" class="btn btn--icon" type="button" aria-label="Switch theme" title="Switch theme"></button></div>
</header>
<main>
<section class="container day" aria-labelledby="h-board">
  <h1 class="day__title" id="h-board">Today</h1>
  <p class="sub" id="sub">Loading the board…</p>
  <dl class="figs" id="kpis"></dl>
  <div class="rail" id="rail" role="img" aria-label="The session from 09:15 to 15:30 with each trade from buy to sell"></div>
</section>
<section class="container sec" aria-labelledby="h-trades">
  <div class="sec__head"><h2 id="h-trades">Trades</h2><p class="sec__note" id="trades-note">Paper fills, simulated at the live Yahoo price. Refreshes every 15 seconds.</p></div>
  <div class="tw" id="trades"><p class="empty">Loading trades…</p></div>
</section>
<section class="container sec" aria-labelledby="h-watch">
  <div class="sec__head"><h2 id="h-watch">Watching</h2></div>
  <dl class="limits" id="limits"></dl>
  <ol class="rows" id="syms"></ol>
</section>
<div class="container sec two">
  <section aria-labelledby="h-events"><div class="sec__head"><h2 id="h-events">Events</h2></div><ul class="notes" id="notes"></ul></section>
  <section aria-labelledby="h-log"><div class="sec__head"><h2 id="h-log">Log, newest last</h2></div><pre id="log" tabindex="0">Loading the log…</pre></section>
</div>
<section class="container sec" aria-labelledby="h-days">
  <div class="sec__head"><h2 id="h-days">Past days</h2><p class="sec__note">Each day's journal, kept in trader/autopilot. Open one to see its trades, charts and log.</p></div>
  <div class="tw days" id="days"></div>
</section>
</main>
<footer class="container foot"><p>Read-only. Places no orders. Yahoo prices can lag a minute or two. Not financial advice.</p></footer>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js" integrity="sha384-stKllnUqA9AD0gsKCuUtf5XlqAW7PwIgDagoNsTWkjkBmJ/GZ/uHTgEBxdLV2VSK" crossorigin="anonymous"></script>
<script>
const $=id=>document.getElementById(id);
const el=(t,cls,txt)=>{const e=document.createElement(t);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e};
const inr=x=>x==null||isNaN(x)?"-":"\u20b9"+Number(x).toLocaleString("en-IN",{minimumFractionDigits:2,maximumFractionDigits:2});
const sgn=x=>x==null||isNaN(x)?"-":(x>=0?"+":"-")+inr(Math.abs(x));
const cls=x=>x==null?"":x>0?"up":x<0?"down":"";
const VIEW=(new URLSearchParams(location.search).get("date")||"").match(/^\d{4}-\d\d-\d\d$/)?new URLSearchParams(location.search).get("date"):null;
const fmtDay=(d,yr)=>new Date(d+"T00:00:00Z").toLocaleDateString("en-IN",Object.assign({weekday:"long",day:"numeric",month:"long",timeZone:"UTC"},yr?{year:"numeric"}:{}));
try{const t=localStorage.getItem("theme");if(t)document.documentElement.dataset.theme=t}catch(e){}
const isDark=()=>{const r=document.documentElement;return r.dataset.theme?r.dataset.theme==="dark":matchMedia("(prefers-color-scheme: dark)").matches};
const SUN='<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';
const MOON='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
function themeIcon(){const b=$("theme"),dark=isDark(),label=dark?"Switch to light theme":"Switch to dark theme";b.innerHTML=dark?SUN:MOON;b.setAttribute("aria-label",label);b.title=label}
themeIcon();
$("theme").onclick=()=>{const n=isDark()?"light":"dark";document.documentElement.dataset.theme=n;try{localStorage.setItem("theme",n)}catch(e){}themeIcon();if(typeof retheme==="function")retheme()};
function fig(label,value,c,small,lead){const d=el("div",lead?"lead":null);d.append(el("dt",null,label));const v=el("dd",c||"",value);if(small)v.append(el("span","sm",small));d.append(v);return d}
function lim(label,value){const d=el("div");d.append(el("dt",null,label),el("dd",null,value));return d}
const OPEN=555,CLOSE=930;
const mins=hm=>{const m=/^(\d\d):(\d\d)/.exec(hm||"");return m?+m[1]*60+ +m[2]:null};
const pos=m=>Math.max(0,Math.min(100,(m-OPEN)/(CLOSE-OPEN)*100));
const hm=m=>String(Math.floor(m/60)).padStart(2,"0")+":"+String(m%60).padStart(2,"0");
function renderRail(d,st,trades){const R=$("rail"),L=d.limits||{},plot=el("div","rail__plot"),track=el("div","rail__track"),lanes=el("div","lanes"),axis=el("div","axis");
 const at=(node,m)=>{node.style.left=pos(m)+"%";return node};
 const seg=(a,b,c)=>{const s=at(el("div","rail__seg "+c),a);s.style.width=(pos(b)-pos(a))+"%";track.append(s)};
 const eb=mins(L.no_entries_before)||570,ea=mins(L.no_entries_after)||870,fb=mins(L.flat_by)||910;
 seg(eb,ea,"is-entries");seg(OPEN,OPEN+15,"is-range");
 const nm=d.live?mins(d.now.slice(11,16)):null,now=nm!=null&&nm>=OPEN&&nm<=CLOSE?nm:null,clear=m=>now==null||Math.abs(pos(m)-pos(now))>9;   // tags give way to the now pill
 plot.append(at(el("span","rail__tag","Opening range"),OPEN));
 if(clear(ea))plot.append(at(el("span","rail__tag is-end is-minor","Entries until "+hm(ea)),ea));
 if(clear(fb))plot.append(at(el("span","rail__tag is-end","Flat by "+hm(fb)),fb));
 plot.append(track,at(el("div","rail__flat"),fb));
 if(now!=null){const n=at(el("div","rail__now"),now);n.append(el("span",null,hm(now)));plot.append(n)}
 const shown=trades.filter(t=>t.entry_time&&t.entry_price!=null).sort((a,b)=>a.entry_time<b.entry_time?-1:1);
 for(const t of shown){const open=t.status==="open",q=d.quotes[t.symbol]||{},a=mins(t.entry_time);
  const b=open?(d.live?mins(d.now.slice(11,16)):fb):mins(t.touch_bar||t.exit_time)||a;
  const pnl=open?(q.price!=null?(q.price-t.entry_price)*t.qty:null):(t.net??t.gross);
  const lane=el("div","lane "+(pnl>0?"is-up":pnl<0?"is-down":"")+(open?" is-open":"")),bar=at(el("div","lane__bar"),a);
  bar.style.width=Math.max(0,pos(Math.min(Math.max(b,a),CLOSE))-pos(a))+"%";
  const lab=at(el("span","lane__label"),a);if(pos(a)>62)lab.classList.add("is-flip");
  lab.append(el("b",null,t.symbol),el("span","lane__time"," "+t.entry_time+" to "+(open?"open":(t.touch_bar||t.exit_time)))," ",el("span",cls(pnl),pnl==null?"":sgn(pnl)+(open?" open":" net")));
  lane.append(bar,lab);lanes.append(lane)}
 plot.append(lanes);
 if(!shown.length)plot.append(el("p","lanes__empty",d.live?"No trades yet. A lane appears here when the autopilot buys.":"No trades that day."));
 for(const m of[OPEN,600,660,720,780,840,900,CLOSE]){const s=at(el("span",m===OPEN||m===CLOSE||m%120===0?null:"is-minor",hm(m)),m);axis.append(s)}
 R.replaceChildren(plot,axis)}
function spark(q,tr){const NS="http://www.w3.org/2000/svg",s=document.createElementNS(NS,"svg");s.setAttribute("viewBox","0 0 300 56");s.setAttribute("preserveAspectRatio","none");s.setAttribute("aria-hidden","true");
 const b=(q&&q.bars)||[];if(b.length<2)return s;let lo=Math.min(...b.map(x=>x[3])),hi=Math.max(...b.map(x=>x[2]));
 const lv=[];if(tr){for(const k of["stop","target","entry_price"])if(tr[k]!=null)lv.push([k,+tr[k]])}
 for(const[,v]of lv){lo=Math.min(lo,v);hi=Math.max(hi,v)}const pad=(hi-lo)*.08||1;lo-=pad;hi+=pad;
 const X=i=>i/(b.length-1)*300,Y=v=>56-(v-lo)/(hi-lo)*56;
 const orb=b.slice(0,3);if(orb.length){const oh=Math.max(...orb.map(x=>x[2])),ol=Math.min(...orb.map(x=>x[3]));const r=document.createElementNS(NS,"rect");
  r.setAttribute("x",0);r.setAttribute("width",300);r.setAttribute("y",Y(oh));r.setAttribute("height",Math.max(1,Y(ol)-Y(oh)));r.setAttribute("class","ch-band");s.append(r)}
 const lc={stop:"ch-stop",target:"ch-target",entry_price:"ch-entry"};
 for(const[k,v]of lv){const l=document.createElementNS(NS,"line");l.setAttribute("x1",0);l.setAttribute("x2",300);l.setAttribute("y1",Y(v));l.setAttribute("y2",Y(v));
  l.setAttribute("class",lc[k]);l.setAttribute("vector-effect","non-scaling-stroke");s.append(l)}
 const p=document.createElementNS(NS,"polyline");p.setAttribute("points",b.map((x,i)=>X(i)+","+Y(x[1])).join(" "));p.setAttribute("class","ch-px");
 p.setAttribute("vector-effect","non-scaling-stroke");s.append(p);return s}
const LWC=window.LightweightCharts,rows=new Map();let lastD=null;
function tok(name){const c=document.createElement("canvas");c.width=c.height=1;const x=c.getContext("2d");
 x.fillStyle=getComputedStyle(document.documentElement).getPropertyValue(name).trim()||"gray";x.fillRect(0,0,1,1);const p=x.getImageData(0,0,1,1).data;return "rgb("+p[0]+","+p[1]+","+p[2]+")"}
function chartTheme(){const line={color:tok("--muted"),labelBackgroundColor:tok("--ink")};
 return{layout:{background:{type:"solid",color:tok("--paper")},textColor:tok("--muted"),fontFamily:getComputedStyle(document.body).fontFamily,fontSize:11},
  grid:{vertLines:{visible:false},horzLines:{color:tok("--rule")}},rightPriceScale:{borderColor:tok("--rule")},
  timeScale:{borderColor:tok("--rule"),timeVisible:true,secondsVisible:false},crosshair:{vertLine:line,horzLine:line}}}
function candleTheme(){const u=tok("--up"),w=tok("--down");return{upColor:u,downColor:w,borderUpColor:u,borderDownColor:w,wickUpColor:u,wickDownColor:w}}
function makeRow(s){const li=el("li","row"),a=el("div"),stt=el("span","row__state"),b=el("div"),px=el("span","row__px"),chg=el("span","row__chg"),box=el("div","row__chart"),m=el("div","row__meta"),info=el("div"),btn=el("button","link","Larger chart");
 a.append(el("span","row__name",s),stt);b.append(px,chg);btn.type="button";btn.setAttribute("aria-expanded","false");m.append(info,btn);li.append(a,b,box,m);
 box.setAttribute("role","img");box.setAttribute("aria-label",s+" 5-minute candles");
 const r={li,stt,px,chg,box,info,btn,chart:null,series:null,lines:[],big:false,n:0};
 if(LWC){r.chart=LWC.createChart(box,Object.assign(chartTheme(),{autoSize:true,handleScroll:false,handleScale:false}));
  r.series=r.chart.addCandlestickSeries(Object.assign(candleTheme(),{priceLineVisible:false,lastValueVisible:true}));
  new ResizeObserver(()=>{if(!r.big)r.chart.timeScale().fitContent()}).observe(box)}else btn.hidden=true;
 btn.onclick=()=>{r.big=!r.big;li.classList.toggle("is-big",r.big);btn.textContent=r.big?"Smaller chart":"Larger chart";btn.setAttribute("aria-expanded",String(r.big));
  if(r.chart){r.chart.applyOptions({handleScroll:r.big,handleScale:r.big});if(lastD)render(lastD);requestAnimationFrame(()=>r.chart.timeScale().fitContent())}};
 return r}
function barTime(date,hm){if(!hm)return null;const[h,m]=hm.split(":").map(Number);return Date.UTC(+date.slice(0,4),+date.slice(5,7)-1,+date.slice(8,10),h,m-m%5)/1000}
function drawChart(r,q,tr,date){if(!r.chart){r.box.replaceChildren(spark(q,tr));return}
 const b=(q.bars||[]).filter(x=>x.length>=7);if(!b.length)return;
 r.series.setData(b.map(x=>({time:x[6],open:x[4],high:x[2],low:x[3],close:x[1]})));
 for(const l of r.lines)r.series.removePriceLine(l);r.lines=[];
 const S=LWC.LineStyle,add=(price,color,title,style)=>r.lines.push(r.series.createPriceLine({price:+price,color,lineWidth:1,lineStyle:style,axisLabelVisible:r.big,title:r.big?title:""}));
 if(b.length>=3){const o=b.slice(0,3);add(Math.max(...o.map(x=>x[2])),tok("--accent"),"range high",S.Solid);add(Math.min(...o.map(x=>x[3])),tok("--muted"),"range low",S.Solid)}
 const mk=[];
 if(tr){if(tr.stop!=null)add(tr.stop,tok("--down"),"stop",S.Dashed);if(tr.target!=null)add(tr.target,tok("--up"),"target",S.Dashed);
  if(tr.entry_price!=null)add(tr.entry_price,tok("--ink-2"),"entry",S.Dotted);
  const t1=barTime(date,tr.entry_time),t2=barTime(date,tr.touch_bar||tr.exit_time);
  if(t1)mk.push({time:t1,position:"belowBar",color:tok("--accent"),shape:"arrowUp",text:r.big?"Buy "+tr.entry_price:""});
  if(t2)mk.push({time:t2,position:"aboveBar",color:tok("--ink"),shape:"arrowDown",text:r.big?"Sell "+tr.exit_price:""})}
 r.series.setMarkers(mk.sort((x,y)=>x.time-y.time));
 if(!r.big||!r.n)requestAnimationFrame(()=>r.chart.timeScale().fitContent());r.n=b.length}
function renderSyms(d,st){const sy=$("syms");
 if(!d.symbols.length){sy.replaceChildren(el("li","row",d.live?"No symbols in autopilot.symbols or trader/universe.txt.":"No symbols were picked or traded that day."));for(const r of rows.values())if(r.chart)r.chart.remove();rows.clear();return}
 for(const[k,r]of rows)if(!d.symbols.includes(k)){if(r.chart)r.chart.remove();r.li.remove();rows.delete(k)}
 for(const s of d.symbols){let r=rows.get(s);if(!r){r=makeRow(s);rows.set(s,r)}sy.append(r.li);
  const q=d.quotes[s]||{},tr=(st.trades||{})[s],done=(st.done||{})[s],state=tr?tr.status:done?done:(d.live?"watching":"watched");
  const ch=q.price!=null&&q.prev?(q.price-q.prev)/q.prev*100:null;
  r.stt.textContent=state;r.stt.className="row__state"+(state==="open"?" is-live":"");
  r.px.textContent=q.price!=null?inr(q.price):"-";r.chg.className="row__chg "+cls(ch);r.chg.textContent=ch==null?(q.error?"no chart":""):(ch>=0?"+":"")+ch.toFixed(2)+(d.live?"% today":"% that day");
  const info=[];if(q.bars&&q.bars.length>=3){const o=q.bars.slice(0,3);info.push("Range "+Math.min(...o.map(x=>x[3])).toFixed(2)+" to "+Math.max(...o.map(x=>x[2])).toFixed(2))}
  if(tr&&tr.stop!=null)info.push("Stop "+tr.stop+", target "+tr.target);if(q.as_of)info.push((d.live?"Price at ":"Close at ")+q.as_of+(q.stale?", stale":""));
  r.info.replaceChildren(...info.map(x=>el("span",null,x)));drawChart(r,q,tr,d.date)}}
function renderDays(d){const box=$("days"),days=d.days||[];box.replaceChildren();
 if(!days.length){box.append(el("p","empty","No saved days yet. A day appears here once the autopilot has written its journal."));return}
 const t=el("table"),th=el("thead"),h=el("tr"),tb=el("tbody");
 ["Day","Trades","Closed","Net after charges"].forEach((x,i)=>{const c=el("th",i?"r":"",x);c.scope="col";h.append(c)});th.append(h);t.append(th);
 for(const x of days){const row=el("tr"),c0=el("td"),a=el("a",null,fmtDay(x.date,true)+(x.date===d.today?", today":""));
  a.href=x.date===d.today?"./":"?date="+x.date;if(x.date===d.date){a.setAttribute("aria-current","page");row.className="is-current"}
  c0.append(a);row.append(c0,el("td","r",x.trades??"-"),el("td","r",x.closed??"-"),el("td","r "+cls(x.net),x.net==null?"-":sgn(x.net)));tb.append(row)}
 t.append(tb);box.append(t)}
function retheme(){for(const r of rows.values())if(r.chart){r.chart.applyOptions(chartTheme());r.series.applyOptions(candleTheme())}if(lastD)render(lastD)}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change",()=>{themeIcon();retheme()});
function render(d){lastD=d;
 const st=d.state||{},trades=Object.values(st.trades||{}),tot=(d.journal||{}).totals||{trades:0,closed:0,gross:0,charges:0,net:0},L=d.limits||{};
 const tick=(d.last_tick||"").match(/(\d{4}-\d\d-\d\d) (\d\d:\d\d) IST/);
 $("h-board").textContent=(d.live?"Today, ":"")+fmtDay(d.date,!d.live);
 document.title=d.live?"Autopilot Dashboard":"Autopilot, "+d.date;
 $("back").hidden=d.live;
 $("sub").textContent=d.live?d.now.slice(11)+" IST, last tick "+(tick?tick[2]:"none yet")+". Refreshes every 15 seconds."
  :"A past session, read from the files the autopilot saved that day. Charts come from Yahoo, which keeps 5-minute bars about 60 days.";
 $("trades-note").textContent=d.live?"Paper fills, simulated at the live Yahoo price.":"Paper fills as booked that day.";
 $("h-watch").textContent=d.live?"Watching":"Watched that day";
 $("mode").textContent=d.paper?"Paper":"Live";$("mode").className="cell "+(d.paper?"cell--paper":"cell--bad");
 let age=null;if(tick){age=(new Date(d.now.replace(" ","T"))-new Date(tick[1]+"T"+tick[2]))/60000}
 const hhmm=d.now.slice(11,16),inSess=hhmm>="08:55"&&hhmm<="15:35";
 $("live").textContent=!d.live?"Past day":!inSess?"Outside hours":age!=null&&age<=5?"Running":"No recent tick";
 $("live").className="cell "+(!d.live||!inSess?"cell--quiet":age!=null&&age<=5?"cell--live":"cell--warn");
 let unreal=0,invested=0,value=0,nOpen=0;for(const t of trades)if(t.status==="open"){const q=d.quotes[t.symbol],p=q&&q.price!=null?q.price:t.entry_price;
  nOpen++;invested+=t.entry_price*t.qty;value+=p*t.qty;unreal+=(p-t.entry_price)*t.qty}
 const lead=fig("Net P&L after charges",sgn(tot.net),cls(tot.net),tot.closed+" of "+tot.trades+" trade"+(tot.trades===1?"":"s")+" closed",true);
 $("kpis").replaceChildren(lead,...(d.live?[
  fig("Open P&L, gross",sgn(unreal),cls(unreal),nOpen?nOpen+" open":"none open"),
  fig("Invested in "+nOpen+" open trade"+(nOpen===1?"":"s"),inr(invested),"",L.budget?(invested/L.budget*100).toFixed(1)+"% of "+inr(L.budget):null),
  fig("Open value now",inr(value))]:[
  fig("Gross P&L",sgn(tot.gross),cls(tot.gross)),
  fig("Charges",inr(tot.charges)),
  fig("Trades",String(tot.trades),"",L.max_trades_per_day?"of "+L.max_trades_per_day+" allowed":null)]));
 renderRail(d,st,trades);
 $("limits").replaceChildren(
  lim("Trades",tot.trades+" of "+(L.max_trades_per_day??"-")),
  lim("Daily loss limit",inr(L.max_daily_loss_inr)),
  lim("Max loss a trade",inr(L.max_loss_per_trade_inr)),
  lim("Entries",(L.no_entries_before||"-")+" to "+(L.no_entries_after||"-")),
  lim("Flat by",L.flat_by||"-"),
  ...(st.pick?[lim("Picked",(st.pick.at?st.pick.at+", ":"")+((st.pick.symbols||[]).join(", ")||"none"))]:[]));
 renderSyms(d,st);
 const tw=$("trades");tw.replaceChildren();
 if(!trades.length){tw.append(el("p","empty",d.live?"No trades yet. The autopilot buys on paper when a 5-minute bar closes above the opening range.":"No trades that day."))}else{const t=el("table"),th=el("thead"),h=el("tr"),tb=el("tbody");
  const heads=["Symbol","Status","Qty","Entry","Invested","Now","Stop","Target","Exit","Gross","Charges","Net"],right=new Set([2,4,5,9,10,11]);
  heads.forEach((x,i)=>{const c=el("th",right.has(i)?"r":"",x);c.scope="col";h.append(c)});th.append(h);t.append(th);
  for(const r of trades){const q=d.quotes[r.symbol]||{},row=el("tr"),open=r.status==="open",g=open&&q.price!=null?(q.price-r.entry_price)*r.qty:r.gross;
   const cells=[[r.symbol,"sym"],[r.status+(r.exit_reason?", "+r.exit_reason:r.why?", "+r.why:""),"st"],[r.closed_qty||r.qty,"r"],
    [r.entry_price!=null?(r.entry_time||"")+"  "+inr(r.entry_price):"-",""],[r.entry_price!=null?inr(r.entry_price*(r.closed_qty||r.qty)):"-","r"],
    [open&&q.price!=null?inr(q.price):"-","r now"],[r.stop??"-",""],[r.target??"-",""],
    [r.exit_price!=null?(r.touch_bar||r.exit_time)+"  "+inr(r.exit_price)+(r.touch_bar&&r.booked_at!==r.touch_bar?" (booked "+(r.booked_at||r.exit_time)+")":""):"-",""],[sgn(g),"r "+cls(g)],[r.charges!=null?inr(r.charges):"-","r"],[sgn(r.net),"r "+cls(r.net)]];
   for(const[v,c]of cells)row.append(el("td",c,String(v)));tb.append(row)}
  t.append(tb);tw.append(t)}
 const n=$("notes");n.replaceChildren();const notes=(st.notes||[]).slice().reverse();
 if(!notes.length)n.append(el("li","quiet",d.live?"No events yet today. The first is the 09:00 news check.":"No events that day."));for(const x of notes)n.append(el("li",null,x));
 const lg=$("log"),atEnd=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-20;lg.textContent=(d.log||[]).join("\n")||(d.live?"No log yet today.":"No log for that day.");if(atEnd)lg.scrollTop=lg.scrollHeight;
 renderDays(d);
 if(!d.live&&timer){clearInterval(timer);timer=null}}
let timer=null;
async function load(){try{const r=await fetch("api"+(VIEW?"?date="+VIEW:""),{cache:"no-store"});render(await r.json())}catch(e){$("sub").textContent="The dashboard server is not reachable: "+e+". Start dashboard.py again."}}
load();timer=setInterval(load,15000);
</script></body></html>
"""


def make_handler(project):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api":
                day = (urllib.parse.parse_qs(url.query).get("date") or [None])[0]
                self._send(200, json.dumps(snapshot(project, day), ensure_ascii=False), "application/json; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")

        def log_message(self, fmt, *args):
            pass
    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live read-only dashboard for trader-autopilot. Never places orders.")
    ap.add_argument("--project", default=os.getcwd(), help="the folder holding trader/ (default: here)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    a = ap.parse_args(argv)
    project = os.path.abspath(os.path.expanduser(a.project))
    if not os.path.isdir(os.path.join(project, "trader")):
        print("no trader/ folder in %s" % project, file=sys.stderr)
        return 2
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(project))
    url = "http://127.0.0.1:%d/" % a.port
    print("Autopilot dashboard for %s at %s (Ctrl+C stops it; the autopilot keeps running)" % (project, url))
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
