#!/usr/bin/env python3
"""Order tickets and the decision log for trader-broker. Standard library only.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket. A READY or
            approved live ticket also shows the exact tool calls Claude will
            make on your broker's MCP server: Zerodha (Kite MCP place_order)
            or Angel One (angel-one-mcp search_scrip, then place_order). An
            order is READY only if trader-risk passed THIS exact order (same
            fingerprint, same config) in the last 5 minutes, nobody has
            decided on it yet, and for an entry the market is open and it is
            before no_entries_after.
  approve   records your explicit yes for ONE order, with your reply word for
            word and the fingerprint shown on the ticket. It refuses "sure",
            "ok", "yes to all", a yes with anything beyond a plain yes for
            this order, a stale PASS, and a ticket that changed.
  decline   records a no.
  result    records what happened after the Allow once / Deny card:
            placed (with the broker's order id), denied, or failed. In paper
            mode, --status paper records a simulated fill instead.
  stop      step 2 of a bracket: after the entry fills, the protective SL-M
            stop for the filled qty. Prints the exact call, then records the
            stop's broker order id (--status placed --broker-id ID).
  target    step 3 of a bracket in "resting" mode: after the stop is placed,
            a resting LIMIT sell at the target for the filled qty.
  cancelled records that an open stop or target was cancelled (or that the
            cancel failed because it executed), e.g. the other leg after one
            leg filled.
  fill      records a fill in trader/portfolio.json (cash and shares).
  flatten   builds a sell ticket that closes a symbol's open position by
            flat_by: cancel every open stop, then a MARKET SELL. It still goes
            through trader-risk and your yes.
  log       prints today's decisions.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --fingerprint 1a2b3c4d --reply "yes"
    python3 ticket.py result --index 1 --status placed --broker-id 2511030001 --filled-qty 20 --filled-price 1228.10
    python3 ticket.py result --index 1 --status paper            # paper mode: simulated fill
    python3 ticket.py stop --index 1                              # the stop call for the filled qty
    python3 ticket.py stop --index 1 --status placed --broker-id 2511030002
    python3 ticket.py target --index 1                            # step 3 (resting mode): the target call
    python3 ticket.py target --index 1 --status placed --broker-id 2511030003
    python3 ticket.py fill --symbol RELIANCE --side sell --qty 20 --price 1245.00 --target-hit
    python3 ticket.py cancelled --symbol RELIANCE --broker-id 2511030002
    python3 ticket.py flatten --symbol RELIANCE --qty 20
    python3 ticket.py log

The target ("target_order" in trader/config.json; the default depends on the broker):
  robo     Angel One only, the default there: entry, target and stop go as ONE
           Robo bracket (variety ROBO, product BO, a LIMIT entry). Angel One
           cancels the other leg itself when one fills, so it cannot leave you
           short. Robo is intraday only and Angel refuses it on low-volume stocks.
  resting  the Zerodha default: the SL-M stop after the fill, then a resting
           LIMIT sell at the target. Neither broker links the two for intraday,
           so when one fills the other must be cancelled at once (trader-alerts
           rings, then ticket.py fill --stop-hit / --target-hit prints the
           cancel). If both fill before the cancel, you are short: get_positions
           shows a negative quantity; buy it back at once.
  watched  the old way: only the stop rests; trader-alerts watches the target
           and you flatten when it rings. A wick between two checks is missed.

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

# The fields that make an order this order: keep identical to trader-risk/check.py.
FINGERPRINT_FIELDS = ("symbol", "exchange", "side", "qty", "type", "time_in_force", "limit_price", "product",
                      "client_order_id", "order_class", "take_profit_limit_price", "stop_loss_stop_price")
BROKERS = {"zerodha": "Zerodha (Kite MCP)", "angelone": "Angel One (angel-one-mcp)"}
NSE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
TYPES = ("market", "limit")
SIDES = ("buy", "sell")
PRODUCTS = ("MIS", "CNC")
PRODUCT_WORDS = {"MIS": "intraday (MIS)", "CNC": "delivery (CNC)"}
ANGEL_PRODUCT = {"MIS": "INTRADAY", "CNC": "DELIVERY"}
TIF_WORDS = {"day": "good for today"}
YES_WORDS = ("yes", "y")
# the only words a yes may carry besides the order's own symbol, side, qty and prices
FILLER_WORDS = {"place", "it", "order", "please", "go", "ahead", "confirm", "at", "for", "shares", "share",
                "of", "on", "nse", "do", "now"}
FRESH_PASS = datetime.timedelta(minutes=5)
TARGET_WHY = ("The target is not sent as an order: Kite and Angel One have no bracket for NSE equity "
              "intraday, and a resting target next to a resting stop could both fill and leave you short. "
              "trader-alerts watches the target; when it rings, flatten.")
TARGET_MODES = ("robo", "resting", "watched")
ROBO_WHY = ("Robo bracket: Angel One holds the target and the stop as one order and cancels the other leg "
            "when one fills, so they can never both fill. Robo needs a LIMIT entry: if the price has run "
            "past it, the entry does not fill.")
RESTING_WHY = ("The target rests as a LIMIT order next to the SL-M stop. The broker does not link them: when "
               "one fills, cancel the other at once (ticket.py fill --target-hit or --stop-hit prints the "
               "cancel). If both fill first you are short: check get_positions and buy back a negative quantity.")
AUTO_REPLY = "auto-paper (delegated by the user in config; no human yes)"
CANCEL_FAILS = ("If any cancel fails because the stop already executed, do NOT send the sell; "
                "check get_positions. If it fails because the stop was already cancelled, the stop is gone: "
                "send the sell. Record each cancel as it happens: ticket.py result --index N --cancelled ID, "
                "or --cancel-failed ID --reason executed|already-cancelled.")

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
# Special sessions (Muhurat trading on Sunday 2026-11-08, budget-day Sundays) are not modelled.
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)


class TicketError(Exception):
    pass


def is_paper(cfg):
    """Live only when the config says "paper": false, literally. Keep identical to trader-risk/check.py."""
    return (cfg or {}).get("paper", True) is not False


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


def parse_utc(text):
    """"2026-10-19T04:20:00Z" -> naive UTC, or None."""
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def ist_iso(now_utc):
    """naive UTC -> "2026-11-02T09:45:00+05:30" (IST wall clock with its offset)."""
    return to_ist(now_utc).replace(microsecond=0).isoformat() + "+05:30"


def parse_hhmm(text, default):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text or default))
    if not m:
        raise TicketError("a time in the config wants HH:MM, like 14:45 (got %r)" % text)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def market_closed_why(now_utc):
    """None while NSE's regular session is open, else the reason it is not."""
    local = to_ist(now_utc)
    d = local.date()
    if d.year not in CALENDAR_YEARS:
        return "no NSE holiday list for %d in this tool yet" % d.year
    if d.weekday() >= 5:
        return "the market is closed (weekend)"
    if d.isoformat() in NSE_HOLIDAYS:
        return "the market is closed (NSE holiday: %s)" % NSE_HOLIDAYS[d.isoformat()]
    if not OPEN <= local.time() < CLOSE:
        return "the market is closed (NSE is open 9:15 AM to 3:30 PM IST)"
    return None


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

FP_DEFAULTS = {"product": "MIS", "broker": "zerodha", "paper": True}   # trader-risk's config defaults


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


def fingerprint(order, cfg=None):
    """The exact order under the exact config. Keep identical to trader-risk/check.py: an
    order is READY only if this hash matches. The ticket shows the first 8 hex; Kite's tag
    is "dts" + those 8."""
    c = dict(FP_DEFAULTS)
    c.update(cfg or {})
    plan = order.get("plan") if isinstance(order.get("plan"), dict) else None
    payload = {k: _fp_value(k, order.get(k)) for k in FINGERPRINT_FIELDS}
    payload["plan"] = ({"entry": plan.get("entry"), "stop": plan.get("stop"), "target": plan.get("target")}
                       if plan is not None else None)
    payload["cancel_stop"] = sorted(cancel_ids(order)) or None
    payload["product_resolved"] = str(order.get("product") or c.get("product") or "MIS").strip().upper()
    payload["broker"] = str(c.get("broker") or "zerodha").strip().lower()
    payload["paper"] = c.get("paper", True) is not False
    if str(c.get("target_order") or "").strip():
        payload["target_order"] = str(c["target_order"]).strip().lower()
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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


def canonical(order, cfg=None):
    """A working copy with every field in its one canonical form (side "buy", type "limit",
    product "MIS"...). Anything that is not canonical after that is refused, so a "LIMIT"
    order can never be read as a market order."""
    w = dict(order)
    w["symbol"] = norm_symbol(order.get("symbol"))
    w["side"] = str(order.get("side") or "").strip().lower()
    w["type"] = str(order.get("type") or "market").strip().lower()
    w["time_in_force"] = str(order.get("time_in_force") or "day").strip().lower()
    w["exchange"] = str(order.get("exchange") or "NSE").strip().upper()
    w["product"] = str(order.get("product") or (cfg or {}).get("product") or "MIS").strip().upper()
    oc = order.get("order_class")
    w["order_class"] = str(oc).strip().lower() if oc not in (None, "") else None
    what = "order for %s" % w["symbol"]
    if w["side"] not in SIDES:
        raise TicketError("%s: side %r is not buy or sell" % (what, order.get("side")))
    if w["type"] not in TYPES:
        raise TicketError("%s: type %r is not market or limit" % (what, order.get("type")))
    if w["time_in_force"] not in TIF_WORDS:
        raise TicketError("%s: time_in_force %r is not day" % (what, order.get("time_in_force")))
    if w["exchange"] != "NSE":
        raise TicketError("%s: exchange %r is not NSE" % (what, order.get("exchange")))
    if w["product"] not in PRODUCTS:
        raise TicketError("%s: product %r is not MIS or CNC" % (what, w["product"]))
    if w["order_class"] not in (None, "bracket"):
        raise TicketError("%s: order_class %r is not bracket" % (what, oc))
    q = _f(order.get("qty"))
    if q is None or q <= 0 or q != int(q):
        raise TicketError("%s: qty %r is not a whole number of shares" % (what, order.get("qty")))
    if w["type"] == "limit" and not (_f(order.get("limit_price")) or 0) > 0:
        raise TicketError("%s: a limit order needs a limit_price" % what)
    for k in ("limit_price", "stop_loss_stop_price", "take_profit_limit_price"):
        if order.get(k) is not None and not (_f(order.get(k)) or 0) > 0:
            raise TicketError("%s: %s %r is not a price" % (what, k, order.get(k)))
    w["target_mode"] = target_mode(cfg, w)
    return w


