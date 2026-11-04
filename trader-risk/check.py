#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught. NSE cash equity only; money is in rupees.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-10-29 09:40"        # pin the clock (IST)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        missing symbol or side; no whole-share qty (NSE has no
                fractional shares and no rupee-amount orders); a limit order
                without a limit price; a type other than market or limit; a
                time_in_force other than day; an exchange other than NSE; a
                product other than MIS or CNC; a price off NSE's tick (0.01
                under Rs 250, 0.05 to Rs 1,000, 0.10 to Rs 5,000, 0.50 to
                Rs 10,000, 1.00 to Rs 20,000, 5.00 above; within 10% of a band
                edge only the coarser tick, because NSE sets each stock's
                tick from last month's close); a bracket that misses its stop
                or target. Case does not matter ("LIMIT" is "limit")
  asset         anything that is not a plain NSE symbol (RELIANCE, M&M,
                BAJAJ-AUTO): no .BO, no .NS suffix, no options or futures
  short         selling more than you hold. Short selling is not supported end
                to end yet, so this blocks even with "allow_short": true

What blocks an entry (on top):

  blocked       a symbol on your "blocked" list
  product       CNC (delivery) unless "allow_delivery": true: this is a
                day-trading pack, so entries are MIS (intraday)
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin.
                "allow_margin": true lets an MIS entry use the broker's
                intraday margin: then it needs only mis_margin_pct (20) of its
                value in cash
  duplicate     the same symbol and side twice in this batch, or already
                placed (or approved and not yet resolved) today
                (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (a bracket stop_loss_stop_price
                or the order's plan stop); a stop on the wrong side of the entry;
                a fresh price already at or through the stop; on a live
                account ("paper": false), a stop that is only in the plan
                (no exchange stop: use a bracket order)
  price         a market entry with neither a fresh price (under 15 minutes)
                nor a fresh plan entry: a plan's entry counts only while it is
                under 15 minutes old, timed by the OLDER of the plan's
                created_at and its date and entry_time
  loss          qty x (entry - stop), plus the estimated round-trip charges
                (brokerage, STT, NSE, SEBI, stamp, GST; "include_charges"),
                over max_loss_per_trade_inr. The entry is the limit price for a
                limit order, else the HIGHER of the plan's entry and a fresh
                price (under 15 minutes old), so a stale plan cannot
                understate the risk
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses (charges included) at the stops of today's
                entries plus this one over max_daily_loss_inr (win or lose,
                each counts in full)
  trades        max_trades_per_day entries already placed today
  window        the market is closed (weekend, NSE holiday, outside 9:15 to
                15:30), always, even with the bounds blank; or before
                no_entries_before or after no_entries_after (IST)

Warnings, not blocks: a flat_by later than your broker's MIS auto square-off
(Zerodha 15:20, Angel One 15:15), a live account, a stop in the plan only.

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order
(its fields, its plan's entry, stop and target, the stop it cancels, and the
config's product, broker and paper flag), the time it was checked and the
all-in max loss, so trader-broker only shows an order as ready if THIS exact
order passed today, under this config, a few minutes ago.
Today's entries, trades and daily loss count orders placed today and orders
approved but not yet resolved, leaving out the order's own records (by
fingerprint), so re-checking an approved order does not block it as its own
duplicate. A logged entry with no max loss counts as a full
max_loss_per_trade_inr. The account is live only when "paper" is literally
false.
Education and tooling, not financial advice.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 80000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 1000, "max_daily_loss_inr": 3000, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10",
    "broker": "zerodha", "product": "MIS", "paper": True, "blocked": [], "allow_options": False,
    "allow_margin": False, "mis_margin_pct": 20, "allow_short": False, "allow_delivery": False,
    "include_charges": True,
}
OLD_KEYS = ("max_order_usd", "min_order_usd", "max_loss_per_trade_usd", "max_daily_loss_usd")
# the fields that make an order what it is: same tuple in trader-broker/ticket.py
FINGERPRINT_FIELDS = ("symbol", "exchange", "side", "qty", "type", "time_in_force", "limit_price", "product",
                "client_order_id", "order_class", "take_profit_limit_price", "stop_loss_stop_price")
PRICE_FIELDS = ("limit_price", "take_profit_limit_price", "stop_loss_stop_price")
TYPES = {"market", "limit"}
TIFS = {"day"}
PRODUCTS = {"MIS", "CNC"}
NSE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CENT = 0.005
FRESH_MINUTES = 15
ENTRY_STATUSES = ("approved", "declined", "placed", "denied", "failed")
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stale price": "an old price"}
SQUARE_OFF = {"zerodha": ("Zerodha", datetime.time(15, 20)), "angelone": ("Angel One", datetime.time(15, 15))}

# NSE cash tick sizes in paise: a price below X rupees moves in steps of this many paise
TICKS = ((250, 1), (1000, 5), (5000, 10), (10000, 50), (20000, 100), (None, 500))
# NSE fixes each stock's tick from the previous month's close, which this pack does not know. Within
# 10% of a band edge the stock could sit in either band, so only the coarser tick is safe there (a
# price on the coarser tick is also on the finer one).
BAND_EDGES = (250, 1000, 5000, 10000, 20000)
EDGE_ZONE = 0.10

# NSE equity intraday charges (estimates; "charges" in the config overrides any of them)
CHARGES = {
    "zerodha": {"brokerage_pct": 0.03, "brokerage_cap": 20.0, "brokerage_min": 0.0},
    "angelone": {"brokerage_pct": 0.10, "brokerage_cap": 20.0, "brokerage_min": 5.0},
}
RATES = {
    "stt_sell_pct": 0.025,      # on the sell value
    "exchange_pct": 0.00297,    # NSE transaction charge, both sides
    "sebi_pct": 0.0001,         # Rs 10 per crore, both sides
    "stamp_buy_pct": 0.003,     # on the buy value
    "gst_pct": 18.0,            # on brokerage + exchange + SEBI
}

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
CALENDAR_YEARS = (2026,)   # NSE publishes next year's list in December: add 2027 then
# Special sessions (Muhurat trading on Sunday 2026-11-08, budget-day Sundays) are not modelled:
# weekends stay closed.
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- time (IST, no tz files)

IST = datetime.timedelta(hours=5, minutes=30)   # India Standard Time, no daylight saving


def to_ist(utc):
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    return local.replace(tzinfo=None) - IST


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise RiskError("--now wants a time like \"2026-10-29 09:40\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def hhmm(value, key):
    """"09:30" -> time(9, 30); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an IST time like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def plan_time(plan):
    """When a plan's entry price is from, as naive UTC: the OLDER of its created_at and its date and
    entry_time (IST, "9:40 AM" or "09:40"). The entry bar's time is the price's time; a plan written
    later from an older trigger is no fresher than the trigger. None when the plan says neither."""
    times = [t for t in (parse_utc(plan.get("created_at")), _entry_bar_time(plan)) if t]
    return min(times) if times else None


def _entry_bar_time(plan):
    """A plan's date and entry_time as naive UTC, or None."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(plan.get("entry_time") or ""))
    if not m or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(plan.get("date") or "")):
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        h = h % 12 + (12 if m.group(3).lower() == "pm" else 0)
    if h > 23 or mi > 59:
        return None
    d = datetime.date.fromisoformat(plan["date"])
    return ist_to_utc(datetime.datetime.combine(d, datetime.time(h, mi)))


def window_check(now_ist, c):
    """Reasons an entry cannot be placed now (IST). NSE must be open (a trading day, 9:15 to 15:30)
    whatever the config says; no_entries_before / no_entries_after narrow that further when set."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    warns = []
    d, t = now_ist.date(), now_ist.time().replace(second=0, microsecond=0)
    if d.year not in CALENDAR_YEARS:
        warns.append("the NSE holiday list covers %s only: check nseindia.com for %s"
                     % (" and ".join(map(str, CALENDAR_YEARS)), d.isoformat()))
    ok, why = trading_day(d)
    stamp = now_ist.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        return ["window: the market is closed today (%s, %s IST)" % (why, stamp)], warns
    if t < OPEN or now_ist.time() >= CLOSE:
        return ["window: the market is closed (%s IST; the NSE session is %s to %s)"
                % (stamp, OPEN.strftime("%H:%M"), CLOSE.strftime("%H:%M"))], warns
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                    t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"),
                                                                   t.strftime("%H:%M"))], warns
    return [], warns


def square_off_warning(c):
    """A warning when flat_by is at or after the broker's MIS auto square-off, else None."""
    flat = hhmm(c.get("flat_by"), "flat_by")
    broker = str(c.get("broker") or "").strip().lower()
    if broker not in SQUARE_OFF:
        return "broker %r is not zerodha or angelone: trader-broker cannot send to it" % c.get("broker")
    name, cut = SQUARE_OFF[broker]
    if flat is not None and str(c.get("product") or "MIS").upper() == "MIS" and flat >= cut:
        return ("flat_by %s is not before %s's MIS auto square-off at %s: the broker will close the "
                "position for you, at the market, with a fee. Set flat_by earlier (15:10 is the default)"
                % (flat.strftime("%H:%M"), name, cut.strftime("%H:%M")))
    return None


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def _fp_symbol(text):
    sym = str(text or "").strip().upper()
    return sym[:-3] if sym.endswith(".NS") else sym


def _fp_value(key, value):
    if value is None:
        return None
    if key in ("type", "side", "time_in_force", "order_class"):
        return str(value).strip().lower()
    if key in ("product", "exchange"):
        return str(value).strip().upper()
    if key == "symbol":
        return _fp_symbol(value)
    return str(value).strip() if isinstance(value, str) else str(value)


def is_paper(cfg):
    """Paper unless the config says "paper": false, literally. Keep identical to trader-broker/ticket.py."""
    return (cfg or {}).get("paper", True) is not False


def cancel_ids(order):
    """The broker ids of the stops an order cancels (a flatten). Keep identical to trader-broker/ticket.py."""
    cs = order.get("cancel_stop") or {}
    if not isinstance(cs, dict):
        return []
    ids = cs.get("broker_order_ids")
    if isinstance(ids, (list, tuple)):
        raw = [str(x) for x in ids if x is not None]
    else:
        raw = str(cs.get("broker_order_id") or "").split(",")
    return [x.strip() for x in raw if x.strip()]


def fingerprint(order, cfg=None):
    """The exact order that was checked, under the config it was checked with.

    Keep identical to trader-broker/ticket.py: an order is READY there only if this hash matches.
    Config defaults (product MIS, broker zerodha, paper true) apply when the config leaves them out."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    plan = order.get("plan") if isinstance(order.get("plan"), dict) else None
    payload = {k: _fp_value(k, order.get(k)) for k in FINGERPRINT_FIELDS}
    payload["plan"] = ({"entry": plan.get("entry"), "stop": plan.get("stop"), "target": plan.get("target")}
                       if plan is not None else None)
    payload["cancel_stop"] = sorted(cancel_ids(order)) or None
    payload["product_resolved"] = str(order.get("product") or c.get("product") or "MIS").strip().upper()
    payload["broker"] = str(c.get("broker") or "zerodha").strip().lower()
    payload["paper"] = is_paper(c)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def paise(x):
    """Rupees -> whole paise (half up)."""
    return int(math.floor(float(x) * 100 + 0.5 + 1e-9))


def band_tick_paise(price):
    """NSE's tick, in paise, for the band a price is in."""
    p = paise(price)
    for below, tick in TICKS:
        if below is None or p < below * 100:
            return tick
    return TICKS[-1][1]


def tick_paise(price):
    """The safe tick, in paise: the band's tick, or the coarser one within 10% of a band edge."""
    t = band_tick_paise(price)
    p = float(price)
    for edge in BAND_EDGES:
        if edge * (1 - EDGE_ZONE) <= p <= edge * (1 + EDGE_ZONE):
            t = max(t, band_tick_paise(edge))
    return t


def tick_hint(price):
    """"1163.35" -> (False, "NSE's 0.10 tick for this price: 1163.30 or 1163.40"); on tick -> (True, "")."""
    p, t = paise(price), tick_paise(price)
    if p % t == 0:
        return True, ""
    lo = (p // t) * t
    near = "%.2f or %.2f" % (lo / 100.0, (lo + t) / 100.0)
    if p % band_tick_paise(price) == 0:
        return False, "near a tick-band edge: use the coarser %.2f tick (%s), since NSE may have this stock " \
                      "in either band" % (t / 100.0, near)
    return False, "NSE's %.2f tick for this price: %s" % (t / 100.0, near)


def round_trip_charges(c, buy_value, sell_value):
    """Estimated NSE intraday charges for one buy and one sell, in rupees (same as trader-budget)."""
    broker = str(c.get("broker") or "zerodha").strip().lower()
    r = dict(CHARGES.get(broker, CHARGES["zerodha"]))
    r.update(RATES)
    if isinstance(c.get("charges"), dict):
        for k, v in c["charges"].items():
            if k in r and _f(v) is not None:
                r[k] = float(v)

    def brokerage(value):
        if value <= 0:
            return 0.0
        return min(r["brokerage_cap"], max(r["brokerage_min"], value * r["brokerage_pct"] / 100.0))

    turnover = buy_value + sell_value
    parts = [brokerage(buy_value) + brokerage(sell_value), sell_value * r["stt_sell_pct"] / 100.0,
             turnover * r["exchange_pct"] / 100.0, turnover * r["sebi_pct"] / 100.0,
             buy_value * r["stamp_buy_pct"] / 100.0]
    gst = (parts[0] + parts[2] + parts[3]) * r["gst_pct"] / 100.0
    return round(sum(paise(x) for x in parts + [gst]) / 100.0, 2)


def inr(x):
    """Rupees with Indian digit grouping: 100000 -> "₹1,00,000", 73980.9 -> "₹73,980.90"."""
    x = float(x)
    neg = x < -0.0049
    whole, frac = divmod(paise(abs(x)), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    text = "₹" + digits + ("" if frac == 0 else ".%02d" % frac)
    return ("-" if neg else "") + text


def _cent(x):
    """A price to the paisa, half up (1176.475 -> 1176.48)."""
    return paise(x) / 100.0


def _p(x):
    return "%.2f" % float(x)


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or "").upper()
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, inr(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Today's entries and exits that count: placed, or approved and not yet resolved.

    trader-broker's log is append-only (approved, then placed / denied / failed for the same
    fingerprint), so the latest record per fingerprint decides. A yes that has not come back yet
    still counts toward duplicates, trades, orders and the daily loss."""
    latest, keys = {}, []
    for i, rec in enumerate((state or {}).get("orders", [])):
        if rec_day(rec) != today or rec.get("status") not in ENTRY_STATUSES:
            continue
        key = rec.get("fingerprint") or ("#", i)
        if key not in latest:
            keys.append(key)
        latest[key] = rec
    return [latest[k] for k in keys if latest[k].get("status") in ("placed", "approved")]


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its IST date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh"""
    old = [k for k in OLD_KEYS if k in (cfg or {})]
    if old:
        raise RiskError("your config has %s: this NSE pack counts rupees. Rename %s, and set the amounts "
                        "in rupees (see trader-budget/config.example.json)"
                        % (", ".join(old), ", ".join("%s to %s" % (k, k[:-4] + "_inr") for k in old)))
    c = dict(DEFAULTS)
    c.update(cfg or {})
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    now_utc = now or parse_now(None)
    now_ist = to_ist(now_utc)
    checked_at = now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = today or now_ist.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).strip().upper() for b in c.get("blocked") or []}
    default_product = str(c.get("product") or "MIS").strip().upper()
    with_charges = bool(c.get("include_charges", True))
    margin_pct = float(c.get("mis_margin_pct") or 100)
    done_today = placed_today(state, today)
    full_loss = _f(c.get("max_loss_per_trade_inr"))

    def tally(own_fp=None):
        """Today's (orders, entries, loss at risk, (symbol, side) keys, notes), leaving out this order's
        own records, so re-checking an approved or placed order does not count it against itself."""
        recs = [r for r in done_today if own_fp is None or r.get("fingerprint") != own_fp]
        entries = [r for r in recs if rec_is_entry(r)]
        loss, notes = 0.0, []
        for r in entries:
            ml = _f(r.get("max_loss"))
            if ml is None and full_loss:
                loss += full_loss
                notes.append("an entry today (%s %s) has no max loss in the log; counted as your full %s max "
                             "loss per trade" % (r.get("side"), r.get("symbol"), inr(full_loss)))
            elif ml is None:
                notes.append("an entry today (%s %s) has no max loss in the log; not counted in the daily loss"
                             % (r.get("side"), r.get("symbol")))
            else:
                loss += ml
        keys = {(str(r.get("symbol", "")).upper(), str(r.get("side", "")).lower()) for r in recs}
        return len(recs), entries, loss, keys, notes

    day_count, day_entries, day_loss, _, day_notes = tally()
    top_warn.extend(day_notes)
    win_reasons, win_warns = window_check(now_ist, c)
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    paper = is_paper(c)
    if not paper:
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    sq = square_off_warning(c)
    if sq:
        top_warn.append(sq)
    top_warn.extend(win_warns)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = str(o.get("symbol", "")).strip().upper()
        side = str(o.get("side", "")).strip().lower()
        typ = str(o.get("type", "market")).strip().lower()
        tif = str(o.get("time_in_force", "day")).strip().lower()
        exchange = str(o.get("exchange") or "").strip().upper()
        product = str(o.get("product") or "").strip().upper()
        qty = o.get("qty")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        oc = str(o.get("order_class") or "").strip().lower()
        own_fp = fingerprint(o, c)
        count_today, entries_today, loss_today, placed_keys, _ = tally(own_fp)
        legs = o.get("take_profit_limit_price") is not None or o.get("stop_loss_stop_price") is not None
        bracket = oc == "bracket" or (not oc and legs)
        value = None

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        q = _f(qty)
        if o.get("notional") is not None:
            reasons.append("format: NSE orders are in whole shares: give qty, not a rupee amount (notional)")
        if qty is None:
            reasons.append("format: no qty (whole shares)")
        elif q is None or q <= 0 or abs(q - round(q)) > 1e-9:
            reasons.append("format: qty must be a whole number of shares above 0, got %r" % qty)
            q = None
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                else:
                    ok, hint = tick_hint(v)
                    if not ok:
                        reasons.append("format: %s %s is off tick: %s" % (k, _p(v), hint))
        if typ not in TYPES:
            reasons.append("format: order type must be market or limit, got %r" % typ)
        if tif not in TIFS:
            reasons.append("format: time_in_force must be day, got %r" % tif)
        if typ == "limit" and o.get("limit_price") is None:
            reasons.append("format: a limit order needs a limit_price")
        if not exchange:
            warns.append("no exchange on the order: taken as NSE")
        elif exchange != "NSE":
            reasons.append("format: exchange must be NSE, got %r: this pack trades NSE cash equity only" % exchange)
        if not product:
            product = default_product
            warns.append("no product on the order: taken as %s from your config" % product)
        if product not in PRODUCTS:
            reasons.append("format: product must be MIS (intraday) or CNC (delivery), got %r" % product)
        if bracket and (o.get("take_profit_limit_price") is None or o.get("stop_loss_stop_price") is None):
            reasons.append("format: a bracket order needs both take_profit_limit_price and stop_loss_stop_price")
        elif oc and oc != "bracket":
            reasons.append("format: unknown order_class %r (bracket or none)" % oc)

        # asset
        if sym.endswith(".BO"):
            reasons.append("asset: %s is a BSE symbol; this pack trades NSE only" % sym)
        elif sym.endswith(".NS"):
            reasons.append("asset: %s: use the plain NSE symbol %s (the .NS suffix is Yahoo's)" % (sym, sym[:-3]))
        elif sym and not NSE_SYMBOL.match(sym):
            reasons.append("asset: %s does not look like an NSE symbol (like RELIANCE, M&M or BAJAJ-AUTO); "
                           "this pack trades NSE cash equity only, no options or futures" % sym)

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        if plan_entry and typ == "market":
            pt = plan_time(plan)
            age = (now_utc - pt).total_seconds() if pt else None
            if age is None or not -60 <= age <= FRESH_MINUTES * 60:
                warns.append("the plan's entry %s is %s: not used for a market order (older than %d minutes)"
                             % (_p(plan_entry), ("from %s IST" % to_ist(pt).strftime("%Y-%m-%d %H:%M"))
                                if pt else "undated", FRESH_MINUTES))
                plan_entry = None
        ref, ref_from = None, None
        if typ == "limit":
            ref, ref_from = _f(o.get("limit_price")), "limit"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = _cent(ref)
                if live and ref_from == "price" and plan_entry and live > plan_entry + CENT:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "market":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        if q and ref:
            value = q * ref

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9 and q is not None:
            exiting = q <= held + 1e-9
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and typ == "market" and not plan_entry and live is None:
            later.append("price: a market entry needs a fresh price (under %d minutes old) or a plan entry to "
                         "measure its risk, and %s has neither (trader-alerts: alerts.py quote %s --write)"
                         % (FRESH_MINUTES, sym, sym))
        elif kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %s-share order; add it to "
                         "trader/prices.json (trader-alerts: alerts.py quote %s --write)" % (sym, qty, sym))

        # blocked list, product and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if product == "CNC" and not c.get("allow_delivery"):
            later.append("product: CNC is delivery (you keep the shares overnight); this is a day-trading pack, "
                         "so entries are MIS unless \"allow_delivery\": true")
        if value is not None:
            if value < float(c.get("min_order_inr") or 0) - CENT:
                later.append("size: %s is under your %s minimum order"
                             % (inr(value), inr(c["min_order_inr"])))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + CENT:
                later.append("size: %s is over your %s max order"
                             % (inr(value), inr(c["max_order_inr"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        margin = bool(c.get("allow_margin")) and product == "MIS"
        needed = None
        if value is not None and side == "buy" and kind == "entry":
            needed = value * margin_pct / 100.0 if margin else value
            after_cash = cash - needed
            if after_cash < -CENT:
                if margin:
                    later.append("cash: needs %s (%s%% MIS margin of %s) but only %s cash"
                                 % (inr(needed), _pct(margin_pct), inr(value), inr(cash)))
                else:
                    later.append("cash: needs %s but only %s cash; that would be margin (borrowed money, "
                                 "\"allow_margin\": false)" % (inr(value), inr(cash)))
            elif after_cash < buffer_inr - CENT:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (inr(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), inr(buffer_inr)))
            if margin:
                info.append("MIS margin: %s of cash blocked for %s of stock (%s%%)"
                            % (inr(needed), inr(value), _pct(margin_pct)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + CENT:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, inr(pos_after), 100.0 * pos_after / total, inr(total),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            over = q > held + 1e-9 if q is not None else held <= 0
            if over:
                reasons.append("short: selling more %s than you hold (%s shares). Short selling is not supported "
                               "end to end yet, so it is blocked even with \"allow_short\": true. If you bought "
                               "it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window
        max_loss = risk_ps = rr = stop = target = charges = loss_at_stop = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop, plan_stop = _f(o.get("stop_loss_stop_price")), _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f(o.get("take_profit_limit_price")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > CENT:
                warns.append("the bracket stop %s differs from the plan's stop %s: the bracket's is the one "
                             "trader-broker places" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                if paper:
                    warns.append("the stop %s is in the plan only, not at the broker: set a stop alert "
                                 "(trader-alerts: alerts.py add --from-plan)" % _p(stop))
                else:
                    later.append("stop: no exchange stop: the stop %s is in the plan only. A live entry needs a "
                                 "bracket order (stop_loss_stop_price) so trader-broker places the stop at the "
                                 "broker" % _p(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): a bracket "
                                 "stop_loss_stop_price, from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif live is not None and ((long and live <= stop + 1e-9) or (not long and live >= stop - 1e-9)):
                later.append("stop: the price is already through the stop: %s last %s is at or %s the stop %s"
                             % (sym, _p(live), "below" if long else "above", _p(stop)))
            elif ref:
                if (long and stop >= ref - CENT) or (not long and stop <= ref + CENT):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_c = abs(paise(ref) - paise(stop))
                    risk_ps = risk_c / 100.0
                    shares = q or 0
                    loss_at_stop = round(shares * risk_c / 100.0, 2)
                    if with_charges and shares:
                        buy_v, sell_v = (shares * ref, shares * stop) if long else (shares * stop, shares * ref)
                        charges = round_trip_charges(c, buy_v, sell_v)
                    max_loss = round(loss_at_stop + (charges or 0.0), 2)
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and max_loss > cap + CENT:
                        msg = "loss: %s shares x %s risk a share = %s at the stop" % (_num(shares), _p(risk_ps),
                                                                                   inr(loss_at_stop))
                        if charges is not None:
                            msg += " + %s estimated charges = %s" % (inr(charges), inr(max_loss))
                        later.append(msg + ", over your %s max loss per trade" % inr(cap))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + CENT) or (not long and target >= ref - CENT):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(abs(paise(target) - paise(ref)) / float(risk_c), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss_inr"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + CENT:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (inr(used), inr(max_loss), inr(used + max_loss), inr(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                cap = _f(c.get("max_loss_per_trade_inr"))
                loss_words = inr(max_loss)
                if charges is not None:
                    loss_words = "%s (%s at the stop + %s estimated charges)" % (inr(max_loss), inr(loss_at_stop),
                                                                                  inr(charges))
                info.append("max loss %s%s, reward:risk %s%s"
                            % (loss_words, (" (limit %s)" % inr(cap)) if cap else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               inr(loss_today + batch_loss), inr(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "buy":
                if value is not None:
                    acct["cash"] -= needed if needed is not None else value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if q is not None:
                    acct["shares"][sym] = held + q
            elif side == "sell":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                if q is not None:
                    acct["shares"][sym] = held - q
        results.append({
            "index": i, "symbol": sym, "exchange": exchange or "NSE", "side": side, "kind": kind,
            "product": product, "amount": "%s shares" % qty,
            "value_inr": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "loss_at_stop": loss_at_stop,
            "charges": charges, "max_loss": max_loss, "reward_risk": rr, "fingerprint": own_fp,
            "checked_at": checked_at,
            "verdict": verdict, "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now_ist.strftime("%Y-%m-%d %H:%M"),
        "paper": paper,
        "broker": str(c.get("broker") or ""),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": day_count, "entries_placed": len(day_entries),
                  "loss_at_risk": round(day_loss, 2)},
    }


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s (%s), %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                     r["product"], r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_ist"], inr(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        s = str(s).upper()
        out[s] = float(p)
        d = detail.get(s) or {}
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
    return out, times


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed NSE orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 09:40\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in India)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-29")
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
