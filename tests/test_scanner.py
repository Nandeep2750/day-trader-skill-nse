import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

scan = load("trader-scanner", "scan")

SDIR = os.path.join(FIX, "trader-scanner")
WALL = os.path.join(SDIR, "wall5m-2026-10-29.json.gz")         # 9 real NSE v8 5-min charts, Thu Oct 29 2026
SPARK5 = os.path.join(SDIR, "spark-5m-2026-10-29.json")        # a real 19-symbol 5-min spark answer, cut to Oct 29
DAILY = os.path.join(SDIR, "daily-spark-3mo-2026-11-03.json")  # a real 3-month daily spark answer, fetched 4:46 PM
NSE = os.path.join(SDIR, "nse")                                # trimmed EQUITY_L.csv and ind_nifty500list.csv
DT = datetime.datetime
D = datetime.date
SPARK_SYMS = ["AXISBANK", "BAJAJ-AUTO", "BHARTIARTL", "HDFCBANK", "ICICIBANK", "IDEA", "INFY", "ITC", "KOTAKBANK",
              "LT", "M&M", "MARUTI", "NTPC", "RELIANCE", "SBIN", "SUNPHARMA", "TATASTEEL", "TCS", "YESBANK"]
RELIANCE = {"symbol": "RELIANCE", "setups": ["orb"], "price": 1174.3, "at": "9:40 AM", "bar_start": "09:35",
            "bar_index": 4, "or_high": 1170.5, "or_low": 1156.2, "pct_over": 0.32}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(ist_text):
    """IST wall-clock text -> unix seconds."""
    utc = scan.ist_to_utc(DT.strptime(ist_text, "%Y-%m-%d %H:%M:%S" if ist_text.count(":") == 2
                                      else "%Y-%m-%d %H:%M"))
    return int((utc - DT(1970, 1, 1)).total_seconds())


