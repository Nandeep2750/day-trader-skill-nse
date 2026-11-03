import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("trader-budget", "size")

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
# RELIANCE, Thu 2026-10-29, real 5-minute bars (Yahoo RELIANCE.NS): opening range 1156.20 to 1170.50,
# the 9:35 bar closed at 1174.30. The plan's stop is the raw midpoint 1163.35, off NSE's 0.10 tick,
# so these tests also see trader-budget put it on the tick (up, to 1163.40).
PLAN = os.path.join(FIX, "trader-budget", "plan-reliance.json")
TARGETS = os.path.join(ROOT, "trader-budget", "targets.example.json")
NOW = "2026-10-29 09:40"
TODAY = "2026-10-29"
# illustrative prices for the weights-mode tests, not quotes
PRICES = {"RELIANCE": 1307.90, "HDFCBANK": 980.00, "INFY": 1500.00, "ITC": 410.00, "TCS": 3100.00}
EXAMPLE_CFG = {
    "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 80000, "min_order_inr": 500,
    "max_loss_per_trade_inr": 1000, "max_daily_loss_inr": 3000, "max_trades_per_day": 3, "max_orders_per_day": 10,
    "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10",
    "broker": "zerodha", "product": "MIS", "paper": True, "blocked": [],
    "allow_options": False, "allow_margin": False, "mis_margin_pct": 20, "allow_short": False,
    "include_charges": True,
}
# weights mode: Rs 1,00,000, 10% cash, 25% cap
SPLIT = {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_inr": 40000,
         "min_order_inr": 500, "paper": True, "blocked": []}
# room for the max loss to bind: the position cap and cash are out of the way
BIG = {"budget": 200000, "max_position_pct": 100, "max_order_inr": 200000}


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def split(**over):
    c = dict(SPLIT)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def rows(path):
    with open(path) as fh:
        return size.parse_weights(json.load(fh))


def shares(result):
    return {p["ticker"]: p["shares"] for p in result["positions"]}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def prices_doc(price, quote_time="2026-10-29T04:22:13Z"):
    return {"as_of": quote_time, "source": "yahoo", "prices": {"RELIANCE": price},
            "detail": {"RELIANCE": {"prev_close": 1158.0, "time": quote_time, "source": "yahoo"}}}


