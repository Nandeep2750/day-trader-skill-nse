"""trader-entry tests. Fixtures in tests/fixtures/trader-entry/:

RELIANCE-2026-10-29-5m.json  RELIANCE's real NSE five-minute bars for Thu 2026-10-29
                             (Yahoo chart JSON v8 for RELIANCE.NS, cut from a range=60d
                             answer fetched 2026-11-03). meta trimmed to one day, like a
                             range=1d answer, with regularMarketTime at the 15:30 close and
                             previousClose the 2026-10-28 last close (1158.00). Yahoo sends
                             the 9:15 bar with zero volume and the 15:20 bar as nulls.
DEMO-1m-synthetic.json       SYNTHETIC one-minute bars (not market data) on the NSE session
                             (first bar 9:15 IST): a wick above the range high, a close AT
                             the high, a null bar, a live tick.
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

entry = load("trader-entry", "entry")

EDIR = os.path.join(FIX, "trader-entry")
REL = os.path.join(EDIR, "RELIANCE-2026-10-29-5m.json")
DEMO = os.path.join(EDIR, "DEMO-1m-synthetic.json")
OPEN_TS = 1793245500       # 2026-10-29 09:15 IST (03:45 UTC)
DT = datetime.datetime


def ist(text):
    return entry.parse_now(text)


def doc(path=REL):
    with open(path) as fh:
        return json.load(fh)


def chart(d=None, sym="RELIANCE"):
    return entry.parse_chart(json.dumps(d if d is not None else doc()), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ist(text), **kw)


def shift_to(d, day):
    """The same session moved to another date: 9:15 IST is 03:45 UTC every day of the year."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    new_open = int((DT.combine(day, datetime.time(3, 45)) - DT(1970, 1, 1)).total_seconds())
    delta = new_open - OPEN_TS
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d


def cap(d, first, last, close=1170.00, high=1170.40):
    """Bars `first`..`last` (slots) stay under the 1170.50 range high."""
    d = copy.deepcopy(d)
    q = d["chart"]["result"][0]["indicators"]["quote"][0]
    for i in range(first, min(last + 1, len(q["close"]))):
        if q["close"][i] is None:
            continue
        q["close"][i] = min(q["close"][i], close)
        q["high"][i] = min(q["high"][i], high)
        q["open"][i] = min(q["open"][i], close)
    return d


def no_breakout(d=None):
    """Every bar after the range stays under the 1170.50 high."""
    return cap(d if d is not None else doc(), 3, 999)


def late_breakout(d=None):
    """The 9:30 to 9:50 bars stay under the high: the first close above is the 9:55 bar (1173.80)."""
    return cap(d if d is not None else doc(), 3, 7)


