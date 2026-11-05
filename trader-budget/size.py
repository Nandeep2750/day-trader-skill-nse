#!/usr/bin/env python3
"""Position sizing for trader-budget. Standard library only, no network.

Default mode: sizes ONE NSE day trade from trader/plan.json (written by
trader-exit) to your budget and your max loss per trade, then writes one
bracket order (entry, stop-loss, target) to trader/orders.json. It never
places anything: trader-risk checks the order and trader-broker asks you
yes or no, then sends it to Zerodha or Angel One (or records it on paper).

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit                          # a limit order at the plan's entry
    python3 size.py --limit 1174.30 --write          # a limit order at 1174.30
    python3 size.py --now "2026-10-29 09:40" --json  # pin the clock (IST)

How many shares. Whole shares only: NSE has no fractional shares.

    shares = the largest whole number that fits ALL of
      max loss       shares x (entry minus stop) + estimated charges <= max_loss_per_trade_inr
                     (charges left out with "include_charges": false)
      position cap   budget x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget minus the cash buffer) / entry
                     (and no more than the cash in trader/portfolio.json, if you have one).
                     With "allow_margin": true an MIS trade needs only mis_margin_pct
                     of its value in cash (the broker lends the rest): off by default.

Prices go on NSE's tick (0.01 under Rs 250, 0.05 to Rs 1,000, 0.10 to Rs 5,000,
0.50 to Rs 10,000, 1.00 to Rs 20,000, 5.00 above), rounded so the risk never
grows: the stop rounds UP, the target DOWN, a buy limit DOWN. NSE sets each
stock's tick from last month's close, so within 10% of a band edge (225 to 275,
900 to 1,100, 4,500 to 5,500, 9,000 to 11,000, 18,000 to 22,000) the coarser
tick is used: it is valid in either band.

Estimated charges: an NSE intraday round trip (buy at the entry, sell at the
stop) at your broker's rates: brokerage (Zerodha 0.03% capped at Rs 20 an order;
Angel One 0.1%, Rs 5 to Rs 20 an order), STT 0.025% of the sell, NSE
transaction charge 0.00297%, SEBI fee, stamp duty 0.003% of the buy, GST 18%.
An estimate: check your broker's calculator. "charges" in the config overrides
any rate.

The entry it sizes at: a limit order's limit price; for a market order the
plan's entry, or the price in trader/prices.json if that is fresh (under 15
minutes old) and higher, because a price that ran up since the plan means a
bigger loss at the same stop. Zero shares (the stop is too far away for your
max loss) is a plain refusal: no order is written. So is a reward:risk under
min_reward_risk (the price ran toward the target since the plan).

Weights mode (kept from v1, no longer the headline): split a budget across a
list of NSE stocks by weight, in whole shares (needs prices).

    python3 size.py --weights RELIANCE=30,HDFCBANK=25,INFY=20,ITC=15,TCS=10 --prices trader/prices.json
    python3 size.py --targets my-weights.json --budget 100000 --write

  1. cash buffer   budget x cash_buffer_pct stays in cash, never invested
  2. weights       renormalised to 100 over the stocks you keep (blocked
                   symbols are dropped first; --top N keeps the N biggest)
  3. cap           no stock gets more than max_position_pct of the budget;
                   anything over the cap is shared out to the others by
                   weight, again and again until nobody is over
  4. min order     a slice under min_order_inr is dropped and its weight
                   shared out, so you never place a Rs 40 order
  5. whole shares  each slice is floored to whole shares, then one more share
                   at a time goes to the stock furthest below its slice

Money that cannot be invested is reported as "left idle", never hidden.
Education and tooling, not financial advice.
"""

import argparse
import datetime
import json
import math
import os
import re
import sys