def target_mode(cfg, w):
    """How a bracket's target goes to the broker: robo, resting or watched (see the top of this file).
    Only a bracket with both a stop and a target has a target leg; everything else is "watched"."""
    if not (has_stop(w) and w.get("take_profit_limit_price") is not None):
        return "watched"
    cfg = cfg or {}
    broker = broker_of(cfg)
    raw = str(cfg.get("target_order") or "").strip().lower()
    mode = raw or ("robo" if broker == "angelone" else "resting")
    if mode not in TARGET_MODES:
        raise TicketError("target_order %r is not robo, resting or watched" % cfg.get("target_order"))
    if mode == "robo" and broker != "angelone":
        raise TicketError("target_order \"robo\" is an Angel One bracket; Zerodha has none: use \"resting\"")
    if mode == "robo" and w["product"] != "MIS":
        mode = "resting"   # Robo is intraday only
    return mode


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


def is_bracket(w):
    legs = w.get("take_profit_limit_price") is not None or w.get("stop_loss_stop_price") is not None
    return w.get("order_class") == "bracket" or (not w.get("order_class") and legs)


def has_stop(w):
    return is_bracket(w) and w.get("stop_loss_stop_price") is not None


def opposite(side):
    return "SELL" if str(side).lower() == "buy" else "BUY"


def ref_price(w):
    """The price a paper fill is simulated at: the limit price, else the plan's entry."""
    if w["type"] == "limit":
        return _f(w["limit_price"])
    return _f((w.get("plan") or {}).get("entry"))


def max_loss(w):
    """(rupees, stop) all in at the stop: trader-budget's plan.max_loss (it includes the charges),
    else the loss at the stop price for the limit price or the plan's entry. (None, stop) if unknown."""
    stop = _f(w.get("stop_loss_stop_price")) or _f((w.get("plan") or {}).get("stop"))
    q = _f(w.get("qty"))
    if not stop or not q:
        return None, stop
    plan = w.get("plan") or {}
    if w.get("type") == "limit":
        entry = _f(w["limit_price"])
    elif _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    return round(q * int(round(abs(entry - stop) * 100)) / 100.0, 2), stop


def loss_at_stop(w):
    """(rupees, stop) lost at the stop price alone, before charges. trader-budget's plan.max_loss
    is all in (it includes the charges), so it is not used here; plan.loss_at_stop is."""
    stop = _f(w.get("stop_loss_stop_price")) or _f((w.get("plan") or {}).get("stop"))
    q = _f(w.get("qty"))
    plan = w.get("plan") or {}
    if not stop or not q:
        return None, stop
    if w.get("type") != "limit" and _f(plan.get("loss_at_stop")) is not None:
        return round(_f(plan["loss_at_stop"]), 2), stop
    entry = _f(w.get("limit_price")) if w.get("type") == "limit" else _f(plan.get("entry"))
    if not entry:
        return None, stop
    return round(q * int(round(abs(entry - stop) * 100)) / 100.0, 2), stop


def plain_parts(w):
    """The ticket in plain words, one part per line (w is canonical)."""
    side = w["side"].upper()
    prod = PRODUCT_WORDS[w["product"]]
    tif = TIF_WORDS[w["time_in_force"]]
    if is_bracket(w):
        how = "limit %s" % _p(w["limit_price"]) if w["type"] == "limit" else "market"
        parts = ["%s %s %s on NSE, %s, %s, %s" % (side, _str_num(w.get("qty")), w["symbol"], how, prod, tif)]
        mode = w.get("target_mode", "watched")
        if mode == "robo":
            entry = robo_entry_price(w)
            parts[0] = "%s %s %s on NSE, Angel One Robo bracket, LIMIT entry %s, intraday, %s" % (
                side, _str_num(w.get("qty")), w["symbol"], _p(entry) if entry else "(no price)", tif)
            parts.append("stop-loss %s and target %s in the same Robo order: Angel cancels the other leg when "
                         "one fills" % (_p(w["stop_loss_stop_price"]), _p(w["take_profit_limit_price"])))
        else:
            if w.get("stop_loss_stop_price") is not None:
                parts.append("stop-loss %s (SL-M, placed after the entry fills)" % _p(w["stop_loss_stop_price"]))
            if w.get("take_profit_limit_price") is not None:
                parts.append(("target %s (a resting LIMIT order, placed after the stop; when one fills, cancel "
                              "the other)" if mode == "resting" else
                              "target %s (watched by trader-alerts, not a resting order)")
                             % _p(w["take_profit_limit_price"]))
        loss, stop = loss_at_stop(w)
        if loss is not None:
            parts.append("loss about %s if the stop fills at %s (it can fill worse in a fast market)"
                         % (inr(loss), _p(stop)))
        charges = _f((w.get("plan") or {}).get("charges"))
        if charges and loss is not None:
            parts.append("plus about %s estimated charges (brokerage, STT, exchange, SEBI, stamp, GST): "
                         "max loss about %s all in" % (inr(charges), inr(round(loss + charges, 2))))
        return parts
    q = _str_num(w.get("qty"))
    what = "%s share%s of %s on NSE" % (q, "" if q == "1" else "s", w["symbol"])
    how = "limit order at %s" % inr(w["limit_price"]) if w["type"] == "limit" else "market order"
    return ["%s %s, %s, %s, %s" % (side, what, how, prod, tif)]


def plain(w):
    """BUY 20 RELIANCE on NSE, market, intraday (MIS), good for today / stop-loss ... / target ... / loss ..."""
    return " / ".join(plain_parts(w))


# ---------------------------------------------------------------- the broker calls

def cancel_ids(order):
    """The stop ids a flatten cancels: broker_order_ids, else broker_order_id split on commas.
    Keep identical to trader-risk/check.py (the fingerprint covers them)."""
    cs = order.get("cancel_stop") or {}
    ids = cs.get("broker_order_ids")
    if ids is None:
        ids = str(cs.get("broker_order_id") or "").split(",")
    return [str(i).strip() for i in ids if str(i).strip()]


def robo_entry_price(w):
    """A Robo bracket needs a LIMIT entry: the order's limit price, else the plan's entry."""
    return _f(w["limit_price"]) if w["type"] == "limit" else _f((w.get("plan") or {}).get("entry"))


ROBO_FRESH = datetime.timedelta(minutes=15)   # trader-risk's FRESH_MINUTES for a plan's entry


def plan_time(plan):
    """When a plan's entry price is from, as naive UTC: the older of created_at and date + entry_time (IST).
    The same rule as trader-risk/check.py; None when the plan says neither."""
    times = [parse_utc(plan.get("created_at"))]
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(plan.get("entry_time") or ""))
    if m and re.match(r"^\d{4}-\d{2}-\d{2}$", str(plan.get("date") or "")):
        h, mi = int(m.group(1)), int(m.group(2))
        if m.group(3):
            h = h % 12 + (12 if m.group(3).lower() == "pm" else 0)
        if h <= 23 and mi <= 59:
            times.append(ist_to_utc(datetime.datetime.strptime(plan["date"], "%Y-%m-%d").replace(hour=h, minute=mi)))
    times = [t for t in times if t]
    return min(times) if times else None


def robo_problem(w, now):
    """Why this order cannot go as a Robo bracket, or None. A market order is sent at the plan's entry, so
    that entry must be fresh (what trader-risk measured the risk from); the target must sit beyond the
    entry and the stop behind it, or Angel's distances would point the wrong way."""
    if w.get("target_mode") != "robo":
        return None
    entry = robo_entry_price(w)
    if not entry:
        return "a Robo bracket needs a LIMIT entry and this order has no limit price or plan entry"
    if w["type"] != "limit":
        pt = plan_time(w.get("plan") or {})
        if pt is None or not -60 <= (now - pt).total_seconds() <= ROBO_FRESH.total_seconds():
            return ("a Robo bracket goes in as a LIMIT at the plan's entry %s, and that entry is %s: re-run the "
                    "plan (trader-exit, trader-budget, trader-risk) or use a limit order"
                    % (_p(entry), "undated" if pt is None else "older than 15 minutes"))
    target, stop = _f(w["take_profit_limit_price"]), _f(w["stop_loss_stop_price"])
    ok = stop < entry < target if w["side"] == "buy" else target < entry < stop
    if not ok:
        return ("a Robo bracket needs the %s: entry %s, stop %s, target %s" % (
            "stop below the entry and the target above it" if w["side"] == "buy" else
            "stop above the entry and the target below it", _p(entry), _p(stop), _p(target)))
    return None