def live_cut(d, last_slot, tick_ts):
    """The day as Yahoo serves it live: bars up to `last_slot` (that one still in progress)
    plus the live tick at tick_ts, which is also Yahoo's last trade."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    keep = [i for i, t in enumerate(r["timestamp"]) if t <= OPEN_TS + last_slot * 300]
    q = r["indicators"]["quote"][0]
    r["timestamp"] = [r["timestamp"][i] for i in keep] + [tick_ts]
    for k in q:
        q[k] = [q[k][i] for i in keep] + [q[k][keep[-1]] if k != "volume" else 0]
    r["meta"]["regularMarketTime"] = tick_ts
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheWalkthrough(unittest.TestCase):
    def test_triggers_at_940_on_the_935_bar(self):
        r = at("2026-10-29 09:40")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["opening_range"]["high"], 1170.5)
        self.assertEqual(r["opening_range"]["low"], 1156.2)
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual(t["time"], "9:40 AM")
        self.assertEqual(t["bar_start"], "09:35")
        self.assertEqual(t["bar_index"], 4)
        self.assertEqual(t["price"], 1174.3)
        self.assertEqual(t["close"], 1174.300048828125)
        self.assertEqual(t["pct_over"], 0.32)
        self.assertEqual(r["level"], 1170.5)
        self.assertEqual(r["prev_close"], 1158.0)
        self.assertEqual((r["opens"], r["closes"]), ("09:15", "15:30"))

    def test_at_937_the_935_bar_is_still_forming(self):
        r = at("2026-10-29 09:37")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:30")
        self.assertEqual(r["last"]["close"], 1169.5)
        self.assertEqual(r["forming"]["bar_start"], "09:35")
        self.assertIn("\u20b91.00 under the range high \u20b91170.50", r["reason"])
        self.assertIn("The 9:35 bar is still forming", "\n".join(entry.describe(r)))
        self.assertEqual(at("2026-10-29 09:39:59")["status"], "waiting")

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-10-29 09:05")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("the NSE opens at 9:15", r["reason"])
        r = at("2026-10-29 09:29")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-10-29 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        tick = OPEN_TS + 49 * 60 + 45                                   # 10:04:45 IST
        d = live_cut(doc(), 9, tick)                                    # the 10:00 bar still in progress
        c = chart(d)
        self.assertEqual(c["bars"][-1]["t"], tick)
        r = entry.evaluate(c, entry.utc_of(c["data_time"]))          # read as of the file's own time
        self.assertEqual(r["now"], "2026-10-29 10:04:45")
        self.assertEqual(r["_bars"][-1]["slot"], 8)                     # the 9:55 bar
        self.assertEqual(r["forming"]["bar_start"], "10:00")
        self.assertEqual(r["bars_closed"], 9)
        # 10:05: closed by the clock, but Yahoo's last trade (10:04:45) is before its close
        r = at("2026-10-29 10:05", d)
        self.assertEqual(r["_bars"][-1]["slot"], 8)
        self.assertIn("Yahoo's last trade is 10:04:45", r["lag_note"])
        # a minute later with no newer trade it counts; the 10:04:45 tick never becomes a bar
        r = at("2026-10-29 10:06", d)
        self.assertEqual(r["_bars"][-1]["slot"], 9)
        self.assertEqual(r["bars_closed"], 10)
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))

    def test_null_bars_and_the_full_session(self):
        r = at("2026-10-29 15:35")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["bars_closed"], 74)                          # 75 slots, the 15:20 bar came as nulls
        self.assertNotIn(73, [b["slot"] for b in r["_bars"]])
        text = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text)
        self.assertNotIn("This is the moment", text)


class Rules(unittest.TestCase):
    def test_volume_confirmation_moves_the_trigger(self):
        r = at("2026-10-29 11:00", min_volume_x=1)
        self.assertEqual(r["opening_range"]["avg_volume"], 245338)      # the 9:15 bar's zero counts
        self.assertEqual((r["trigger"]["time"], r["trigger"]["volume_x"]), ("9:40 AM", 1.1))
        r = at("2026-10-29 11:00", min_volume_x=1.5)
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["price"]), ("10:00 AM", 8, 1173.8))
        self.assertEqual(t["volume_x"], 3.38)
        r = at("2026-10-29 09:41", min_volume_x=1.5)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("1.10x the range's volume, under your 1.50x", r["reason"])

    def test_level_setup(self):
        r = at("2026-10-29 11:00", setup="level", level=1178)
        self.assertEqual(r["trigger"]["time"], "9:55 AM")
        self.assertEqual(r["trigger"]["bar_index"], 7)
        self.assertEqual(r["trigger"]["price"], 1179.0)
        # a level under the open: the first eligible close is the 9:25 bar's, at 9:30 (no_entries_before)
        r = at("2026-10-29 11:00", setup="level", level=1100)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:30 AM", 2))
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 11:00", setup="level")

    def test_no_breakout_waits_then_expires_at_the_cutoff(self):
        d = no_breakout()
        r = at("2026-10-29 11:00", d)
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(at("2026-10-29 14:44", d)["status"], "waiting")
        r = at("2026-10-29 14:45", d)
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 14:45", r["reason"])
        self.assertEqual(at("2026-10-29 15:40", d)["phase"], "after-close")

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        d = no_breakout()
        r0 = d["chart"]["result"][0]
        keep = [i for i, t in enumerate(r0["timestamp"]) if t <= OPEN_TS + 14 * 300]   # live: up to the 10:25 bar
        r0["timestamp"] = [r0["timestamp"][i] for i in keep]
        q = r0["indicators"]["quote"][0]
        for k in q:
            q[k] = [q[k][i] for i in keep]
        r0["meta"]["regularMarketTime"] = OPEN_TS + 74 * 60 + 40                       # last trade 10:29:40
        cfg = {"no_entries_before": "09:30", "no_entries_after": "10:30"}
        r = at("2026-10-29 10:30:10", d, cfg=cfg)                       # the 10:25 bar closed by the clock only
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo's last trade is 10:29:40", r["lag_note"])
        self.assertEqual(at("2026-10-29 10:31", d, cfg=cfg)["status"], "expired")

    def test_config_cutoff(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "09:35"}
        r = at("2026-10-29 09:40", cfg=cfg)
        self.assertEqual(r["status"], "expired")
        cfg = {"no_entries_before": "09:45", "no_entries_after": "14:45"}
        r = at("2026-10-29 10:00", cfg=cfg)
        self.assertEqual(r["trigger"]["time"], "9:45 AM")       # the 9:40 bar, first to close at or after 9:45
        cfg = {"no_entries_before": "09:30", "no_entries_after": "16:00"}
        self.assertEqual(at("2026-10-29 10:00", cfg=cfg)["no_entries_after"], "15:30")   # never past the close

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 09:40", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-29 09:40", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        # IST is UTC+5:30: 9:40 IST is 04:10 UTC
        self.assertEqual(entry.parse_now("2026-10-29 09:40"), DT(2026, 10, 29, 4, 10))
        self.assertEqual(entry.parse_now("2026-10-29T04:10:00Z"), DT(2026, 10, 29, 4, 10))
        self.assertEqual(entry.parse_now("2026-10-29 09:40 IST"), DT(2026, 10, 29, 4, 10))
        self.assertEqual(entry.parse_now("2026-10-29T09:40:00+05:30"), DT(2026, 10, 29, 4, 10))

    def test_or_minutes_30(self):
        r = at("2026-10-29 11:00", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 1175.7)
        self.assertEqual(r["trigger"]["time"], "9:50 AM")

    def test_nse_symbols(self):
        self.assertEqual(entry.nse_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(entry.nse_symbol(" M&M "), "M&M")
        self.assertEqual(entry.nse_symbol("bajaj-auto"), "BAJAJ-AUTO")
        self.assertEqual(entry.yahoo_symbol("360one"), "360ONE.NS")
        for bad in ("RELIANCE.BO", "", "TSLA$", "BRK.B", "A" * 21):
            with self.assertRaises(entry.EntryError):
                entry.nse_symbol(bad)
        with self.assertRaises(entry.EntryError) as cm:
            entry.nse_symbol("500325.BO")
        self.assertIn("NSE only", str(cm.exception))


class OneMinute(unittest.TestCase):
    def c(self):
        return entry.parse_chart(json.dumps(doc(DEMO)), "DEMO")

    def test_wick_and_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-10-29 09:51:30")
        self.assertEqual(r["opening_range"], {"high": 100.5, "low": 99.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 10667})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("9:44 AM", "09:43", 28, 100.62))
        r = entry.evaluate(c, ist("2026-10-29 09:43:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:43")
        self.assertEqual(r["last"]["close"], 100.5)             # the 9:42 bar closed AT the high

    def test_null_bar_tick_and_volume(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:47 AM", 31))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(19, slots)                               # 9:34 came as nulls
        self.assertEqual(slots[-1], 35)                           # 9:51 still forming, 9:51:30 tick dropped
        self.assertEqual(r["forming"]["bar_start"], "09:51")

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "DEMO", "--offline", DEMO])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_ist_has_no_daylight_saving(self):
        for day in (datetime.date(2026, 1, 5), datetime.date(2026, 3, 9), datetime.date(2026, 7, 1),
                    datetime.date(2026, 11, 2), datetime.date(2026, 12, 7)):
            d = shift_to(doc(), day)
            r = entry.evaluate(chart(d), ist("%s 09:40" % day))
            self.assertEqual(r["status"], "triggered", day)
            self.assertEqual(r["trigger"]["time"], "9:40 AM", day)
            self.assertEqual(r["trigger"]["bar_index"], 4, day)
            self.assertEqual(entry.evaluate(chart(d), ist("%s 09:37" % day))["status"], "waiting", day)
            self.assertEqual(ist("%s 09:40" % day), DT.combine(day, datetime.time(4, 10)), day)

    def test_no_early_closes(self):
        self.assertEqual(entry.session(datetime.date(2026, 11, 27)), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertEqual(entry.session(datetime.date(2026, 12, 24)), (datetime.time(9, 15), datetime.time(15, 30)))

    def test_weekend_holiday_and_empty_data(self):
        r = at("2026-10-31 10:30")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertEqual(r["next_open"], "2026-11-02 09:15")
        r = at("2026-10-20 10:30")
        self.assertIn("NSE holiday: Dussehra", r["reason"])
        r = at("2026-11-10 10:30")
        self.assertIn("Diwali Balipratipada", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-11 09:15")
        r = at("2026-11-08 18:00")                                # Muhurat trading Sunday: not modelled, closed
        self.assertEqual(r["phase"], "closed")
        self.assertEqual(at("2026-11-24 10:00")["next_open"], "2026-11-25 09:15")  # Guru Nanak Jayanti
        empty = copy.deepcopy(doc())
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-10-29 09:40", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-10-30 10:15")                                # Yahoo sent yesterday's bars only
        self.assertEqual(r["status"], "no-data")
        r = at("2027-03-01 10:30")
        self.assertFalse(r["calendar_known"])
        self.assertIn("NSE holiday list", "\n".join(entry.describe(r)))

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "RELIANCE")


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and _sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        with open(REL) as fh:
            self.body = fh.read()

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 29), today=datetime.date(2026, 10, 29))
        self.assertEqual(len(c["bars"]), 74)
        self.assertEqual(c["symbol"], "RELIANCE")                  # the NSE symbol, not Yahoo's RELIANCE.NS
        self.assertEqual(self.calls,
                         ["https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 29), today=datetime.date(2026, 10, 29))
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 29), today=datetime.date(2026, 10, 29))
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "RELIANCE", "--now", "2026-10-29 09:40"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_symbol_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("ZZZZ", "5m", datetime.date(2026, 10, 29), today=datetime.date(2026, 10, 29))
        self.assertIn("No data found", str(cm.exception))

    def test_ranges_for_past_sessions(self):
        today = datetime.date(2026, 10, 30)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 29), today, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 12), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 9, 10), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 10, 12), today, "1m")
        self.assertIn("/chart/M%26M.NS?", entry.chart_path("m&m", "1d", "5m"))
        self.assertIn("/chart/BAJAJ-AUTO.NS?", entry.chart_path("BAJAJ-AUTO.NS", "1d", "5m"))


def live_provider(d, lag_until=None, calls=None):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike to 1171.00 (above the high, then it closes under), plus a live tick.
    lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = 1171.00, max(b["h"], 1171.00)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": 1171.0, "h": 1171.0, "l": 1171.0, "c": 1171.0, "v": 0})
        return {"symbol": "RELIANCE", "meta": {}, "bars": bars, "prev_close": 1158.0, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), calls=calls), "2026-10-29 09:32", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "9:40 AM")
        self.assertEqual(fired[0][0]["trigger"]["price"], 1174.3)
        # it looked at 9:32 (9:30 bar spiking to 1171 in progress) and at 9:35:02 (that bar closed 1169.50)
        self.assertEqual([entry.to_ist(c).strftime("%H:%M:%S") for c in calls], ["09:32:00", "09:35:02", "09:40:02"])
        text = "\n".join(lines)
        self.assertIn(" 9:30 bar  closed \u20b91169.50  \u20b91.00 under \u20b91170.50", text)
        self.assertIn(" 9:35 bar  closed \u20b91174.30  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        lag = ist("2026-10-29 09:40:30")
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), lag_until=lag, calls=calls),
                                                   "2026-10-29 09:37", every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times, ["09:37:00", "09:40:02", "09:40:32"])   # 9:40:02: Yahoo still at 9:39:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "9:40 AM")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc()), "2026-10-29 09:00", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn("IST", lines[0])
        self.assertIn(" 9:25 bar  closed \u20b91169.00  opening range set: high \u20b91170.50, low \u20b91156.20", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 5)    # 9:15 ... 9:35, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-10-29 09:31"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(late_breakout(), calls=calls), clock.now, clock.sleep,
                                     lines.append, dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["09:31:00", "09:49:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in (" 9:30", " 9:35", " 9:40", " 9:45"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "10:00 AM")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-29 14:20", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(entry.to_ist(clock.now()).strftime("%H:%M"), "14:45")
        until = DT(2026, 10, 29, 10, 30)
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-29 10:00", every=300,
                                                   until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 10:30", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-10-31 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-11-10 10:00")
        self.assertIn("NSE holiday", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-10-29 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:40", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE  Thu 2026-10-29 09:40 IST", text)
        self.assertIn("TRIGGERED 9:40 AM at \u20b91174.30", text)
        self.assertIn("/trader-exit", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            e = json.load(fh)
        want = {"created_by": "trader-entry", "symbol": "RELIANCE", "setup": "orb", "side": "buy",
                "date": "2026-10-29", "status": "triggered", "time": "9:40 AM", "bar_start": "09:35",
                "bar_index": 4, "price": 1174.3, "level": 1170.5, "interval": "5m",
                "opening_range": {"high": 1170.5, "low": 1156.2, "minutes": 15},
                "source": "yahoo", "checked_at": "2026-10-29T04:10:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:37", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:37", "--write", "e.json"])
        with open("e.json") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", text)

    def test_offline_gz_folder_suffix_and_mismatch(self):
        os.mkdir("charts")
        with open(REL, "rb") as src, gzip.open(os.path.join("charts", "RELIANCE.json.gz"), "wb") as dst:
            dst.write(src.read())
        code, text, _ = run(["check", "reliance.ns", "--offline", "charts", "--now", "2026-10-29 09:40"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE  Thu", text)
        code, _, err = run(["check", "INFY", "--offline", REL])
        self.assertEqual(code, 1)
        self.assertIn("holds RELIANCE, not INFY", err)
        code, _, err = run(["check", "RELIANCE.BO", "--offline", REL])
        self.assertEqual(code, 1)
        self.assertIn("NSE only", err)

    def test_both_sides_and_flag_errors(self):
        # --side short watches the range LOW instead of the high. RELIANCE broke out that
        # day and never closed under 1156.20, so the short side never triggered.
        code, out, _ = run(["check", "RELIANCE", "--offline", REL, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("opening-range breakdown", out)
        self.assertIn("no close below the range low ₹1156.20", out)
        # the same bars on the long side are a trigger, off the range HIGH
        code, out, _ = run(["check", "RELIANCE", "--offline", REL])
        self.assertEqual(code, 0)
        self.assertIn("closed above the range high ₹1170.50", out)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--level", "1100"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--setup", "level"])
        self.assertIn("needs --level", err)

    def test_config_is_read(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "09:35"}, fh)
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:40"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "RELIANCE", "--offline", REL])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:20", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn(" 9:35 bar  closed \u20b91174.30  TRIGGERED", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 4)
        code, text, _ = run(["watch", "RELIANCE", "--offline", REL, "--now", "2026-10-29 09:20", "--until", "09:35",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(REL, os.path.join("charts", "RELIANCE.json"))
        shutil.copy(DEMO, os.path.join("charts", "DEMO.json"))
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [
                {"symbol": "RELIANCE", "setups": ["orb"], "or_high": 1170.5, "or_low": 1156.2},
                {"symbol": "ZZZZ", "setups": ["orb"]}, {"symbol": "DEMO", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-29 09:40"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("RELIANCE    triggered  9:40 AM   \u20b91174.30    \u20b91170.50    \u20b91174.30    +0.32%"), rows[1])
        self.assertIn("ZZZZ        error: no offline file", rows[2])
        self.assertIn("DEMO        error: DEMO has 1m bars", rows[3])
        self.assertIn("1 of 3 triggered", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-29 09:37", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /trader-scanner first", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "day trading loses most people money", "/trader-exit", "--side sell",
                     "in the background", "/loop", "nothing was placed", "9:40 am", "\u20b91174.30", "bar 4",
                     "ist", "nse", "9:15"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "new york", "nyse", " et "):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()


class BreakdownTrigger(unittest.TestCase):
    """--side short watches the opening range's LOW: the mirror of the breakout."""

    def bars(self, closes):
        """A v8 chart body of 5-minute bars whose highs and lows bracket each close."""
        base = 1793245500          # 2026-10-29 09:15 IST (03:45 UTC) in epoch seconds
        return {"chart": {"result": [{
            "meta": {"symbol": "TEST", "chartPreviousClose": closes[0], "regularMarketTime": base + 300 * len(closes)},
            "timestamp": [base + 300 * i for i in range(len(closes))],
            "indicators": {"quote": [{"open": list(closes),
                                      "high": [c + 1 for c in closes],
                                      "low": [c - 1 for c in closes],
                                      "close": list(closes),
                                      "volume": [10000] * len(closes)}]}}]}}

    def at(self, closes, side, hhmm="2026-10-29 10:30"):
        return entry.evaluate(chart(self.bars(closes), "TEST"), ist(hhmm), side=side)

    def test_a_close_under_the_range_low_triggers_a_short(self):
        # the range is the first three bars: low 99, high 104. The fifth closes at 97.
        res = self.at([100, 103, 100, 99.5, 97.0], "sell")
        self.assertEqual(res["status"], "triggered")
        self.assertEqual(res["side"], "sell")
        self.assertEqual(res["level"], 99.0)                 # the range LOW, not its high
        self.assertEqual(res["trigger"]["price"], 97.0)

    def test_the_same_bars_do_not_trigger_a_long(self):
        res = self.at([100, 103, 100, 99.5, 97.0], "buy")
        self.assertNotEqual(res["status"], "triggered")
        self.assertEqual(res["level"], 104.0)                # the range HIGH

    def test_a_close_inside_the_range_is_not_a_breakdown(self):
        res = self.at([100, 103, 100, 99.5, 99.2], "sell")
        self.assertNotEqual(res["status"], "triggered")

    def test_a_wick_under_the_low_is_not_a_trigger(self):
        # the close holds at 99.5; only the bar's low reaches 98.5, under the range low 99
        res = self.at([100, 103, 100, 99.5, 99.5], "sell")
        self.assertNotEqual(res["status"], "triggered")