class TheWalkthrough(unittest.TestCase):
    """Rs 1,00,000, 10% buffer, Rs 1,000 max loss, RELIANCE risk 10.90 -> 63 shares (position cap)."""

    def test_config_example_is_the_contract(self):
        with open(CFG) as fh:
            self.assertEqual(json.load(fh), EXAMPLE_CFG)

    def test_reliance_is_63_shares_bound_by_the_position_cap(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual((s["entry"], s["stop"], s["target"]), (1174.30, 1163.40, 1185.20))
        self.assertEqual(s["rounding"], ["stop 1163.35 -> 1163.40 (up to the tick)"])
        self.assertEqual(s["qty"], 63)
        self.assertEqual(s["bound_by"], "position cap")
        self.assertEqual(s["risk_per_share"], 10.90)
        self.assertEqual(s["cost"], 73980.90)
        self.assertEqual((s["loss_at_stop"], s["charges"], s["max_loss"]), (686.70, 73.07, 759.77))
        self.assertEqual(s["reward_risk"], 1.0)
        self.assertEqual(s["cash_buffer"], 10000)
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 84), ("position cap", 63), ("max order", 68), ("cash", 76)])
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["position cap"])
        self.assertIsNone(s["refusal"])

    def test_the_bracket_order(self):
        o = size.bracket_order(size.size_trade(plan(), cfg(), today=TODAY))
        why = o.pop("why")
        self.assertEqual(o, {
            "symbol": "RELIANCE", "exchange": "NSE", "side": "buy", "qty": "63", "type": "market",
            "time_in_force": "day", "product": "MIS", "order_class": "bracket",
            "take_profit_limit_price": "1185.20", "stop_loss_stop_price": "1163.40",
            "plan": {"entry": 1174.30, "stop": 1163.40, "target": 1185.20, "risk_per_share": 10.90,
                     "loss_at_stop": 686.70, "charges": 73.07, "max_loss": 759.77, "broker": "zerodha",
                     "setup": "orb", "entry_time": "9:40 AM", "flat_by": "15:10"}})
        self.assertIn("bound by your position cap", why)
        self.assertIn("₹686.70 at the stop + ₹73.07 estimated charges = ₹759.77", why)

    def test_the_fixture_orders_file_is_what_it_writes(self):
        with open(os.path.join(FIX, "trader-risk", "orders-reliance.json")) as fh:
            fixture = json.load(fh)["orders"][0]
        self.assertEqual(size.bracket_order(size.size_trade(plan(), cfg(), today=TODAY)), fixture)

    def test_a_limit_order_goes_down_to_the_tick(self):
        s = size.size_trade(plan(), cfg(), limit=1174.33, today=TODAY)
        self.assertEqual((s["entry"], s["qty"]), (1174.30, 63))
        self.assertIn("limit 1174.33 -> 1174.30 (down to the tick)", s["rounding"])
        o = size.bracket_order(s)
        self.assertEqual((o["type"], o["limit_price"]), ("limit", "1174.30"))

    def test_cli_writes_one_bracket_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("Size: 63 shares of RELIANCE = ₹73,980.90 at 1174.30 (MIS, intraday)", text)
            self.assertIn("63 x 10.90 = ₹686.70", text)
            self.assertIn("Estimated charges (Zerodha, round trip): ₹73.07. All in: ₹759.77.", text)
            self.assertIn("On NSE's tick: stop 1163.35 -> 1163.40", text)
            self.assertIn("Flat by 15:10 IST", text)
            self.assertIn("<- binds", text)
            self.assertIn("not financial advice", text)
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["status"], "proposed")
            self.assertIs(doc["paper"], True)
            self.assertEqual(doc["broker"], "zerodha")
            self.assertEqual(doc["created_at"], "2026-10-29T04:10:00Z")
            self.assertEqual(len(doc["orders"]), 1)
            self.assertEqual(doc["orders"][0]["plan"]["max_loss"], 759.77)

    def test_cli_json_and_limit(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--limit", "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["sizing"]["qty"], 63)
        self.assertEqual(out["orders"][0]["limit_price"], "1174.30")


class Ticks(unittest.TestCase):
    def test_tick_bands(self):
        for price, tick in ((99.99, 1), (249.99, 1), (250.00, 5), (999.95, 5), (1000.00, 10), (4999.90, 10),
                            (5000.00, 50), (10000.00, 100), (20000.00, 500), (31000.00, 500)):
            self.assertEqual(size.tick_paise(price), tick, price)

    def test_rounding_never_grows_the_risk(self):
        self.assertEqual(size.to_tick(1163.35, "up"), 1163.40)
        self.assertEqual(size.to_tick(1163.35, "down"), 1163.30)
        self.assertEqual(size.to_tick(1185.27, "down"), 1185.20)
        self.assertEqual(size.to_tick(212.34, "down"), 212.34)
        self.assertEqual(size.to_tick(999.97, "up"), 1000.00)
        self.assertEqual(size.to_tick(5012.30, "up"), 5012.50)
        self.assertEqual(size.to_tick(25003, "down"), 25000.0)
        self.assertTrue(size.on_tick(1174.30))
        self.assertFalse(size.on_tick(1174.35))
        self.assertTrue(size.on_tick(212.37))

    def test_the_plan_target_goes_down_to_the_tick(self):
        s = size.size_trade(plan(target=1185.27), cfg(), today=TODAY)
        self.assertEqual(s["target"], 1185.20)
        self.assertIn("target 1185.27 -> 1185.20 (down to the tick)", s["rounding"])