def cancel_kind(w, oid):
    """stop, target or robo: what a cancel id on a flatten ticket is (older tickets: a stop)."""
    cs = w.get("cancel_stop") or {}
    if str(oid) in [str(x) for x in (cs.get("robo_ids") or [])]:
        return "robo"
    if str(oid) in [str(x) for x in (cs.get("target_ids") or [])]:
        return "target"
    return "stop"


def cancel_call(broker, kind, oid):
    """(tool, arguments) that cancels one open leg."""
    if broker == "angelone":
        return ("cancel_order", {"variety": {"robo": "ROBO", "target": "NORMAL"}.get(kind, "STOPLOSS"),
                                 "orderid": str(oid)})
    return ("cancel_order", {"variety": "regular", "order_id": str(oid)})


def entry_calls(w, broker, fp):
    """[(tool, arguments)] for step 1: every leg cancel (a flatten), then the order itself."""
    sym = w["symbol"]
    side = w["side"].upper()
    qty = int(float(w["qty"]))
    limit = w["type"] == "limit"
    calls = [cancel_call(broker, cancel_kind(w, oid), oid) for oid in cancel_ids(w)]
    if broker == "angelone" and w.get("target_mode") == "robo":
        entry = robo_entry_price(w)
        if not entry:
            raise TicketError("a Robo bracket needs a LIMIT entry and %s has no limit price or plan entry" % sym)
        tp, sl = _f(w["take_profit_limit_price"]), _f(w["stop_loss_stop_price"])
        if not (sl < entry < tp if w["side"] == "buy" else tp < entry < sl):
            raise TicketError("a Robo bracket needs the target beyond the entry and the stop behind it "
                              "(entry %s, stop %s, target %s)" % (_p(entry), _p(sl), _p(tp)))
        calls.append(("search_scrip", {"exchange": "NSE", "searchscrip": sym}))
        calls.append(("place_order", {
            "variety": "ROBO", "tradingsymbol": sym + "-EQ",
            "symboltoken": "<from search_scrip: the row whose tradingsymbol is exactly %s-EQ>" % sym,
            "transactiontype": side, "exchange": "NSE", "ordertype": "LIMIT", "producttype": "BO",
            "duration": "DAY", "quantity": str(qty), "price": _p(entry),
            "squareoff": _p(abs(_f(w["take_profit_limit_price"]) - entry)),
            "stoploss": _p(abs(entry - _f(w["stop_loss_stop_price"])))}))
        return calls
    if broker == "angelone":
        calls.append(("search_scrip", {"exchange": "NSE", "searchscrip": sym}))
        args = {"variety": "NORMAL", "tradingsymbol": sym + "-EQ",
                "symboltoken": "<from search_scrip: the row whose tradingsymbol is exactly %s-EQ>" % sym,
                "transactiontype": side, "exchange": "NSE", "ordertype": "LIMIT" if limit else "MARKET",
                "producttype": ANGEL_PRODUCT[w["product"]], "duration": "DAY", "quantity": str(qty),
                "price": _p(w["limit_price"]) if limit else "0"}
        calls.append(("place_order", args))
        return calls
    args = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": side,
            "quantity": qty, "product": w["product"], "order_type": "LIMIT" if limit else "MARKET",
            "validity": "DAY", "tag": "dts" + fp[:8]}
    if limit:
        args["price"] = _paise(w["limit_price"])
    calls.append(("place_order", args))
    return calls


def stop_call(w, broker, qty, fp):
    """(tool, arguments) for step 2: the protective SL-M stop for the filled qty."""
    if w.get("stop_loss_stop_price") is None:
        raise TicketError("this order has no stop_loss_stop_price")
    sym = w["symbol"]
    side = opposite(w["side"])
    qty = int(float(qty))
    stop = w["stop_loss_stop_price"]
    if broker == "angelone":
        return ("place_order", {"variety": "STOPLOSS", "tradingsymbol": sym + "-EQ",
                                "symboltoken": "<the same symboltoken as the entry>",
                                "transactiontype": side, "exchange": "NSE", "ordertype": "STOPLOSS_MARKET",
                                "producttype": ANGEL_PRODUCT[w["product"]], "duration": "DAY",
                                "quantity": str(qty), "price": "0", "triggerprice": _p(stop)})
    return ("place_order", {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym,
                            "transaction_type": side, "quantity": qty, "product": w["product"],
                            "order_type": "SL-M", "trigger_price": _paise(stop), "validity": "DAY",
                            "tag": "dts" + fp[:8] + "sl"})


def target_call(w, broker, qty, fp):
    """(tool, arguments) for step 3 in resting mode: a LIMIT order at the target for the filled qty."""
    if w.get("take_profit_limit_price") is None:
        raise TicketError("this order has no take_profit_limit_price")
    sym = w["symbol"]
    side = opposite(w["side"])
    qty = int(float(qty))
    target = w["take_profit_limit_price"]
    if broker == "angelone":
        return ("place_order", {"variety": "NORMAL", "tradingsymbol": sym + "-EQ",
                                "symboltoken": "<the same symboltoken as the entry>",
                                "transactiontype": side, "exchange": "NSE", "ordertype": "LIMIT",
                                "producttype": ANGEL_PRODUCT[w["product"]], "duration": "DAY",
                                "quantity": str(qty), "price": _p(target)})
    return ("place_order", {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym,
                            "transaction_type": side, "quantity": qty, "product": w["product"],
                            "order_type": "LIMIT", "price": _paise(target), "validity": "DAY",
                            "tag": "dts" + fp[:8] + "tg"})


def modify_stop_call(w, broker, order_id, qty):
    """(tool, arguments) to change the quantity of a stop that is already open."""
    sym = w["symbol"]
    qty = int(float(qty))
    stop = w["stop_loss_stop_price"]
    if broker == "angelone":
        return ("modify_order", {"variety": "STOPLOSS", "orderid": str(order_id), "ordertype": "STOPLOSS_MARKET",
                                 "producttype": ANGEL_PRODUCT[w["product"]], "duration": "DAY", "price": "0",
                                 "quantity": str(qty), "tradingsymbol": sym + "-EQ",
                                 "symboltoken": "<the same symboltoken as the entry>", "exchange": "NSE",
                                 "triggerprice": _p(stop)})
    return ("modify_order", {"variety": "regular", "order_id": str(order_id), "order_type": "SL-M",
                             "quantity": qty, "trigger_price": _paise(stop)})


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


def _same(n, values):
    return any(v is not None and abs(n - v) < 0.005 for v in values)


def loss_numbers(w, rv=None):
    """The loss figures the ticket shows: all in, at the stop alone, and trader-risk's max loss."""
    out = [max_loss(w)[0], loss_at_stop(w)[0]]
    if rv and _f(rv.get("max_loss")) is not None:
        out.append(_f(rv["max_loss"]))
    return [x for x in out if x is not None]


def parse_yes(reply, w, batch_symbols=(), rv=None):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why).
    "yes" or "y", then nothing but this order's symbol, its side, a few filler words, and
    numbers: a bare number must be the qty or the limit price; a loss figure counts only
    as "max loss N" or "₹N". Anything else is not a yes."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    sym = w["symbol"]
    mark = "\u0001"
    text = re.sub(r"(?<![A-Za-z0-9&\-])%s(?:\.NS)?(?![A-Za-z0-9&\-])" % re.escape(sym), " %s " % mark, raw,
                  flags=re.I)
    token_re = mark + r"|\u20b9\s*\d[\d,]*(?:\.\d+)?|[A-Za-z][A-Za-z0-9&]*|\d[\d,]*(?:\.\d+)?"
    if re.sub(r"[\s,.!;@\-]", "", re.sub(token_re, "", text)):
        return False, "the reply has characters a plain yes does not; ask again"
    tokens = re.findall(token_re, text)
    if not tokens or tokens[0].lower() not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, haan, go, do it)"
    others = {str(s).upper() for s in batch_symbols} - {sym}
    bare = [_f(w.get("qty")), _f(w.get("limit_price")) if w["type"] == "limit" else None]
    losses = loss_numbers(w, rv)
    rest = tokens[1:]
    i = 0
    while i < len(rest):
        t = rest[i]
        low = t.lower()
        i += 1
        if t == mark or low in FILLER_WORDS or low == w["side"]:
            continue
        if t.startswith("\u20b9"):
            n = float(t[1:].strip().replace(",", ""))
            if not _same(n, losses + bare[1:]):
                return False, "the reply says %s, but this order is %s" % (t, plain(w))
            continue
        if low == "max" and i < len(rest) and rest[i].lower() == "loss":
            nxt = rest[i + 1] if i + 1 < len(rest) else ""
            amount = nxt.lstrip("\u20b9").strip().replace(",", "")
            if not re.match(r"^\d+(?:\.\d+)?$", amount) or not _same(float(amount), losses):
                return False, "the reply's max loss is not this order's (%s)" % plain(w)
            i += 2
            continue
        if t[0].isdigit():
            if not _same(float(t.replace(",", "")), bare):
                return False, "the reply says %s, but this order is %s" % (t, plain(w))
            continue
        if t.upper() in others:
            return False, "the reply names %s, but this order is for %s" % (t.upper(), sym)
        if low in SIDES:
            return False, "the reply says %s, but this order is a %s" % (low, w["side"])
        return False, "the reply has more than a plain yes (\"%s\"); ask again" % t
    return True, "explicit yes for %s" % plain(w)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


ENTRY_STATUSES = ("approved", "declined", "placed", "denied", "failed")


def latest(state, fp, today, statuses=None):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            if statuses is None or rec.get("status") in statuses:
                found = rec
    return found


