import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
# trader-budget's order for RELIANCE, Thu 2026-10-29 (tests/test_budget.py writes the same one)
REL_PATH = os.path.join(FIX, "trader-risk", "orders-reliance.json")
# trader-budget's weights-mode split: whole-share CNC buys, no stops
SPLIT_PATH = os.path.join(FIX, "trader-risk", "orders-weights.json")
NOW = "2026-10-29 09:40"
TODAY = "2026-10-29"
PRICES = {"RELIANCE": 1307.90, "HDFCBANK": 980.00, "INFY": 1500.00, "ITC": 410.00, "TCS": 3100.00}
QUOTE_TIME = datetime.datetime(2026, 10, 29, 4, 22, 13)   # a RELIANCE quote of 1176.475 at 09:52:13 IST
# the split's rules with every day-trade check switched off: the account checks still hold
SPLIT = {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_inr": 40000,
         "min_order_inr": 500, "max_orders_per_day": 10, "require_stop": False, "no_entries_before": None,
         "no_entries_after": None, "max_trades_per_day": 0, "max_daily_loss_inr": 0,
         "max_loss_per_trade_inr": 0, "min_reward_risk": 0, "allow_delivery": True, "product": "CNC"}
BIG = {"budget": 200000, "max_position_pct": 100, "max_order_inr": 200000}


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def split(**over):
    c = dict(SPLIT)
    c.update(over)
    return c


def rel(**over):
    with open(REL_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def bracket(sym, qty, entry, stop, target):
    return {"symbol": sym, "exchange": "NSE", "side": "buy", "qty": str(qty), "type": "market",
            "time_in_force": "day", "product": "MIS", "order_class": "bracket",
            "take_profit_limit_price": "%.2f" % target, "stop_loss_stop_price": "%.2f" % stop,
            "plan": {"entry": entry, "stop": stop, "target": target}}


def split_orders():
    with open(SPLIT_PATH) as fh:
        return json.load(fh)["orders"]


def buy(sym, qty, product="CNC"):
    return {"symbol": sym, "exchange": "NSE", "side": "buy", "qty": str(qty), "type": "market",
            "time_in_force": "day", "product": product}


def placed(sym, side="buy", day=TODAY, max_loss=759.77, kind=None, status="placed"):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "at": day + "T09:45:00+05:30"}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if kind:
        r["kind"] = kind
    return r


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


class TheWalkthrough(unittest.TestCase):
    def test_the_reliance_bracket_passes_every_check(self):
        res = check.check([rel()], cfg(), now=at(NOW))
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual((r["kind"], r["product"], r["entry_ref"], r["stop"], r["target"]),
                         ("entry", "MIS", 1174.30, 1163.40, 1185.20))
        self.assertEqual((r["risk_per_share"], r["loss_at_stop"], r["charges"], r["max_loss"], r["reward_risk"]),
                         (10.90, 686.70, 73.07, 759.77, 1.0))
        self.assertEqual(r["value_inr"], 73980.90)
        self.assertEqual(res["cash_after"], 26019.10)
        self.assertEqual(res["date"], TODAY)
        self.assertIs(res["paper"], True)
        self.assertEqual(res["warnings"], [])

    def test_85_shares_is_blocked_by_the_max_loss_only(self):
        res = check.check([rel(qty="85")], cfg(**BIG), now=at(NOW))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 85 shares x 10.90 risk a share = ₹926.50 at the stop + ₹82.11 estimated "
                          "charges = ₹1,008.61, over your ₹1,000 max loss per trade"])
        res = check.check([rel(qty="85")], cfg(include_charges=False, **BIG), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["results"][0]["max_loss"], 926.50)

    def test_fingerprint_is_shared_with_the_broker(self):
        o = rel()
        self.assertEqual(check.fingerprint(o), ticket.fingerprint(o))
        self.assertEqual(check.fingerprint(dict(o, why="anything", plan={})), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, qty="64")), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, stop_loss_stop_price="1163.00")), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, product="CNC")), check.fingerprint(o))
        self.assertEqual(check.FINGERPRINT_FIELDS, ticket.FINGERPRINT_FIELDS)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", REL_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", none, "--state", none, "--now", NOW, "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("PASS  #1 buy RELIANCE 63 shares (MIS), entry", text)
            self.assertIn("entry 1174.30 (the plan's entry), stop 1163.40, target 1185.20: risk 10.90 a share", text)
            self.assertIn("max loss ₹759.77 (₹686.70 at the stop + ₹73.07 estimated charges) "
                          "(limit ₹1,000), reward:risk 1.00 (min 1)", text)
            self.assertIn("1 passed, 0 blocked at 2026-10-29 09:40 IST", text)
            self.assertIn("₹26,019.10", text)
            self.assertIn("not financial advice", text)
            with open(out, encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["now_ist"], "2026-10-29 09:40")
            self.assertEqual(rec["results"][0]["kind"], "entry")
            self.assertEqual(rec["results"][0]["fingerprint"], check.fingerprint(rel()))