class Charges(unittest.TestCase):
    def test_zerodha_round_trip_on_a_lakh(self):
        self.assertEqual(size.round_trip_charges({"broker": "zerodha"}, 100000, 100000),
                         {"brokerage": 40.0, "stt": 25.0, "exchange": 5.94, "sebi": 0.2, "stamp": 3.0,
                          "gst": 8.31, "total": 82.45})

    def test_angel_one_minimum_brokerage(self):
        c = size.round_trip_charges({"broker": "angelone"}, 3000, 3000)
        self.assertEqual((c["brokerage"], c["gst"], c["total"]), (10.0, 1.83, 12.86))
        z = size.round_trip_charges({"broker": "zerodha"}, 3000, 3000)
        self.assertEqual(z["brokerage"], 1.8)

    def test_config_overrides_a_rate(self):
        c = size.round_trip_charges({"broker": "zerodha", "charges": {"brokerage_cap": 0}}, 100000, 100000)
        self.assertEqual(c["brokerage"], 0.0)

    def test_charges_shrink_the_max_loss_size(self):
        s = size.size_trade(plan(), cfg(**BIG), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (84, "max loss"))
        self.assertEqual((s["loss_at_stop"], s["charges"], s["max_loss"]), (915.60, 81.71, 997.31))
        s = size.size_trade(plan(), cfg(include_charges=False, **BIG), today=TODAY)
        self.assertEqual((s["qty"], s["charges"], s["max_loss"]), (91, 0.0, 991.90))


class PlanRules(unittest.TestCase):
    def test_zero_shares_is_a_plain_refusal(self):
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=10), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("more than your ₹10 max loss per trade", s["refusal"])
        self.assertIsNone(size.bracket_order(s))
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(cfg(max_loss_per_trade_inr=10), fh)
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", c, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order:", text)
            self.assertFalse(os.path.exists(out))

    def test_each_limit_can_bind(self):
        self.assertEqual(size.size_trade(plan(), cfg(max_order_inr=50000), today=TODAY)["bound_by"], "max order")
        s = size.size_trade(plan(), cfg(), cash=20000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"]), (8, "cash", 10000))
        self.assertEqual(size.size_trade(plan(), cfg(**BIG), today=TODAY)["bound_by"], "max loss")

    def test_margin_lets_mis_use_a_fifth_of_the_value(self):
        c = cfg(allow_margin=True, budget=100000, max_position_pct=500, max_order_inr=10 ** 7,
                max_loss_per_trade_inr=5000)
        s = size.size_trade(plan(), c, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["cash_needed"]), (383, "cash", 89951.38))
        self.assertTrue(s["margin"])
        self.assertIn("MIS margin", size.plan_report(s, size.bracket_order(s)))
        off = size.size_trade(plan(), dict(c, allow_margin=False), today=TODAY)
        self.assertEqual(off["qty"], 76)

    def test_a_fresh_higher_price_means_a_bigger_risk(self):
        s = size.size_trade(plan(), cfg(min_reward_risk=0), price=1176.475, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"]), (1176.48, "price", 13.08))
        self.assertEqual((s["qty"], s["reward_risk"]), (63, 0.67))
        self.assertIsNone(s["refusal"])
        s = size.size_trade(plan(), cfg(), price=1176.475, today=TODAY)
        self.assertIn("reward:risk 0.67 (target 1185.20, entry 1176.48, stop 1163.40) is under your minimum 1",
                      s["refusal"])
        self.assertIsNone(size.bracket_order(s))
        lower = size.size_trade(plan(), cfg(), price=1170.00, today=TODAY)
        self.assertEqual((lower["entry"], lower["qty"]), (1174.30, 63))

    def test_prices_file_is_used_only_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump(prices_doc(1176.475), fh)
            base = ["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"), "--prices", pp, "--json"]
            code, text, _ = run(base + ["--now", "2026-10-29 10:00"])
            out = json.loads(text)
            self.assertEqual((code, out["orders"]), (1, []))
            self.assertIn("above the plan's entry", out["notes"][0])
            self.assertIn("IST", out["notes"][0])
            out = json.loads(run(base + ["--now", "2026-10-29 10:30"])[1])
            self.assertEqual(out["sizing"]["qty"], 63)
            self.assertIn("not used", out["notes"][0])

    def test_the_portfolio_cash_caps_the_size(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "portfolio.json")
            with open(pf, "w") as fh:
                json.dump({"cash": 20000, "positions": []}, fh)
            out = json.loads(run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--now", NOW, "--json"])[1])
            self.assertEqual((out["sizing"]["qty"], out["sizing"]["bound_by"]), (8, "cash"))

    def test_bad_plans(self):
        for p, word in ((plan(stop=1175.0), "not below the entry"),
                        (plan(target=1170.0), "already happened"),
                        (plan(target=None), "positive target"),
                        (plan(side="sell"), "long (buy) day trades only"),
                        (plan(symbol=""), "no symbol"),
                        (plan(symbol="RELIANCE.BO"), "BSE symbol"),
                        (plan(symbol="BTC/INR"), "does not look like an NSE symbol")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(p, cfg(), today=TODAY)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), price=1186.00, today=TODAY)
        self.assertIn("already happened", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(blocked=["reliance"]), today=TODAY)
        self.assertIn("blocked list", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), today="2026-10-30")
        self.assertIn("make a new plan", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(product="NRML"), today=TODAY)
        self.assertIn("MIS (intraday) or CNC", str(e.exception))

    def test_a_yahoo_symbol_is_accepted(self):
        s = size.size_trade(plan(symbol="reliance.ns"), cfg(), today=TODAY)
        self.assertEqual(s["symbol"], "RELIANCE")
        s = size.size_trade(plan(symbol="M&M"), cfg(), today=TODAY)
        self.assertEqual(size.bracket_order(s)["symbol"], "M&M")

    def test_dollar_keys_are_refused(self):
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), dict(cfg(), max_loss_per_trade_usd=50), today=TODAY)
        self.assertIn("rename max_loss_per_trade_usd to max_loss_per_trade_inr", str(e.exception).lower())
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump({"budget": 5000, "max_order_usd": 4000}, fh)
            code, _, err = run(["--plan", PLAN, "--config", c, "--now", NOW])
            self.assertEqual(code, 1)
            self.assertIn("this NSE pack counts rupees", err)

    def test_under_the_minimum_order(self):
        cheap = plan(symbol="IDEA", entry=7.50, stop=7.00, target=8.00)
        s = size.size_trade(cheap, cfg(max_loss_per_trade_inr=30, include_charges=False), today=TODAY)
        self.assertEqual(s["qty"], 60)
        self.assertIn("under your ₹500 minimum order", s["refusal"])
        self.assertIsNone(size.bracket_order(s))

    def test_missing_plan_and_weights_only_flags(self):
        code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--top", "2"])
        self.assertEqual(code, 1)
        self.assertIn("--weights", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "tomorrow"])
        self.assertEqual(code, 1)
        self.assertIn("IST", err)
        self.assertNotIn("Traceback", err)

    def test_indian_digit_grouping(self):
        self.assertEqual(size.inr(100000), "₹1,00,000")
        self.assertEqual(size.inr(73980.9), "₹73,980.90")
        self.assertEqual(size.inr(12345678.5), "₹1,23,45,678.50")
        self.assertEqual(size.inr(999), "₹999")
        self.assertEqual(size.inr(-686.7), "-₹686.70")


