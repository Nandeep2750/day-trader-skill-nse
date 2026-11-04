#!/usr/bin/env python3
"""A live, read-only dashboard for trader-autopilot (trader-autopilot).

    python3 dashboard.py --project ~/paper-test            # then open http://127.0.0.1:8765
    python3 dashboard.py --project ~/paper-test --port 9000 --open

Serves one page on 127.0.0.1 only. The page refreshes itself every 15 seconds from the
files the autopilot writes (trader/state/autopilot-DATE.json, the journal, today's log,
trader/config.json) and a 5-minute Yahoo chart per symbol (cached 60 seconds). It never
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


def get_chart_json(sym):
    """query1, then query2, then query1 again: a cold first request sometimes times out."""
    err = None
    for host in ("query1", "query2", "query1"):
        url = ("https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
               % (host, urllib.parse.quote(sym + ".NS")))
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                return json.load(r)
        except Exception as e:
            err = e
            time.sleep(0.5)
    raise err


def fetch_chart(sym):
    doc = get_chart_json(sym)
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
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    stamp = meta.get("regularMarketTime")
    as_of = ((datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(stamp)) + IST).strftime("%H:%M")
             if stamp else None)
    return {"price": price, "prev": prev, "as_of": as_of, "bars": bars}


def quote(sym):
    with _quotes_lock:
        hit = _quotes.get(sym)
    if hit and time.time() - hit[0] < QUOTE_TTL:
        return hit[1]
    try:
        data = fetch_chart(sym)
        stamp = time.time()
    except Exception as e:  # network, rate limit, bad JSON: show the last good one, marked stale
        if hit and "error" not in hit[1]:
            return dict(hit[1], stale=True)
        data = {"error": str(e)[:120]}
        stamp = time.time() - QUOTE_TTL + 15   # retry a failed symbol after 15 seconds
    with _quotes_lock:
        _quotes[sym] = (stamp, data)
    return data


def quotes(syms):
    """All symbols at once, so one slow fetch does not hold up the rest."""
    out = {}

    def one(s):
        out[s] = quote(s)
    threads = [threading.Thread(target=one, args=(s,)) for s in syms]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return {s: out.get(s, {"error": "no quote"}) for s in syms}


def snapshot(project):
    now = now_ist()
    date = now.date().isoformat()
    trader = os.path.join(project, "trader")
    cfg = read_json(os.path.join(trader, "config.json")) or {}
    state = read_json(os.path.join(trader, "state", "autopilot-%s.json" % date))
    journal = read_json(os.path.join(trader, "autopilot", "journal-%s.json" % date))
    ticks = [ln for ln in tail(os.path.join(trader, "autopilot", "logs", "launchd.log"), 40) if ln.strip()]
    syms = symbols_of(project, cfg)
    limits = {k: cfg.get(k) for k in ("budget", "max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day",
                                      "no_entries_before", "no_entries_after", "flat_by", "broker", "product")}
    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"), "date": date,
        "paper": cfg.get("paper", True) is not False,
        "auto_paper": ((cfg.get("autopilot") or {}).get("auto_paper") is True),
        "limits": limits, "symbols": syms,
        "state": state, "journal": journal,
        "last_tick": ticks[-1] if ticks else None,
        "log": tail(os.path.join(trader, "autopilot", "logs", date + ".log"), LOG_LINES),
        "quotes": quotes(syms),
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Autopilot Dashboard</title>
<meta name="description" content="Live board for the paper-trading autopilot: open trades, stop and target levels, P&amp;L, events and today's log.">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Chivo+Mono:wght@400;500;800&display=swap" rel="stylesheet">
<style>
/* Hallmark · shape: app screen · genre: modern-minimal · theme: Wayfind (deal 2/3, seed live-dashboard-where-one-4248·43cc4644·2026-11-04·r0) · redesign: full
 * nav: top bar, wordmark with mode, live state and theme · signature: the trade board on the ink band, live price in amber like a departure minute · states: loading, empty, error, full · wayfind: platform, night for dark
 * axes: light/mono/cool · posture: committed · contrast: pass · v2.0.0 */
/* Hallmark · pre-emit critique: P4 H5 E4 S4 R4 V4 · weakest: P, a board is the obvious reading of live trades; kept it because the trader reads it at a glance from across the desk */
/* Hallmark · waive 64 · every face on the page is Chivo Mono · Wayfind's material is the mono board face, so labels share it */
:root {
  color-scheme: light;
  --space-3xs: 2px; --space-2xs: 4px; --space-xs: 8px; --space-sm: 12px; --space-md: 16px; --space-lg: 24px; --space-xl: 40px; --space-2xl: 64px;
  --text-xs: 0.75rem; --text-sm: 0.875rem; --text-md: 1rem; --text-lg: 1.2rem; --text-xl: 1.44rem; --text-2xl: 1.728rem; --text-3xl: 2.074rem;
  --dur-micro: 120ms; --dur-short: 220ms;
  --container: 1280px; --gutter: 40px;
  --color-paper: oklch(95.5% 0.006 250); --color-paper-2: oklch(91% 0.009 250); --color-paper-3: oklch(99.5% 0.005 250);
  --color-ink: oklch(16% 0.014 262); --color-ink-2: oklch(29% 0.014 262); --color-muted: oklch(47% 0.016 260);
  --color-rule: oklch(80% 0.011 250); --color-ink-rule: oklch(34% 0.014 262);
  --color-accent: oklch(51% 0.22 264); --color-accent-2: oklch(81% 0.16 82); --color-accent-ink: oklch(99.5% 0.003 250); --color-focus: oklch(51% 0.22 264);
  --color-on-amber: oklch(16% 0.014 262);
  --color-accent-fill: oklch(44% 0.21 264); --color-down-fill: oklch(45% 0.17 27);
  --color-band: var(--color-ink); --color-band-ink: var(--color-paper); --color-band-muted: oklch(72% 0.012 250);
  --color-up: oklch(46% 0.12 150); --color-down: oklch(50% 0.19 27);
  --color-up-band: oklch(78% 0.14 150); --color-down-band: oklch(74% 0.15 27);
  --color-chart-band: oklch(91% 0.03 264);
  --font-body: "Chivo Mono", ui-monospace, "SF Mono", Menlo, monospace;
  --rule-hair: 1px; --rule-bar: 2px;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  color-scheme: dark;
  --color-paper: oklch(15% 0.012 262); --color-paper-2: oklch(20% 0.013 262); --color-paper-3: oklch(18% 0.012 262);
  --color-ink: oklch(96% 0.005 250); --color-ink-2: oklch(86% 0.008 250); --color-muted: oklch(70% 0.012 250);
  --color-rule: oklch(31% 0.014 262); --color-ink-rule: oklch(34% 0.014 262);
  --color-accent: oklch(62% 0.2 264); --color-focus: oklch(70% 0.17 264);
  --color-band: oklch(21% 0.014 262); --color-band-ink: oklch(96% 0.005 250);
  --color-up: oklch(76% 0.14 150); --color-down: oklch(72% 0.16 27);
  --color-chart-band: oklch(27% 0.05 264);
} }
:root[data-theme="dark"] {
  color-scheme: dark;
  --color-paper: oklch(15% 0.012 262); --color-paper-2: oklch(20% 0.013 262); --color-paper-3: oklch(18% 0.012 262);
  --color-ink: oklch(96% 0.005 250); --color-ink-2: oklch(86% 0.008 250); --color-muted: oklch(70% 0.012 250);
  --color-rule: oklch(31% 0.014 262); --color-ink-rule: oklch(34% 0.014 262);
  --color-accent: oklch(62% 0.2 264); --color-focus: oklch(70% 0.17 264);
  --color-band: oklch(21% 0.014 262); --color-band-ink: oklch(96% 0.005 250);
  --color-up: oklch(76% 0.14 150); --color-down: oklch(72% 0.16 27);
  --color-chart-band: oklch(27% 0.05 264);
}
*, *::before, *::after { box-sizing: border-box; }
html, body { overflow-x: clip; }
html { scrollbar-color: var(--color-muted) var(--color-paper); }
body { margin: 0; background: var(--color-paper); color: var(--color-ink); font-family: var(--font-body); font-size: var(--text-md); line-height: 1.55; font-variant-numeric: tabular-nums; -webkit-font-smoothing: antialiased; }
::selection { background: var(--color-accent-2); color: var(--color-on-amber); }
:focus-visible { outline: 2px solid var(--color-focus); outline-offset: 2px; }
h1, h2 { margin: 0; font-weight: 800; letter-spacing: -0.03em; line-height: 1.05; text-wrap: balance; overflow-wrap: break-word; min-width: 0; }
h1 { font-size: var(--text-xl); }
h2 { font-size: var(--text-lg); }
p { margin: 0; }
.container { width: 100%; max-width: var(--container); margin-inline: auto; padding-inline: var(--gutter); }

.bar { border-bottom: var(--rule-bar) solid var(--color-ink); }
.bar__in { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--space-sm) var(--space-lg); min-height: 72px; padding-block: var(--space-sm); }
.sub { font-size: var(--text-sm); color: var(--color-muted); }
.bar__right { display: flex; align-items: stretch; }
.cell { display: inline-flex; align-items: center; min-height: 40px; padding: 0 var(--space-md); border: var(--rule-bar) solid var(--color-ink); font-size: var(--text-sm); font-weight: 500; }
.cell + .cell { border-left: 0; }
.cell--paper { background: var(--color-ink); color: var(--color-paper); font-weight: 800; }
.cell--live { background: var(--color-accent-fill); border-color: var(--color-accent-fill); color: var(--color-accent-ink); }
.cell--warn { background: var(--color-accent-2); color: var(--color-on-amber); }
.cell--bad { background: var(--color-down-fill); border-color: var(--color-down-fill); color: var(--color-accent-ink); }
.cell--quiet { color: var(--color-muted); }
.btn { min-height: 40px; padding: 0 var(--space-md); margin-left: var(--space-sm); border: var(--rule-bar) solid var(--color-ink); background: transparent; color: var(--color-ink); font-family: var(--font-body); font-size: var(--text-sm); font-weight: 800; cursor: pointer; transition: background-color var(--dur-micro) steps(1), color var(--dur-micro) steps(1); }
.btn:hover { background: var(--color-ink); color: var(--color-paper); }
.btn:active { background: var(--color-ink-2); color: var(--color-paper); }
.btn:focus-visible { outline: 2px solid var(--color-focus); outline-offset: 2px; transition: none; }
.btn:disabled { opacity: 0.55; cursor: not-allowed; }

.band { background: var(--color-band); color: var(--color-band-ink); padding-block: var(--space-xl); }
.band h2 { color: var(--color-band-ink); }
.band__head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: var(--space-xs) var(--space-lg); margin-bottom: var(--space-lg); }
.band__note { font-size: var(--text-sm); color: var(--color-band-muted); }
.figs { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 0 0 var(--space-xl); border-top: var(--rule-bar) solid var(--color-band-ink); }
.figs > div { padding: var(--space-md) var(--space-md) 0 0; min-width: 0; }
.figs dt { font-size: var(--text-sm); font-weight: 500; color: var(--color-band-muted); }
.figs dd { margin: var(--space-2xs) 0 0; font-size: var(--text-2xl); font-weight: 800; letter-spacing: -0.03em; color: var(--color-accent-2); overflow-wrap: anywhere; }
.figs .sm { display: block; font-size: var(--text-sm); font-weight: 500; letter-spacing: 0; color: var(--color-band-muted); }
.figs .up { color: var(--color-up-band); } .figs .down { color: var(--color-down-band); }

.tw { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: var(--space-sm) var(--space-md) var(--space-sm) 0; white-space: nowrap; vertical-align: baseline; }
th { font-size: var(--text-sm); font-weight: 500; color: var(--color-band-muted); border-bottom: var(--rule-bar) solid var(--color-band-ink); }
td { border-bottom: var(--rule-hair) solid var(--color-ink-rule); }
th.r, td.r { text-align: right; }
td.sym { font-weight: 800; position: sticky; left: 0; background: var(--color-band); }
td.now { font-size: var(--text-xl); font-weight: 800; color: var(--color-accent-2); }
td.up { color: var(--color-up-band); } td.down { color: var(--color-down-band); }
td.st { color: var(--color-band-muted); }
.empty { padding: var(--space-lg) 0; border-top: var(--rule-bar) solid var(--color-band-ink); color: var(--color-band-muted); }

.sec { padding-block: var(--space-xl); }
.sec h2 { margin-bottom: var(--space-md); }
.limits { display: flex; flex-wrap: wrap; gap: var(--space-xs) var(--space-xl); margin: 0; padding: var(--space-md) 0; border-block: var(--rule-hair) solid var(--color-rule); font-size: var(--text-sm); }
.limits > div { display: flex; gap: var(--space-xs); }
.limits dt { color: var(--color-muted); font-weight: 500; }
.limits dd { margin: 0; font-weight: 800; }

.rows { list-style: none; margin: 0; padding: 0; border-top: var(--rule-bar) solid var(--color-ink); }
.row { display: grid; grid-template-columns: minmax(0, 13rem) minmax(0, 10rem) minmax(0, 1fr) minmax(0, 17rem); gap: var(--space-lg); align-items: center; padding: var(--space-md) 0; border-bottom: var(--rule-hair) solid var(--color-rule); }
.row__name { font-weight: 800; font-size: var(--text-lg); }
.row__state { display: block; font-size: var(--text-sm); color: var(--color-muted); }
.row__state.is-live { color: var(--color-accent); font-weight: 500; }
.row__px { font-weight: 800; font-size: var(--text-lg); }
.row__chg { display: block; font-size: var(--text-sm); }
.row__meta { font-size: var(--text-sm); color: var(--color-muted); }
.row__meta span { display: block; }
.row svg { width: 100%; height: 56px; display: block; }
.row__chart { height: 150px; min-width: 0; }
.row.is-big .row__chart { grid-column: 1 / -1; grid-row: 2; height: 400px; }
.row__meta .link { margin-top: var(--space-2xs); padding: var(--space-2xs) 0; min-height: 32px; border: 0; background: none; color: var(--color-ink); font: 500 var(--text-sm)/1 var(--font-body); text-decoration: underline; text-underline-offset: 0.16em; cursor: pointer; transition: color var(--dur-micro) steps(1); }
.row__meta .link:hover { color: var(--color-accent); }
.row__meta .link:active { color: var(--color-ink-2); }
.row__meta .link:focus-visible { outline: 2px solid var(--color-focus); outline-offset: 2px; transition: none; }
.row__meta .link:disabled { opacity: 0.55; cursor: not-allowed; }
.up { color: var(--color-up); } .down { color: var(--color-down); }
.ch-band { fill: var(--color-chart-band); }
.ch-px { fill: none; stroke: var(--color-accent); stroke-width: 1.6; }
.ch-stop { stroke: var(--color-down); stroke-dasharray: 4 3; }
.ch-target { stroke: var(--color-up); stroke-dasharray: 4 3; }
.ch-entry { stroke: var(--color-muted); stroke-dasharray: 2 3; }

.two h2 { margin-bottom: var(--space-md); }
.two { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: var(--space-xl); padding-bottom: var(--space-xl); }
.notes { list-style: none; margin: 0; padding: 0; border-top: var(--rule-bar) solid var(--color-ink); font-size: var(--text-sm); }
.notes li { padding: var(--space-sm) 0; border-bottom: var(--rule-hair) solid var(--color-rule); }
.notes li.quiet { color: var(--color-muted); }
pre { margin: 0; max-height: 400px; overflow: auto; padding: var(--space-md); border: var(--rule-bar) solid var(--color-ink); background: var(--color-paper-3); font: 400 var(--text-xs)/1.5 var(--font-body); white-space: pre-wrap; overflow-wrap: anywhere; color: var(--color-ink-2); }
.foot { padding-block: var(--space-lg); border-top: var(--rule-hair) solid var(--color-rule); font-size: var(--text-sm); color: var(--color-muted); }

@media (max-width: 60rem) {
  :root { --gutter: 24px; }
  .figs { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .row { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: var(--space-xs) var(--space-md); }
  .row svg, .row__chart, .row__meta { grid-column: 1 / -1; }
  .row.is-big .row__chart { grid-row: auto; height: 320px; }
  .two { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 40rem) {
  :root { --gutter: 16px; }
  .bar__right { flex-wrap: wrap; }
  .figs dd { font-size: var(--text-xl); }
}
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; } }
</style></head><body>
<header class="bar"><div class="container bar__in">
  <div><h1>Autopilot</h1><p class="sub" id="sub">Loading the board…</p></div>
  <div class="bar__right"><span id="mode" class="cell"></span><span id="live" class="cell"></span><button id="theme" class="btn" type="button">Theme</button></div>
</div></header>
<main>
<section class="band" aria-labelledby="h-board"><div class="container">
  <div class="band__head"><h2 id="h-board">Today’s trades</h2><p class="band__note">Paper fills, simulated at the live Yahoo price. Refreshes every 15 seconds.</p></div>
  <dl class="figs" id="kpis"></dl>
  <div class="tw" id="trades"><p class="empty">Loading trades…</p></div>
</div></section>
<section class="container sec" aria-labelledby="h-watch">
  <h2 id="h-watch">Watching</h2>
  <dl class="limits" id="limits"></dl>
  <ol class="rows" id="syms"></ol>
</section>
<div class="container two">
  <section aria-labelledby="h-events"><h2 id="h-events">Events</h2><ul class="notes" id="notes"></ul></section>
  <section aria-labelledby="h-log"><h2 id="h-log">Log, newest last</h2><pre id="log" tabindex="0">Loading the log…</pre></section>
</div>
</main>
<footer class="container foot"><p>Read-only. Places no orders. Yahoo prices can lag a minute or two. Not financial advice.</p></footer>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js" integrity="sha384-stKllnUqA9AD0gsKCuUtf5XlqAW7PwIgDagoNsTWkjkBmJ/GZ/uHTgEBxdLV2VSK" crossorigin="anonymous"></script>
<script>
const $=id=>document.getElementById(id);
const el=(t,cls,txt)=>{const e=document.createElement(t);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e};
const inr=x=>x==null||isNaN(x)?"-":"\u20b9"+Number(x).toLocaleString("en-IN",{minimumFractionDigits:2,maximumFractionDigits:2});
const sgn=x=>x==null||isNaN(x)?"-":(x>=0?"+":"-")+inr(Math.abs(x));
const cls=x=>x==null?"":x>0?"up":x<0?"down":"";
try{const t=localStorage.getItem("theme");if(t)document.documentElement.dataset.theme=t}catch(e){}
$("theme").onclick=()=>{const r=document.documentElement,dark=r.dataset.theme?r.dataset.theme==="dark":matchMedia("(prefers-color-scheme: dark)").matches,n=dark?"light":"dark";r.dataset.theme=n;try{localStorage.setItem("theme",n)}catch(e){}if(typeof retheme==="function")retheme()};
function fig(label,value,c,small){const d=el("div");d.append(el("dt",null,label));const v=el("dd",c||"",value);if(small)v.append(el("span","sm",small));d.append(v);return d}
function lim(label,value){const d=el("div");d.append(el("dt",null,label),el("dd",null,value));return d}
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
function chartTheme(){const line={color:tok("--color-muted"),labelBackgroundColor:tok("--color-ink")};
 return{layout:{background:{type:"solid",color:tok("--color-paper")},textColor:tok("--color-muted"),fontFamily:getComputedStyle(document.body).fontFamily,fontSize:11},
  grid:{vertLines:{visible:false},horzLines:{color:tok("--color-rule")}},rightPriceScale:{borderColor:tok("--color-rule")},
  timeScale:{borderColor:tok("--color-rule"),timeVisible:true,secondsVisible:false},crosshair:{vertLine:line,horzLine:line}}}
function candleTheme(){const u=tok("--color-up"),w=tok("--color-down");return{upColor:u,downColor:w,borderUpColor:u,borderDownColor:w,wickUpColor:u,wickDownColor:w}}
function makeRow(s){const li=el("li","row"),a=el("div"),stt=el("span","row__state"),b=el("div"),px=el("span","row__px"),chg=el("span","row__chg"),box=el("div","row__chart"),m=el("div","row__meta"),info=el("div"),btn=el("button","link","Larger chart");
 a.append(el("span","row__name",s),stt);b.append(px,chg);btn.type="button";btn.setAttribute("aria-expanded","false");m.append(info,btn);li.append(a,b,box,m);
 box.setAttribute("role","img");box.setAttribute("aria-label",s+" 5-minute candles today");
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
 if(b.length>=3){const o=b.slice(0,3);add(Math.max(...o.map(x=>x[2])),tok("--color-accent"),"range high",S.Solid);add(Math.min(...o.map(x=>x[3])),tok("--color-muted"),"range low",S.Solid)}
 const mk=[];
 if(tr){if(tr.stop!=null)add(tr.stop,tok("--color-down"),"stop",S.Dashed);if(tr.target!=null)add(tr.target,tok("--color-up"),"target",S.Dashed);
  if(tr.entry_price!=null)add(tr.entry_price,tok("--color-ink-2"),"entry",S.Dotted);
  const t1=barTime(date,tr.entry_time),t2=barTime(date,tr.touch_bar||tr.exit_time);
  if(t1)mk.push({time:t1,position:"belowBar",color:tok("--color-accent"),shape:"arrowUp",text:r.big?"Buy "+tr.entry_price:""});
  if(t2)mk.push({time:t2,position:"aboveBar",color:tok("--color-ink"),shape:"arrowDown",text:r.big?"Sell "+tr.exit_price:""})}
 r.series.setMarkers(mk.sort((x,y)=>x.time-y.time));
 if(!r.big||!r.n)requestAnimationFrame(()=>r.chart.timeScale().fitContent());r.n=b.length}
function renderSyms(d,st){const sy=$("syms");
 if(!d.symbols.length){sy.replaceChildren(el("li","row","No symbols in autopilot.symbols or trader/universe.txt."));rows.clear();return}
 for(const[k,r]of rows)if(!d.symbols.includes(k)){if(r.chart)r.chart.remove();r.li.remove();rows.delete(k)}
 for(const s of d.symbols){let r=rows.get(s);if(!r){r=makeRow(s);rows.set(s,r)}sy.append(r.li);
  const q=d.quotes[s]||{},tr=(st.trades||{})[s],done=(st.done||{})[s],state=tr?tr.status:done?done:"watching";
  const ch=q.price!=null&&q.prev?(q.price-q.prev)/q.prev*100:null;
  r.stt.textContent=state;r.stt.className="row__state"+(state==="open"?" is-live":"");
  r.px.textContent=q.price!=null?inr(q.price):"-";r.chg.className="row__chg "+cls(ch);r.chg.textContent=ch==null?(q.error?"no quote yet":""):(ch>=0?"+":"")+ch.toFixed(2)+"% today";
  const info=[];if(q.bars&&q.bars.length>=3){const o=q.bars.slice(0,3);info.push("Range "+Math.min(...o.map(x=>x[3])).toFixed(2)+" to "+Math.max(...o.map(x=>x[2])).toFixed(2))}
  if(tr&&tr.stop!=null)info.push("Stop "+tr.stop+", target "+tr.target);if(q.as_of)info.push("Price at "+q.as_of+(q.stale?", stale":""));
  r.info.replaceChildren(...info.map(x=>el("span",null,x)));drawChart(r,q,tr,d.date)}}
function retheme(){for(const r of rows.values())if(r.chart){r.chart.applyOptions(chartTheme());r.series.applyOptions(candleTheme())}if(lastD)render(lastD)}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change",retheme);
function render(d){lastD=d;
 const st=d.state||{},trades=Object.values(st.trades||{}),tot=(d.journal||{}).totals||{trades:0,closed:0,gross:0,charges:0,net:0},L=d.limits||{};
 const tick=(d.last_tick||"").match(/(\d{4}-\d\d-\d\d) (\d\d:\d\d) IST/);
 $("sub").textContent=d.now.slice(11)+" IST, last tick "+(tick?tick[2]:"none yet");
 $("mode").textContent=d.paper?"Paper":"Live";$("mode").className="cell "+(d.paper?"cell--paper":"cell--bad");
 let age=null;if(tick){age=(new Date(d.now.replace(" ","T"))-new Date(tick[1]+"T"+tick[2]))/60000}
 const hhmm=d.now.slice(11,16),inSess=hhmm>="08:55"&&hhmm<="15:35";
 $("live").textContent=!inSess?"Outside hours":age!=null&&age<=5?"Running":"No recent tick";
 $("live").className="cell "+(!inSess?"cell--quiet":age!=null&&age<=5?"cell--live":"cell--warn");
 let unreal=0,invested=0,value=0,nOpen=0;for(const t of trades)if(t.status==="open"){const q=d.quotes[t.symbol],p=q&&q.price!=null?q.price:t.entry_price;
  nOpen++;invested+=t.entry_price*t.qty;value+=p*t.qty;unreal+=(p-t.entry_price)*t.qty}
 $("kpis").replaceChildren(
  fig("Invested in "+nOpen+" open trade"+(nOpen===1?"":"s"),inr(invested),"",L.budget?(invested/L.budget*100).toFixed(1)+"% of "+inr(L.budget):null),
  fig("Open value now",inr(value)),
  fig("Open P&L, gross",sgn(unreal),cls(unreal)),
  fig("Net P&L, closed after charges",sgn(tot.net),cls(tot.net),tot.closed+" closed"));
 $("limits").replaceChildren(
  lim("Trades",tot.trades+" of "+(L.max_trades_per_day??"-")),
  lim("Daily loss limit",inr(L.max_daily_loss_inr)),
  lim("Max loss a trade",inr(L.max_loss_per_trade_inr)),
  lim("Entries",(L.no_entries_before||"-")+" to "+(L.no_entries_after||"-")),
  lim("Flat by",L.flat_by||"-"),
  ...(st.pick?[lim("Picked",(st.pick.at?st.pick.at+", ":"")+((st.pick.symbols||[]).join(", ")||"none"))]:[]));
 renderSyms(d,st);
 const tw=$("trades");tw.replaceChildren();
 if(!trades.length){tw.append(el("p","empty","No trades yet. The autopilot buys on paper when a 5-minute bar closes above the opening range."))}else{const t=el("table"),th=el("thead"),h=el("tr"),tb=el("tbody");
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
 if(!notes.length)n.append(el("li","quiet","No events yet today. The first is the 09:00 news check."));for(const x of notes)n.append(el("li",null,x));
 const lg=$("log"),atEnd=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-20;lg.textContent=(d.log||[]).join("\n")||"No log yet today.";if(atEnd)lg.scrollTop=lg.scrollHeight}
async function load(){try{const r=await fetch("api",{cache:"no-store"});render(await r.json())}catch(e){$("sub").textContent="The dashboard server is not reachable: "+e+". Start dashboard.py again."}}
load();setInterval(load,15000);
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
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api":
                self._send(200, json.dumps(snapshot(project), ensure_ascii=False), "application/json; charset=utf-8")
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
