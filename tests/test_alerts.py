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

from _load import load, ROOT, FIX

alerts = load("trader-alerts", "alerts")

# Real Yahoo .NS chart responses (range=1d, interval=1d), fetched 2026-11-03 after the close
QDIR = os.path.join(FIX, "trader-alerts")
PLAN = os.path.join(QDIR, "plan-reliance.json")
DT = datetime.datetime
UA = "Mozilla/5.0 (compatible; day-trader-skill-nse/1.0)"

# NSE equity segment trading holidays, 2026 (weekdays)
NSE_2026 = ["2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
            "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
            "2026-11-10", "2026-11-24", "2026-12-25"]
RS = "₹"


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules(cfg=None):
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh), cfg)


class Calendar(unittest.TestCase):
    def test_holidays_match_the_published_list(self):
        self.assertEqual(sorted(alerts.NSE_HOLIDAYS), NSE_2026)
        for d in alerts.NSE_HOLIDAYS:
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5, d)
        self.assertEqual(alerts.CALENDAR_YEARS, (2026,))
        self.assertFalse(hasattr(alerts, "NYSE_EARLY_CLOSE"))

    def test_ist_has_no_daylight_saving(self):
        self.assertEqual(alerts.to_ist(DT(2026, 1, 15, 3, 45)), DT(2026, 1, 15, 9, 15))
        self.assertEqual(alerts.to_ist(DT(2026, 7, 15, 3, 45)), DT(2026, 7, 15, 9, 15))
        self.assertEqual(alerts.ist_to_utc(DT(2026, 11, 3, 15, 30)), DT(2026, 11, 3, 10, 0))

    def test_parse_now(self):
        self.assertEqual(ist("2026-11-03T09:15"), DT(2026, 11, 3, 3, 45))
        self.assertEqual(ist("2026-11-03 15:30"), DT(2026, 11, 3, 10, 0))
        self.assertEqual(ist("2026-11-03T09:59:39Z"), DT(2026, 11, 3, 9, 59, 39))
        self.assertEqual(ist("2026-11-03T15:29:39+05:30"), DT(2026, 11, 3, 9, 59, 39))
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_market_status(self):
        st = alerts.market_status(ist("2026-11-03 10:00"))
        self.assertTrue(st["open"])
        self.assertEqual((st["opens"], st["closes"], st["now_ist"]), ("09:15", "15:30", "2026-11-03 10:00"))
        self.assertFalse(alerts.market_status(ist("2026-11-03 09:14"))["open"])
        self.assertTrue(alerts.market_status(ist("2026-11-03 09:15"))["open"])
        self.assertEqual(alerts.market_status(ist("2026-11-03 09:14"))["next_open"], "2026-11-03 09:15")
        self.assertTrue(alerts.market_status(ist("2026-11-03 15:29"))["open"])
        after = alerts.market_status(ist("2026-11-03 15:30"))
        self.assertFalse(after["open"])
        self.assertEqual((after["reason"], after["next_open"]), ("after the close", "2026-11-04 09:15"))
        diwali = alerts.market_status(ist("2026-11-10 11:00"))
        self.assertFalse(diwali["open"])
        self.assertIn("NSE holiday: Diwali Balipratipada", diwali["reason"])
        self.assertEqual(diwali["next_open"], "2026-11-11 09:15")
        # Friday 2026-11-06 after the close: the weekend, then Mon 11-09 (Tue 11-10 is Diwali)
        self.assertEqual(alerts.market_status(ist("2026-11-06 16:00"))["next_open"], "2026-11-09 09:15")
        # Muhurat trading (Sunday 2026-11-08) is not modelled: weekends stay closed
        self.assertEqual(alerts.market_status(ist("2026-11-08 18:30"))["reason"], "weekend")
        xmas = alerts.market_status(ist("2026-12-25 10:00"))
        self.assertEqual(xmas["next_open"], "2026-12-28 09:15")
        self.assertFalse(alerts.market_status(ist("2027-03-01 10:00"))["calendar_known"])