# NSE defaults: config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 80000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 1000,
    "max_daily_loss_inr": 3000,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:45",
    "flat_by": "15:00",
    "broker": "zerodha",
    "product": "MIS",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "mis_margin_pct": 20,
    "allow_short": False,
    "include_charges": True,
}
OLD_KEYS = ("max_order_usd", "min_order_usd", "max_loss_per_trade_usd", "max_daily_loss_usd")
EPS = 1e-9
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}
NSE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")

# NSE cash tick sizes in paise: a price below X rupees moves in steps of this many paise
TICKS = ((250, 1), (1000, 5), (5000, 10), (10000, 50), (20000, 100), (None, 500))
# NSE fixes each stock's tick from the previous month's close, which this pack does not know. Within
# 10% of a band edge the stock could sit in either band, so prices there go on the coarser tick (a
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


class SizeError(Exception):
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
        raise SizeError("--now wants a time like \"2026-10-29 09:40\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-10-19T14:52:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def check_config(cfg):
    """Refuse a config written for the US pack: its money keys are dollars."""
    old = [k for k in OLD_KEYS if k in (cfg or {})]
    if old:
        raise SizeError("your config has %s: this NSE pack counts rupees. Rename %s, and set the amounts "
                        "in rupees (see trader-budget/config.example.json)"
                        % (", ".join(old), ", ".join("%s to %s" % (k, k[:-4] + "_inr") for k in old)))
    return cfg


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(check_config(read_json(path, "config")))
    return cfg


def clean_symbol(text):
    """" reliance.ns " -> "RELIANCE". A BSE (.BO) symbol is refused: NSE only."""
    sym = str(text or "").strip().upper()
    if sym.endswith(".BO"):
        raise SizeError("%s is a BSE symbol: this pack trades NSE only" % sym)
    if sym.endswith(".NS"):
        sym = sym[:-3]
    return sym


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_weights(data):
    """Accept a weights file, a {ticker: weight} dict or a list of rows.

    Returns [{"ticker", "name", "weight"}] with weight as a float."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"ticker": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("ticker") or r.get("symbol")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a ticker and a weight" % i)
            rows.append({"ticker": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["ticker"], r["weight"]))
        out.append({"ticker": clean_symbol(r["ticker"]), "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    """RELIANCE=30,HDFCBANK=25 -> {symbol: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants SYMBOL=WEIGHT pairs, got %r" % part)
        t, w = part.split("=", 1)
        out[t.strip().upper()] = w.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in prices.items():
        try:
            out[clean_symbol(t)] = float(p)
        except (TypeError, ValueError):
            continue
    return out


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, fetched_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def paise(x):
    """Rupees -> whole paise (half up)."""
    return int(math.floor(float(x) * 100 + 0.5 + EPS))


def round_paise(x):
    """Round a price to the paisa (half up)."""
    return paise(x) / 100.0


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


def to_tick(price, direction):
    """A price on NSE's tick: direction "up", "down" or "near" (half up)."""
    p = paise(price)
    t = tick_paise(price)
    if direction == "up":
        q = -(-p // t) * t
    elif direction == "down":
        q = (p // t) * t
    else:
        q = ((p + t // 2) // t) * t
    return q / 100.0


def on_tick(price):
    return paise(price) % tick_paise(price) == 0


def shares_for(inr_amount, per_share):
    """Whole shares that fit in an amount at per_share, in exact integer paise."""
    u, p = paise(inr_amount), paise(per_share)
    if p <= 0 or u <= 0:
        return 0
    return u // p


def charge_rates(cfg):
    """The broker's rates plus the statutory ones, with any "charges" overrides from the config."""
    c = cfg or {}
    broker = str(c.get("broker") or "zerodha").strip().lower()
    rates = dict(CHARGES.get(broker, CHARGES["zerodha"]))
    rates.update(RATES)
    over = c.get("charges")
    if isinstance(over, dict):
        for k, v in over.items():
            if k in rates and _f(v) is not None:
                rates[k] = float(v)
    return broker, rates


def round_trip_charges(cfg, buy_value, sell_value):
    """Estimated NSE intraday charges for one buy and one sell, in rupees (to the paisa).

    Returns {"total", "brokerage", "stt", "exchange", "sebi", "stamp", "gst"}."""
    _, r = charge_rates(cfg)

    def brokerage(value):
        if value <= 0:
            return 0.0
        return min(r["brokerage_cap"], max(r["brokerage_min"], value * r["brokerage_pct"] / 100.0))

    turnover = buy_value + sell_value
    out = {
        "brokerage": brokerage(buy_value) + brokerage(sell_value),
        "stt": sell_value * r["stt_sell_pct"] / 100.0,
        "exchange": turnover * r["exchange_pct"] / 100.0,
        "sebi": turnover * r["sebi_pct"] / 100.0,
        "stamp": buy_value * r["stamp_buy_pct"] / 100.0,
    }
    out["gst"] = (out["brokerage"] + out["exchange"] + out["sebi"]) * r["gst_pct"] / 100.0
    out = {k: round_paise(v) for k, v in out.items()}
    out["total"] = round_paise(sum(out.values()))
    return out


def _price(x):
    """1174.3 -> "1174.30": prices are written as strings to the paisa."""
    return "%.2f" % round_paise(x)


def _num(x):
    """1250 -> "1250", 1118.13 -> "1118.13"."""
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def inr(x):
    """Rupees with Indian digit grouping: 100000 -> "₹1,00,000", 73980.9 -> "₹73,980.90"."""
    x = float(x)
    neg = x < -0.0049
    p = abs(paise(abs(x)))
    whole, frac = divmod(p, 100)
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


# ---------------------------------------------------------------- plan mode (the default)

def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a limit order (None = market); it goes DOWN to the tick
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in trader/portfolio.json, if there is one
    today  IST date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(check_config(cfg or {}))
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = clean_symbol(plan.get("symbol"))
    if not sym:
        raise SizeError("the plan has no symbol (run trader-exit)")
    if not NSE_SYMBOL.match(sym):
        raise SizeError("%r does not look like an NSE symbol (like RELIANCE, M&M or BAJAJ-AUTO)" % sym)
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in India: make a new plan with trader-exit"
                        % (plan["date"], today))
    entry_plan, stop_plan, target_plan = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop_plan), ("target", target_plan)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit); a bracket order needs entry, "
                            "stop and target" % name)
    blocked = {str(b).strip().upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)
    product = str(c.get("product") or "MIS").strip().upper()
    if product not in ("MIS", "CNC"):
        raise SizeError("product must be MIS (intraday) or CNC (delivery), got %r" % c.get("product"))

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    rounding = []
    if limit is not None:
        ref, ref_from = to_tick(limit, "down"), "limit"
        if abs(ref - round_paise(limit)) > EPS:
            rounding.append("limit %s -> %s (down to the tick)" % (_price(limit), _price(ref)))
    elif price is not None and round_paise(price) > round_paise(entry_plan):
        ref, ref_from = round_paise(price), "price"
    else:
        ref, ref_from = round_paise(entry_plan), "plan"
    stop, target = to_tick(stop_plan, "up"), to_tick(target_plan, "down")
    if abs(stop - round_paise(stop_plan)) > EPS:
        rounding.append("stop %s -> %s (up to the tick)" % (_price(stop_plan), _price(stop)))
    if abs(target - round_paise(target_plan)) > EPS:
        rounding.append("target %s -> %s (down to the tick)" % (_price(target_plan), _price(target)))
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (_price(target), _price(ref)))
    risk_c = paise(ref) - paise(stop)
    reward_c = paise(target) - paise(ref)
    risk = risk_c / 100.0
    with_charges = bool(c.get("include_charges", True))
    broker, _ = charge_rates(c)

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (inr(budget), inr(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (inr(cash), inr(cash_buffer))
    available = max(round(available, 2), 0.0)
    margin = bool(c.get("allow_margin")) and product == "MIS"
    margin_pct = float(c.get("mis_margin_pct") or 100)
    if margin and not 0 < margin_pct <= 100:
        raise SizeError("mis_margin_pct must be above 0 and at most 100")

    def loss_for(q):
        """(price loss, charges, all-in loss) at the stop for q shares, in rupees."""
        price_loss = q * risk_c / 100.0
        ch = round_trip_charges(c, q * ref, q * stop)["total"] if (with_charges and q > 0) else 0.0
        return round(price_loss, 2), ch, round(price_loss + ch, 2)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if max_loss:
        q = shares_for(max_loss, risk)
        while q > 0 and loss_for(q)[2] > max_loss + 0.005:
            q -= 1
        how = "%s max loss / %s risk a share" % (inr(max_loss), _price(risk))
        if with_charges:
            how += ", charges included"
        limits.append({"name": "max loss", "inr": max_loss, "per": risk, "shares": q, "how": how})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "inr": cap, "per": ref, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), inr(budget), inr(cap), _price(ref))})
    max_order = _f(c.get("max_order_inr"))
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (inr(max_order), _price(ref))})
    if margin:
        per = ref * margin_pct / 100.0
        limits.append({"name": "cash", "inr": available, "per": per, "shares": shares_for(available, per),
                       "how": "%s = %s / (%s%% MIS margin of %s)" % (cash_note, inr(available), _num(margin_pct),
                                                                    _price(ref))})
    else:
        limits.append({"name": "cash", "inr": available, "per": ref, "shares": shares_for(available, ref),
                       "how": "%s = %s / %s" % (cash_note, inr(available), _price(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = round(qty * ref, 2)
    loss_at_stop, charges, loss = loss_for(qty)
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share risks %s (entry %s minus stop %s)%s, more than your %s max loss per trade. "
                       "No order. Skip this trade, or plan a closer stop with trader-exit."
                       % (inr(risk), _price(ref), _price(stop),
                          " plus charges" if with_charges else "", inr(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, inr(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and reward_c < _f(c["min_reward_risk"]) * risk_c - EPS:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so trader-risk "
                   "would block it. No order. Skip it, or make a new plan with trader-exit."
                   % (reward_c / float(risk_c), _price(target), _price(ref), _price(stop), _num(c["min_reward_risk"])))
    elif cost < float(c.get("min_order_inr") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, inr(cost), inr(c["min_order_inr"])))
    return {
        "symbol": sym, "exchange": "NSE", "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "plan_created_at": plan.get("created_at"),
        "entry_plan": round_paise(entry_plan),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_risk": round(reward_c / float(risk_c), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "rounding": rounding, "product": product, "broker": broker, "margin": margin,
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "cost": cost, "cash_needed": round(cost * margin_pct / 100.0, 2) if margin else cost,
        "loss_at_stop": loss_at_stop, "charges": charges, "include_charges": with_charges, "max_loss": loss,
        "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "order_type": "limit" if limit is not None else "market", "refusal": refusal,
    }


def bracket_order(s):
    """The one proposed order (the pack's NSE order shape) plus why and plan."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    o = {"symbol": s["symbol"], "exchange": "NSE", "side": "buy", "qty": str(int(s["qty"])),
         "type": s["order_type"], "time_in_force": "day", "product": s["product"]}
    if s["order_type"] == "limit":
        o["limit_price"] = _price(s["entry"])
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    at_stop = "%d x %s = %s at the stop" % (s["qty"], _price(s["risk_per_share"]), inr(s["loss_at_stop"]))
    if s["include_charges"]:
        at_stop += " + %s estimated charges = %s" % (inr(s["charges"]), inr(s["max_loss"]))
    why = ("%s on %s: %d shares, bound by your %s (%s)"
           % (setup, s["symbol"], s["qty"], {"max loss": "%s max loss per trade" % inr(s["max_loss_per_trade_inr"] or 0),
                                             "position cap": "position cap", "max order": "max order",
                                             "cash": "cash after the buffer"}[s["bound_by"]], at_stop))
    o.update({
        "order_class": "bracket",
        "take_profit_limit_price": _price(s["target"]),
        "stop_loss_stop_price": _price(s["stop"]),
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "loss_at_stop": s["loss_at_stop"],
                 "charges": s["charges"], "max_loss": s["max_loss"], "broker": s["broker"],
                 "setup": s["setup"], "date": s["date"], "entry_time": s["entry_time"],
                 "flat_by": s["flat_by"]},
    })
    if s["plan_created_at"]:
        o["plan"]["created_at"] = s["plan_created_at"]   # when the plan's entry price is from (trader-risk)
    return o


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s (NSE) buy, %s, entry %s%s" % (s["symbol"], setup, _price(s["entry_plan"]),
                                                  (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    if s["rounding"]:
        lines.append("On NSE's tick: " + "; ".join(s["rounding"]) + ".")
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f."
                 % (_price(s["entry"]), src, _price(s["risk_per_share"]), _price(s["entry"]), _price(s["stop"]),
                    s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-62s %4d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s (%s, %s)."
                 % (s["qty"], s["symbol"], inr(s["cost"]), _price(s["entry"]), s["product"],
                    "intraday" if s["product"] == "MIS" else "delivery"))
    if s["margin"]:
        lines.append("MIS margin: about %s of your cash is blocked; the broker lends the rest. Margin "
                     "magnifies losses." % inr(s["cash_needed"]))
    lines.append("Max loss at the stop: %d x %s = %s." % (s["qty"], _price(s["risk_per_share"]), inr(s["loss_at_stop"])))
    if s["include_charges"]:
        lines.append("Estimated charges (%s, round trip): %s. All in: %s."
                     % ({"zerodha": "Zerodha", "angelone": "Angel One"}.get(s["broker"], s["broker"]),
                        inr(s["charges"]), inr(s["max_loss"])))
    lines.append("A stop is an order, not a promise: in a fast market or a circuit it can fill below %s."
                 % _price(s["stop"]))
    lines.append("")
    lines.append("Proposed bracket order (nothing has been placed):")
    how = "limit %s" % order["limit_price"] if order["type"] == "limit" else "market"
    lines.append("  buy %s %s on NSE, %s, %s, day; then a stop-loss (SL-M) at %s; target %s watched by trader-alerts"
                 % (order["qty"], order["symbol"], how, order["product"], order["stop_loss_stop_price"],
                    order["take_profit_limit_price"]))
    if s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets closed (trader-alerts rings, "
                     "trader-broker flatten), before your broker's auto square-off." % s["flat_by"])
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode (v1)

def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {str(b).upper() for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["ticker"])
        seen.add(r["ticker"])
        if r["ticker"] in blocked:
            dropped.append({"ticker": r["ticker"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"ticker": r["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"ticker": r["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact (unrounded) rupees per symbol with the cap enforced.

    Proportional to weight; any stock over the cap is pinned at the cap and
    the rest of the money is shared over the others, repeated until no stock
    is over. Returns ({symbol: rupees}, set of capped symbols)."""
    alloc, capped = {}, set()
    active = [r for r in rows]
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["ticker"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["ticker"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["ticker"]] = cap
            capped.add(r["ticker"])
            money_left -= cap
        active = [r for r in active if r["ticker"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["ticker"]] = 0.0
            break
    return alloc, capped


def round_to_unit(exact, capped, order, cap, unit):
    """Round to whole rupees (unit 1) or paise (unit 0.01) and keep the total.

    The total placed is the exact total rounded DOWN to the unit, so it never
    spends money you do not have. Each slice is rounded to the nearest unit,
    then the difference (usually one unit) goes to the biggest stock that is
    not capped, then the next, never pushing anyone over the cap."""
    def units(x):
        return int(math.floor(x / unit + EPS))

    cap_u = units(cap)
    total_u = units(sum(exact.values()))
    out = {}
    for t, v in exact.items():
        out[t] = cap_u if t in capped else int(math.floor(v / unit + 0.5 + EPS))
    diff = total_u - sum(out.values())
    takers = [t for t in order if t not in capped]
    guard = 0
    while diff != 0 and takers and guard < 100000:
        moved = False
        for t in takers:
            if diff == 0:
                break
            if diff > 0 and out[t] + 1 <= cap_u:
                out[t] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[t] > 0:
                out[t] -= 1
                diff += 1
                moved = True
        if not moved:
            break
        guard += 1
    return {t: round(u * unit, 2) for t, u in out.items()}


def whole_shares(rupees, prices, order, cap, cash):
    """Whole shares: floor each slice, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {}
    for t in order:
        shares[t] = int(math.floor(rupees[t] / prices[t] + EPS)) if rupees[t] > 0 else 0
    spent = sum(shares[t] * prices[t] for t in order)
    left = cash - spent
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = rupees[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, budget=None, prices=None, top=None):
    """Weights mode. Pure function: weights + rules + prices -> the full sizing result (a dict).
    Whole shares only (NSE has no fractional shares), so every stock needs a price."""
    check_config(cfg)
    budget = float(budget if budget is not None else (cfg.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(cfg.get("cash_buffer_pct", 10))
    cap_pct = float(cfg.get("max_position_pct", 25))
    min_order = float(cfg.get("min_order_inr", 500) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    prices = prices or {}

    keep, dropped = normalise(rows, cfg.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100.0, 2)
    unit = 0.01

    missing = [r["ticker"] for r in keep if not prices.get(r["ticker"])]
    if missing:
        raise SizeError("whole shares need a price for: %s (add them to trader/prices.json, "
                        "e.g. with trader-alerts: alerts.py quote %s --write)"
                        % (", ".join(missing), " ".join(missing)))

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["ticker"] for r in sorted(live, key=lambda r: -exact[r["ticker"]])]
        rupees = round_to_unit(exact, capped, order, cap, unit)
        shares = whole_shares(rupees, prices, order, cap, sum(rupees.values()))
        rupees = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if rupees[r["ticker"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["ticker"]))
        if shares[worst["ticker"]] == 0 and prices.get(worst["ticker"], 0) > 0:
            reason = ("one share costs %s, more than its slice; it cannot be bought in whole shares"
                      % inr(prices[worst["ticker"]]))
        else:
            reason = "its slice (%s) is under your %s minimum order" % (
                inr(rupees[worst["ticker"]]), inr(min_order))
        dropped.append({"ticker": worst["ticker"], "reason": reason})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")

    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["ticker"]
        positions.append({"ticker": t, "name": r["name"], "weight": round(r["weight"], 2),
                          "source_weight": r["source_weight"], "rupees": rupees[t],
                          "capped": t in capped, "price": prices[t], "shares": shares[t]})
    invested = round(sum(p["rupees"] for p in positions), 2)
    return {
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "investable": investable, "max_position_pct": cap_pct, "cap": cap,
        "min_order_inr": min_order,
        "positions": positions, "invested": invested,
        "idle": round(investable - invested, 2), "dropped": dropped,
    }


def orders_from(result, source="", product="CNC"):
    """Weights mode: proposed buy orders, one per position, in the pack's NSE order shape.
    A split across stocks is held, not day traded, so the product is CNC (delivery)."""
    out = []
    for p in result["positions"]:
        if p["shares"] <= 0:
            continue
        why = "%.2f%% of the split" % p["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_num(result["max_position_pct"]), inr(result["budget"]))
        out.append({"symbol": p["ticker"], "exchange": "NSE", "side": "buy", "qty": str(int(p["shares"])),
                    "type": "market", "time_in_force": "day", "product": product, "why": why})
    return out


def report(result, orders, source=""):
    """Weights mode table."""
    r = result
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (inr(r["budget"]), _num(r["cash_buffer_pct"]), inr(r["cash_buffer"]),
                    inr(r["investable"]), _num(r["max_position_pct"]), inr(r["cap"])))
    lines.append("")
    lines.append("%-11s %-22s %8s %13s %11s %7s  note" % ("symbol", "name", "weight", "rupees", "price", "shares"))
    for p in r["positions"]:
        row = "%-11s %-22s %7.2f%% %13s %11s %7d" % (p["ticker"], (p["name"] or "")[:22], p["weight"],
                                                      inr(p["rupees"]), inr(p["price"]), p["shares"])
        if p["capped"]:
            row += "  capped at %s%%" % _num(r["max_position_pct"])
        lines.append(row)
    lines.append("%-11s %-22s %8s %13s" % ("", "invested", "", inr(r["invested"])))
    lines.append("%-11s %-22s %8s %13s" % ("", "cash buffer", "", inr(r["cash_buffer"])))
    lines.append("%-11s %-22s %8s %13s" % ("", "left idle", "", inr(r["idle"])))
    if r["idle"] >= 1:
        lines.append("")
        capped_all = all(p["capped"] for p in r["positions"])
        lines.append("Left idle %s: %s" % (inr(r["idle"]), (
            "every stock is at its cap; add stocks or raise max_position_pct" if capped_all
            else "whole shares do not divide evenly")))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["ticker"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (nothing has been placed):")
    for o in orders:
        lines.append("  %s %s %s on NSE, %s, %s, %s" % (o["side"], o["qty"], o["symbol"], o["type"],
                                                       o["product"], o["time_in_force"]))
    lines.append("")
    lines.append("These are CNC (delivery) buys with no stop: trader-risk blocks them unless \"allow_delivery\" "
                 "is true and \"require_stop\" is false, and applies your limits to every buy.")
    lines.append("Next: trader-risk checks every order, then trader-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": cfg.get("paper", True) is not False,   # live only when "paper" is literally false
        "broker": str(cfg.get("broker") or "zerodha"),
        "note": ("Proposals only. Nothing is placed until trader-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = to_ist(now).date().isoformat()
    plan = read_json(a.plan, "plan (run trader-exit first, or use --weights for a split)")
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
        else:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 1174.30")
    sym = clean_symbol((plan or {}).get("symbol"))
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and limit is None and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        if p is not None and fresh:
            price = p
            if round_paise(p) > round_paise(_f(plan.get("entry")) or 0):
                notes.append("%s last %s (as of %s IST) is above the plan's entry %s, so the risk "
                             "is bigger: sized at %s." % (sym, _price(p), to_ist(t).strftime("%H:%M"),
                                                          _price(plan.get("entry")), _price(p)))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_ist(t).strftime("%Y-%m-%d %H:%M") + " IST")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today)
    order = bracket_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2,
                         ensure_ascii=False))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed bracket order to %s" % a.write)
    return 0 if order else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    prices = load_prices(a.prices)
    if not prices and os.path.exists("trader/prices.json") and not a.prices:
        prices = load_prices("trader/prices.json")
    result = size(rows, cfg, a.budget, prices, a.top)
    orders = orders_from(result, source)
    if a.write:
        write_orders(a.write, orders, cfg, source, parse_now(a.now))
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2, ensure_ascii=False))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size an NSE day trade to your budget and max loss (trader-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config (rupees)")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a limit order instead of market, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-29 09:40\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    g = ap.add_argument_group("weights mode (split a budget across a list, whole shares)")
    g.add_argument("--weights", help="inline weights: RELIANCE=30,HDFCBANK=25")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"ticker\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    a = ap.parse_args(argv)
    try:
        cfg = load_config(a.config)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        if a.top:
            raise SizeError("--top works with --weights or --targets")
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