def config_matches(rec, cfg):
    return bool(rec.get("paper")) == is_paper(cfg) and rec.get("broker") == broker_of(cfg)


def mine(recs, fp=None, paper=None):
    """Only the legs of one order (fingerprint) and of one mode (paper or live), when asked."""
    return [r for r in recs if (fp is None or r.get("fingerprint") == fp)
            and (paper is None or bool(r.get("paper")) == bool(paper))]


def open_stops(state, symbol, today):
    """Today's protective stops for a symbol that are still open: placed, minus cancelled or filled."""
    live, gone = [], set()
    for rec in state.get("orders", []):
        if rec_day(rec) != today or str(rec.get("symbol", "")).upper() != symbol:
            continue
        if rec.get("status") == "stop_placed":
            live.append(rec)
        elif rec.get("status") in ("stop_cancelled", "stop_filled"):
            gone.add(rec.get("broker_order_id"))
    return [r for r in live if r.get("broker_order_id") not in gone]


def open_legs(state, symbol, today, kind):
    """Today's open target or robo legs for a symbol: placed, minus cancelled or filled (as open_stops)."""
    live, gone = [], set()
    for rec in state.get("orders", []):
        if rec_day(rec) != today or str(rec.get("symbol", "")).upper() != symbol:
            continue
        if rec.get("status") == kind + "_placed":
            live.append(rec)
        elif rec.get("status") in (kind + "_cancelled", kind + "_filled"):
            gone.add(rec.get("broker_order_id"))
    return [r for r in live if r.get("broker_order_id") not in gone]


def leg_record(now, today, idx, w, fp, qty, kind, status, broker_id=None, paper=False, note=None):
    """A target or robo leg record (stops use stop_record)."""
    price = _f(w.get("take_profit_limit_price"))
    what = ("resting target: LIMIT %s %s %s at %s" % (opposite(w["side"]), _str_num(float(qty)), w["symbol"], _p(price))
            if kind == "target" else
            "Robo bracket %s %s %s: target %s, stop %s" % (w["side"].upper(), _str_num(float(qty)), w["symbol"],
                                                           _p(price), _p(w.get("stop_loss_stop_price"))))
    rec = {"at": ist_iso(now), "date": today, "index": idx, "fingerprint": fp, "symbol": w["symbol"],
           "qty": _str_num(float(qty)), "target": price, "stop": _f(w.get("stop_loss_stop_price")),
           "status": "%s_%s" % (kind, status), "plain": what + (" (paper)" if paper else "")}
    if paper and not broker_id:
        broker_id = "paper-%s-%s" % (kind, fp[:8])
    if broker_id:
        rec["broker_order_id"] = str(broker_id)
    if paper:
        rec["paper"] = True
    if note:
        rec["note"] = note
    return rec


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def kind_of(w, rv):
    """entry or exit, as trader-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if w["side"] == "buy" else "exit"


def clock_why(kind, cfg, now):
    """None if an entry may go in at this moment (trading day, 9:15 to 15:30 IST, not after
    no_entries_after), else why not. Exits have no time-of-day gate."""
    if kind != "entry":
        return None
    closed = market_closed_why(now)
    if closed:
        return closed
    raw = cfg.get("no_entries_after", "14:45")      # trader-risk's default; blank or null turns it off
    if raw is None or not str(raw).strip():
        return None
    cutoff = parse_hhmm(raw, "14:45")
    if to_ist(now).replace(second=0, microsecond=0).time() > cutoff:   # 14:45:59 is still 14:45
        return "no new entries after %s IST (no_entries_after)" % cutoff.strftime("%H:%M")
    return None


def stale_why(w, rv, cfg, now):
    """None if a PASS can be acted on right now, else why not. A yes may not be older than the
    PASS it relies on, so a check stamped after now does not count either."""
    checked = parse_utc(rv.get("checked_at"))
    if checked is None or not datetime.timedelta(0) <= now - checked <= FRESH_PASS:
        return "PASS is stale: re-run trader-risk (a PASS counts for 5 minutes)"
    why = clock_why(kind_of(w, rv), cfg, now)
    if why:
        return "NOT READY: " + why
    if kind_of(w, rv) == "entry" and not is_paper(cfg) and not has_stop(w) \
            and _f((w.get("plan") or {}).get("stop")):
        return "NOT READY: no exchange stop: use a bracket order (the plan's stop is not an order)"
    return None


def approval_why(rec, kind, cfg, now):
    """None while a recorded yes can still be acted on: at most 5 minutes old, and for an entry
    the clock gates still hold. Else why not."""
    at = parse_utc(rec.get("at"))
    if at is None or not datetime.timedelta(0) <= now - at <= FRESH_PASS:
        return "approval expired (a yes counts for 5 minutes)"
    return clock_why(kind, cfg, now)


def unresolved(fp, cfg, why):
    if is_paper(cfg):
        where = "record the paper result (ticket.py result --status paper)"
    else:
        where = ("check get_orders (Kite tag dts%s / Angel order book) and record the result" % fp[:8])
    return ("UNRESOLVED: the yes can no longer be acted on (%s). %s; if it was never sent, record "
            "--status denied" % (why, where[0].upper() + where[1:]))


def status_of(w, fp, cfg, risk, state, today, now):
    """(ready, approved, label, risk_result). approved: the user's yes is recorded, still fresh,
    and the broker call is still to be made. The decision record is read before the risk
    verdict, so a later check that lists a placed order as a duplicate does not hide it."""
    rv, why = risk_verdict(risk, fp, today)
    rec = latest(state, fp, today, ENTRY_STATUSES)
    if rec and config_matches(rec, cfg):
        st = rec.get("status")
        at = parse_utc(rec.get("at"))
        newer = (rv is not None and rv.get("verdict") == "PASS" and at is not None
                 and (parse_utc(rv.get("checked_at")) or at) > at)
        if st == "placed":
            return False, False, "DONE: placed%s at %s" % (" (paper)" if rec.get("paper") else "", rec.get("at")), rv
        if st == "approved":
            expired = approval_why(rec, rec.get("kind") or kind_of(w, rv), cfg, now)
            if not expired:
                return False, True, "APPROVED by you at %s, waiting for the broker call" % rec.get("at"), rv
            # never falls through to READY: the call may have gone out, so its result comes first
            return False, False, unresolved(fp, cfg, expired), rv
        elif not newer:   # declined, denied, failed: a new PASS and a new yes re-open it
            return False, False, "DONE: %s at %s (a new trader-risk PASS re-opens it)" % (st, rec.get("at")), rv
    if rv is None:
        return False, False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    stale = stale_why(w, rv, cfg, now)
    if stale:
        return False, False, stale, rv
    robo = None if is_paper(cfg) else robo_problem(w, now)
    if robo:
        return False, False, "NOT READY: " + robo, rv
    return True, False, "READY: trader-risk PASS, waiting for your yes", rv


# ---------------------------------------------------------------- rendering

def header(idx, total, cfg):
    broker = broker_of(cfg)
    if is_paper(cfg):
        acct = "PAPER (SIMULATED: no order goes to %s)" % BROKERS[broker].split(" (")[0]
    else:
        acct = "LIVE ACCOUNT, REAL MONEY, %s" % BROKERS[broker]
    return "ORDER %d of %d, %s" % (idx, total, acct)


def render(order, idx, total, cfg, risk, state, today, now):
    w = canonical(order, cfg)
    fp = fingerprint(order, cfg)
    ready, approved, label, rv = status_of(w, fp, cfg, risk, state, today, now)
    broker = broker_of(cfg)
    paper = is_paper(cfg)
    lines = [header(idx, total, cfg)]
    for part in plain_parts(w):
        lines.append("  " + part)
    if w.get("why"):
        lines.append("  why: " + w["why"])
    lines.append("  fingerprint: " + fp[:8])
    lines.append("  " + label)
    for warn in (rv or {}).get("warnings", []):
        lines.append("  note: " + warn)
    mode = w.get("target_mode", "watched")
    if paper:
        lines.append("  PAPER: Claude calls no broker tool. After your yes the fill is simulated here")
        lines.append("  (ticket.py result --index %d --status paper)%s." % (
            idx, "" if not has_stop(w) else ", and so are the stop and the target" if mode != "watched"
            else ", and so is the stop"))
        if ref_price(w) is None:
            lines.append("  It has no price of its own: pass --filled-price P (the live price), or keep")
            lines.append("  trader/prices.json fresh (under 15 minutes old).")
    elif (ready or approved) and robo_problem(w, now):
        lines.append("  No broker call: " + robo_problem(w, now) + ".")
    elif ready or approved:
        lines.append("  Step 1, after your yes, Claude calls the %s tools, in order:" % BROKERS[broker])
        lines.extend(call_lines(entry_calls(w, broker, fp)))
        if broker == "angelone":
            lines.append("  Use the symboltoken only if search_scrip returns a row whose tradingsymbol is")
            lines.append("  exactly %s-EQ. No exact match: stop and tell the user. Never pass force: true." % w["symbol"])
        if cancel_ids(w):
            lines.append("  The cancels go first: an open stop or target would otherwise sell these shares a second time.")
            if any(cancel_kind(w, i) == "robo" for i in cancel_ids(w)):
                lines.append("  After cancelling a Robo bracket, call get_positions: Angel may already have closed the")
                lines.append("  position. Send the sell only for the quantity still held; if none, record --status failed.")
            lines.append("  " + CANCEL_FAILS)
        if has_stop(w) and mode != "robo":
            tool, args = stop_call(w, broker, w["qty"], fp)
            lines.append("  Step 2, only after the entry fills: the protective stop for the filled qty")
            lines.append("  (ticket.py stop --index %d prints it with the real filled qty):" % idx)
            lines.extend(call_lines([(tool, args)]))
        if mode == "resting":
            tool, args = target_call(w, broker, w["qty"], fp)
            lines.append("  Step 3, only after the stop is placed: the resting target for the filled qty")
            lines.append("  (ticket.py target --index %d prints it with the real filled qty):" % idx)
            lines.extend(call_lines([(tool, args)]))
    else:
        lines.append("  No broker call: this ticket is not ready.")
    if has_stop(w) and w.get("take_profit_limit_price") is not None:
        lines.append("  " + {"robo": ROBO_WHY, "resting": RESTING_WHY}.get(mode, TARGET_WHY))
    if ready:
        lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
        if not paper:
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today, now):
    w = canonical(order, cfg)
    fp = fingerprint(order, cfg)
    ready, approved, label, rv = status_of(w, fp, cfg, risk, state, today, now)
    loss, stop = max_loss(w)
    if rv and _f(rv.get("max_loss")) is not None:
        loss = _f(rv["max_loss"])
    broker = broker_of(cfg)
    paper = is_paper(cfg)
    live = not paper and (ready or approved) and not robo_problem(w, now)
    out = {"index": idx, "ready": ready, "approved": approved, "status": label, "plain": plain(w),
           "fingerprint": fp, "paper": paper, "broker": broker,
           "why": w.get("why", ""), "bracket": is_bracket(w), "max_loss": loss, "stop": stop,
           "calls": [{"tool": t, "input": a} for t, a in entry_calls(w, broker, fp)] if live else []}
    out["target_mode"] = w.get("target_mode", "watched")
    if live and has_stop(w) and out["target_mode"] != "robo":
        t, a = stop_call(w, broker, w["qty"], fp)
        out["stop_call"] = {"tool": t, "input": a, "after": "the entry fills; quantity = the filled qty"}
    if live and out["target_mode"] == "resting":
        t, a = target_call(w, broker, w["qty"], fp)
        out["target_call"] = {"tool": t, "input": a, "after": "the stop is placed; quantity = the filled qty"}
    return out


# ---------------------------------------------------------------- fills -> portfolio

def compute_fill(portfolio_path, cfg, symbol, side, qty, price, date, product=None):
    """The portfolio after a fill, NOT written: cash and shares, with a cost-basis lot per buy
    (oldest lots are relieved first on a sell). Raises before anything is written."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    symbol = symbol.upper()
    qty, price = _f(qty), _f(price)
    if not qty or not price or qty <= 0 or price <= 0:
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
        if product:
            pos["product"] = product
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": int(qty), "cost": cost})
        pos["shares"] = int(round(float(pos.get("shares", 0)) + qty))
        pf["cash"] = round(float(pf.get("cash", 0)) - cost, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s (short sales are not "
                              "recorded)" % (_str_num(qty), symbol, pos.get("shares", 0) if pos else 0))
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
    return pf


def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, product=None):
    pf = compute_fill(portfolio_path, cfg, symbol, side, qty, price, date, product)
    write_json(portfolio_path, pf)
    return pf


