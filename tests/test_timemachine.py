import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")

DIR = os.path.join(FIX, "trader-timemachine")
# real data: Yahoo v8 chart for RELIANCE.NS, range=60d interval=5m, fetched 2026-11-03 16:44 IST
# (2026-08-07 to 2026-11-03; like every NSE day on Yahoo, the 15:20 bar is empty, often 15:25 too)
RELIANCE = os.path.join(DIR, "RELIANCE-60d5m.json.gz")
with gzip.open(RELIANCE, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = "2026-10-29 09:45"
NOW_UTC = tm.parse_now(NOW)

# the README walkthrough's plan, as trader-exit writes it: RELIANCE, Thursday 2026-10-29, 15-minute
# opening range 1156.20 to 1170.50, the 9:35 bar closed above it at 1174.30, stop at the range's
# midpoint 1163.35 rounded up to the 0.10 tick, 1R target 1185.20, flat by 15:10
PLAN = {"created_by": "trader-exit", "symbol": "RELIANCE", "exchange": "NSE", "side": "buy",
        "date": "2026-10-29", "setup": "orb", "entry": 1174.30, "entry_time": "9:40 AM", "entry_bar": 4,
        "interval": "5m", "opening_range": {"high": 1170.50, "low": 1156.20, "minutes": 15},
        "stop": 1163.40, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 1185.20, "target_r": 1.0, "target_label": "1R (one times the risk)",
        "risk_per_share": 10.90, "reward_risk": 1.0, "flat_by": "15:10"}
SEED = 29

# fingerprints of the walkthrough run (seed 29): any change to the draws or the data shows up here
SIM_BANDS = "814f0b76da9ecdb5"
SIM_PATHS = "b200b394639700da"
SIM_TRADES = "3db3973b56c66fd2"
SIM_HIST = {"lo": 1149.12, "hi": 1197.04,
            "bins": [101, 116, 140, 178, 328, 385, 432, 565, 638, 703, 744, 761, 815, 733, 702, 560, 493, 431,
                     325, 223, 194, 118, 103, 51]}


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    kw.setdefault("seed", SEED)
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def ist(ts):
    return tm.to_ist(tm.utc_of(ts))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class Walkthrough(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 29, 2, "15:10"))
        self.assertEqual(m["pool_sessions"], 55)
        self.assertEqual(m["bars_left"], 66)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-07", "2026-10-28"))
        self.assertEqual(m["p_target"], 0.2419)
        self.assertEqual(m["p_stop"], 0.3173)
        self.assertEqual(m["p_neither"], 0.4408)
        self.assertEqual(m["p_green"], 0.4401)
        self.assertEqual(m["median_close"], 1172.72)
        self.assertEqual(m["p5_close"], 1156.23)
        self.assertEqual(m["p95_close"], 1189.76)
        self.assertEqual(m["avg_R"], -0.101)
        self.assertEqual(m["r_at_target"], 1.0)

    def test_bit_for_bit(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), SIM_BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), SIM_PATHS)
        self.assertEqual(m["hist"], SIM_HIST)
        self.assertEqual(m["bands"][-1], [1156.34, 1165.4, 1172.47, 1179.56, 1189.65])

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (55, 26))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (12, 11, 3, 13))
        self.assertEqual(h["avg_R"], -0.02)
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0})
        slim = [{"date": t["date"], "entry": t["entry"], "outcome": t["outcome"], "R": t["R"]} for t in h["trades"]]
        self.assertEqual(sha(slim), SIM_TRADES)
        first = h["trades"][0]
        self.assertEqual((first["date"], first["time"], first["entry"], first["stop"], first["target"],
                          first["outcome"]), ("2026-08-07", "11:30 AM", 1331.8, 1325.9, 1337.7, "stop"))

    def test_history_prices_sit_on_the_nse_tick(self):
        for t in self.doc["history"]["trades"]:
            for k in ("stop", "target"):
                paise = round(t[k] * 100)
                self.assertEqual(paise % tm.tick_paise(t[k]), 0, t)

    def test_the_verdict(self):
        self.assertEqual(self.doc["verdict"], "Leans toward the stop: target first 24.2 % vs stop first 31.7 %.")
        # 1R breaks even at 50 % of the replays that hit one side; this one got 43.3 %
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), even), (0.433, 0.5))
        self.assertEqual(self.doc["verdict_notes"],
                         ["44.1 % of the replays hit neither by 3:10 PM; the price at 3:10 PM decides those."])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1174.3] * 5)
        self.assertEqual(len(m["bands"]), 67)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("09:40", "15:10", 67))
        self.assertTrue(all(p[0] == 1174.3 and len(p) == 67 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["exchange"], p["entry"], p["stop"], p["target"], p["entry_bar"],
                          p["entry_time"]), ("RELIANCE", "NSE", 1174.3, 1163.4, 1185.2, 4, "9:40 AM"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"]), (10.9, 1.0, "15:10"))
        self.assertEqual(self.doc["warnings"], [])
        self.assertEqual(self.doc["data"]["symbol"], "RELIANCE.NS")
        self.assertEqual(self.doc["data"]["last_bar"], "2026-10-29 09:40 IST")

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("RELIANCE buy, Thursday 2026-10-29: entry 1174.30 at 9:40 AM (bar 4), stop 1163.40, "
                     "target 1185.20 (1R, risk 10.90 a share)",
                     "10,000 replays", "66 five-minute bars", "seed 29", "55 complete sessions",
                     "target first   24.2 %", "stop first     31.7 %", "neither        44.1 %   still open at 3:10 PM",
                     "green          44.0 %", "median 1172.72, 90 % of replays between 1156.23 and 1189.76",
                     "Average R: -0.101", "fired 26 times: 12 target, 11 stop, 3 neither. Average R -0.020",
                     "Leans toward the stop: target first 24.2 % vs stop first 31.7 %.\n44.1 % of the replays "
                     "hit neither by 3:10 PM; the price at 3:10 PM decides those.",
                     "Left out 1 session(s) with a gap before 3:10 PM: 2026-10-15.",
                     "not a prediction", "--seed 29"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "New York", " ET"):
            self.assertNotIn(word, text)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.2349, 0.3159, 1172.47))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), SIM_BANDS)

    def test_the_draws_are_stable_across_pythons(self):
        # random.Random(seed), randint and choice are stable since Python 3.2; the numbers above
        # are the real proof, this pins the first draws so a failure points at the RNG
        r = random.Random(29)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [2, -2, 0, 2, 2, 0, -2, 2])
        self.assertEqual(r.choice(range(55)), 22)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "RELIANCE")

    def test_bars_after_the_clock_are_dropped(self):
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        self.assertEqual(dropped, {"no_price": 112, "off_grid": 0, "not_complete": 290, "outside_hours": 0})
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-10-29", "09:40", 5))
        # with the real clock the data's own time (15:29:39 on 2026-11-03) still marks 15:25 as in
        # progress, and Yahoo left 15:20 empty
        last = tm.parse_bars(self.result, tm.parse_now(None))[1][-1]
        self.assertEqual((last["date"], last["start"]), ("2026-11-03", "15:15"))
        entry = [b for b in bars if b["date"] == "2026-10-29" and b["slot"] == 4][0]
        self.assertEqual((entry["start"], entry["c"]), ("09:35", 1174.300048828125))

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, tm.parse_now("2026-10-29 09:37"))[1]
        today = [b for b in bars if b["date"] == "2026-10-29"]
        self.assertEqual(len(today), 4)
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-10-29 09:37"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("09:30 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-10-29 09:40"), replays=200)["mc"]["pool_sessions"], 55)

    def test_ist_has_no_daylight_saving(self):
        stamps = [1793331900,   # 2026-10-30 03:45Z = 9:15 IST
                  1793416500,   # 2026-10-31 03:15Z = 8:45 IST, before the open
                  1793591100,   # 2026-11-02 03:45Z = 9:15 IST, after the US clock change: no change here
                  1793613600]   # 2026-11-02 10:00Z = 15:30 IST, the close: not a bar
        res = {"meta": {"symbol": "X.NS"}, "timestamp": stamps,
               "indicators": {"quote": [{"open": [1] * 4, "high": [1] * 4, "low": [1] * 4, "close": [1] * 4,
                                         "volume": [0] * 4}]}}
        meta, bars, dropped = tm.parse_bars(res, tm.parse_now("2026-11-03T00:00Z"))
        self.assertEqual([(b["date"], b["start"], b["slot"]) for b in bars],
                         [("2026-10-30", "09:15", 0), ("2026-11-02", "09:15", 0)])
        self.assertEqual(dropped["outside_hours"], 2)
        self.assertEqual(tm.to_ist(tm.utc_of(1793591100)).strftime("%H:%M"), "09:15")
        self.assertEqual(tm.parse_now("2026-10-29 09:45"), tm.parse_now("2026-10-29T04:15Z"))
        self.assertEqual(tm.parse_now("2026-10-29T09:45+05:30"), tm.parse_now("2026-10-29T04:15Z"))

    def test_a_short_day_is_left_out(self):
        def cut(res):  # 2026-09-04 stops at 1:00 PM, a gap before the 15:10 flat-by
            keep_bars(res, lambda ts: not (ist(ts).date().isoformat() == "2026-09-04" and ist(ts).hour >= 13))
        doc = machine(text=edited(cut), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 54)
        self.assertIn({"date": "2026-09-04", "bars": 45}, doc["data"]["short_sessions_left_out"])
        self.assertEqual(doc["history"]["sessions"], 54)
        self.assertNotIn("2026-09-04", [t["date"] for t in doc["history"]["trades"]])
        self.assertIn("Left out 2 session(s) with a gap before 3:10 PM", "\n".join(tm.report(doc)))

    def test_yahoos_empty_last_bars_do_not_throw_days_away(self):
        # every NSE day in the data lacks the 15:20 bar, so a replay to the 15:30 close has no complete
        # session; up to the 15:10 flat-by they all count (one has a real midday gap)
        days = tm.split_sessions(tm.parse_bars(self.result, NOW_UTC)[1])
        self.assertTrue(all(73 not in d for d in days.values()))   # slot 73 = the 15:20 bar
        self.assertEqual(machine(replays=200)["mc"]["pool_sessions"], 55)
        self.assertEqual(machine(until="15:15", replays=200)["mc"]["pool_sessions"], 55)
        self.assertEqual(machine(until="15:20", replays=200)["mc"]["pool_sessions"], 49)
        with self.assertRaises(tm.TMError) as cm:
            machine(until="15:30", replays=10)
        self.assertIn("only 0 session(s) of RELIANCE before 2026-10-29 are complete up to 3:30 PM", str(cm.exception))
        self.assertIn("try an earlier --until", str(cm.exception))

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200, now=tm.parse_now("2026-10-29 15:30"))
        self.assertEqual(doc["mc"]["pool_to"], "2026-10-28")
        self.assertNotIn("2026-10-29", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_price_is_dropped(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 113)
        self.assertEqual(doc["mc"]["pool_sessions"], 54)   # 2026-08-07 now has a gap

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        def trim(res):   # 20 days from 2026-09-29, but 2026-10-15 has a gap: 19
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-09-29")
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(trim))
        self.assertIn("only 19 session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))

        def twenty(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-09-28")
        self.assertEqual(machine(text=edited(twenty), replays=200)["mc"]["pool_sessions"], 20)

    def test_the_data_must_be_the_plans_symbol(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="TCS"))
        self.assertIn("data is for RELIANCE.NS", str(cm.exception))


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-30"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("no bars for that day", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # the 9:45 bar has not happened at 9:45
            machine(plan(entry_time="9:50 AM", entry_bar=None))
        self.assertIn("not in the data", str(cm.exception))

    def test_nse_holidays_and_weekends_are_closed(self):
        for day, words in (("2026-10-20", "NSE holiday (Dussehra)"), ("2026-11-10", "Diwali Balipratipada"),
                           ("2026-11-08", "a weekend")):
            with self.assertRaises(tm.TMError) as cm:
                plan(date=day)
            self.assertIn("NSE is closed on %s" % day, str(cm.exception))
            self.assertIn(words, str(cm.exception))
        self.assertEqual(tm.trading_day(tm.datetime.date(2026, 10, 29)), (True, ""))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="9:42 AM", entry_bar=None), "5-minute grid"),
            (dict(entry_time="3:30 PM", entry_bar=None), "between 9:20 AM and 3:25 PM"),
            (dict(entry_bar=5), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=1175.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "NSE symbol"),
            (dict(symbol="RELIANCE.BO"), "no .BO"),
            (dict(symbol="RELI ANCE"), "NSE symbol"),
            (dict(exchange="BSE"), "NSE trades only"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="Oct 29"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_nse_symbols(self):
        self.assertEqual(plan(symbol="reliance.ns")["symbol"], "RELIANCE")
        self.assertEqual(plan(symbol="M&M")["symbol"], "M&M")
        self.assertEqual(plan(symbol="bajaj-auto")["symbol"], "BAJAJ-AUTO")
        self.assertEqual(plan(symbol="360ONE")["symbol"], "360ONE")
        self.assertIsNone(tm.nse_symbol("TCS.BO"))
        self.assertEqual(plan(exchange=None)["exchange"], "NSE")

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "9:40 AM")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 4)
        self.assertEqual(plan(entry_time="9:40", entry_bar=None)["entry_bar"], 4)
        self.assertEqual(plan(entry_time="9:40am", entry_bar=None)["entry_bar"], 4)
        self.assertEqual(plan(entry_time="1:15", entry_bar=None)["entry_bar"], 47)   # a bare 1:15 is p.m.

    def test_until_cuts_the_replay(self):
        doc = machine(until="14:30", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("14:30", 58, "14:30"))
        self.assertIn("still open at 2:30 PM", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="2:30 PM", replays=500)["mc"], m)
        for bad, words in (("9:40 AM", "nothing to replay"), ("15:35", "no later than 3:30 PM"),
                           ("15:07", "5-minute grid"), ("noon", "IST time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_flat_by_is_the_default_until(self):
        self.assertEqual(machine(plan(flat_by="14:00"), replays=100)["mc"]["until"], "14:00")
        self.assertEqual(machine(plan(flat_by=None), replays=100)["mc"]["until"], "15:10")
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(flat_by="15:12"), replays=10)
        self.assertIn("flat_by must be on the 5-minute grid", str(cm.exception))

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1180.0, stop=1170.0, target=1190.0), replays=200)
        self.assertIn("outside that bar's range", doc["warnings"][0])

    def test_a_date_past_the_holiday_list_is_flagged(self):
        self.assertEqual(tm.CALENDAR_YEARS, (2026,))
        self.assertEqual(len(tm.NSE_HOLIDAYS), 15)
        self.assertTrue(all(tm.datetime.date.fromisoformat(d).weekday() < 5 for d in tm.NSE_HOLIDAYS))

    def test_nse_tick_rounding(self):
        self.assertEqual(tm.tick_up((1156.2 + 1170.5) / 2), 1163.4)
        self.assertEqual(tm.tick_down(1174.3 + 10.9), 1185.2)
        self.assertEqual((tm.tick_up(249.991), tm.tick_down(249.999)), (250.0, 249.99))   # 1 paisa below 250
        self.assertEqual((tm.tick_up(512.32), tm.tick_down(512.32)), (512.35, 512.3))    # 5 paise
        self.assertEqual((tm.tick_up(1163.35), tm.tick_down(1163.35)), (1163.4, 1163.3))  # 10 paise
        self.assertEqual((tm.tick_up(7000.2), tm.tick_down(7000.2)), (7000.5, 7000.0))    # 50 paise
        self.assertEqual((tm.tick_up(12000.2), tm.tick_down(12000.2)), (12001.0, 12000.0))
        self.assertEqual((tm.tick_up(25001.0), tm.tick_down(25001.0)), (25005.0, 25000.0))
        self.assertEqual(tm.tick_up(1170.5), 1170.5)              # already on the tick
        self.assertEqual(tm.tick_up(1170.5000000001), 1170.5)    # float noise is not a tick
        self.assertEqual((tm.toward_entry(1163.35, True), tm.toward_entry(1163.35, False)), (1163.4, 1163.3))

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=1156.2, target=1192.4), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=1165.0, target=1192.9), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.792, 2.0))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 55)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=1185.2, target=1163.4, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["r_at_target"], 1.0)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        self.assertEqual(doc["history"]["triggered"], 39)
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("below the entry", "\n".join(tm.report(doc)))

    def test_the_verdict_thresholds_at_1r(self):
        # 1R: the target's share of the replays that hit one side against 50 %
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:10"}),
                         "No replay reached the target or the stop by 3:10 PM: the price at 3:10 PM decides every one.")

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Strongly toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_2r_on_the_fixture(self):
        # the walkthrough's entry and stop with a 2R target 1196.10: target first only 2.6 %
        doc = machine(plan(target=1196.1))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (2.0, 0.026, 0.3191, 0.6549, -0.118))
        self.assertEqual(doc["verdict"], "Strongly toward the stop: at 2R the target has to come first in 33 % of "
                                         "the replays that hit one side; it did in 8 % (target first 2.6 % vs stop "
                                         "first 31.9 %).")
        self.assertEqual(doc["verdict_notes"],
                         ["65.5 % of the replays hit neither by 3:10 PM; the price at 3:10 PM decides those."])
        text = "\n".join(tm.report(doc))
        self.assertIn(doc["verdict"] + "\n" + doc["verdict_notes"][0], text)


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", tm.chart_url("M&M", "query1"))
        self.assertIn("/chart/BAJAJ-AUTO.NS?", tm.chart_url("BAJAJ-AUTO"))
        self.assertEqual(tm.UA, "Mozilla/5.0 (compatible; day-trader-skill-nse/1.0)")

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2", "query1"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_one_429_then_ok(self):
        self.answer([429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2"])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query1"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("RELIANCE")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZZZ")
        self.assertIn("ZZZZ.NS", str(cm.exception))
        self.assertIn("check the NSE symbol", str(cm.exception))

    def test_run_over_the_network_path(self):
        self.answer([200])
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(PLAN, fh)
            code, text, _ = run(["run", "--plan", p, "--now", NOW, "--replays", "300"])
            self.assertEqual(code, 0)
            self.assertEqual(self.calls, ["query2"])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "trader", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--seed", "29",
                               "--write", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("target first   24.2 %", text)
        self.assertIn("Leans toward the stop: target first 24.2 % vs stop first 31.7 %.", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/trader-budget", text)
        with open(self.out) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history") if k in doc), ["history", "mc", "plan"])
        self.assertEqual(doc["created_by"], "trader-timemachine")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"]), (0.2419, -0.101))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), SIM_PATHS)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (26, -0.02))
        self.assertEqual(doc["plan"]["from"], self.plan)

    def test_the_default_seed_is_19(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["mc"]["seed"], 19)

    def test_json_output(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--seed", "29",
                             "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.3173)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertEqual(doc["verdict"][:21], "Leans toward the stop")
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "reliance.ns", "--date", "2026-10-29", "--entry", "1174.30",
                               "--stop", "1163.40", "--target", "1185.20", "--entry-time", "9:40 AM",
                               "--offline", RELIANCE, "--now", NOW, "--seed", "29", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"], doc["mc"]["until"]), (0.2419, 0.4401, "15:10"))
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (12, 11, -0.02))
        code, _, err = run(["run", "--symbol", "RELIANCE", "--entry", "1174.30", "--offline", RELIANCE])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_offline_folder_and_default_paths(self):
        shutil.copy(RELIANCE, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("trader", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("trader", "timemachine.json")) as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", RELIANCE, "--until", "9:00"],
                     ["run", "--plan", self.plan, "--offline", RELIANCE, "--until", "15:30"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--seed", "29", "--write", self.out])
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("2,000 replay paths from the 9:40 AM entry (seed 29)", lines[0])
        self.assertTrue(any(l.startswith("  1185.20 |") and l.endswith("  target") for l in lines))
        self.assertTrue(any(l.startswith("  1163.40 |") and l.endswith("  stop") for l in lines))
        self.assertTrue(any(l.startswith("  1174.30 |*") and l.endswith("  entry") for l in lines))
        self.assertIn("9:40", text)
        self.assertIn("3:10", text)
        self.assertEqual(lines[-2:], ["Leans toward the stop: target first 24.2 % vs stop first 31.7 %.",
                                      "44.1 % of the replays hit neither by 3:10 PM; the price at 3:10 PM decides those."])
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--seed", "29"])
        self.assertEqual(again, text)


if __name__ == "__main__":
    unittest.main()