class WeightsMode(unittest.TestCase):
    """--weights / --targets: Rs 1,00,000, 10% cash, 25% cap, whole shares at illustrative prices."""

    def test_the_example_split(self):
        r = size.size(rows(TARGETS), split(), prices=PRICES)
        self.assertEqual(shares(r), {"RELIANCE": 19, "HDFCBANK": 24, "INFY": 12, "ITC": 34, "TCS": 3})
        self.assertEqual(r["cash_buffer"], 10000)
        self.assertEqual(r["cap"], 25000)
        self.assertEqual(r["invested"], 89610.10)
        self.assertEqual(r["idle"], 389.90)
        self.assertEqual([p["ticker"] for p in r["positions"] if p["capped"]], ["RELIANCE"])
        for p in r["positions"]:
            self.assertAlmostEqual(p["rupees"], p["shares"] * PRICES[p["ticker"]], places=2)
            self.assertLessEqual(p["rupees"], 25000)

    def test_from_inline_weights(self):
        w = size.parse_weights(size.weights_from_arg("RELIANCE=30,HDFCBANK=25,INFY=20,ITC=15,TCS=10"))
        self.assertEqual(shares(size.size(w, split(), prices=PRICES)),
                         shares(size.size(rows(TARGETS), split(), prices=PRICES)))

    def test_orders_are_whole_share_cnc_buys(self):
        r = size.size(rows(TARGETS), split(), prices=PRICES)
        orders = size.orders_from(r, "an example split")
        self.assertEqual(orders[0], {"symbol": "RELIANCE", "exchange": "NSE", "side": "buy", "qty": "19",
                                     "type": "market", "time_in_force": "day", "product": "CNC",
                                     "why": orders[0]["why"]})
        self.assertIn("capped at 25%", orders[0]["why"])
        self.assertTrue(all("notional" not in o for o in orders))
        with open(os.path.join(FIX, "trader-risk", "orders-weights.json")) as fh:
            fixture = json.load(fh)["orders"]
        self.assertEqual([{k: v for k, v in o.items() if k != "why"} for o in orders], fixture)

    def test_prices_are_required(self):
        with self.assertRaises(size.SizeError) as e:
            size.size(rows(TARGETS), split())
        self.assertIn("need a price", str(e.exception))

    def test_cap_redistributes_until_nobody_is_over(self):
        w = size.parse_weights({"A": 60, "B": 30, "C": 10})
        r = size.size(w, {"budget": 1000, "cash_buffer_pct": 0, "max_position_pct": 40, "min_order_inr": 1},
                      prices={"A": 1, "B": 1, "C": 1})
        self.assertEqual(shares(r), {"A": 400, "B": 400, "C": 200})
        self.assertEqual(sorted(p["ticker"] for p in r["positions"] if p["capped"]), ["A", "B"])

    def test_everything_capped_leaves_money_idle(self):
        w = size.parse_weights({"RELIANCE": 50, "ITC": 50})
        r = size.size(w, split(), prices=PRICES)
        self.assertEqual(shares(r), {"RELIANCE": 19, "ITC": 60})
        self.assertGreater(r["idle"], 40000)
        self.assertIn("every stock is at its cap", size.report(r, size.orders_from(r)))

    def test_slices_under_the_minimum_are_dropped_and_shared_out(self):
        w = size.parse_weights({"A": 90, "B": 7, "C": 3})
        r = size.size(w, {"budget": 10000, "cash_buffer_pct": 10, "max_position_pct": 100, "min_order_inr": 500},
                      prices={"A": 10, "B": 10, "C": 10})
        self.assertNotIn("C", shares(r))
        self.assertTrue(all(p["rupees"] >= 500 for p in r["positions"]))
        self.assertEqual(r["dropped"][0]["ticker"], "C")
        self.assertIn("minimum order", r["dropped"][0]["reason"])

    def test_a_share_too_dear_for_its_slice(self):
        w = size.parse_weights({"ITC": 90, "MRF": 10})
        r = size.size(w, split(max_position_pct=100), prices={"ITC": 410.0, "MRF": 140000.0})
        self.assertNotIn("MRF", shares(r))
        self.assertIn("one share costs ₹1,40,000", r["dropped"][0]["reason"])

    def test_blocked_symbols_are_dropped_first(self):
        r = size.size(rows(TARGETS), split(blocked=["itc"]), prices=PRICES)
        self.assertNotIn("ITC", shares(r))
        self.assertIn("blocked", r["dropped"][0]["reason"])

    def test_top_n(self):
        r = size.size(rows(TARGETS), split(max_position_pct=100), prices=PRICES, top=2)
        self.assertEqual(sorted(shares(r)), ["HDFCBANK", "RELIANCE"])

    def test_no_budget_and_duplicates_are_errors(self):
        with self.assertRaises(size.SizeError):
            size.size(rows(TARGETS), {"cash_buffer_pct": 10}, prices=PRICES)
        with self.assertRaises(size.SizeError):
            size.size([{"ticker": "ITC", "name": "", "weight": 1}, {"ticker": "ITC", "name": "", "weight": 2}],
                      split(), prices=PRICES)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(split(), fh)
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"prices": PRICES}, fh)
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--targets", TARGETS, "--config", c, "--prices", pp, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("left idle", text)
            self.assertIn("allow_delivery", text)
            self.assertIn("₹1,00,000", text)
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual([o["symbol"] for o in doc["orders"]], ["RELIANCE", "HDFCBANK", "INFY", "ITC", "TCS"])
            code, text, _ = run(["--targets", TARGETS, "--config", c, "--prices", pp, "--json"])
            self.assertEqual(json.loads(text)["result"]["idle"], 389.90)
            code, _, err = run(["--targets", "/nonexistent/targets.json", "--config", c])
            self.assertEqual(code, 1)
            self.assertIn("no weights file", err)


if __name__ == "__main__":
    unittest.main()