class NseRules(unittest.TestCase):
    def one(self, order, now=NOW, **over):
        return check.check([order], cfg(**over), now=at(now))

    def test_prices_must_be_on_the_tick(self):
        res = self.one(rel(stop_loss_stop_price="1163.35"))
        self.assertIn("format: stop_loss_stop_price 1163.35 is off NSE's 0.10 tick for this price: "
                      "1163.30 or 1163.40", reasons(res))
        res = self.one(rel(take_profit_limit_price="1185.25"))
        self.assertIn("take_profit_limit_price 1185.25 is off", reasons(res))
        self.assertIn("limit_price 1174.33 is off", reasons(self.one(rel(type="limit", limit_price="1174.33"))))
        cheap = bracket("IDEA", 100, 7.50, 7.13, 8.27)     # under Rs 250 the tick is 0.01
        self.assertEqual(verdicts(self.one(cheap, max_position_pct=100)), ["PASS"])

    def test_delivery_is_blocked_unless_allowed(self):
        res = self.one(rel(product="CNC"))
        self.assertIn("product: CNC is delivery", reasons(res))
        self.assertEqual(verdicts(self.one(rel(product="CNC"), allow_delivery=True)), ["PASS"])
        self.assertIn("product must be MIS (intraday) or CNC", reasons(self.one(rel(product="NRML"))))

    def test_no_product_or_exchange_falls_back_with_a_warning(self):
        o = rel()
        o.pop("product")
        o.pop("exchange")
        res = self.one(o)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("taken as MIS", warnings(res))
        self.assertIn("taken as NSE", warnings(res))
        self.assertIn("exchange must be NSE", reasons(self.one(rel(exchange="BSE"))))

    def test_nse_symbols_only(self):
        self.assertIn("BSE symbol", reasons(self.one(rel(symbol="RELIANCE.BO"))))
        self.assertIn("use the plain NSE symbol RELIANCE", reasons(self.one(rel(symbol="RELIANCE.NS"))))
        self.assertIn("exchange must be NSE", reasons(self.one(rel(symbol="NIFTY26NOV24000CE", exchange="NFO"))))
        self.assertIn("does not look like an NSE symbol", reasons(self.one(rel(symbol="BTC/INR"))))
        for sym in ("M&M", "BAJAJ-AUTO", "360ONE"):
            r = self.one(bracket(sym, 10, 500.0, 490.0, 510.0))["results"][0]
            self.assertNotIn("asset", " ".join(r["reasons"]), sym)

    def test_whole_shares_only(self):
        self.assertIn("whole number of shares", reasons(self.one(rel(qty="63.5"))))
        self.assertIn("give qty, not a rupee amount", reasons(self.one(dict(rel(qty=None), notional="70000"))))
        self.assertIn("no qty", reasons(self.one(rel(qty=None))))

    def test_order_types_and_validity(self):
        self.assertIn("time_in_force must be day", reasons(self.one(rel(time_in_force="gtc"))))
        self.assertIn("order type must be market or limit", reasons(self.one(rel(type="stop"))))
        self.assertIn("a limit order needs a limit_price", reasons(self.one(rel(type="limit"))))
        o = rel()
        o.pop("take_profit_limit_price")
        self.assertIn("needs both take_profit_limit_price and stop_loss_stop_price", reasons(self.one(o)))

    def test_dollar_keys_are_refused(self):
        with self.assertRaises(check.RiskError) as e:
            check.check([rel()], dict(cfg(), max_loss_per_trade_usd=50), now=at(NOW))
        self.assertIn("this NSE pack counts rupees", str(e.exception))

    def test_flat_by_after_the_auto_square_off_warns(self):
        self.assertEqual(self.one(rel())["warnings"], [])
        res = self.one(rel(), flat_by="15:20")
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("not before Zerodha's MIS auto square-off at 15:20", " ".join(res["warnings"]))
        res = self.one(rel(), flat_by="15:15", broker="angelone")
        self.assertIn("Angel One's MIS auto square-off at 15:15", " ".join(res["warnings"]))
        self.assertEqual(self.one(rel(), flat_by="15:14", broker="angelone")["warnings"], [])
        self.assertIn("not zerodha or angelone", " ".join(self.one(rel(), broker="alpaca")["warnings"]))

    def test_margin(self):
        pf = {"cash": 20000, "positions": []}
        res = check.check([rel()], cfg(max_position_pct=400), portfolio=pf, now=at(NOW))
        self.assertIn('that would be margin (borrowed money, "allow_margin": false)', reasons(res))
        res = check.check([rel()], cfg(max_position_pct=400, allow_margin=True), portfolio=pf, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("MIS margin: ₹14,796.18 of cash blocked for ₹73,980.90 of stock (20%)",
                      " ".join(res["results"][0]["info"]))
        self.assertEqual(res["cash_after"], 5203.82)
        res = check.check([rel(qty="90")], cfg(max_position_pct=600, allow_margin=True, max_order_inr=10 ** 6),
                          portfolio=pf, now=at(NOW))
        self.assertIn("cash: needs ₹21,137.40 (20% MIS margin of ₹1,05,687) but only ₹20,000 cash",
                      reasons(res))


class DayTradeChecks(unittest.TestCase):
    def one(self, order, now=NOW, **over):
        return check.check([order], cfg(**over), now=at(now))

    def test_a_stop_is_required(self):
        o = rel()
        for k in ("order_class", "take_profit_limit_price", "stop_loss_stop_price", "plan"):
            o.pop(k)
        o["plan"] = {"entry": 1174.30}
        res = self.one(o)
        self.assertIn("stop: no stop", reasons(res))
        res = self.one(o, require_stop=False, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop", warnings(res))

    def test_a_plan_stop_counts_but_warns(self):
        o = rel()
        for k in ("order_class", "take_profit_limit_price", "stop_loss_stop_price"):
            o.pop(k)
        res = self.one(o)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("in the plan only", warnings(res))
        self.assertEqual(res["results"][0]["max_loss"], 759.77)

    def test_stop_and_target_on_the_right_side(self):
        self.assertIn("must be below the entry 1174.30", reasons(self.one(rel(stop_loss_stop_price="1175.00"))))
        res = self.one(rel(take_profit_limit_price="1174.00"))
        self.assertIn("target: the target 1174.00 must be above the entry 1174.30", reasons(res))

    def test_reward_risk(self):
        res = self.one(rel(take_profit_limit_price="1181.40"))
        self.assertIn("reward:risk: 0.65", reasons(res))
        self.assertEqual(verdicts(self.one(rel(take_profit_limit_price="1181.40"), min_reward_risk=0.5)), ["PASS"])

    def test_a_fresh_higher_price_is_the_entry(self):
        res = check.check([rel()], cfg(), prices={"RELIANCE": 1176.475}, now=at("2026-10-29 10:00"),
                          price_times={"RELIANCE": QUOTE_TIME})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1176.48, "price", 13.08))
        self.assertEqual(r["max_loss"], 897.12)
        self.assertIn("reward:risk: 0.67 (target 1185.20, entry 1176.48, stop 1163.40)", reasons(res))

    def test_a_stale_or_future_price_is_not_used(self):
        for now in ("2026-10-29 10:30", "2026-10-29 09:40"):
            res = check.check([rel()], cfg(), prices={"RELIANCE": 1176.475}, now=at(now),
                              price_times={"RELIANCE": QUOTE_TIME})
            self.assertEqual(verdicts(res), ["PASS"], now)
            self.assertEqual(res["results"][0]["entry_ref"], 1174.30)
            self.assertIn("older than 15 minutes", warnings(res))

    def test_a_lower_price_does_not_shrink_the_risk(self):
        res = check.check([rel()], cfg(), prices={"RELIANCE": 1170.00}, now=at(NOW))
        self.assertEqual(res["results"][0]["entry_ref"], 1174.30)

    def test_a_limit_order_uses_its_limit_price(self):
        o = rel(type="limit", limit_price="1174.00")
        res = check.check([o], cfg(), prices={"RELIANCE": 1176.475}, now=at(NOW))
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["max_loss"], r["verdict"]), (1174.0, 740.87, "PASS"))

    def test_daily_loss_and_trades_per_day(self):
        two = {"orders": [placed("TCS"), placed("INFY")]}
        res = check.check([rel()], cfg(), state=two, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 2 of 3 trades, ₹1,519.54 of ₹3,000 daily loss",
                      res["results"][0]["info"][-1])
        three = {"orders": [placed("TCS"), placed("INFY"), placed("ITC")]}
        res = check.check([rel()], cfg(), state=three, now=at(NOW))
        why = reasons(res)
        self.assertIn("trades: 3 entries placed today", why)
        self.assertIn("daily loss: ₹2,279.31 already at risk today plus ₹759.77 for this one = "
                      "₹3,039.08, over your ₹3,000", why)
        yesterday = {"orders": [placed(s, day="2026-10-28") for s in ("TCS", "INFY", "ITC")]}
        self.assertEqual(verdicts(check.check([rel()], cfg(), state=yesterday, now=at(NOW))), ["PASS"])

    def test_exits_and_declines_do_not_count_as_trades(self):
        st = {"orders": [placed("TCS"), placed("TCS", side="sell", kind="exit", max_loss=None),
                         placed("INFY", status="declined"), placed("ITC", status="failed")]}
        res = check.check([rel()], cfg(), state=st, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["entries_placed"], 1)

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [bracket(s, 1, 1000.0, 100.0, 1900.0) for s in ("AAA", "BBB", "CCC", "DDD")]
        res = check.check(orders, cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        why = reasons(res, 3)
        self.assertIn("trades: 0 entries placed today plus 3 passed earlier", why)
        self.assertIn("daily loss: ₹2,701.47 already at risk today plus ₹900.49 for this one", why)

    def test_entry_window_ist(self):
        cases = [("2026-10-29 09:29", "no new entries before 09:30 IST (it is 09:29)"),
                 ("2026-10-29 14:46", "no new entries after 14:45 IST (it is 14:46)"),
                 ("2026-10-29 09:10", "the market is closed (Thu 2026-10-29 09:10"),
                 ("2026-10-29 15:30", "the market is closed"),
                 ("2026-10-31 10:15", "the market is closed today (weekend"),
                 ("2026-11-10 10:15", "the market is closed today (NSE holiday: Diwali Balipratipada"),
                 ("2026-10-20 10:15", "the market is closed today (NSE holiday: Dussehra"),
                 ("2026-10-29T03:55:00Z", "no new entries before 09:30 IST (it is 09:25)")]
        for now, want in cases:
            res = self.one(rel(), now=now)
            self.assertIn("window: " + want, reasons(res), now)
        for now in ("2026-10-29 09:30", "2026-10-29 14:45", "2026-10-29T04:10:00Z", "2026-10-29T09:15:00Z"):
            self.assertEqual(verdicts(self.one(rel(), now=now)), ["PASS"], now)
        res = self.one(rel(), now="2026-10-31 10:15", no_entries_before=None, no_entries_after=None)
        self.assertEqual(verdicts(res), ["PASS"])
        with self.assertRaises(check.RiskError):
            self.one(rel(), no_entries_before="9.30am")

    def test_next_years_dates_warn(self):
        res = self.one(rel(), now="2027-01-05 10:00")
        self.assertIn("the NSE holiday list covers 2026 only", " ".join(res["warnings"]))

    def test_the_window_uses_ist_not_the_machine(self):
        self.assertEqual(check.to_ist(at("2026-10-29 09:40")), datetime.datetime(2026, 10, 29, 9, 40))
        self.assertEqual(at("2026-10-29 09:40"), datetime.datetime(2026, 10, 29, 4, 10))
        self.assertEqual(at("2026-03-10 09:40"), datetime.datetime(2026, 3, 10, 4, 10))   # no DST in India

    def test_a_short_entry_when_allowed(self):
        short = {"symbol": "RELIANCE", "exchange": "NSE", "side": "sell", "qty": "63", "type": "market",
                 "time_in_force": "day", "product": "MIS", "order_class": "bracket",
                 "take_profit_limit_price": "1163.40", "stop_loss_stop_price": "1185.20",
                 "plan": {"entry": 1174.30}}
        res = self.one(short)
        self.assertIn("short: selling more RELIANCE than you hold", reasons(res))
        res = self.one(short, allow_short=True)
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"], r["max_loss"], r["reward_risk"]), ("PASS", "entry", 760.02, 1.0))
        res = self.one(dict(short, stop_loss_stop_price="1170.00"), allow_short=True)
        self.assertIn("must be above the entry 1174.30 for a sell", reasons(res))


class Exits(unittest.TestCase):
    PF = {"cash": 26019.10, "positions": [{"ticker": "RELIANCE", "shares": 63,
                                            "lots": [{"date": TODAY, "shares": 63, "cost": 73980.90}]}]}
    SELL = {"symbol": "RELIANCE", "exchange": "NSE", "side": "sell", "qty": "63", "type": "market",
            "time_in_force": "day", "product": "MIS"}

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        full = {"orders": [placed("RELIANCE"), placed("TCS"), placed("INFY")] +
                          [placed("T%d" % i, max_loss=1) for i in range(6)] +
                          [placed("RELIANCE", side="sell", kind="exit", max_loss=None)]}
        c = cfg(blocked=["RELIANCE"], max_order_inr=100, max_daily_loss_inr=50)
        res = check.check([self.SELL], c, portfolio=self.PF, state=full, now=at("2026-10-29 15:12"))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        why = warnings(res)
        for word in ("blocked:", "duplicate:", "daily limit:"):
            self.assertIn("exit, not blocked: " + word, why)
        self.assertNotIn("price", why)
        res = check.check([self.SELL], cfg(), portfolio=self.PF, now=at("2026-10-31 12:00"))
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_partial_exit_and_the_rest(self):
        half = dict(self.SELL, qty="30")
        res = check.check([half, half], cfg(), portfolio=self.PF, prices={"RELIANCE": 1180.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual([r["kind"] for r in res["results"]], ["exit", "exit"])
        self.assertIn("exit, not blocked: duplicate", warnings(res, 1))
        res = check.check([half, half, half], cfg(), portfolio=self.PF, prices={"RELIANCE": 1180.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("short:", reasons(res, 2))

    def test_format_asset_and_short_still_apply_to_a_sell(self):
        res = check.check([dict(self.SELL, type="limit")], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("needs a limit_price", reasons(res))
        res = check.check([dict(self.SELL, type="limit", limit_price="1180.03")], cfg(), portfolio=self.PF,
                          now=at(NOW))
        self.assertIn("off NSE's 0.10 tick", reasons(res))
        res = check.check([dict(self.SELL, qty="64")], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("short: selling more RELIANCE than you hold (63 shares)", reasons(res))
        res = check.check([self.SELL], cfg(), now=at(NOW))
        self.assertIn("record the fill first", reasons(res))

    def test_buy_then_sell_in_one_batch(self):
        res = check.check([rel(), self.SELL], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")


class SplitChecks(unittest.TestCase):
    """trader-budget's weights mode: CNC buys with the day-trade checks off; the account checks still hold."""

    def one(self, order, **over):
        return check.check([order], split(**over), prices=PRICES, today=TODAY)["results"][0]

    def test_the_five_buys_pass_on_a_fresh_paper_account(self):
        res = check.check(split_orders(), split(), prices=PRICES, today=TODAY)
        self.assertEqual(verdicts(res), ["PASS"] * 5)
        self.assertEqual(res["cash_after"], 10389.90)
        self.assertEqual(res["results"][0]["value_inr"], 24850.10)
        self.assertEqual(res["results"][0]["amount"], "19 shares")

    def test_a_sixth_buy_is_blocked_three_ways(self):
        res = check.check(split_orders() + [buy("RELIANCE", 5)], split(), prices=PRICES, today=TODAY)
        self.assertEqual(verdicts(res), ["PASS"] * 5 + ["BLOCK"])
        why = reasons(res, 5)
        self.assertIn("cash:", why)
        self.assertIn("position:", why)
        self.assertIn("duplicate:", why)

    def test_the_day_trading_config_blocks_them(self):
        res = check.check(split_orders(), cfg(), prices=PRICES, now=at(NOW))
        self.assertEqual(verdicts(res)[0], "BLOCK")
        self.assertIn("stop: no stop", reasons(res))
        self.assertIn("product: CNC is delivery", reasons(res))

    def test_max_and_min_order(self):
        self.assertIn("over your ₹40,000 max order",
                      " ".join(self.one(buy("ITC", 100), max_position_pct=100)["reasons"]))
        self.assertIn("under your ₹500 minimum", " ".join(self.one(buy("ITC", 1))["reasons"]))

    def test_blocked_list(self):
        r = self.one(buy("ITC", 10), blocked=["itc"])
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("blocked list", r["reasons"][0])

    def test_share_orders_need_a_price(self):
        q = buy("ZZZZ", 2)
        self.assertIn("no price for ZZZZ", " ".join(self.one(q)["reasons"]))
        r = self.one(buy("ITC", 2))
        self.assertEqual(r["verdict"], "PASS")
        self.assertAlmostEqual(r["value_inr"], 820.0)

    def test_format_rules(self):
        lim = {"symbol": "ITC", "exchange": "NSE", "side": "buy", "qty": "1", "type": "limit",
               "time_in_force": "day", "product": "CNC"}
        self.assertIn("needs a limit_price", " ".join(self.one(lim)["reasons"]))
        bad = {"symbol": "ITC", "side": "hold", "qty": "1"}
        self.assertIn("side must be buy or sell", " ".join(self.one(bad)["reasons"]))

    PF = {"cash": 500.0, "positions": [{"ticker": "ITC", "shares": 34,
                                         "lots": [{"date": "2026-10-28", "shares": 34, "cost": 13940.0}]}]}

    def test_short_sales_are_blocked(self):
        sell = {"symbol": "ITC", "exchange": "NSE", "side": "sell", "qty": "40", "type": "market",
                "time_in_force": "day", "product": "CNC"}
        r = check.check([sell], split(), portfolio=self.PF, prices=PRICES, today=TODAY)["results"][0]
        self.assertIn("short:", " ".join(r["reasons"]))
        sell["qty"] = "10"
        r = check.check([sell], split(), portfolio=self.PF, prices=PRICES, today=TODAY)["results"][0]
        self.assertEqual(r["verdict"], "PASS")

    def test_a_sell_frees_cash_for_a_later_buy(self):
        orders = [{"symbol": "ITC", "exchange": "NSE", "side": "sell", "qty": "20", "type": "market",
                   "time_in_force": "day", "product": "CNC"}, buy("HDFCBANK", 7)]
        res = check.check(orders, split(max_position_pct=100), portfolio=self.PF, prices=PRICES, today=TODAY)
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        res = check.check([buy("HDFCBANK", 7)], split(max_position_pct=100), portfolio=self.PF, prices=PRICES,
                          today=TODAY)
        self.assertEqual(verdicts(res), ["BLOCK"])

    def test_daily_limit_and_already_placed_today(self):
        st = {"orders": [{"symbol": "T%d" % i, "side": "buy", "status": "placed", "at": TODAY + "T10:00:00"}
                         for i in range(10)]}
        res = check.check([buy("ITC", 10)], split(), state=st, prices=PRICES, today=TODAY)
        self.assertIn("daily limit", reasons(res))
        res = check.check([buy("ITC", 10)], split(), state=st, prices=PRICES, today="2026-10-30")
        self.assertEqual(verdicts(res), ["PASS"])
        itc = {"orders": [{"symbol": "ITC", "side": "buy", "status": "placed", "at": TODAY + "T10:00:00"}]}
        res = check.check([buy("ITC", 10)], split(), state=itc, prices=PRICES, today=TODAY)
        self.assertIn("already placed today", reasons(res))
        declined = {"orders": [{"symbol": "ITC", "side": "buy", "status": "declined", "at": TODAY + "T10:00:00"}]}
        self.assertEqual(verdicts(check.check([buy("ITC", 10)], split(), state=declined, prices=PRICES,
                                              today=TODAY)), ["PASS"])

    def test_live_account_warns(self):
        res = check.check([buy("ITC", 10)], split(paper=False), prices=PRICES, today=TODAY)
        self.assertIn("LIVE", res["warnings"][0])


class Cli(unittest.TestCase):
    def test_prices_file_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-10-29T04:25:00Z", "prices": {"RELIANCE": 1176.475},
                           "detail": {"RELIANCE": {"time": "2026-10-29T04:22:13Z",
                                                   "fetched": "2026-10-29T04:25:00Z"}}}, fh)
            none = os.path.join(d, "none")
            base = ["--orders", REL_PATH, "--config", CFG_PATH, "--portfolio", none, "--prices", pp,
                    "--state", none, "--no-write"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(base + ["--now", "2026-10-29 10:00"]), 1)
            self.assertIn("reward:risk: 0.67", buf.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(base + ["--now", "2026-10-29 10:30"]), 0)

    def test_split_orders_with_a_split_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(split(), fh)
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"prices": PRICES}, fh)
            none = os.path.join(d, "none")
            out = os.path.join(d, "state", "risk.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", SPLIT_PATH, "--config", c, "--portfolio", none, "--prices", pp,
                                   "--state", none, "--today", TODAY, "--out", out])
            self.assertEqual(code, 0)
            self.assertIn("5 passed, 0 blocked", buf.getvalue())
            with open(out, encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertTrue(all(len(r["fingerprint"]) == 16 for r in rec["results"]))
            bad = os.path.join(d, "orders.json")
            with open(bad, "w") as fh:
                json.dump({"orders": split_orders() + [buy("RELIANCE", 5)]}, fh)
            with redirect_stdout(io.StringIO()):
                code = check.main(["--orders", bad, "--config", c, "--portfolio", none, "--prices", pp,
                                   "--state", none, "--today", TODAY, "--no-write"])
            self.assertEqual(code, 1)

    def test_errors_are_one_line(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", "/nonexistent.json", "--no-write"]), 1)
        self.assertIn("no orders file", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", REL_PATH, "--now", "noon", "--no-write"]), 1)
        self.assertIn("--now wants", err.getvalue())
        self.assertIn("IST", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", REL_PATH, "--today", "monday", "--no-write"]), 1)
        self.assertIn("--today wants", err.getvalue())
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump({"budget": 5000, "max_order_usd": 4000}, fh)
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(check.main(["--orders", REL_PATH, "--config", c, "--no-write"]), 1)
            self.assertIn("Rename max_order_usd to max_order_inr", err.getvalue())


if __name__ == "__main__":
    unittest.main()
