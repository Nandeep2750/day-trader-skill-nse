#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker. Standard library only.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket plus the
            exact tool calls Claude will make on your broker's MCP server:
            Zerodha (Kite MCP place_order) or Angel One (angel-one-mcp
            search_scrip, then place_order). A bracket shows its stop-loss,
            its target and the max loss at the stop. An order is READY only
            if trader-risk passed THIS exact order today (matched by
            fingerprint) and nobody has decided on it yet.
  approve   records your explicit yes for ONE order, with your reply word for
            word. It refuses "yes to all", "sure", "ok", a yes that names a
            different stock or amount, and any order that is not READY.
  decline   records a no.
  result    records what happened after the Allow once / Deny card:
            placed (with the broker's order id), denied, or failed. In paper
            mode, --status paper records a simulated fill instead.
  stop      step 2 of a bracket: after the entry fills, the protective SL-M
            stop for the filled qty. Prints the exact call, then records the
            stop's broker order id (--status placed --broker-id ID).
  fill      records a fill in trader/portfolio.json (cash and shares).
  flatten   builds a sell ticket that closes a symbol's open position by
            flat_by: cancel the open stop, then a MARKET SELL. It still goes
            through trader-risk and your yes.
  log       prints today's decisions.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py result --index 1 --status placed --broker-id 2511030001 --filled-qty 20 --filled-price 1228.10
    python3 ticket.py result --index 1 --status paper            # paper mode: simulated fill
    python3 ticket.py stop --index 1                              # the stop call for the filled qty
    python3 ticket.py stop --index 1 --status placed --broker-id 2511030002
    python3 ticket.py fill --symbol RELIANCE --side sell --qty 20 --price 1245.00
    python3 ticket.py flatten --symbol RELIANCE --qty 20
    python3 ticket.py log

Why the target is not a resting order: neither Kite nor Angel One offers a
bracket for NSE equity intraday any more. A resting target sell next to a
resting stop sell could both fill and leave you short. So the stop rests at
the exchange and trader-alerts watches the target; at the target you flatten.

Paper mode ("paper": true, the default): neither broker has a paper account,
so in paper mode Claude calls no broker tool at all. The fills are simulated
here and logged with "paper": true.

Everything is written to trader/state/broker.json (append only). Dates are
IST dates; --now "2026-11-02 09:45" pins the clock (IST).
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

# The fields that make an order this order: the same tuple as trader-risk/check.py.
FINGERPRINT_FIELDS = ("symbol", "exchange", "side", "qty", "type", "time_in_force", "limit_price", "product",
                      "client_order_id", "order_class", "take_profit_limit_price", "stop_loss_stop_price")
BROKERS = {"zerodha": "Zerodha (Kite MCP)", "angelone": "Angel One (angel-one-mcp)"}
NSE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
PRODUCT_WORDS = {"MIS": "intraday (MIS)", "CNC": "delivery (CNC)"}
ANGEL_PRODUCT = {"MIS": "INTRADAY", "CNC": "DELIVERY"}
TIF_WORDS = {"day": "good for today"}
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
TARGET_WHY = ("The target is not sent as an order: Kite and Angel One have no bracket for NSE equity "
              "intraday, and a resting target next to a resting stop could both fill and leave you short. "
              "trader-alerts watches the target; when it rings, flatten.")


class TicketError(Exception):
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
        raise TicketError("--now wants a time like \"2026-11-02 09:45\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def ist_iso(now_utc):
    """naive UTC -> "2026-11-02T09:45:00+05:30" (IST wall clock with its offset)."""
    return to_ist(now_utc).replace(microsecond=0).isoformat() + "+05:30"


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a decision belongs to: its "date" (the day it was made under), else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as trader-risk/check.py: identifies the exact order."""
    core = {k: order[k] for k in FINGERPRINT_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def norm_symbol(text):
    """"reliance.ns" -> "RELIANCE". NSE only: a .BO (BSE) symbol is refused."""
    s = str(text or "").strip().upper()
    if s.endswith(".BO"):
        raise TicketError("%s is a BSE symbol; this pack trades NSE only" % s)
    if s.endswith(".NS"):
        s = s[:-3]
    if not NSE_SYMBOL.match(s):
        raise TicketError("%r is not an NSE symbol (like RELIANCE, M&M, BAJAJ-AUTO)" % text)
    return s


def _str_num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else repr(f)
    return str(v)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def inr(v):
    """1234567.5 -> "₹12,34,567.50"; whole rupees drop the paise ("₹1,000")."""
    f = round(float(v), 2)
    sign = "-" if f < 0 else ""
    whole, paise = ("%.2f" % abs(f)).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return "%s₹%s%s" % (sign, whole, "" if paise == "00" else "." + paise)


def _p(v):
    return "%.2f" % float(v)


def _paise(v):
    return round(float(v), 2)


def product_of(order, cfg=None):
    return str(order.get("product") or (cfg or {}).get("product") or "MIS").upper()


def is_bracket(order):
    oc = str(order.get("order_class") or "").lower()
    legs = order.get("take_profit_limit_price") is not None or order.get("stop_loss_stop_price") is not None
    return oc == "bracket" or (not oc and legs)


def opposite(side):
    return "SELL" if str(side).lower() == "buy" else "BUY"


def ref_price(order):
    """The price a paper fill is simulated at: the limit price, else the plan's entry."""
    if str(order.get("type", "market")) == "limit" and _f(order.get("limit_price")):
        return _f(order["limit_price"])
    return _f((order.get("plan") or {}).get("entry"))


def max_loss(order):
    """(rupees, stop) lost if the stop fills exactly at the stop, or (None, stop)."""
    stop = _f(order.get("stop_loss_stop_price")) or _f((order.get("plan") or {}).get("stop"))
    q = _f(order.get("qty"))
    if not stop or not q:
        return None, stop
    plan = order.get("plan") or {}
    if str(order.get("type", "market")) == "limit" and _f(order.get("limit_price")):
        entry = _f(order["limit_price"])
    elif _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    risk = abs(entry - stop)
    return round(q * int(round(risk * 100)) / 100.0, 2), stop


def loss_at_stop(order):
    """(rupees, stop) lost at the stop price alone, before charges. trader-budget's plan.max_loss
    is all in (it includes the charges), so it is not used here; plan.loss_at_stop is."""
    stop = _f(order.get("stop_loss_stop_price")) or _f((order.get("plan") or {}).get("stop"))
    q = _f(order.get("qty"))
    plan = order.get("plan") or {}
    if not stop or not q:
        return None, stop
    if str(order.get("type", "market")) != "limit" and _f(plan.get("loss_at_stop")) is not None:
        return round(_f(plan["loss_at_stop"]), 2), stop
    entry = _f(order.get("limit_price")) if str(order.get("type", "market")) == "limit" else _f(plan.get("entry"))
    if not entry:
        return None, stop
    return round(q * int(round(abs(entry - stop) * 100)) / 100.0, 2), stop


def plain_parts(order, cfg=None):
    """The ticket in plain words, one part per line."""
    side = str(order.get("side", "")).upper()
    sym = order.get("symbol", "?")
    exch = order.get("exchange") or "NSE"
    typ = str(order.get("type", "market"))
    prod = PRODUCT_WORDS.get(product_of(order, cfg), product_of(order, cfg))
    tif = TIF_WORDS.get(str(order.get("time_in_force", "day")), order.get("time_in_force"))
    if is_bracket(order):
        how = "limit %s" % _p(order["limit_price"]) if typ == "limit" and order.get("limit_price") else typ
        parts = ["%s %s %s on %s, %s, %s, %s" % (side, _str_num(order.get("qty")), sym, exch, how, prod, tif)]
        if order.get("stop_loss_stop_price") is not None:
            parts.append("stop-loss %s (SL-M, placed after the entry fills)" % _p(order["stop_loss_stop_price"]))
        if order.get("take_profit_limit_price") is not None:
            parts.append("target %s (watched by trader-alerts, not a resting order)"
                         % _p(order["take_profit_limit_price"]))
        loss, stop = loss_at_stop(order)
        if loss is not None:
            parts.append("loss about %s if the stop fills at %s (it can fill worse in a fast market)"
                         % (inr(loss), _p(stop)))
        charges = _f((order.get("plan") or {}).get("charges"))
        if charges and loss is not None:
            parts.append("plus about %s estimated charges (brokerage, STT, exchange, SEBI, stamp, GST): "
                         "max loss about %s all in" % (inr(charges), inr(round(loss + charges, 2))))
        return parts
    q = _str_num(order.get("qty"))
    what = "%s share%s of %s on %s" % (q, "" if q == "1" else "s", sym, exch)
    if typ == "limit":
        how = "limit order at %s" % inr(order.get("limit_price"))
    else:
        how = "%s order" % typ.replace("_", " ")
    return ["%s %s, %s, %s, %s" % (side, what, how, prod, tif)]


def plain(order, cfg=None):
    """BUY 20 RELIANCE on NSE, market, intraday (MIS), good for today / stop-loss ... / target ... / max loss ..."""
    return " / ".join(plain_parts(order, cfg))


# ---------------------------------------------------------------- the broker calls

def _tag(order, suffix=""):
    """Kite's tag: alphanumeric, at most 20 characters."""
    return "dts" + fingerprint(order)[:8] + suffix


def entry_calls(order, broker, cfg=None):
    """[(tool, arguments)] for step 1: the order itself (a flatten adds the stop's cancel first)."""
    sym = str(order.get("symbol", "")).upper()
    prod = product_of(order, cfg)
    side = str(order.get("side", "")).upper()
    qty = int(float(order.get("qty")))
    limit = str(order.get("type", "market")) == "limit"
    calls = []
    cancel = order.get("cancel_stop") or {}
    if broker == "angelone":
        if cancel.get("broker_order_id"):
            calls.append(("cancel_order", {"variety": "STOPLOSS", "orderid": str(cancel["broker_order_id"])}))
        calls.append(("search_scrip", {"exchange": "NSE", "searchscrip": sym}))
        args = {"variety": "NORMAL", "tradingsymbol": sym + "-EQ",
                "symboltoken": "<from search_scrip: the row whose tradingsymbol is exactly %s-EQ>" % sym,
                "transactiontype": side, "exchange": "NSE", "ordertype": "LIMIT" if limit else "MARKET",
                "producttype": ANGEL_PRODUCT.get(prod, prod), "duration": "DAY", "quantity": str(qty)}
        if limit:
            args["price"] = _p(order["limit_price"])
        calls.append(("place_order", args))
        return calls
    if cancel.get("broker_order_id"):
        calls.append(("cancel_order", {"variety": "regular", "order_id": str(cancel["broker_order_id"])}))
    args = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": side,
            "quantity": qty, "product": prod, "order_type": "LIMIT" if limit else "MARKET",
            "validity": "DAY", "tag": _tag(order)}
    if limit:
        args["price"] = _paise(order["limit_price"])
    calls.append(("place_order", args))
    return calls


def stop_call(order, broker, qty, cfg=None):
    """(tool, arguments) for step 2: the protective SL-M stop for the filled qty."""
    sym = str(order.get("symbol", "")).upper()
    prod = product_of(order, cfg)
    side = opposite(order.get("side"))
    stop = order.get("stop_loss_stop_price")
    if stop is None:
        raise TicketError("this order has no stop_loss_stop_price")
    qty = int(float(qty))
    if broker == "angelone":
        return ("place_order", {"variety": "STOPLOSS", "tradingsymbol": sym + "-EQ",
                                "symboltoken": "<the same symboltoken as the entry>",
                                "transactiontype": side, "exchange": "NSE", "ordertype": "STOPLOSS_MARKET",
                                "producttype": ANGEL_PRODUCT.get(prod, prod), "duration": "DAY",
                                "quantity": str(qty), "triggerprice": _p(stop)})
    return ("place_order", {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym,
                            "transaction_type": side, "quantity": qty, "product": prod, "order_type": "SL-M",
                            "trigger_price": _paise(stop), "validity": "DAY", "tag": _tag(order, "sl")})


def broker_of(cfg):
    b = str((cfg or {}).get("broker") or "zerodha").lower().replace(" ", "").replace("_", "")
    if b in ("angel", "angelone"):
        return "angelone"
    if b in ("zerodha", "kite"):
        return "zerodha"
    raise TicketError("broker %r is not supported: use \"zerodha\" or \"angelone\" in trader/config.json"
                      % cfg.get("broker"))


def call_lines(calls, indent="    "):
    lines = []
    for n, (tool, args) in enumerate(calls, 1):
        lines.append("%s%d. %s with exactly:" % (indent, n, tool))
        for l in json.dumps(args, indent=2, ensure_ascii=False).splitlines():
            lines.append(indent + "   " + l)
    return lines


def amount_numbers(order):
    nums = []
    for k in ("qty", "limit_price", "stop_loss_stop_price", "take_profit_limit_price"):
        if order.get(k) is not None:
            v = _f(order[k])
            if v is not None:
                nums.append(v)
    for loss, _ in (max_loss(order), loss_at_stop(order)):  # the ticket shows both
        if loss is not None:
            nums.append(loss)
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, haan, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("symbol", "")).upper()
    upper = raw.upper()
    tokens = set(re.findall(r"[A-Z0-9][A-Z0-9&\-]{0,19}", upper))
    others = {str(s).upper() for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    # symbols like 360ONE carry digits: take them out before reading amounts
    for s in sorted({sym} | others, key=len, reverse=True):
        if s:
            upper = re.sub(r"(?<![A-Z0-9&\-])%s(?:\.NS)?(?![A-Z0-9&\-])" % re.escape(s), " ", upper)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", upper)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(_str_num(n) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today, statuses=None):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            if statuses is None or rec.get("status") in statuses:
                found = rec
    return found


ENTRY_STATUSES = ("approved", "declined", "placed", "denied", "failed")


def open_stop(state, symbol, today):
    """Today's live protective stop for a symbol (placed, not yet cancelled), or None."""
    live = None
    for rec in state.get("orders", []):
        if rec_day(rec) != today or str(rec.get("symbol", "")).upper() != symbol:
            continue
        if rec.get("status") == "stop_placed":
            live = rec
        elif rec.get("status") in ("stop_cancelled", "stop_filled") and live \
                and rec.get("broker_order_id") == live.get("broker_order_id"):
            live = None
    return live


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today, ENTRY_STATUSES)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for the broker call" % rec.get("at"), rv
        return False, "DONE: %s%s at %s" % (st, " (paper)" if rec.get("paper") else "", rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    """entry or exit, as trader-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("side", "")).lower() == "buy" else "exit"


# ---------------------------------------------------------------- rendering

def header(idx, total, cfg):
    broker = broker_of(cfg)
    if cfg.get("paper", True):
        acct = "PAPER (SIMULATED: no order goes to %s)" % BROKERS[broker].split(" (")[0]
    else:
        acct = "LIVE ACCOUNT, REAL MONEY, %s" % BROKERS[broker]
    return "ORDER %d of %d, %s" % (idx, total, acct)


def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    broker = broker_of(cfg)
    paper = cfg.get("paper", True)
    lines = [header(idx, total, cfg)]
    for part in plain_parts(order, cfg):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    bracket = is_bracket(order) and order.get("stop_loss_stop_price") is not None
    if paper:
        lines.append("  PAPER: Claude calls no broker tool. After your yes the fill is simulated here")
        lines.append("  (ticket.py result --index %d --status paper)%s." % (
            idx, ", and so is the stop" if bracket else ""))
    else:
        lines.append("  Step 1, after your yes, Claude calls the %s tools:" % BROKERS[broker])
        lines.extend(call_lines(entry_calls(order, broker, cfg)))
        if broker == "angelone":
            lines.append("  Use the symboltoken only if search_scrip returns a row whose tradingsymbol is")
            lines.append("  exactly %s-EQ. No exact match: stop and tell the user. Never pass force: true."
                         % str(order.get("symbol", "")).upper())
        if order.get("cancel_stop"):
            lines.append("  The cancel goes first: the open stop would otherwise sell these shares a second time.")
        if bracket:
            tool, args = stop_call(order, broker, order.get("qty"), cfg)
            lines.append("  Step 2, only after the entry fills: the protective stop for the filled qty")
            lines.append("  (ticket.py stop --index %d prints it with the real filled qty):" % idx)
            lines.extend(call_lines([(tool, args)]))
    if bracket and order.get("take_profit_limit_price") is not None:
        lines.append("  " + TARGET_WHY)
    if ready:
        lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
        if not paper:
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    loss, stop = max_loss(order)
    broker = broker_of(cfg)
    paper = bool(cfg.get("paper", True))
    out = {"index": idx, "ready": ready, "status": label, "plain": plain(order, cfg),
           "fingerprint": fingerprint(order), "paper": paper, "broker": broker,
           "why": order.get("why", ""), "bracket": is_bracket(order), "max_loss": loss, "stop": stop,
           "calls": [] if paper else [{"tool": t, "input": a} for t, a in entry_calls(order, broker, cfg)]}
    if not paper and is_bracket(order) and order.get("stop_loss_stop_price") is not None:
        t, a = stop_call(order, broker, order.get("qty"), cfg)
        out["stop_call"] = {"tool": t, "input": a, "after": "the entry fills; quantity = the filled qty"}
    return out


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date):
    """Record a filled order in trader/portfolio.json: cash and shares, with a
    cost-basis lot per buy (oldest lots are relieved first on a sell)."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    symbol = symbol.upper()
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    if qty != int(qty):
        raise TicketError("NSE trades whole shares only: qty %s" % _str_num(qty))
    cost = round(qty * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker", "")).upper() == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": int(qty), "cost": cost})
        pos["shares"] = int(round(float(pos.get("shares", 0)) + qty))
        pf["cash"] = round(float(pf.get("cash", 0)) - cost, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (_str_num(qty), symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = int(round(float(lot["shares"]) - take))
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = int(round(float(pos["shares"]) - qty))
        if pos["shares"] <= 0:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost, 2)
    write_json(portfolio_path, pf)
    return pf


def log_fill(state_path, now, today, symbol, side, qty, price, paper=False):
    """A fill in the log, so trader-risk sees the day's trades (a buy and a sell the same day)."""
    state = load_state(state_path)
    rec = {"at": ist_iso(now), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
           "qty": _str_num(float(qty)), "price": float(price),
           "plain": "%s %s %s at %s%s" % (side.upper(), _str_num(float(qty)), symbol.upper(), _p(price),
                                          " (paper)" if paper else "")}
    if paper:
        rec["paper"] = True
    state.setdefault("orders", []).append(rec)
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-11-02")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    broker_of(cfg)
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, stop = max_loss(order)
    broker = broker_of(cfg)
    paper = bool(cfg.get("paper", True))
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "qty": order.get("qty"), "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
           "plain": plain(order, cfg), "status": "approved", "reply": a.reply,
           "paper": paper, "broker": broker}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order, cfg)))
    if paper:
        print("PAPER: call no broker tool. Record the simulated fill: ticket.py result --index %d --status paper"
              " [--filled-price P]" % a.index)
        return 0
    print("Now call the %s tools, in order:" % BROKERS[broker])
    print("\n".join(call_lines(entry_calls(order, broker, cfg), indent="  ")))
    print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
          "placed|denied|failed --broker-id ID" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "plain": plain(order, cfg), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order, cfg)))
    return 0