class Symbols(unittest.TestCase):
    def test_nse_symbols(self):
        self.assertEqual(alerts.nse_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(alerts.nse_symbol("M&M"), "M&M")
        self.assertEqual(alerts.nse_symbol("bajaj-auto"), "BAJAJ-AUTO")
        self.assertEqual(alerts.nse_symbol("360ONE"), "360ONE")
        for bad in ("RELIANCE.BO", "", "TSLA US", "BRK.B", "-X", "A" * 21):
            with self.assertRaises(alerts.AlertError):
                alerts.nse_symbol(bad)

    def test_rupees_with_indian_grouping(self):
        self.assertEqual(alerts.inr(1307.9), RS + "1,307.90")
        self.assertEqual(alerts.inr(725.5), RS + "725.50")
        self.assertEqual(alerts.inr(100000), RS + "1,00,000.00")
        self.assertEqual(alerts.inr(12345678.5), RS + "1,23,45,678.50")
        self.assertEqual(alerts.inr(-1000), "-" + RS + "1,000.00")


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "RELIANCE.NS")
        self.assertEqual((q["symbol"], q["price"], q["prev_close"], q["currency"]), ("RELIANCE", 1307.9, 1300.0, "INR"))
        self.assertEqual(round(q["open"], 2), 1287.9)
        self.assertEqual(q["time"], "2026-11-03T09:59:39Z")          # 15:29:39 IST
        with open(os.path.join(QDIR, "yahoo-M&M.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "M&M")
        self.assertEqual((q["price"], q["prev_close"]), (2908.0, 2868.8))

    def test_urls_and_user_agent(self):
        self.assertEqual(alerts.UA, UA)
        self.assertEqual(alerts.yahoo_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d")
        self.assertEqual(alerts.yahoo_url("M&M"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS?range=1d&interval=1d")
        self.assertIn("//query1.finance.yahoo.com/", alerts.yahoo_url("INFY", alerts.YAHOO_HOSTS[1]))
        self.assertFalse(hasattr(alerts, "stooq_url"))

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes(["RELIANCE", "infy.ns", "ZZZZ", "NOPE", "TCS.BO"], QDIR)
        self.assertEqual(sorted(q), ["INFY", "RELIANCE"])
        self.assertEqual(q["INFY"]["price"], 1148.1)
        self.assertEqual(len(errors), 3)
        self.assertIn("No data found", errors[0])
        self.assertIn("no offline quote file", errors[1])
        self.assertIn("NSE only", errors[2])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-RELIANCE.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-RELIANCE.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["RELIANCE"], d)
            self.assertEqual((q["RELIANCE"]["price"], errors), (1307.9, []))
        finally:
            shutil.rmtree(d)


class Bars(unittest.TestCase):
    """alerts.py bars: today's 5-minute highs and lows, read-only (the autopilot's touch check)."""

    FX = os.path.join(FIX, "trader-entry", "RELIANCE-2026-10-29-5m.json")

    def setUp(self):
        self.d = tempfile.mkdtemp()
        shutil.copy(self.FX, os.path.join(self.d, "bars-RELIANCE.json"))

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_parse_real_bars(self):
        with open(self.FX) as fh:
            bars = alerts.parse_bars(fh.read(), "reliance.ns")
        day = [b for b in bars if b["start"].startswith("2026-10-29")]
        self.assertEqual((day[0]["start"], day[-1]["start"], len(day)), ("2026-10-29 09:15", "2026-10-29 15:25", 74))
        b955 = [b for b in day if b["start"] == "2026-10-29 09:55"][0]
        self.assertEqual((b955["high"], b955["low"], b955["close"]), (1182.0, 1171.0, 1173.8))

    def test_url_interval(self):
        self.assertEqual(alerts.yahoo_url("M&M", interval="5m"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/M%26M.NS?range=1d&interval=5m")

    def test_replay_shows_only_closed_bars_of_that_day(self):
        code, out, err = run(["bars", "RELIANCE", "--offline", self.d, "--now", "2026-10-29 10:02", "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual((doc["date"], doc["interval"]), ("2026-10-29", "5m"))
        starts = [b["start"][11:] for b in doc["bars"]["RELIANCE"]]
        self.assertEqual((starts[0], starts[-1]), ("09:15", "09:55"))   # the 10:00 bar is still forming at 10:02

    def test_one_minute_replay_cut(self):
        code, out, _ = run(["bars", "RELIANCE", "--interval", "1m", "--offline", self.d, "--now", "2026-10-29 10:02",
                            "--json"])
        doc = json.loads(out)
        self.assertEqual(doc["interval"], "1m")
        self.assertEqual(doc["bars"]["RELIANCE"][-1]["start"], "2026-10-29 10:00")   # closed by 10:01, so shown

    def test_other_day_has_no_bars_and_missing_file_is_an_error(self):
        code, out, _ = run(["bars", "RELIANCE", "TCS", "--offline", self.d, "--now", "2026-10-30 10:00", "--json"])
        doc = json.loads(out)
        self.assertEqual(doc["bars"]["RELIANCE"], [])
        self.assertIn("no offline bars file", doc["errors"][0])
        code, out, _ = run(["bars", "RELIANCE", "--offline", self.d, "--now", "2026-10-29 10:02"])
        self.assertIn("RELIANCE   9 bars today, high \u20b91,182.00", out)
        self.assertEqual(run(["bars"])[0], 1)


class FakeNet(object):
    """Stands in for urllib: answers each request with the next status code."""

    def __init__(self, codes, body=None):
        self.codes, self.calls, self.sleeps = list(codes), [], []
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json")) as fh:
            self.body = body or fh.read()

    def urlopen(self, req, timeout=None):
        self.calls.append((req.full_url.split("/")[2], req.get_header("User-agent")))
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, fp)
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def net(self, codes):
        f = FakeNet(codes)
        alerts._urlopen = f.urlopen
        alerts._sleep = f.sleeps.append
        return f

    def hosts(self, f):
        return [h.split(".")[0] for h, _ in f.calls]

    def test_query2_first_with_the_user_agent(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual((q["RELIANCE"]["price"], errors), (1307.9, []))
        self.assertEqual(f.calls, [("query2.finance.yahoo.com", UA)])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes(["RELIANCE"])[0]["RELIANCE"]["price"], 1307.9)
        self.assertEqual(self.hosts(f), ["query2", "query2"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query1_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        f = self.net([503, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query1"])
        f = self.net([500, 502])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual(q, {})
        self.assertIn("query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS answered HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes(["NOPE"])
        self.assertIn("No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])
        f = self.net([403])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])

    def test_a_pause_between_symbols_and_no_request_for_a_bad_one(self):
        f = self.net([200, 200])
        q, errors = alerts.get_quotes(["RELIANCE", "TCS.BO", "INFY"])
        self.assertEqual(f.sleeps, [alerts.PAUSE])
        self.assertEqual(len(f.calls), 2)
        self.assertIn("NSE only", errors[0])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["RELIANCE", "HDFCBANK", "INFY", "M&M"], QDIR)
        self.now = ist("2026-11-03 10:00")
        self.open = alerts.market_status(self.now)

    def at(self, text, rules, state=None, quotes=None, held=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now),
                               state or {}, now, held)

    def test_an_alert_level_rings_once(self):
        rules = [{"id": "infy-1140", "symbol": "INFY", "type": "above", "price": 1140}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertIn("INFY is %s1,148.10: at or above your alert level %s1,140.00" % (RS, RS), rings[0]["message"])
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])

    def test_stop_and_rearm(self):
        rules = [{"id": "infy-stop", "symbol": "INFY", "type": "stop", "price": 1150, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("your stop %s1,150.00" % RS, rings[0]["message"])
        self.assertIn("SL-M order at the broker", rings[0]["message"])
        self.q["INFY"]["price"] = 1155.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("infy-stop", st["rung"])
        self.q["INFY"]["price"] = 1149.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(len(rings), 1)

    def test_pct_move_from_previous_close(self):
        big = [{"id": "infy-3", "symbol": "INFY", "type": "pct_move", "pct": 3}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        small = [{"id": "infy-1", "symbol": "INFY", "type": "pct_move", "pct": 1, "direction": "down"}]
        rings, st = alerts.evaluate(small, self.q, self.open, {}, self.now)
        self.assertIn("-1.11%", rings[0]["message"])
        self.assertEqual(alerts.evaluate(small, self.q, self.open, st, self.now)[0], [])
        up = [{"id": "infy-up", "symbol": "INFY", "type": "pct_move", "pct": 1, "direction": "up"}]
        self.assertEqual(alerts.evaluate(up, self.q, self.open, {}, self.now)[0], [])
        hdfc = [{"id": "hdfc-3", "symbol": "HDFCBANK", "type": "pct_move", "pct": 3}]
        rings, _ = self.at("2026-11-03 15:29", hdfc)
        self.assertIn("HDFCBANK moved +3.64%% to %s725.50 (from %s700.00)" % (RS, RS), rings[0]["message"])

    def test_opening_bell_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        early = ist("2026-11-03 09:00")
        self.assertEqual(alerts.evaluate(rules, {}, alerts.market_status(early), {}, early)[0], [])
        rings, st = alerts.evaluate(rules, {}, self.open, {}, self.now)
        self.assertIn("Opening bell: NSE is open (2026-11-03 10:00 IST), regular session until 15:30.",
                      rings[0]["message"])
        self.assertEqual(alerts.evaluate(rules, {}, self.open, st, ist("2026-11-03 11:00"))[0], [])
        nxt = ist("2026-11-04 09:16")
        self.assertEqual(len(alerts.evaluate(rules, {}, alerts.market_status(nxt), st, nxt)[0]), 1)

    def test_the_plan_gives_stop_target_flat_by_and_square_off(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("reliance-plan-stop", "stop"), ("reliance-plan-target", "target"),
                          ("reliance-flat-by", "time"), ("reliance-square-off", "square_off")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (1295.5, 1304.5))
        self.assertEqual((rules[2]["at"], rules[2]["label"]), ("15:10", "flat by close"))
        self.assertNotIn("before_close", rules[2])
        self.assertEqual(rules[3]["broker"], "zerodha")
        self.assertTrue(all(r["date"] == "2026-11-03" for r in rules))
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["price"] = 1300.0
        rings, st = self.at("2026-11-03 10:00", rules, quotes=q)
        self.assertEqual(rings, [])
        q["RELIANCE"]["price"] = 1295.1
        rings, st = self.at("2026-11-03 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-stop"])
        self.assertIn("RELIANCE is %s1,295.10: at or below your stop %s1,295.50 (plan stop: opening-range midpoint)"
                      % (RS, RS), rings[0]["message"])
        rings, st = self.at("2026-11-03 15:00", rules, st)      # the real 15:29 quote, 1307.90
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-target"])
        self.assertIn("If it is only watched, run /trader-broker flatten (cancels the stop, then sells).",
                      rings[0]["message"])

    def test_flat_by_and_the_config(self):
        rules = plan_rules({"broker": "angelone", "flat_by": "14:00"})
        self.assertEqual((rules[2]["at"], rules[3]["broker"]), ("15:10", "angelone"))   # the plan's flat_by wins
        with open(PLAN) as fh:
            plan = json.load(fh)
        del plan["flat_by"]
        self.assertEqual(alerts.rules_from_plan(plan, {"flat_by": "14:00"})[2]["at"], "14:00")
        self.assertEqual(alerts.rules_from_plan(plan)[2]["at"], "15:10")
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan(plan, {"broker": "alpaca"})

    def test_flat_by_close_rings_once_per_trading_day(self):
        rules = plan_rules()[2:3]
        self.assertEqual(self.at("2026-11-03 15:09", rules)[0], [])
        rings, st = self.at("2026-11-03 15:10", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("Flat by close: it is 15:10 IST (set for 15:10), the market closes at 15:30", rings[0]["message"])
        self.assertIn("trader-broker flatten --symbol RELIANCE builds the ticket (cancel the stop, then sell)",
                      rings[0]["message"])
        self.assertEqual(self.at("2026-11-03 15:20", rules, st)[0], [])
        rings, _ = self.at("2026-11-03 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-11-04 15:11", rules, st)[0], [])     # plan rules: their day only

    def test_a_time_alert_skips_weekends_and_holidays(self):
        rule = [{"id": "t", "type": "time", "at": "15:10", "label": "check"}]
        self.assertEqual(self.at("2026-11-07 15:11", rule)[0], [])         # Saturday
        self.assertEqual(self.at("2026-11-10 15:11", rule)[0], [])         # Diwali Balipratipada
        rings, st = self.at("2026-11-11 15:10", rule)
        self.assertEqual(len(rings), 1)
        self.assertEqual(st["rung"]["t"], "2026-11-11T09:40:00Z")

    def test_square_off_warning_five_minutes_before_the_broker(self):
        self.assertEqual(alerts.square_off_warning("zerodha"), (datetime.time(15, 20), datetime.time(15, 15)))
        self.assertEqual(alerts.square_off_warning("Angel One"), (datetime.time(15, 15), datetime.time(15, 10)))
        z = plan_rules()[3:]
        self.assertEqual(self.at("2026-11-03 15:14", z, held={"RELIANCE"})[0], [])
        rings, st = self.at("2026-11-03 15:15", z, held={"RELIANCE"})
        self.assertEqual(len(rings), 1)
        self.assertIn("Square-off in 5 minutes: Zerodha auto squares off intraday (MIS) positions at 15:20 IST",
                      rings[0]["message"])
        self.assertIn("RELIANCE is still open", rings[0]["message"])
        self.assertIn("trader-broker flatten --symbol RELIANCE", rings[0]["message"])
        self.assertEqual(self.at("2026-11-03 15:16", z, st, held={"RELIANCE"})[0], [])
        a = plan_rules({"broker": "angelone"})[3:]
        self.assertEqual(self.at("2026-11-03 15:09", a, held={"RELIANCE"})[0], [])
        self.assertIn("Angel One auto squares off", self.at("2026-11-03 15:10", a, held={"RELIANCE"})[0][0]["message"])

    def test_square_off_stays_quiet_when_flat(self):
        z = plan_rules()[3:]
        rings, st = self.at("2026-11-03 15:15", z, held=set())
        self.assertEqual(rings, [])
        self.assertNotIn("reliance-square-off", st["rung"])
        rings, _ = self.at("2026-11-03 15:17", z, st, held={"RELIANCE"})    # a fill landed: still rings
        self.assertEqual(len(rings), 1)
        rings, _ = self.at("2026-11-03 15:15", z, held=None)                 # no portfolio: rings, worded "may"
        self.assertIn("RELIANCE may still be open", rings[0]["message"])

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("reliance-")], plan_rules())

    def test_bad_rules(self):
        for bad in ({"id": "x", "type": "moon"}, {"id": "x", "type": "above", "symbol": "INFY"},
                    {"id": "x", "type": "time", "at": "3pm"}, {"id": "x", "type": "square_off"},
                    {"id": "x", "type": "square_off", "symbol": "INFY", "broker": "alpaca"},
                    {"id": "x", "type": "above", "symbol": "AAPL.BO", "price": 1}):
            with self.assertRaises(alerts.AlertError):
                alerts.evaluate([bad], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1295.5})


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")
        self.pf = os.path.join(self.d, "portfolio.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--portfolio", self.pf, "--offline", QDIR, "--now", now])

    def test_a_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none")])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-square-off", text)
        self.assertIn("Zerodha auto squares off MIS positions at 15:20 IST: the square-off alert rings at 15:15",
                      text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)
        with open(self.pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"ticker": "RELIANCE", "shares": 20}]}, fh)

        code, text, _ = self.check("2026-11-03 10:00")
        self.assertEqual(code, 0)
        self.assertIn("NSE OPEN (2026-11-03 10:00 IST, regular session).", text)
        self.assertIn("ALERT Opening bell", text)
        self.assertIn("ALERT RELIANCE is %s1,307.90: at or above your target %s1,304.50" % (RS, RS), text)
        self.assertIn("not an order", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"RELIANCE": 1307.9})
        self.assertEqual(doc["detail"]["RELIANCE"]["time"], "2026-11-03T09:59:39Z")
        self.assertEqual(doc["detail"]["RELIANCE"]["fetched"], "2026-11-03T04:30:00Z")
        code, text, _ = self.check("2026-11-03 10:05")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 5 rule(s) watched today, RELIANCE %s1,307.90." % RS, text)

        code, text, _ = self.check("2026-11-03 15:10")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by close", text)
        code, text, _ = self.check("2026-11-03 15:15")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Square-off in 5 minutes", text)
        self.assertEqual(self.check("2026-11-03 15:17")[0], 1)
        code, text, _ = self.check("2026-11-04 09:20")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-04 09:25")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state])
        with open(self.rules) as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "reliance-flat-by", "reliance-plan-stop",
                                       "reliance-plan-target", "reliance-square-off"])
        with open(self.state) as fh:
            self.assertNotIn("reliance-flat-by", json.load(fh)["rung"])

    def test_the_broker_comes_from_the_config(self):
        cfg = os.path.join(self.d, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"broker": "angelone"}, fh)
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", cfg])
        self.assertIn("Angel One auto squares off MIS positions at 15:15 IST: the square-off alert rings at 15:10",
                      text)
        code, text, _ = run(["add", "--rules", self.rules, "--square-off", "--symbol", "infy.ns", "--config", cfg])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--square-off", "--symbol", "M&M", "--broker", "zerodha",
                             "--config", cfg])
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[-2], {"id": "infy-square-off", "symbol": "INFY", "type": "square_off",
                                     "broker": "angelone"})
        self.assertEqual(rules[-1]["broker"], "zerodha")
        self.assertEqual(run(["add", "--rules", self.rules, "--square-off"])[0], 1)

    def test_watchlist_evening_and_morning(self):
        for args in (["--opening-bell"], ["--symbol", "INFY", "--above", "1200"],
                     ["--symbol", "HDFCBANK", "--stop", "650"]):
            code, _, _ = run(["add", "--rules", self.rules] + args)
            self.assertEqual(code, 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "INFY", "--above", "1200"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        code, text, _ = self.check("2026-11-03 16:00")
        self.assertEqual(code, 1)
        self.assertIn("NSE closed (2026-11-03 16:00 IST, after the close).", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"INFY": 1148.1, "HDFCBANK": 725.5})

        code, text, _ = self.check("2026-11-04 09:16")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        self.assertEqual(self.check("2026-11-04 09:45")[0], 1)

        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--at", "15:10", "--label", "flat by close",
                             "--symbol", "reliance"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "RELIANCE.NS", "--target", "1304.5",
                             "--date", "2026-11-03"])
        self.assertEqual(code, 0)
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1510", "type": "time", "at": "15:10", "label": "flat by close",
                                    "symbol": "RELIANCE"})
        self.assertEqual(rules[1], {"id": "reliance-target-1304.50", "symbol": "RELIANCE", "type": "target",
                                    "price": 1304.5, "date": "2026-11-03"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "3pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "TCS.BO", "--above", "1"])
        self.assertEqual(code, 1)
        self.assertIn("NSE only", err)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "RELIANCE", "M&M", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-11-03T10:00:00Z"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE   %s1,307.90 (+0.61%% vs prev close %s1,300.00)" % (RS, RS), text)
        self.assertIn("M&M        %s2,908.00" % RS, text)
        self.assertIn("delayed", text)
        run(["quote", "INFY", "--offline", QDIR, "--write", self.prices])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["INFY", "M&M", "RELIANCE"])
        self.assertEqual(doc["detail"]["M&M"]["prev_close"], 2868.8)
        self.assertEqual(doc["detail"]["M&M"]["fetched"], "2026-11-03T10:00:00Z")

    def test_quote_holdings_from_portfolio_and_plan(self):
        with open(self.pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"ticker": "INFY", "shares": 1}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", self.pf, "--plan", PLAN, "--offline", QDIR,
                             "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["INFY", "RELIANCE"])

    def test_clock_exit_codes(self):
        code, text, _ = run(["clock", "--now", "2026-11-03 10:15"])
        self.assertEqual(code, 0)
        self.assertIn("IST 2026-11-03 10:15 (Tuesday): NSE OPEN, regular session.", text)
        self.assertIn("Today's session: 09:15 to 15:30 IST.", text)
        code, text, _ = run(["clock", "--now", "2026-12-25T10:00"])
        self.assertEqual(code, 1)
        self.assertIn("Christmas", text)
        self.assertIn("Next open: 2026-12-28 09:15 IST.", text)
        code, text, _ = run(["clock", "--now", "2027-01-04 10:00"])
        self.assertIn("Holiday list covers 2026 only", text)

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


if __name__ == "__main__":
    unittest.main()