def bars_series(day, closes, highs=None, lows=None, start="09:15", extra=None):
    """A v8-like series of 5-min bars from `start` on `day` (YYYY-MM-DD)."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    s = {"t": t, "c": list(closes), "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
         "l": list(lows or [c - 0.1 if c is not None else None for c in closes]), "o": None,
         "v": [1000] * len(closes), "meta": {}}
    for when, c in (extra or []):
        s["t"].append(ts(when))
        for k in ("c", "h", "l"):
            s[k].append(c)
        s["v"].append(10)
    return s


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Universe(unittest.TestCase):
    def test_eq_series_only(self):
        kept, counts = scan.build_universe(read_text(os.path.join(NSE, "EQUITY_L.csv")))
        self.assertEqual([s for s, _ in kept],
                         ["20MICRONS", "360ONE", "ARE&M", "BAJAJ-AUTO", "GMRP&UI", "HDFCBANK", "IDEA", "INFY", "M&M",
                          "RELIANCE", "SBIN", "TCS", "YESBANK"])
        self.assertEqual(counts, {"rows": 17, "keep": 13, "series BE (trade-to-trade, no intraday)": 2,
                                  "series BZ (trade-to-trade, no intraday)": 2})
        self.assertEqual(dict(kept)["M&M"], "Mahindra & Mahindra Limited")

    def test_nifty500_list(self):
        kept, counts = scan.build_universe(read_text(os.path.join(NSE, "ind_nifty500list.csv")))
        self.assertEqual(len(kept), 10)
        self.assertEqual(dict(kept)["BAJAJ-AUTO"], "Bajaj Auto Ltd.")

    def test_one_case_per_rule(self):
        cases = [({"symbol": "RELIANCE", "series": "EQ"}, "keep"),
                 ({"symbol": "M&M", "series": "EQ"}, "keep"),
                 ({"symbol": "3IINFOLTD", "series": "BE"}, "series BE (trade-to-trade, no intraday)"),
                 ({"symbol": "AGSTRA", "series": "BZ"}, "series BZ (trade-to-trade, no intraday)"),
                 ({"symbol": "SOMEBOND", "series": "N1"}, "series N1"),
                 ({"symbol": "BAD SYM", "series": "EQ"}, "not a plain NSE symbol"),
                 ({"symbol": "", "series": "EQ"}, "not a plain NSE symbol")]
        for row, want in cases:
            self.assertEqual(scan.classify(row), want, row)

    def test_not_an_nse_list(self):
        with self.assertRaises(scan.ScanError):
            scan.parse_nse_list("<html>maintenance</html>")
        with self.assertRaises(scan.ScanError):
            scan.parse_nse_list("")


class Symbols(unittest.TestCase):
    def test_nse_symbols(self):
        self.assertEqual(scan.nse_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(scan.nse_symbol(" M&M "), "M&M")
        self.assertEqual(scan.nse_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO")
        self.assertEqual(scan.nse_symbol("360ONE"), "360ONE")
        with self.assertRaises(scan.ScanError) as cm:
            scan.nse_symbol("RELIANCE.BO")
        self.assertIn("NSE only", str(cm.exception))
        with self.assertRaises(scan.ScanError):
            scan.nse_symbol("BRK.B")

    def test_urls(self):
        self.assertEqual(scan.spark_path(["M&M", "reliance"], "3mo", "1d"),
                         "/v7/finance/spark?symbols=M%26M.NS,RELIANCE.NS&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("BAJAJ-AUTO", "1d", "5m"),
                         "/v8/finance/chart/BAJAJ-AUTO.NS?range=1d&interval=5m")
        self.assertEqual(scan.from_yahoo("M&M.NS"), "M&M")


class Time(unittest.TestCase):
    def test_ist_and_clock(self):
        self.assertEqual(scan.ts_to_ist(ts("2026-10-29 09:15")), DT(2026, 10, 29, 9, 15))
        self.assertEqual(scan.ts_to_ist(1793677500), DT(2026, 11, 3, 9, 15))         # Yahoo's 9:15 bar, Nov 3
        self.assertEqual(scan.parse_now("2026-10-29 09:45"), DT(2026, 10, 29, 4, 15))
        self.assertEqual(scan.parse_now("2026-01-15 09:45"), DT(2026, 1, 15, 4, 15))  # no daylight saving
        self.assertEqual(scan.parse_now("2026-10-29T04:15:00Z"), DT(2026, 10, 29, 4, 15))
        self.assertEqual(scan.parse_now("2026-10-29T09:45:00+05:30"), DT(2026, 10, 29, 4, 15))
        self.assertEqual(scan.clock_label(DT(2026, 10, 29, 9, 40)), "9:40 AM")
        self.assertEqual(scan.clock_label(datetime.time(15, 25)), "3:25 PM")
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")

    def test_nse_calendar(self):
        self.assertEqual(scan.trading_day(D(2026, 10, 29)), (True, ""))
        self.assertEqual(scan.trading_day(D(2026, 11, 10)), (False, "NSE holiday: Diwali Balipratipada"))
        self.assertEqual(scan.trading_day(D(2026, 11, 8)), (False, "weekend"))    # Muhurat Sunday: not modelled
        self.assertEqual(scan.session(D(2026, 12, 24)), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertIsNone(scan.session(D(2026, 12, 25)))


class Spark(unittest.TestCase):
    def test_a_real_daily_spark_answer(self):
        text = read_text(DAILY)
        wanted = {scan.to_yahoo(s): s for s in ["M&M", "RELIANCE", "ZZZZQX"]}
        data = scan.parse_spark(text, wanted)
        self.assertEqual(sorted(data), SPARK_SYMS)   # 20 asked; Yahoo silently drops the made-up ZZZZQX
        self.assertIn("M&M", data)                   # M&M.NS comes back as ours
        r = data["RELIANCE"]
        self.assertEqual(len(r["t"]), 64)
        self.assertIsNone(r["h"])                    # spark is closes only
        self.assertEqual(r["c"][-1], 1307.9)
        self.assertEqual(r["meta"]["regularMarketVolume"], 42318217)
        self.assertEqual(data["IDEA"]["meta"]["regularMarketPrice"], 12.26)
        closes = scan.daily_closes(r, scan.parse_now("2026-11-04 09:00"))
        self.assertEqual(closes[-1], (D(2026, 11, 3), 1307.9))
        # the same answer read during Tuesday's session: Tuesday's bar is still in progress
        closes = scan.daily_closes(r, scan.parse_now("2026-11-03 15:29"))
        self.assertEqual(closes[-1], (D(2026, 11, 2), 1300.0))
        closes = scan.daily_closes(r, scan.parse_now("2026-11-03 15:30"))
        self.assertEqual(closes[-1], (D(2026, 11, 3), 1307.9))

    def test_spark_errors(self):
        too_many = '{"spark":{"result":null,"error":{"code":"Bad Request",' \
                   '"description":"Number of symbols needs to be less than or equal to 20"}}}'
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')

    def test_spark_leaves_out_unchanged_closes(self):
        # real: YESBANK at Rs 15 has its 10:00, 10:15 and 10:20 bars missing from spark (same close as
        # the bar before) and the 3:20 PM bar that is always empty; the v8 chart has every bar
        sp = scan.parse_spark(read_text(SPARK5))
        day, cut = D(2026, 10, 29), DT(2026, 10, 29, 15, 30)
        bars = scan.session_bars(sp["YESBANK"], day, cut)
        self.assertEqual(len(bars), 69)
        self.assertFalse(scan.usable(bars, day, cut, 3))
        filled = scan.fill_closes(bars, day, cut)
        self.assertEqual(len(filled), 75)
        self.assertTrue(scan.usable(filled, day, cut, 3))
        by = dict((b["start"].strftime("%H:%M"), b["c"]) for b in filled)
        self.assertEqual(by["10:00"], by["09:55"])


class Network(unittest.TestCase):
    """429s and 5xx without the network."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        self.body = read_text(DAILY)

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split("/v")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        text = scan.yahoo_get("/v7/finance/spark?symbols=RELIANCE.NS&range=3mo&interval=1d")
        self.assertEqual(text, self.body)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com",
                                      "https://query2.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_5xx_goes_to_query1_without_waiting(self):
        self.answers([503, 200])
        scan.yahoo_get("/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        syms = SPARK_SYMS + ["ZZZZQX"] + ["X%02d" % i for i in range(20)]
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.3)
        self.assertEqual(len(data), 19)
        self.assertEqual(failed["ZZZZQX"], "no data from Yahoo")
        self.assertEqual(len(failed), 21)
        self.assertIn("HTTP 429", failed["X00"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])  # a pause between batches, one backoff
        hits, counts, _ = scan.scan_daily(data, syms, scan.parse_now("2026-11-03 09:00"), 50, 500000)
        self.assertEqual(counts["requested"], 40)
        self.assertEqual(counts["fetched"], 19)

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/NOPE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)