def _record_stop(state, now, today, idx, order, qty, status, broker_id=None, paper=False, note=None):
    rec = {"at": ist_iso(now), "date": today, "index": idx, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": opposite(order.get("side")).lower(),
           "qty": _str_num(float(qty)), "stop": _f(order.get("stop_loss_stop_price")), "status": status,
           "plain": "protective stop: SL-M %s %s %s, trigger %s%s" % (
               opposite(order.get("side")), _str_num(float(qty)), str(order.get("symbol", "")).upper(),
               _p(order.get("stop_loss_stop_price")), " (paper)" if paper else "")}
    if broker_id:
        rec["broker_order_id"] = str(broker_id)
    if paper:
        rec["paper"] = True
    if note:
        rec["note"] = note
    state.setdefault("orders", []).append(rec)
    return rec


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today, ENTRY_STATUSES)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    paper = bool(cfg.get("paper", True))
    if a.status == "paper" and not paper:
        raise TicketError("--status paper is for paper mode only; the config says \"paper\": false")
    if a.status != "paper" and paper:
        raise TicketError("paper mode: nothing went to a broker, so record --status paper")
    filled_qty, filled_price = a.filled_qty, a.filled_price
    if a.status == "paper":
        filled_qty = filled_qty or order.get("qty")
        filled_price = filled_price or ref_price(order)
        if not filled_price:
            raise TicketError("no price to simulate the fill at: pass --filled-price")
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"],
           "status": "placed" if a.status == "paper" else a.status}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.status == "paper":
        new["paper"] = True
    if filled_qty:
        new["filled_qty"] = _str_num(float(filled_qty))
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, "placed (paper, simulated)" if a.status == "paper" else a.status))
    if new["status"] != "placed":
        return 0
    if filled_qty and filled_price:
        apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], filled_qty, filled_price, today)
        log_fill(a.state, now, today, rec["symbol"], rec["side"], filled_qty, filled_price, paper=paper)
        print("Added the fill (%s %s at %s) to %s." % (_str_num(float(filled_qty)), rec["symbol"],
                                                       _p(filled_price), a.portfolio))
    cancel = order.get("cancel_stop")
    if cancel:
        state = load_state(a.state)
        state["orders"].append({"at": ist_iso(now), "date": today, "index": a.index, "symbol": rec["symbol"],
                                "status": "stop_cancelled", "broker_order_id": cancel.get("broker_order_id"),
                                "paper": bool(cancel.get("paper")) or None,
                                "plain": "protective stop cancelled for the flatten"})
        state["orders"][-1] = {k: v for k, v in state["orders"][-1].items() if v is not None}
        write_json(a.state, state)
    bracket = rec.get("kind") == "entry" and is_bracket(order) and order.get("stop_loss_stop_price") is not None
    if a.status == "paper":
        if bracket:
            state = load_state(a.state)
            _record_stop(state, now, today, a.index, order, filled_qty, "stop_placed", paper=True)
            write_json(a.state, state)
            print("Simulated protective stop at %s. trader-alerts watches the stop, the target and flat_by."
                  % _p(order["stop_loss_stop_price"]))
        return 0
    if not (filled_qty and filled_price):
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (rec["symbol"], rec["side"]))
    if bracket:
        print("Next, right after the entry fills: the protective stop. ticket.py stop --index %d%s"
              % (a.index, "" if filled_qty else " --filled-qty Q"))
        print("Until the stop is placed the position has NO stop at the exchange.")
    return 0


