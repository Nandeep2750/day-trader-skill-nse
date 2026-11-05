"""trader-exit tests. Bars for the ATR stop reuse tests/fixtures/trader-entry/
(RELIANCE's real NSE five-minute bars for 2026-10-29, see test_entry.py)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

exitp = load("trader-exit", "exit")
entry = load("trader-entry", "entry")

REL = os.path.join(FIX, "trader-entry", "RELIANCE-2026-10-29-5m.json")

# trader/plan.json for the walkthrough, exactly as the contract writes it
WALK_PLAN = {"created_by": "trader-exit", "symbol": "RELIANCE", "exchange": "NSE", "side": "buy",
             "date": "2026-10-29", "setup": "orb", "entry": 1174.3, "entry_time": "9:40 AM", "entry_bar": 4,
             "interval": "5m", "opening_range": {"high": 1170.5, "low": 1156.2, "minutes": 15},
             "stop": 1163.4, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
             "target": 1185.2, "target_r": 1.0, "target_label": "1R (one times the risk)",
             "risk_per_share": 10.9, "reward_risk": 1.0, "flat_by": "15:00"}
WALK_ENTRY = {"created_by": "trader-entry", "symbol": "RELIANCE", "setup": "orb", "side": "buy",
              "date": "2026-10-29", "status": "triggered", "time": "9:40 AM", "bar_start": "09:35", "bar_index": 4,
              "price": 1174.3, "level": 1170.5, "interval": "5m",
              "opening_range": {"high": 1170.5, "low": 1156.2, "minutes": 15},
              "source": "yahoo", "checked_at": "2026-10-29T04:10:00Z"}
R = "₹"


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=1170.5, ol=1156.2, atr=None, n=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n)


class Ticks(unittest.TestCase):
    def test_nse_price_bands(self):
        for price, tick in ((12.34, "0.01"), (249.99, "0.01"), (250, "0.05"), (999.95, "0.05"), (1000, "0.10"),
                            (4999.9, "0.10"), (5000, "0.50"), (9999.5, "0.50"), (10000, "1.00"),
                            (19999, "1.00"), (20000, "5.00"), (145000, "5.00")):
            self.assertEqual(exitp.band_tick(price), Decimal(tick), price)

    def test_the_coarser_tick_near_a_band_edge(self):
        # NSE fixes a stock's tick from last month's close: within 10% of an edge either band is possible
        for price, tick in ((224.99, "0.01"), (225, "0.05"), (249.99, "0.05"), (275, "0.05"), (275.01, "0.05"),
                            (899.95, "0.05"), (900, "0.10"), (999.95, "0.10"), (1100, "0.10"), (1174.3, "0.10"),
                            (4500, "0.50"), (5500, "0.50"), (4499.9, "0.10"), (9000, "1.00"), (11000, "1.00"),
                            (18000, "5.00"), (22000, "5.00"), (23456, "5.00")):
            self.assertEqual(exitp.tick_of(price), Decimal(tick), price)
        self.assertEqual(exitp.tick_up("241.03"), Decimal("241.05"))
        self.assertEqual(exitp.tick_down("991.27"), Decimal("991.20"))

    def test_rounding_never_grows_the_risk(self):
        self.assertEqual(exitp.tick_up("1163.35"), Decimal("1163.40"))
        self.assertEqual(exitp.tick_up("1163.40"), Decimal("1163.40"))
        self.assertEqual(exitp.tick_down("1185.29"), Decimal("1185.20"))
        self.assertEqual(exitp.tick_up("99.013"), Decimal("99.02"))
        self.assertEqual(exitp.tick_up("512.31"), Decimal("512.35"))
        self.assertEqual(exitp.tick_down("512.34"), Decimal("512.30"))
        self.assertEqual(exitp.tick_up("7123.01"), Decimal("7123.50"))
        self.assertEqual(exitp.tick_down("23456"), Decimal("23455.00"))

    def test_rupees_with_indian_grouping(self):
        self.assertEqual(exitp.money(999), R + "999.00")
        self.assertEqual(exitp.money(1174.3), R + "1,174.30")
        self.assertEqual(exitp.money(125000), R + "1,25,000.00")
        self.assertEqual(exitp.money(1234567.5), R + "12,34,567.50")
        self.assertEqual(exitp.money(-10.9), "-" + R + "10.90")


class Math(unittest.TestCase):
    def test_the_walkthrough_or_mid_1r(self):
        p = c(1174.3, "or-mid", "1R")
        self.assertEqual(p["stop"], Decimal("1163.40"))           # 1163.35 up to the 0.10 tick
        self.assertEqual(p["risk"], Decimal("10.90"))
        self.assertEqual(p["target"], Decimal("1185.20"))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertIn("rounded up to the " + R + "0.10 tick", p["stop_detail"])

    def test_the_default_2r(self):
        p = c(1174.3, "or-mid")
        self.assertEqual(p["target"], Decimal("1196.10"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")

    def test_raw_yahoo_floats_give_the_same_prices(self):
        p = c(1174.300048828125, "or-mid", "1R", 1170.5, 1156.199951171875)
        self.assertEqual((p["entry"], p["stop"], p["target"]),
                         (Decimal("1174.30"), Decimal("1163.40"), Decimal("1185.20")))

    def test_other_stops(self):
        p = c(1174.3, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1156.20"), Decimal("18.10"), Decimal("1210.50")))
        p = c(1174.3, "price:1163.33")
        self.assertEqual(p["stop"], Decimal("1163.40"))             # up to the tick, toward the entry
        p = c(1174.3, "pct:1")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1162.60"), Decimal("11.70"), Decimal("1197.70")))
        p = c(1174.3, "atr:1.5", atr=5.8400146484375, n=5)
        self.assertEqual(p["stop"], Decimal("1165.60"))             # 1174.30 - 8.76 = 1165.54, up to 1165.60
        self.assertEqual(p["stop_label"], "1.5 x ATR(5) under the entry")

    def test_cheap_stocks_use_the_paisa_tick(self):
        p = c(100, "price:99.013", "1.5R", None, None)
        self.assertEqual((p["stop"], p["risk"]), (Decimal("99.02"), Decimal("0.98")))
        self.assertEqual(p["target"], Decimal("101.47"))            # 100 + 1.47
        self.assertEqual(p["target_label"], "1.5R (1.5 times the risk)")

    def test_price_targets_round_down_to_the_tick(self):
        p = c(1174.3, "or-mid", "price:1190.07")
        self.assertEqual(p["target"], Decimal("1190.00"))
        self.assertEqual(p["reward_risk"], Decimal("1.44"))
        self.assertEqual(p["target_label"], "your price (1.44R)")
        self.assertEqual(p["target_r"], Decimal("1.44"))

    def test_refusals_say_why(self):
        cases = [("price:1174.30", "2R", "at or above the entry"),
                 ("price:1175", "2R", "at or above the entry"),
                 ("price:1174.25", "2R", "at or above the entry"),            # rounds up to 1174.30
                 ("or-mid", "price:1174", "at or below the entry"),
                 ("price:1174.00", "2R", "too tight to survive the spread"),
                 ("pct:0.04", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1174.3, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # 0.05% of 1174.30 is 0.587: a 0.60 risk is allowed, 0.50 is not
        self.assertEqual(c(1174.3, "price:1173.70")["risk"], Decimal("0.60"))
        with self.assertRaises(exitp.PlanError):
            c(1174.3, "price:1173.80")
        with self.assertRaises(exitp.PlanError):
            c(1160.00, "or-mid")                                     # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1174.3, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1174.3, "or-mid", oh=1156.2, ol=1170.5)

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("RELIANCE", "5m", None, REL)
        day = exitp.datetime.date(2026, 10, 29)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=4)
        self.assertEqual(len(bars), 5)                              # 9:15 ... the 9:35 entry bar
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 1162.2 - 1156.2, places=3)  # the first bar without the overnight gap
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 5)
        self.assertAlmostEqual(atr, sum(trs) / 5, places=9)
        self.assertAlmostEqual(atr, 5.84, places=3)
        # later in the day it uses the last 14 bars; the 15:20 null bar never counts
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(all_bars), 74)
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)

    def test_nse_symbols(self):
        self.assertEqual(exitp.nse_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(exitp.nse_symbol("m&m"), "M&M")
        with self.assertRaises(exitp.PlanError):
            exitp.nse_symbol("RELIANCE.BO")
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.load_chart("INFY", "5m", None, REL)
        self.assertIn("holds RELIANCE, not INFY", str(cm.exception))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-10-29 09:41"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json")) as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, _ = run(entry, ["check", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:40", "--write"])
        self.assertEqual(code, 0)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in WALK_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertIn("Exit plan: RELIANCE (NSE) long, Thu 2026-10-29", text)
        self.assertIn("Stop          " + R + "1,163.40  opening-range midpoint", text)
        self.assertIn("Target        " + R + "1,185.20  1R (one times the risk)", text)
        self.assertIn("Risk a share  " + R + "10.90 (0.93% of the entry)", text)
        self.assertIn("Reward:risk   1.0", text)
        self.assertIn("Flat by       15:00 IST", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/trader-timemachine", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual(self.read_plan()["target"], 1196.1)
        self.assertEqual(self.read_plan()["target_r"], 2.0)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-29 09:45"])
        self.assertEqual(code, 0)
        self.assertIn(R + "1,196.10", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-30 09:45"])
        self.assertIn("not today", text)

    def test_manual_flags_and_json(self):
        code, text, _ = self.plan("--symbol", "reliance.ns", "--entry", "1174.30", "--or-high", "1170.50",
                                  "--or-low", "1156.20", "--time", "9:40", "--entry-bar", "4", "--date", "2026-10-29",
                                  "--target", "1R", "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in WALK_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])
        code, _, err = self.plan("--symbol", "RELIANCE.BO", "--entry", "1174.30", "--stop", "pct:1")
        self.assertEqual(code, 1)
        self.assertIn("NSE only", err)

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("trader", "entry.json"), WALK_ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", REL, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1165.6, 8.7, 1191.7))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 5))
        self.assertAlmostEqual(p["atr"], 5.84, places=3)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", REL, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(WALK_ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("RELIANCE is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "1170.55", "--json")    # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (1170.55, 1163.4, None))

        self.write(os.path.join("trader", "entry.json"), dict(WALK_ENTRY, setup="level", level=1170.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("v2 plans longs only", err)
        self.write(os.path.join("trader", "entry.json"), dict(WALK_ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        code, _, err = self.plan("--stop", "price:1174.10")
        self.assertIn("longs only", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--entry", "1174.30", "--or-high", "1170.50")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_config_square_off_and_late_entries(self):
        self.write(os.path.join("trader", "entry.json"), WALK_ENTRY)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:00", "min_reward_risk": 1.5})
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "15:00")
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])

        # a flat-by at or after the broker's MIS auto square-off is warned about, not refused
        for broker, flat_by, warned, name in (("zerodha", "15:25", True, "Zerodha's MIS auto square-off (15:25)"),
                                              ("zerodha", "15:20", False, ""),
                                              ("angelone", "15:10", True, "Angel One's MIS auto square-off (15:10)"),
                                              ("angelone", "15:05", False, ""),
                                              (None, "15:25", False, "")):
            self.write(os.path.join("trader", "config.json"), {"flat_by": flat_by, "broker": broker})
            code, text, err = self.plan("--json")
            self.assertEqual(code, 0, err)
            p = json.loads(text)
            self.assertEqual(p["flat_by"], flat_by)
            self.assertEqual(any("square-off" in w for w in p["warnings"]), warned, (broker, flat_by))
            if warned:
                self.assertIn(name, " ".join(p["warnings"]))
        self.write(os.path.join("trader", "config.json"), {"broker": "zerodha"})
        code, text, _ = self.plan()
        self.assertIn("Flat by       15:00 IST", text)               # the default is before every broker
        self.assertNotIn("square-off", text)
        os.remove(os.path.join("trader", "config.json"))

        code, _, err = self.plan("--flat-by", "15:30")
        self.assertEqual(code, 1)
        self.assertIn("after the NSE's 3:30 PM close", err)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "16:00"})
        code, _, err = self.plan()
        self.assertIn("flat_by in the config 16:00", err)
        os.remove(os.path.join("trader", "config.json"))
        self.write(os.path.join("trader", "entry.json"), dict(WALK_ENTRY, time="3:12 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money",
                     "/trader-timemachine", "/trader-budget", "longs only", R + "1,163.40", R + "1,185.20",
                     R + "10.90", "1196.10", "too tight to survive the spread", "tick", "15:10", "square-off",
                     "zerodha", "angel one", "ist"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "new york", "nyse", " et "):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