class Daily(unittest.TestCase):
    # Hand-checked from the closes, through Mon Nov 2 2026 (the scan before Tuesday's open).
    EXPECT = {
        "RELIANCE": ["breakout_20d", "reclaim_ma20"],  # 1300.00 > 1218.00; 1164.90 under 1186.41, then over 1193.03
        "BHARTIARTL": ["breakout_20d"],
        "SBIN": ["breakout_20d"],                      # 987.00 > 973.70
        "M&M": ["reclaim_ma20"],                       # 2840.30 under 2850.34, then 2868.80 over 2850.78
        "TCS": ["bull_flag"],                          # up 17.6 % over the 10 sessions, last 5 in a 1.7 % range
        "LT": ["breakout_20d"],
        "MARUTI": ["breakout_20d"],
        "TATASTEEL": ["reclaim_ma20"],                 # 167.60 under 173.20, then 177.37 over 173.17
        "NTPC": ["breakout_20d"],
        "YESBANK": ["oversold", "oversold_bounce"],    # RSI 22.8, and 15.15 -> 15.57
    }

    def setUp(self):
        self.data, self.errors = scan.load_offline(DAILY)
        self.syms = sorted(self.data)

    def test_setups_before_the_open(self):
        self.assertEqual(self.syms, SPARK_SYMS)
        hits, counts, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-03 09:00"), 0, 0)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EXPECT)
        self.assertEqual(last, D(2026, 11, 2))
        self.assertEqual(counts, {"requested": 19, "fetched": 19, "illiquid": 0, "short": 0, "scanned": 19,
                                  "hits": 10})
        rel = hits[0]                                  # the most rupees traded first
        self.assertEqual((rel["symbol"], rel["price"], rel["rsi"], rel["close_date"]),
                         ("RELIANCE", 1300.0, 65.7, "2026-11-02"))

    def test_the_in_progress_bar_is_dropped(self):
        before = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-03 09:00"), 0, 0)
        during = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-03 13:00"), 0, 0)
        self.assertEqual(before, during)
        after = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-03 16:00"), 0, 0)
        self.assertEqual(after[2], D(2026, 11, 3))
        self.assertEqual(after[1]["hits"], 11)

    def test_liquidity_filter(self):
        # meta is Yahoo's after Tuesday's close: the day's full volume
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-03 09:00"), 50, 500000)
        self.assertEqual(sorted(h["symbol"] for h in hits),
                         ["BHARTIARTL", "LT", "M&M", "NTPC", "RELIANCE", "SBIN", "TATASTEEL", "TCS"])
        self.assertEqual(counts["illiquid"], 4)     # IDEA, YESBANK under Rs 50; BAJAJ-AUTO, MARUTI under 5 lakh shares
        self.assertEqual(counts["scanned"], 15)

    def test_short_and_empty_charts(self):
        s = {"t": [ts("2026-10-%02d 09:15" % d) for d in range(5, 15)], "c": [100.0] * 10,
             "meta": {"regularMarketPrice": 100, "regularMarketVolume": 5000000}}
        hits, counts, last = scan.scan_daily({"X": s, "E": {"t": [], "c": [], "meta": {}}}, ["X", "E", "GONE"],
                                             scan.parse_now("2026-10-20 09:00"), 50, 500000)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["illiquid"], counts["scanned"]), (2, 1, 1, 0))
        self.assertIsNone(last)

    def test_rsi_matches_the_textbook(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)  # Wilder's textbook example


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(WALL)
        cls.syms = sorted(set(cls.data) | set(cls.errors))

    def scan_at(self, now, min_price=0, syms=None, or_minutes=15):
        day, cutoff = scan.orb_window(scan.parse_now(now), None, or_minutes, datetime.time(14, 45))
        return scan.scan_orb(self.data, syms or self.syms, day, cutoff, or_minutes, min_price, 0)

    def test_the_readme_scan_at_0945(self):
        hits, counts, failed = self.scan_at("2026-10-29 09:45")
        self.assertEqual(self.syms, ["BAJAJ-AUTO", "HDFCBANK", "ICICIBANK", "INFY", "M&M", "RELIANCE", "SBIN", "TCS",
                                     "YESBANK"])
        self.assertEqual(counts["scanned"], 9)
        self.assertEqual(counts["unusable"], 0)
        # range 9:15 to 9:30: RELIANCE high 1170.5 (the 9:25 bar), low 1156.2 (the 9:15 bar);
        # the 9:30 bar closed 1169.5 (inside), the 9:35 bar closed 1174.3: done at 9:40
        self.assertEqual(hits, [{"symbol": "HDFCBANK", "setups": ["orb"], "price": 682.5, "at": "9:35 AM",
                                 "bar_start": "09:30", "bar_index": 3, "or_high": 682.45, "or_low": 678.3,
                                 "pct_over": 0.01}, RELIANCE])

    def test_the_whole_entry_window(self):
        hits, counts, _ = self.scan_at("2026-10-29 14:45")
        self.assertEqual([(h["symbol"], h["at"]) for h in hits],
                         [("HDFCBANK", "9:35 AM"), ("RELIANCE", "9:40 AM"), ("ICICIBANK", "9:55 AM"),
                          ("SBIN", "10:10 AM"), ("INFY", "11:40 AM"), ("M&M", "1:00 PM")])
        mm = hits[-1]
        self.assertEqual((mm["price"], mm["or_low"], mm["or_high"], mm["pct_over"]), (2891.6, 2858.8, 2887.5, 0.14))

    def test_price_floor(self):
        hits, counts, _ = self.scan_at("2026-10-29 09:45", min_price=50)
        self.assertEqual(counts["illiquid"], 1)            # YESBANK at Rs 15
        self.assertEqual(len(hits), 2)

    def test_in_progress_bar_and_no_breakout_yet(self):
        hits, _, _ = self.scan_at("2026-10-29 09:39", syms=["RELIANCE"])
        self.assertEqual(hits, [])                         # the 9:35 bar is not done until 9:40
        hits, _, _ = self.scan_at("2026-10-29 09:40", syms=["RELIANCE"])
        self.assertEqual(hits, [RELIANCE])

    def test_the_closing_session_bars_are_not_required(self):
        # real: Yahoo's 3:20 PM bar is null on every NSE day; the day is still usable at the close
        s = self.data["RELIANCE"]
        day, cut = D(2026, 10, 29), DT(2026, 10, 29, 15, 30)
        bars = scan.session_bars(s, day, cut)
        self.assertEqual(len(bars), 74)
        self.assertNotIn("15:20", [b["start"].strftime("%H:%M") for b in bars])
        self.assertTrue(scan.usable(bars, day, cut, 3))
        # without the 3:15 and 3:25 bars too: still usable
        thin = [b for b in bars if b["start"].time() < datetime.time(15, 15)]
        self.assertTrue(scan.usable(thin, day, cut, 3))
        # a bar missing before 3:15 is a halt or no trades: unusable
        holed = [b for b in bars if b["start"].strftime("%H:%M") != "11:45"]
        self.assertFalse(scan.usable(holed, day, cut, 3))

    def test_the_live_tick_is_dropped(self):
        closes = [100, 100.5, 101, 100.8, 101.6, 101.2]
        s = bars_series("2026-10-29", closes, extra=[("2026-10-29 09:46:07", 102.0)])
        self.assertEqual(scan.ts_to_ist(s["t"][-1]), DT(2026, 10, 29, 9, 46, 7))     # off the grid
        bars = scan.session_bars(s, D(2026, 10, 29), DT(2026, 10, 29, 9, 46))
        self.assertEqual(len(bars), 6)                     # 9:15 to 9:40; the tick dropped
        self.assertTrue(scan.usable(bars, D(2026, 10, 29), DT(2026, 10, 29, 9, 46), 3))
        hits, _, _ = scan.scan_orb({"X": s}, ["X"], D(2026, 10, 29), DT(2026, 10, 29, 9, 46), 15, 0, 0)
        self.assertEqual((hits[0]["price"], hits[0]["at"]), (101.6, "9:40 AM"))

    def test_stage_one_is_a_necessary_condition(self):
        day, cut = D(2026, 10, 29), DT(2026, 10, 29, 14, 45)
        for sym, s in self.data.items():
            bars = scan.session_bars(s, day, cut)
            if scan.usable(bars, day, cut, 3) and scan.orb_hit(bars, 3):
                self.assertTrue(scan.orb_candidate(bars, 3), sym)

    def test_confirm_uses_the_v8_highs(self):
        spark_like = {k: dict(v, h=None, l=None, v=None) for k, v in self.data.items()}  # closes only, like spark
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        hits, counts, failed = scan.scan_orb(spark_like, self.syms, D(2026, 10, 29), DT(2026, 10, 29, 9, 45), 15,
                                             0, 0, confirm)
        self.assertEqual(hits, [h for h in self.scan_at("2026-10-29 09:45")[0]])
        self.assertEqual(counts["candidates"], len(asked))
        self.assertLess(len(asked), 9)                     # stage 1 skipped some charts

        def broken(sym):
            raise scan.ScanError("Yahoo answered HTTP 429 for /v8/finance/chart/%s.NS" % sym)
        hits, counts, failed = scan.scan_orb(spark_like, ["RELIANCE", "TCS"], D(2026, 10, 29),
                                             DT(2026, 10, 29, 9, 45), 15, 0, 0, broken)
        self.assertEqual(hits, [])
        self.assertIn("429", failed["RELIANCE"])

    def test_the_real_spark_answer_with_v8_confirmation(self):
        sp = scan.parse_spark(read_text(SPARK5))

        def confirm(sym):
            if sym not in self.data:
                raise scan.ScanError("no v8 chart for %s in the fixture" % sym)
            return self.data[sym]
        hits, counts, failed = scan.scan_orb(sp, sorted(sp), D(2026, 10, 29), DT(2026, 10, 29, 9, 45), 15, 0, 0,
                                             confirm)
        self.assertEqual(counts["scanned"], 19)            # every closes-only chart usable once gaps are carried
        self.assertEqual([h["symbol"] for h in hits], ["HDFCBANK", "RELIANCE"])
        self.assertIn(RELIANCE, hits)

    def test_a_missing_bar_makes_a_chart_unusable(self):
        s = bars_series("2026-10-29", [100, 100.2, 100.1, None, 100.5, 100.6, 100.7, 100.8, 100.9])
        bars = scan.session_bars(s, D(2026, 10, 29), DT(2026, 10, 29, 10, 0))
        self.assertFalse(scan.usable(bars, D(2026, 10, 29), DT(2026, 10, 29, 10, 0), 3))
        no_open = bars_series("2026-10-29", [100, 100.2, 100.1, 100.4], start="09:20")
        bars = scan.fill_closes(scan.session_bars(dict(no_open, h=None, l=None), D(2026, 10, 29),
                                                  DT(2026, 10, 29, 9, 40)), D(2026, 10, 29), DT(2026, 10, 29, 9, 40))
        self.assertFalse(scan.usable(bars, D(2026, 10, 29), DT(2026, 10, 29, 9, 40), 3))  # no 9:15 bar to carry

    def test_a_30_minute_range(self):
        closes = [100, 101, 100.5, 100.8, 101.2, 100.9, 101.5] + [101.0] * 40
        s = bars_series("2026-10-29", closes, highs=[c + 0.2 for c in closes])
        hits, counts, _ = scan.scan_orb({"X": s}, ["X"], D(2026, 10, 29), DT(2026, 10, 29, 12, 0), 30, 0, 0)
        self.assertEqual(counts["scanned"], 1)
        self.assertEqual(hits[0]["at"], "9:50 AM")          # range 9:15 to 9:45 (high 101.4), 9:45 bar 101.5
        self.assertEqual(hits[0]["or_high"], 101.4)

    def test_window_messages(self):
        until = datetime.time(14, 45)

        def msg(now, at=None, until=until):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("weekend", msg("2026-10-31 10:15"))
        self.assertIn("NSE holiday: Diwali Balipratipada", msg("2026-11-10 10:15"))
        self.assertIn("runs 9:15 to 9:30", msg("2026-10-29 09:25"))
        self.assertIn("Run orb again after 9:35 AM IST", msg("2026-10-29 09:25"))
        self.assertIn("Run orb again after 9:35 AM IST", msg("2026-10-29 09:34"))
        self.assertIn("expired", msg("2026-10-29 15:00"))
        self.assertIn("expired", msg("2026-10-29 11:30", until=datetime.time(11, 0)))
        self.assertIn("closed at 3:30 PM IST", msg("2026-10-29 16:30"))
        self.assertIn("later than now", msg("2026-10-29 09:40", at=datetime.time(10, 15)))
        self.assertIn("after the 3:30 PM close", msg("2026-10-29 17:00", at=datetime.time(15, 45)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-29 16:30"), datetime.time(9, 45), 15, until),
                         (D(2026, 10, 29), DT(2026, 10, 29, 9, 45)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-29 09:35"), None, 15, until)[1],
                         DT(2026, 10, 29, 9, 35))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-10-29 10:15"), None, 7, until)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        uni = os.path.join(self.t, "universe.txt")
        code, text, _ = run(["universe", "--offline", NSE, "--out", uni])
        self.assertEqual(code, 0)
        self.assertIn("NSE equity list (EQUITY_L.csv): 17 rows.", text)
        self.assertIn("Kept 13 NSE stocks in the EQ series.", text)
        self.assertIn("Dropped: 2 series BE (trade-to-trade, no intraday), 2 series BZ", text)
        lines = read_text(uni).splitlines()
        self.assertEqual(len(lines), 13)
        self.assertIn("M&M|Mahindra & Mahindra Limited", lines)

        code, text, _ = run(["universe", "--offline", NSE, "--index", "nifty500", "--json", "--no-write"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["count"], 10)

        # daily over that universe with the fixture charts: 9 are in both, so partial
        code, text, _ = run(["daily", "--universe", uni, "--offline", DAILY, "--now", "2026-11-03 09:00",
                             "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertIn("Daily scan, Tue 2026-11-03 09:00 IST, closes through 2026-11-02", text)
        self.assertIn("Fetched 9 of 13 charts (4 failed or had no data). 6 passed the liquidity filter "
                      "(₹50+ and 5,00,000+ shares)", text)
        self.assertIn("RELIANCE      ₹1,300.00  RSI  65.7  20-day closing-high breakout, reclaimed the 20-day "
                      "average", text)
        doc = self.read(self.wl)
        self.assertEqual((doc["mode"], doc["universe"], doc["scanned"], doc["failed"]), ("daily", 13, 6, 4))
        self.assertEqual(doc["hits"][0], {"symbol": "RELIANCE", "setups": ["breakout_20d", "reclaim_ma20"],
                                          "price": 1300.0, "close_date": "2026-11-02", "rsi": 65.7,
                                          "volume": 42318217})
        self.assertEqual(doc["as_of"], "2026-11-02 close")

        # the README's 9:45 scan
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-29 09:45", "--min-price", "0",
                             "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertIn("Opening-range breakout scan, Thu 2026-10-29 9:45 AM IST: 15-minute range (9:15 to 9:30 AM)",
                      text)
        self.assertIn("2 of 9 charts closed above their opening-range high by 9:45 AM", text)
        self.assertIn("9:40 AM  RELIANCE      ₹1,174.30  range 1,156.20 to 1,170.50  +0.32% over", text)
        self.assertIn("not a list of stocks to buy", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned",
                                              "source")},
                         {"created_by": "trader-scanner", "created_at": "2026-10-29T04:15:00Z", "mode": "orb",
                          "as_of": "2026-10-29 09:45 IST", "universe": 9, "scanned": 9, "source": "yahoo"})
        self.assertIn(RELIANCE, doc["hits"])

        # the hits read back as a watchlist, with --at instead of --now
        code, text, _ = run(["orb", "--offline", WALL, "--at", "09:45", "--watchlist", self.wl, "--min-price", "0",
                             "--json", "--no-write"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], len(out["hits"])), (2, 2, 2))

    def test_partial_stage_two_is_said(self):
        """The networked path with a fake Yahoo that answers from the Oct 29 bundle."""
        bundle = json.loads(scan._read_maybe_gz(WALL))
        calls = []

        def unq(y):
            return scan.from_yahoo(y.replace("%26", "&"))

        def get(path):
            calls.append(path)
            if path.startswith("/v7/finance/spark"):
                res = []
                for y in path.split("symbols=")[1].split("&range")[0].split(","):
                    r = ((bundle.get(unq(y)) or {}).get("chart") or {}).get("result")
                    if r:
                        closes = r[0]["indicators"]["quote"][0].get("close", [])
                        res.append({"symbol": unq(y) + ".NS",
                                    "response": [dict(r[0], indicators={"quote": [{"close": closes}]})]})
                return json.dumps({"spark": {"result": res, "error": None}})
            return json.dumps(bundle[unq(path.split("/chart/")[1].split("?")[0])])
        old_get, old_sleep, real_now = scan.yahoo_get, scan.SLEEP, scan.parse_now
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.parse_now = lambda text: real_now(text or "2026-10-29 09:50")  # the machine clock, pinned
        uni = os.path.join(self.d, "u.txt")
        with open(uni, "w", encoding="utf-8") as fh:
            fh.write("\n".join(sorted(bundle)) + "\n")
        try:
            code, text, _ = run(["orb", "--universe", uni, "--now", "2026-10-29 09:45", "--min-price", "0",
                                 "--max-confirm", "2", "--no-write"])
        finally:
            scan.yahoo_get, scan.SLEEP, scan.parse_now = old_get, old_sleep, real_now
        self.assertEqual(code, 0)
        self.assertIn("9 usable; 5 candidates, 2 confirmed", text)
        self.assertIn("PARTIAL: 3 more candidates were not checked", text)
        self.assertIn("of the 2 checked charts closed above their opening-range high by 9:45 AM", text)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/")), 2)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v7/")), 1)
        self.assertTrue(all("range=1d&interval=5m" in c for c in calls))
        self.assertIn("M%26M.NS", [c for c in calls if c.startswith("/v7/")][0])

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-31 10:15", "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-10-31 (weekend)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-29 09:30", "--out", self.wl])
        self.assertIn("after 9:35 AM IST", err)
        cfg = os.path.join(self.t, "config.json")
        os.makedirs(self.t)
        with open(cfg, "w") as fh:
            json.dump({"no_entries_after": "09:40"}, fh)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-29 09:45", "--config", cfg])
        self.assertEqual(code, 1)
        self.assertIn("expired", err)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-29 15:00", "--config",
                            os.path.join(self.d, "none.json")])
        self.assertIn("after 14:45", err)                  # the pack's default no_entries_after
        code, _, err = run(["orb", "--offline", WALL])
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-29 09:39", "--symbols", "reliance.ns",
                             "--out", self.wl])
        self.assertEqual(code, 1)                          # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 9:39 AM", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-29 09:45", "--symbols", "RELIANCE.BO"])
        self.assertEqual(code, 1)
        self.assertIn("NSE only", err)
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt")])
        self.assertEqual(code, 1)
        self.assertIn("scan.py universe", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertIn("no offline file", err)
        empty = os.path.join(self.d, "empty.json")
        with open(empty, "w") as fh:
            fh.write("{}")
        code, _, err = run(["daily", "--offline", empty])
        self.assertEqual(code, 1)
        self.assertIn("no symbols to scan", err)
        code, _, err = run(["universe", "--offline", self.d])
        self.assertEqual(code, 1)
        self.assertIn("no EQUITY_L.csv", err)

    def test_offline_folder_of_v8_files(self):
        folder = os.path.join(self.d, "charts")
        os.makedirs(folder)
        bundle = json.loads(scan._read_maybe_gz(WALL))
        for sym in ("RELIANCE", "M&M", "TCS"):
            with open(os.path.join(folder, sym + ".json"), "w") as fh:
                json.dump(bundle[sym], fh)
        with open(os.path.join(folder, "GONE.json"), "w") as fh:   # Yahoo's shape for an unknown symbol
            json.dump({"chart": {"result": None, "error": {"code": "Not Found",
                                                           "description": "No data found, symbol may be delisted"}}},
                      fh)
        code, text, _ = run(["orb", "--offline", folder, "--now", "2026-10-29 09:45", "--no-write", "--json"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (4, 3, 1))
        self.assertEqual([h["symbol"] for h in out["hits"]], ["RELIANCE"])
        self.assertEqual(code, 0)

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w", encoding="utf-8") as fh:
            fh.write("# my list\nRELIANCE|Reliance Industries Limited\nm&m.ns\n\nRELIANCE\n")
        self.assertEqual(scan.symbols_from_file(txt), ["RELIANCE", "M&M", "RELIANCE"])
        js = os.path.join(self.d, "wl.json")
        with open(js, "w") as fh:
            json.dump({"hits": [{"symbol": "reliance"}, {"symbol": "M&M"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["RELIANCE", "M&M"])
        code, text, _ = run(["daily", "--offline", DAILY, "--watchlist", txt, "--now", "2026-11-03 09:00",
                             "--json", "--no-write"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"]), (2, 2))
        code, text, _ = run(["daily", "--offline", DAILY, "--limit", "3", "--now", "2026-11-03 09:00", "--json",
                             "--no-write"])
        self.assertEqual(json.loads(text)["universe"], 3)

    def test_daily_json(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-11-03 09:00", "--json", "--no-write",
                             "--min-volume", "0", "--min-price", "0"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(len(out["hits"]), 10)
        self.assertEqual(out["filters"], {"min_price": 0.0, "min_volume": 0})
        self.assertEqual(out["note"], "A scan is a list of charts that match a rule, not a list of stocks to buy.")


class Money(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(scan.inr(1174.3), "₹1,174.30")
        self.assertEqual(scan.inr(100000), "₹1,00,000.00")
        self.assertEqual(scan.inr(12345678.9), "₹1,23,45,678.90")
        self.assertEqual(scan.inr(15.38), "₹15.38")
        self.assertEqual(scan.inr(-999.999), "-₹1,000.00")
        self.assertEqual(scan.group_in(500000), "5,00,000")


if __name__ == "__main__":
    unittest.main()