def cmd_stop(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    if not (is_bracket(order) and order.get("stop_loss_stop_price") is not None):
        raise TicketError("order %d has no stop-loss" % a.index)
    fp = fingerprint(order)
    placed = latest(state, fp, today, ENTRY_STATUSES)
    if not placed or placed.get("status") != "placed":
        raise TicketError("order %d's entry is not recorded as placed today; the stop comes after the fill" % a.index)
    qty = a.filled_qty or placed.get("filled_qty")
    if not qty or not _f(qty) or _f(qty) <= 0:
        raise TicketError("say how many shares filled: --filled-qty Q (from get_orders / the order book)")
    if _f(qty) > _f(order.get("qty")) + 1e-9:
        raise TicketError("filled qty %s is more than the order's %s" % (_str_num(float(qty)), order.get("qty")))
    paper = bool(cfg.get("paper", True)) or placed.get("paper")
    broker = broker_of(cfg)
    if paper:
        if latest(state, fp, today, ("stop_placed",)):
            print("Paper: the simulated stop for order %d is already recorded." % a.index)
            return 0
        _record_stop(state, now, today, a.index, order, qty, "stop_placed", paper=True)
        write_json(a.state, state)
        print("Paper: simulated protective stop recorded (no broker tool called).")
        return 0
    if not a.status:
        tool, args = stop_call(order, broker, qty, cfg)
        print("Protective stop for order %d: SL-M %s %s %s, trigger %s." % (
            a.index, opposite(order.get("side")), _str_num(float(qty)), str(order.get("symbol", "")).upper(),
            _p(order["stop_loss_stop_price"])))
        print("Your yes for the bracket covered this stop. Call the %s tool:" % BROKERS[broker])
        print("\n".join(call_lines([(tool, args)], indent="  ")))
        print("Then: ticket.py stop --index %d --filled-qty %s --status placed --broker-id ID (or --status failed)"
              % (a.index, _str_num(float(qty))))
        return 0
    if a.status == "placed" and not a.broker_id:
        raise TicketError("--status placed needs --broker-id (flatten cancels the stop by that id)")
    _record_stop(state, now, today, a.index, order, qty, "stop_placed" if a.status == "placed" else "stop_failed",
                 broker_id=a.broker_id, note=a.note)
    write_json(a.state, state)
    if a.status == "placed":
        print("Recorded the protective stop for order %d, broker id %s." % (a.index, a.broker_id))
        return 0
    print("WARNING: the stop was NOT placed. %s %s is open with no stop at the exchange." % (
        _str_num(float(qty)), str(order.get("symbol", "")).upper()))
    print("Retry the stop now, or flatten: ticket.py flatten --symbol %s --qty %s"
          % (str(order.get("symbol", "")).upper(), _str_num(float(qty))))
    return 1


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now = parse_now(a.now)
    date = a.date or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-11-02")
    sym = norm_symbol(a.symbol)
    pf = apply_fill(a.portfolio, cfg, sym, a.side, a.qty, a.price, date)
    log_fill(a.state, now, date, sym, a.side, a.qty, a.price, paper=bool(cfg.get("paper", True)))
    if a.stop_hit:
        state = load_state(a.state)
        live = open_stop(state, sym, date)
        if live:
            rec = dict(live, at=ist_iso(now), status="stop_filled")
            state["orders"].append(rec)
            write_json(a.state, state)
    print("Recorded %s %s %s at %s on %s in %s. Cash now %s." % (a.side, a.qty, sym, inr(a.price), date,
                                                                a.portfolio, inr(pf["cash"])))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    broker_of(cfg)
    sym = norm_symbol(a.symbol)
    now, today = _clock(a)
    source = "the broker, get_positions"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0 or qty != int(qty):
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 20")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if str(p.get("ticker") or p.get("symbol") or "").upper() == sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check the broker (get_positions) "
                              "and pass --qty if you do hold some" % (sym, a.portfolio))
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == sym else None) \
        or cfg.get("flat_by") or "15:10"
    product = str(cfg.get("product") or "MIS").upper()
    order = {"symbol": sym, "exchange": "NSE", "product": product, "side": "sell", "qty": _str_num(qty),
             "type": "market", "time_in_force": "day",
             "why": "flat by %s IST: closes the %s day trade before the broker's auto square-off (qty from %s)"
             % (flat_by, sym, source)}
    state = load_state(a.state)
    live = open_stop(state, sym, today)
    if live:
        order["cancel_stop"] = {"broker_order_id": live.get("broker_order_id"), "paper": bool(live.get("paper"))}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": bool(cfg.get("paper", True)),
           "note": "Flatten ticket. Nothing is placed until trader-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s IST)." % (sym, plain(order, cfg), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against get_positions before you say yes." % source)
    print("Next:")
    print("  1. trader-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    if live and live.get("broker_order_id"):
        print("  3. The ticket cancels the open stop (broker id %s) first, then sells. Without the cancel the"
              % live["broker_order_id"])
        print("     stop could sell the same shares again and leave you short.")
    elif live:
        print("  3. Paper: the simulated stop is cancelled with the simulated sell.")
    else:
        print("  3. No open stop is recorded for %s. If one is open at the broker (get_orders: an open SL-M" % sym)
        print("     SELL), cancel it first with cancel_order, or the stop could sell the shares a second time.")
    print("  Check get_positions first: if the stop already filled, there is nothing to sell.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % inr(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        paper = " (paper)" if r.get("paper") and r["status"] == "placed" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-12s %s%s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), paper, reply, loss,
                                          extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (trader-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-02 09:45\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened after the Allow once / Deny card")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed", "paper"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("stop", help="step 2 of a bracket: the protective SL-M stop after the entry fills")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--filled-qty", help="shares that filled (default: the qty recorded with the result)")
    p.add_argument("--status", choices=["placed", "failed"], help="record what happened to the stop call")
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--stop-hit", action="store_true", help="this sell was the protective stop filling")
    p.add_argument("--date", help="YYYY-MM-DD (default: today in IST)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="a sell ticket that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from get_positions (default: trader/portfolio.json)")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "stop": cmd_stop, "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