def fill_record(now, today, symbol, side, qty, price, paper=False):
    """A fill in the log, so trader-risk sees the day's trades (a buy and a sell the same day)."""
    rec = {"at": ist_iso(now), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
           "qty": _str_num(float(qty)), "price": float(price),
           "plain": "%s %s %s at %s%s" % (side.upper(), _str_num(float(qty)), symbol.upper(), _p(price),
                                          " (paper)" if paper else "")}
    if paper:
        rec["paper"] = True
    return rec


def position_product(portfolio_path, symbol):
    pf = read_json(portfolio_path, "portfolio", required=False) or {}
    for p in pf.get("positions", []):
        if str(p.get("ticker") or p.get("symbol") or "").upper() == symbol and p.get("product"):
            return str(p["product"]).upper()
    return None


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
    for i, o in enumerate(orders, 1):
        try:
            canonical(o, cfg)
        except TicketError as e:
            raise TicketError("order %d is not valid: %s" % (i, e))
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


def _approval(state, fp, idx, cfg, today):
    """Today's live yes for this exact order under this config, or a TicketError that says why not."""
    rec = latest(state, fp, today, ENTRY_STATUSES)
    if rec and rec.get("status") == "approved" and config_matches(rec, cfg):
        return rec
    for r in state.get("orders", []):
        if r.get("status") == "approved" and r.get("index") == idx and rec_day(r) == today \
                and (r.get("fingerprint") != fp or not config_matches(r, cfg)):
            if not config_matches(r, cfg):
                raise TicketError("config changed since your yes: re-check and re-approve")
    raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % idx)


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today, now) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2, ensure_ascii=False))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today, now)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def auto_paper_why(cfg):
    """None if the config delegates PAPER approvals to the autopilot, else why not.
    Every guard is separate and literal: a live config can never auto-approve."""
    if not is_paper(cfg):
        return "--auto-paper is impossible with a live config (\"paper\": false): a live order needs a human yes"
    auto = cfg.get("autopilot")
    if not isinstance(auto, dict) or "auto_paper" not in auto:
        return "--auto-paper needs \"autopilot\": {\"auto_paper\": true} in trader/config.json"
    if auto.get("auto_paper") is not True:
        return "--auto-paper needs autopilot.auto_paper to be the JSON value true (it is %r)" % (auto.get("auto_paper"),)
    return None


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    if a.auto_paper:
        if a.reply is not None:
            raise TicketError("--auto-paper takes no --reply: it is not a human yes")
        why = auto_paper_why(cfg)
        if why:
            raise TicketError(why)
    elif a.reply is None:
        raise TicketError("approve needs --reply with the user's words (or --auto-paper in paper mode)")
    order = _pick(orders, a.index)
    w = canonical(order, cfg)
    fp = fingerprint(order, cfg)
    want = (a.fingerprint or "").strip().lower()
    if len(want) < 8 or not fp.startswith(want):
        raise TicketError("order %d is not the ticket you showed (fingerprint %s, not %s): show it again"
                          % (a.index, fp[:8], want or "missing"))
    ready, approved, label, rv = status_of(w, fp, cfg, risk, state, today, now)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = (True, "auto-paper") if a.auto_paper else \
        parse_yes(a.reply, w, [o.get("symbol", "") for o in orders], rv)
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, stop = max_loss(w)
    if _f(rv.get("max_loss")) is not None:
        loss = round(_f(rv["max_loss"]), 2)
    broker = broker_of(cfg)
    paper = is_paper(cfg)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp,
           "symbol": w["symbol"], "side": w["side"], "qty": w.get("qty"), "product": w["product"],
           "kind": kind_of(w, rv), "max_loss": loss, "stop": stop,
           "plain": plain(w), "status": "approved", "reply": AUTO_REPLY if a.auto_paper else a.reply,
           "pass_checked_at": rv.get("checked_at"), "paper": paper, "broker": broker}
    if a.auto_paper:
        if not paper or rec["paper"] is not True:   # belt and braces: never an automatic live approval
            raise TicketError("--auto-paper is impossible with a live config")
        rec["auto"] = True
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    if a.auto_paper:
        print("AUTO-PAPER: recorded a delegated paper approval for order %d: %s." % (a.index, plain(w)))
    else:
        print("Recorded your yes for order %d: %s." % (a.index, plain(w)))
    if paper:
        print("PAPER: call no broker tool. Record the simulated fill: ticket.py result --index %d --status paper"
              " [--filled-price P]" % a.index)
        return 0
    print("Now call the %s tools, in order:" % BROKERS[broker])
    print("\n".join(call_lines(entry_calls(w, broker, fp), indent="  ")))
    if cancel_ids(w):
        print(CANCEL_FAILS)
    print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
          "placed|denied|failed --broker-id ID" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    w = canonical(order, cfg)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order, cfg),
           "symbol": w["symbol"], "side": w["side"], "plain": plain(w), "status": "declined",
           "reply": a.reply or "no", "paper": is_paper(cfg), "broker": broker_of(cfg)}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(w)))
    return 0


def stop_record(now, today, idx, w, fp, qty, status, broker_id=None, paper=False, note=None):
    rec = {"at": ist_iso(now), "date": today, "index": idx, "fingerprint": fp, "symbol": w["symbol"],
           "side": opposite(w["side"]).lower(), "qty": _str_num(float(qty)),
           "stop": _f(w.get("stop_loss_stop_price")), "status": status,
           "plain": "protective stop: SL-M %s %s %s, trigger %s%s" % (
               opposite(w["side"]), _str_num(float(qty)), w["symbol"], _p(w["stop_loss_stop_price"]),
               " (paper)" if paper else "")}
    if paper and not broker_id:
        broker_id = "paper-" + fp[:8]
    if broker_id:
        rec["broker_order_id"] = str(broker_id)
    if paper:
        rec["paper"] = True
    if note:
        rec["note"] = note
    return rec


def record_cancels(a, state, w, now, today):
    """Record each stop cancel as it happens, so open_stops stays true. Returns 1 if a stop
    turned out to have executed (then the sell must not go)."""
    ids = cancel_ids(w)
    reason = (a.reason or "").strip().lower()
    if a.cancel_failed and reason not in ("executed", "already-cancelled"):
        raise TicketError("--cancel-failed needs --reason executed or --reason already-cancelled")
    for oid in (a.cancelled or []) + (a.cancel_failed or []):
        if oid not in ids:
            raise TicketError("stop %s is not on this ticket (it cancels %s)" % (oid, ", ".join(ids) or "nothing"))
    executed = False
    for oid in a.cancelled or []:
        k = cancel_kind(w, oid)
        state["orders"].append({"at": ist_iso(now), "date": today, "index": a.index, "symbol": w["symbol"],
                                "status": k + "_cancelled", "broker_order_id": oid,
                                "plain": "%s %s cancelled" % (LEG_WORDS[k], oid)})
        print("Recorded: %s %s cancelled." % (LEG_WORDS[k], oid))
    for oid in a.cancel_failed or []:
        k = cancel_kind(w, oid)
        if reason == "already-cancelled":
            state["orders"].append({"at": ist_iso(now), "date": today, "index": a.index, "symbol": w["symbol"],
                                    "status": k + "_cancelled", "broker_order_id": oid,
                                    "note": "cancel failed: already cancelled",
                                    "plain": "%s %s was already cancelled" % (LEG_WORDS[k], oid)})
            print("Recorded: %s %s was already cancelled. It is gone: the sell can go." % (LEG_WORDS[k], oid))
        else:
            executed = True
            state["orders"].append({"at": ist_iso(now), "date": today, "index": a.index, "symbol": w["symbol"],
                                    "status": k + "_filled", "broker_order_id": oid,
                                    "note": "cancel failed: it executed",
                                    "plain": "%s %s executed" % (LEG_WORDS[k], oid)})
            print("Recorded: %s %s EXECUTED. Do NOT send the sell; check get_positions, then record the "
                  "fill with ticket.py fill --side sell." % (LEG_WORDS[k], oid))
    write_json(a.state, state)
    return 1 if executed else 0


LEG_WORDS = {"stop": "protective stop", "target": "resting target", "robo": "Robo bracket"}
PRICE_FRESH = datetime.timedelta(minutes=15)


def fresh_price(path, symbol, now):
    """A price for symbol from trader/prices.json no more than 15 minutes old, else None.
    Its time: the quote's own (detail.time), else when it was fetched, else the file's as_of."""
    doc = read_json(path, "prices", required=False)
    if not isinstance(doc, dict):
        return None
    prices = doc.get("prices", doc)
    if not isinstance(prices, dict):
        return None
    price = None
    for s, p in prices.items():
        if str(s).upper() == symbol and _f(p):
            price = _f(p)
    d = (doc.get("detail") or {}).get(symbol) or {}
    at = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or parse_utc(doc.get("as_of"))
    if price is None or at is None or not datetime.timedelta(0) <= now - at <= PRICE_FRESH:
        return None
    return price


def no_stop_warning(a, cfg, w, fp, now, today):
    """A flatten whose stops were cancelled but whose sell was denied or failed leaves the
    position with NO stop at the exchange. Say so loudly and give the SL-M call to put it back."""
    if w["side"] != "sell" or not cancel_ids(w):
        return 0
    state = load_state(a.state)
    recs = state.get("orders", [])
    gone = [i for i in cancel_ids(w) if any(r.get("status") in ("stop_cancelled", "robo_cancelled")
                                            and r.get("broker_order_id") == i and rec_day(r) == today for r in recs)]
    if not gone or open_stops(state, w["symbol"], today) or open_legs(state, w["symbol"], today, "robo"):
        return 0
    if any(cancel_kind(w, i) == "robo" for i in gone):
        print("Check get_positions first: cancelling a Robo bracket may already have closed the position at Angel. "
              "If shares are still held, they have NO stop now.")
    trigger = None
    for r in recs:
        if r.get("status") == "robo_placed" and r.get("broker_order_id") in gone and _f(r.get("stop")):
            trigger = _f(r["stop"])
    for r in recs:
        if r.get("status") == "stop_placed" and r.get("broker_order_id") in gone and _f(r.get("stop")):
            trigger = _f(r["stop"])
    paper = is_paper(cfg)
    print("WARNING: NO STOP AT THE EXCHANGE for %s. Its stop%s %s %s cancelled, but the sell did not go."
          % (w["symbol"], "" if len(gone) == 1 else "s", ", ".join(gone), "was" if len(gone) == 1 else "were"))
    print("Re-flatten now (ticket.py flatten --symbol %s), or put the stop back:" % w["symbol"])
    if trigger is None:
        print("  (no trigger price recorded for the cancelled stop: use the plan's stop)")
        return 1
    held = {"symbol": w["symbol"], "side": "buy", "product": w["product"], "stop_loss_stop_price": trigger}
    if paper:
        state["orders"].append(dict(stop_record(now, today, a.index, held, fp, w["qty"], "stop_placed", paper=True),
                                    broker_order_id="paper-restop-" + fp[:8]))
        write_json(a.state, state)
        print("  Paper: a simulated SL-M SELL %s %s, trigger %s, is recorded in its place (no broker call)."
              % (w["qty"], w["symbol"], _p(trigger)))
        return 1
    tool, args = stop_call(held, broker_of(cfg), w["qty"], fp)
    print("\n".join(call_lines([(tool, args)], indent="  ")))
    print("Then record it: ticket.py result --index %d --restop-id ID" % a.index)
    return 1


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    w = canonical(order, cfg)
    fp = fingerprint(order, cfg)
    if a.restop_id:   # after a failed flatten: no open yes is needed to record a stop
        trigger = None
        for r in state.get("orders", []):
            if r.get("status") in ("stop_placed", "robo_placed") and r.get("symbol") == w["symbol"] \
                    and _f(r.get("stop")):
                trigger = _f(r["stop"])
        if trigger is None:
            raise TicketError("no stop price recorded for %s: record the re-placed stop by hand" % w["symbol"])
        held = {"symbol": w["symbol"], "side": "buy", "product": w["product"], "stop_loss_stop_price": trigger}
        state["orders"].append(stop_record(now, today, a.index, held, fp, w["qty"], "stop_placed",
                                           broker_id=a.restop_id, note="re-placed after a failed flatten"))
        write_json(a.state, state)
        print("Recorded the re-placed stop for %s, broker id %s." % (w["symbol"], a.restop_id))
        return 0
    rec = _approval(state, fp, a.index, cfg, today)
    paper = is_paper(cfg)
    if not (a.status or a.cancelled or a.cancel_failed):
        raise TicketError("say what happened: --status placed|denied|failed|paper, or --cancelled ID, "
                          "or --cancel-failed ID --reason executed|already-cancelled")
    if a.cancelled or a.cancel_failed:
        code = record_cancels(a, state, w, now, today)
        if not a.status:
            return code
        state = load_state(a.state)
        if code:
            raise TicketError("a stop executed: do NOT record or send the sell")
    if a.status in ("placed", "paper"):
        gone = {r.get("broker_order_id") for r in state.get("orders", [])
                if r.get("status") in ("stop_filled", "target_filled", "robo_filled") and rec_day(r) == today
                and r.get("symbol") == w["symbol"]}
        hit = [i for i in cancel_ids(w) if i in gone]
        if hit:
            raise TicketError("%s executed: the sell should not have been sent; check get_positions"
                              % ", ".join(hit))
    if a.status == "paper" and not paper:
        raise TicketError("--status paper is for paper mode only; the config says \"paper\": false")
    if a.status == "placed" and paper:
        raise TicketError("paper mode: nothing went to a broker, so record --status paper")
    placed = a.status in ("placed", "paper")
    filled_qty, filled_price = a.filled_qty, a.filled_price
    if a.status == "paper":
        filled_qty = filled_qty or w["qty"]
        filled_price = filled_price or ref_price(w) or fresh_price(a.prices, w["symbol"], now)
        if not filled_price:
            raise TicketError("no price to simulate the fill at: pass --filled-price P (the live price, e.g. "
                              "from trader-alerts: alerts.py quote %s --write), or refresh %s" % (w["symbol"], a.prices))
    # everything that can fail is worked out before anything is written
    pf = None
    if placed and filled_qty and filled_price:
        pf = compute_fill(a.portfolio, cfg, w["symbol"], w["side"], filled_qty, filled_price, today,
                          product=w["product"])
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": w["symbol"],
           "side": w["side"], "plain": rec["plain"], "status": "placed" if placed else a.status,
           "paper": paper, "broker": broker_of(cfg)}
    for k in ("qty", "kind", "max_loss", "stop", "product", "auto"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if filled_qty:
        new["filled_qty"] = _str_num(float(filled_qty))
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    adds = [new]
    if placed:
        still = {s.get("broker_order_id") for s in open_stops(state, w["symbol"], today)
                 + open_legs(state, w["symbol"], today, "target") + open_legs(state, w["symbol"], today, "robo")}
        for oid in [i for i in cancel_ids(w) if i in still]:
            k = cancel_kind(w, oid)
            adds.append({"at": ist_iso(now), "date": today, "index": a.index, "symbol": w["symbol"],
                         "status": k + "_cancelled", "broker_order_id": oid,
                         "plain": "%s %s cancelled for the flatten" % (LEG_WORDS[k], oid)})
        if pf is not None:
            adds.append(fill_record(now, today, w["symbol"], w["side"], filled_qty, filled_price, paper=paper))
        bracket = rec.get("kind") == "entry" and has_stop(w)
        mode = w.get("target_mode", "watched")
        if bracket and mode == "robo":
            if a.status == "placed" and not a.broker_id:
                raise TicketError("--status placed needs --broker-id for a Robo bracket (flatten cancels it by that id)")
            adds.append(leg_record(now, today, a.index, w, fp, filled_qty or w["qty"], "robo", "placed",
                                   broker_id=a.broker_id, paper=a.status == "paper"))
        elif a.status == "paper" and bracket:
            adds.append(stop_record(now, today, a.index, w, fp, filled_qty, "stop_placed", paper=True))
            if mode == "resting":
                adds.append(leg_record(now, today, a.index, w, fp, filled_qty, "target", "placed", paper=True))
    state.setdefault("orders", []).extend(adds)
    write_json(a.state, state)
    if pf is not None:
        write_json(a.portfolio, pf)
    print("Recorded order %d as %s." % (a.index, "placed (paper, simulated)" if a.status == "paper" else a.status))
    if not placed:
        return no_stop_warning(a, cfg, w, fp, now, today)
    if pf is not None:
        print("Added the fill (%s %s at %s) to %s." % (_str_num(float(filled_qty)), w["symbol"],
                                                       _p(filled_price), a.portfolio))
    bracket = rec.get("kind") == "entry" and has_stop(w)
    mode = w.get("target_mode", "watched")
    if a.status == "paper":
        if bracket:
            print("Simulated protective stop at %s%s. trader-alerts watches the stop, the target and flat_by."
                  % (_p(w["stop_loss_stop_price"]), "" if mode == "watched" else
                     " and target at %s" % _p(w["take_profit_limit_price"])))
        return 0
    if pf is None:
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (w["symbol"], w["side"]))
    if bracket and mode == "robo":
        print("The Robo bracket holds the stop and the target: no step 2 or 3. Check get_orders that both legs")
        print("are open. When trader-alerts rings, record the fill with ticket.py fill ... --stop-hit or --target-hit.")
        return 0
    if bracket:
        print("Next, right after the entry fills: the protective stop. ticket.py stop --index %d%s"
              % (a.index, "" if filled_qty else " --filled-qty Q"))
        print("Until the stop is placed the position has NO stop at the exchange.")
        if mode == "resting":
            print("Then step 3, the resting target: ticket.py target --index %d" % a.index)
    return 0


def cmd_stop(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    w = canonical(order, cfg)
    if not has_stop(w):
        raise TicketError("order %d has no stop-loss" % a.index)
    if w.get("target_mode") == "robo":
        raise TicketError("order %d is a Robo bracket: its stop is part of that one order; do not place another"
                          % a.index)
    fp = fingerprint(order, cfg)
    placed = latest(state, fp, today, ENTRY_STATUSES)
    if not placed or placed.get("status") != "placed":
        for r in state.get("orders", []):
            if r.get("status") == "placed" and r.get("index") == a.index and rec_day(r) == today \
                    and not config_matches(r, cfg):
                raise TicketError("config changed since your yes: re-check and re-approve")
        raise TicketError("order %d's entry is not recorded as placed today; the stop comes after the fill" % a.index)
    if not config_matches(placed, cfg):
        raise TicketError("config changed since your yes: re-check and re-approve")
    qty = a.filled_qty or placed.get("filled_qty")
    if not qty or not _f(qty) or _f(qty) <= 0 or _f(qty) != int(_f(qty)):
        raise TicketError("say how many shares filled: --filled-qty Q (from get_orders / the order book)")
    if _f(qty) > _f(w["qty"]) + 1e-9:
        raise TicketError("filled qty %s is more than the order's %s" % (_str_num(float(qty)), w["qty"]))
    paper = is_paper(cfg)
    broker = broker_of(cfg)
    already = mine(open_stops(state, w["symbol"], today), paper=paper)
    if paper:
        if already:
            print("Paper: the simulated stop for %s is already recorded." % w["symbol"])
            return 0
        state["orders"].append(stop_record(now, today, a.index, w, fp, qty, "stop_placed", paper=True))
        write_json(a.state, state)
        print("Paper: simulated protective stop recorded (no broker tool called).")
        return 0
    if already:
        print("A protective stop for %s is already open (broker id %s). Do NOT place another one:"
              % (w["symbol"], ", ".join(str(s.get("broker_order_id")) for s in already)))
        print("two stops would sell the shares twice. To change its quantity, modify it instead:")
        tool, args = modify_stop_call(w, broker, already[-1].get("broker_order_id"), qty)
        print("\n".join(call_lines([(tool, args)], indent="  ")))
        return 1
    if not a.status:
        tool, args = stop_call(w, broker, qty, fp)
        print("Protective stop for order %d: SL-M %s %s %s, trigger %s." % (
            a.index, opposite(w["side"]), _str_num(float(qty)), w["symbol"], _p(w["stop_loss_stop_price"])))
        print("Your yes for the bracket covered this stop. Call the %s tool:" % BROKERS[broker])
        print("\n".join(call_lines([(tool, args)], indent="  ")))
        print("Then: ticket.py stop --index %d --filled-qty %s --status placed --broker-id ID (or --status failed)"
              % (a.index, _str_num(float(qty))))
        return 0
    if a.status == "placed" and not a.broker_id:
        raise TicketError("--status placed needs --broker-id (flatten cancels the stop by that id)")
    state["orders"].append(stop_record(now, today, a.index, w, fp, qty,
                                       "stop_placed" if a.status == "placed" else "stop_failed",
                                       broker_id=a.broker_id, note=a.note))
    write_json(a.state, state)
    if a.status == "placed":
        print("Recorded the protective stop for order %d, broker id %s." % (a.index, a.broker_id))
        return 0
    print("WARNING: the stop was NOT placed. %s %s is open with no stop at the exchange." % (
        _str_num(float(qty)), w["symbol"]))
    print("Retry the stop now, or flatten: ticket.py flatten --symbol %s --qty %s" % (w["symbol"], _str_num(float(qty))))
    return 1


def cmd_target(a):
    """Step 3 in resting mode: the LIMIT target for the filled qty, after the stop is open."""
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    w = canonical(order, cfg)
    mode = w.get("target_mode", "watched")
    if mode != "resting":
        raise TicketError("order %d's target is %s, not a resting order%s" % (
            a.index, mode, ": Angel holds it in the Robo bracket" if mode == "robo" else
            " (set \"target_order\": \"resting\" to rest it)"))
    fp = fingerprint(order, cfg)
    placed = latest(state, fp, today, ENTRY_STATUSES)
    if not placed or placed.get("status") != "placed":
        raise TicketError("order %d's entry is not recorded as placed today; the target comes after the fill" % a.index)
    if not config_matches(placed, cfg):
        raise TicketError("config changed since your yes: re-check and re-approve")
    qty = a.filled_qty or placed.get("filled_qty")
    if not qty or not _f(qty) or _f(qty) <= 0 or _f(qty) != int(_f(qty)):
        raise TicketError("say how many shares filled: --filled-qty Q (from get_orders / the order book)")
    if _f(qty) > _f(w["qty"]) + 1e-9:
        raise TicketError("filled qty %s is more than the order's %s" % (_str_num(float(qty)), w["qty"]))
    broker = broker_of(cfg)
    paper = is_paper(cfg)
    done = [r for r in state.get("orders", []) if rec_day(r) == today and r.get("fingerprint") == fp
            and r.get("status") in ("target_placed", "target_filled", "target_cancelled")]
    if done or open_legs(state, w["symbol"], today, "target"):
        print("A target for %s was already placed today (%s). Do NOT place another one: two targets would sell"
              % (w["symbol"], ", ".join(sorted({str(r.get("status")) for r in done})) or "target_placed"))
        print("the shares twice. Only a target recorded as failed can be retried.")
        return 1
    if not mine(open_stops(state, w["symbol"], today), fp, paper):
        raise TicketError("place and record this order's protective stop first (ticket.py stop --index %d): the "
                          "target never goes in before its own stop" % a.index)
    if paper:
        state["orders"].append(leg_record(now, today, a.index, w, fp, qty, "target", "placed", paper=True))
        write_json(a.state, state)
        print("Paper: simulated resting target recorded (no broker tool called).")
        return 0
    if not a.status:
        tool, args = target_call(w, broker, qty, fp)
        print("Resting target for order %d: LIMIT %s %s %s at %s." % (
            a.index, opposite(w["side"]), _str_num(float(qty)), w["symbol"], _p(w["take_profit_limit_price"])))
        print("Your yes for the bracket covered this target. Call the %s tool:" % BROKERS[broker])
        print("\n".join(call_lines([(tool, args)], indent="  ")))
        print("Then: ticket.py target --index %d --filled-qty %s --status placed --broker-id ID (or --status failed)"
              % (a.index, _str_num(float(qty))))
        print(RESTING_WHY)
        return 0
    if a.status == "placed" and not a.broker_id:
        raise TicketError("--status placed needs --broker-id (the stop's fill cancels the target by that id)")
    state["orders"].append(leg_record(now, today, a.index, w, fp, qty, "target",
                                      "placed" if a.status == "placed" else "failed",
                                      broker_id=a.broker_id, note=a.note))
    write_json(a.state, state)
    if a.status == "placed":
        print("Recorded the resting target for order %d, broker id %s." % (a.index, a.broker_id))
        return 0
    print("The target was NOT placed. The stop still protects the position; trader-alerts watches the target.")
    return 1


def other_leg_lines(state, sym, today, filled_kind, broker, paper, now=None):
    """After a stop or target fills: the cancel calls for the leg that is still open, and the short check.
    A simulated leg is cancelled in the log here (appended to state; the caller writes it)."""
    other = open_legs(state, sym, today, "target") if filled_kind == "stop" else open_stops(state, sym, today)
    lines = []
    for r in other:
        kind = "target" if filled_kind == "stop" else "stop"
        oid = r.get("broker_order_id")
        if paper or r.get("paper"):
            state["orders"].append({"at": ist_iso(now) if now else r.get("at"), "date": today, "symbol": sym,
                                    "status": kind + "_cancelled", "broker_order_id": oid, "paper": True,
                                    "plain": "simulated %s %s cancelled with the other leg" % (LEG_WORDS[kind], oid)})
            lines.append("Paper: the simulated %s %s is cancelled with it." % (LEG_WORDS[kind], oid))
            continue
        lines.append("NOW cancel the %s %s, before it fills too (your yes for the bracket covered this cancel):"
                     % (LEG_WORDS[kind], oid))
        lines.extend(call_lines([cancel_call(broker, kind, oid)], indent="  "))
        lines.append("Then record it: ticket.py cancelled --symbol %s --broker-id %s (if it already executed: "
                     "--failed executed)" % (sym, oid))
    if other and not paper:
        lines.append("Then check get_positions: a NEGATIVE quantity for %s means both legs filled and you are "
                     "short. Buy that quantity back at once (a market BUY, %s), after the user's yes." % (sym, "MIS"))
    return lines


def cmd_cancelled(a):
    """Record that an open stop or target was cancelled, or that its cancel failed because it executed."""
    cfg = read_json(a.config, "config", required=False) or {}
    sym = norm_symbol(a.symbol)
    now, today = _clock(a)
    state = load_state(a.state)
    oid = str(a.broker_id)
    kind = None
    for k, recs in (("stop", open_stops(state, sym, today)), ("target", open_legs(state, sym, today, "target")),
                    ("robo", open_legs(state, sym, today, "robo"))):
        if any(str(r.get("broker_order_id")) == oid for r in recs):
            kind = k
    if kind is None:
        raise TicketError("no open stop, target or Robo bracket %s for %s today" % (oid, sym))
    status = kind + ("_filled" if a.failed == "executed" else "_cancelled")
    state["orders"].append({"at": ist_iso(now), "date": today, "symbol": sym, "status": status,
                            "broker_order_id": oid, "paper": is_paper(cfg),
                            "plain": "%s %s %s" % (LEG_WORDS[kind], oid,
                                                   "EXECUTED (cancel failed)" if a.failed == "executed" else
                                                   "cancelled")})
    write_json(a.state, state)
    if a.failed == "executed":
        print("Recorded: %s %s EXECUTED. Check get_positions now: a negative quantity means you are short; buy it "
              "back at once after the user's yes. Record that fill with ticket.py fill." % (LEG_WORDS[kind], oid))
        return 1
    print("Recorded: %s %s cancelled." % (LEG_WORDS[kind], oid))
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now = parse_now(a.now)
    date = a.date or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-11-02")
    sym = norm_symbol(a.symbol)
    paper = is_paper(cfg)
    pf = compute_fill(a.portfolio, cfg, sym, a.side, a.qty, a.price, date,
                      product=str(cfg.get("product") or "MIS").upper())
    state = load_state(a.state)
    state.setdefault("orders", []).append(fill_record(now, date, sym, a.side, a.qty, a.price, paper=paper))
    if a.stop_hit and a.target_hit:
        raise TicketError("one fill is either the stop or the target, not both")
    robo = open_legs(state, sym, date, "robo")
    if a.stop_hit or a.target_hit:
        legs = robo or (open_stops(state, sym, date) if a.stop_hit else open_legs(state, sym, date, "target"))
        if legs and _f(legs[-1].get("qty")) is not None and abs(_f(a.qty) - _f(legs[-1]["qty"])) > 1e-9:
            raise TicketError("a partial fill: %s of the %s's %s shares. The %s stays open for the rest; do not "
                              "cancel the other leg. Check get_orders, then record this sell without --%s-hit "
                              "and cut the other leg to the shares still held (modify_order)" % (
                                  a.qty, "Robo bracket" if robo else "stop" if a.stop_hit else "target",
                                  legs[-1]["qty"], "order", "stop" if a.stop_hit else "target"))
    if (a.stop_hit or a.target_hit) and robo:
        state["orders"].append(dict(robo[-1], at=ist_iso(now), status="robo_filled",
                                    note="its %s leg filled" % ("stop" if a.stop_hit else "target")))
    elif a.stop_hit:
        live = open_stops(state, sym, date)
        if live:
            state["orders"].append(dict(live[-1], at=ist_iso(now), status="stop_filled"))
    elif a.target_hit:
        live = open_legs(state, sym, date, "target")
        if live:
            state["orders"].append(dict(live[-1], at=ist_iso(now), status="target_filled"))
    lines = []
    if (a.stop_hit or a.target_hit) and robo:
        lines = ["Robo bracket: Angel cancels the other leg itself." +
                 ("" if paper else " Confirm in get_orders that nothing is left open.")]
    elif a.stop_hit or a.target_hit:
        lines = other_leg_lines(state, sym, date, "stop" if a.stop_hit else "target", broker_of(cfg), paper, now)
    write_json(a.state, state)
    write_json(a.portfolio, pf)
    print("Recorded %s %s %s at %s on %s in %s. Cash now %s." % (a.side, a.qty, sym, inr(a.price), date,
                                                                a.portfolio, inr(pf["cash"])))
    if lines:
        print("\n".join(lines))
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
    product = position_product(a.portfolio, sym) or str(cfg.get("product") or "MIS").upper()
    order = {"symbol": sym, "exchange": "NSE", "product": product, "side": "sell", "qty": _str_num(qty),
             "type": "market", "time_in_force": "day",
             "why": "flat by %s IST: closes the %s day trade before the broker's auto square-off (qty from %s)"
             % (flat_by, sym, source)}
    state = load_state(a.state)
    targets, robos = open_legs(state, sym, today, "target"), open_legs(state, sym, today, "robo")
    live = open_stops(state, sym, today) + targets + robos
    ids = [str(s.get("broker_order_id")) for s in live if s.get("broker_order_id")]
    if ids:
        order["cancel_stop"] = {"broker_order_id": ",".join(ids), "broker_order_ids": ids,
                                "paper": all(bool(s.get("paper")) for s in live)}
        tids = [str(s.get("broker_order_id")) for s in targets if s.get("broker_order_id")]
        rids = [str(s.get("broker_order_id")) for s in robos if s.get("broker_order_id")]
        if tids:
            order["cancel_stop"]["target_ids"] = tids
        if rids:
            order["cancel_stop"]["robo_ids"] = rids
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": is_paper(cfg),
           "note": "Flatten ticket. Nothing is placed until trader-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    w = canonical(order, cfg)
    print("Flatten %s: %s (flat by %s IST)." % (sym, plain(w), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against get_positions before you say yes." % source)
    print("Next:")
    print("  1. Check get_positions first: if the stop already filled, there is nothing to sell.")
    print("  2. trader-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  3. ticket.py show, then the user's yes for this one ticket.")
    if ids and not order["cancel_stop"]["paper"]:
        print("  4. The ticket cancels every open stop and target first (broker id %s), then sells. Without the"
              % ", ".join(ids))
        print("     cancels they could sell the same shares again and leave you short.")
        if robos:
            print("     After cancelling the Robo bracket, check get_positions: sell only what is still held.")
        print("     " + CANCEL_FAILS)
    elif ids:
        print("  4. Paper: the simulated stop is cancelled with the simulated sell.")
    else:
        print("  4. No open stop is recorded for %s. If one is open at the broker (get_orders: an open SL-M" % sym)
        print("     SELL), cancel it first with cancel_order, or the stop could sell the shares a second time.")
        print("     " + CANCEL_FAILS)
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
        auto = "AUTO-PAPER " if r.get("auto") else ""
        print("%s  %s %-14s %s%s%s%s%s%s" % (r.get("at"), idx, r.get("status"), auto, r.get("plain"), paper, reply,
                                            loss, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    autos = sum(1 for r in rows if r.get("status") == "placed" and r.get("auto"))
    print("%d placed%s%s." % (placed, "" if a.all else " today",
                              (", %d of them AUTO-PAPER (no human yes)" % autos) if autos else ""))
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
    p.add_argument("--fingerprint", required=True, help="the fingerprint printed on the ticket the user saw")
    p.add_argument("--reply", help="the user's reply, word for word")
    p.add_argument("--auto-paper", action="store_true",
                   help="paper only: a delegated approval (needs \"autopilot\": {\"auto_paper\": true})")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened after the Allow once / Deny card")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", choices=["placed", "denied", "failed", "paper"])
    p.add_argument("--cancelled", action="append", help="a stop id whose cancel_order succeeded")
    p.add_argument("--cancel-failed", action="append", help="a stop id whose cancel_order failed")
    p.add_argument("--reason", choices=["executed", "already-cancelled"], help="why the cancel failed")
    p.add_argument("--restop-id", help="the broker id of a stop re-placed after a failed flatten")
    p.add_argument("--prices", default="trader/prices.json", help="paper fills fall back to a fresh price here")
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
    p = sub.add_parser("target", help="step 3 (resting mode): the LIMIT target after the stop is placed")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--filled-qty", help="shares that filled (default: the qty recorded with the result)")
    p.add_argument("--status", choices=["placed", "failed"], help="record what happened to the target call")
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p = sub.add_parser("cancelled", help="record that an open stop or target was cancelled")
    p.add_argument("--symbol", required=True)
    p.add_argument("--broker-id", required=True)
    p.add_argument("--failed", choices=["executed"], help="the cancel failed because the leg executed")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--stop-hit", action="store_true", help="this sell was the protective stop filling")
    p.add_argument("--target-hit", action="store_true", help="this sell was the resting target (or Robo target) filling")
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
                "stop": cmd_stop, "target": cmd_target, "cancelled": cmd_cancelled, "fill": cmd_fill,
                "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
