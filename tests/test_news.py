import datetime
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
# The fixtures are real: NSE's announcements and board-meetings RSS and EQUITY_L.csv (cut to a few
# companies), Yahoo Finance's INFY.NS / TCS.NS headline RSS and INFY.NS / RELIANCE.NS chart meta,
# all fetched on Tue 2026-11-03 after the close. Yahoo's RELIANCE.NS feed was empty that day.
NOW = "2026-11-03 16:30"  # IST


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


def ann():
    return news.parse_nse_rss(raw("nse-announcements.xml"), "announcements")


def board():
    return news.parse_nse_rss(raw("nse-board-meetings.xml"), "board")


class Time(unittest.TestCase):
    def test_ist_is_utc_plus_530_all_year(self):
        for month in (1, 7):
            utc = datetime.datetime(2026, month, 15, 3, 45, tzinfo=datetime.timezone.utc)
            self.assertEqual(news.to_ist(utc), datetime.datetime(2026, month, 15, 9, 15))
        self.assertEqual(news.ist_to_utc(datetime.datetime(2026, 11, 3, 9, 15)).isoformat(),
                         "2026-11-03T03:45:00+00:00")

    def test_parse_time(self):
        self.assertEqual(news.parse_time("Thu, 29 Oct 2026 14:01:10 +0000").hour, 14)
        # NSE's pubDate is IST
        self.assertEqual(news.parse_time("03-Nov-2026 16:19:41").isoformat(), "2026-11-03T10:49:41+00:00")
        # a bare ISO time (like --now) is IST; an offset wins
        self.assertEqual(news.parse_time("2026-11-03 09:15").isoformat(), "2026-11-03T03:45:00+00:00")
        self.assertEqual(news.parse_time("2026-11-03T03:45:00Z").hour, 3)
        self.assertIsNone(news.parse_time("soon"))


class Symbols(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(news.normalize_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(news.normalize_symbol("m&m"), "M&M")
        self.assertEqual(news.normalize_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO")
        with self.assertRaises(news.NewsError):
            news.normalize_symbol("RELIANCE.BO")  # NSE only
        with self.assertRaises(news.NewsError):
            news.normalize_symbol("BRK.B")

    def test_yahoo_symbol_is_quoted(self):
        self.assertEqual(news.yahoo_symbol("INFY"), "INFY.NS")
        self.assertEqual(news.yahoo_symbol("M&M"), "M%26M.NS")
        self.assertIn("s=M%26M.NS&region=IN&lang=en-IN", news.YAHOO % news.yahoo_symbol("M&M"))

    def test_equity_list_and_names(self):
        names = news.parse_equity_list(raw("EQUITY_L.csv"))
        self.assertEqual(names["M&M"], "Mahindra & Mahindra Limited")
        self.assertEqual(names["IDEA"], "Vodafone Idea Limited")
        self.assertEqual(news.norm_name("Mahindra &amp; Mahindra Limited"), "mahindra and mahindra")
        self.assertEqual(news.norm_name("State Bank Of India"), news.norm_name("State Bank of India"))
        self.assertEqual(news.norm_name("Narayana Hrudayalaya Ltd."), "narayana hrudayalaya")
        self.assertEqual(news.norm_name("The Phoenix Mills Limited"), "phoenix mills")
        self.assertEqual(news.short_name("Infosys Limited"), "Infosys")

    def test_names_cache_is_used_for_a_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "cache", "EQUITY_L.csv")
            os.makedirs(os.path.dirname(cache))
            with open(cache, "wb") as fh:
                fh.write(raw("EQUITY_L.csv"))

            class NoNet(news.Fetcher):
                def get(self, url, name):
                    raise news.NewsError("no network in tests")

            self.assertIn("INFY", news.company_names(NoNet(), cache))
            with self.assertRaises(news.NewsError):  # a day old: it goes back to NSE
                news.company_names(NoNet(), cache, now=time.time() + 25 * 3600)


class Feeds(unittest.TestCase):
    def test_yahoo_rss(self):
        items = news.parse_rss(raw("yahoo-INFY.xml"), "INFY")
        self.assertEqual(len(items), 14)
        first = items[0]
        self.assertTrue(first["title"].startswith("Cognizant Sinks as Bookings Drop 7%"))
        self.assertEqual(first["time"], datetime.datetime(2026, 10, 29, 14, 1, 10,
                                                          tzinfo=datetime.timezone.utc))
        self.assertTrue(first["id"].startswith("rss:"))
        self.assertEqual(news.parse_rss(raw("yahoo-RELIANCE.xml"), "RELIANCE"), [])  # an empty feed is fine

    def test_nse_announcements(self):
        entries = ann()
        self.assertEqual(len(entries), 14)
        idea = [e for e in entries if e["company"] == "Vodafone Idea Limited"][0]
        self.assertEqual(idea["subject"], "Board Meeting Intimation")
        self.assertEqual(idea["meeting"], datetime.date(2026, 11, 12))
        self.assertEqual(news.to_ist(idea["time"]), datetime.datetime(2026, 11, 3, 15, 48, 26))
        self.assertNotIn("|SUBJECT", idea["text"])
        mm = entries[0]
        self.assertEqual(mm["company"], "Mahindra & Mahindra Limited")  # &amp; unescaped
        iti = [e for e in entries if e["company"] == "ITI Limited"][0]
        self.assertEqual(iti["link"], "")  # NSE's own query has no link

    def test_nse_board_meetings(self):
        entries = board()
        self.assertEqual([e["meeting"] for e in entries], [datetime.date(2026, 11, 12)] * 4)
        self.assertEqual(entries[1]["text"], "Financial Results")
        self.assertEqual(entries[0]["text"], "Board Meeting Intimation")

    def test_nse_tags(self):
        by = {}
        for e in ann():
            by.setdefault(e["company"], []).append(news.nse_tags(e))
        self.assertIn(["results", "board meeting"], by["Vodafone Idea Limited"])
        self.assertIn(["results"], by["State Bank of India"])            # outcome: the results themselves
        self.assertIn(["merger"], by["Tata Steel Limited"])
        self.assertIn(["order win"], by["RITES Limited"])
        self.assertIn(["exchange query"], by["ITI Limited"])             # spurt in volume
        self.assertIn(["exchange query"], by["Vedanta Limited"])         # news verification

    def test_company_names_not_substrings(self):
        names = news.parse_equity_list(raw("EQUITY_L.csv"))
        by_name = {news.norm_name(names["INFY"]): "INFY", news.norm_name(names["SBIN"]): "SBIN"}
        items = news.nse_items(ann(), by_name)
        self.assertEqual({i["ticker"] for i in items}, {"SBIN"})  # "Slone Infosystems" is not Infosys
        self.assertEqual(len(items), 5)                           # both spellings of State Bank
        self.assertTrue(all(i["id"].startswith("nse-announcements:") for i in items))
        results = [i for i in items if "Outcome of Board Meeting" in i["title"]][0]
        self.assertTrue(results["important"])


class Tagging(unittest.TestCase):
    def test_headline_touches_and_tags(self):
        items = news.parse_rss(raw("yahoo-TCS.xml"), "TCS")
        watch = {"TCS": "Tata Consultancy Services Limited", "INFY": "Infosys Limited"}
        news.tag(items, watch)
        def by(start):
            return [i for i in items if i["title"].startswith(start)][0]
        infy = by("Infosys sees no material impact")
        self.assertEqual(infy["touches"], ["INFY", "TCS"])  # in the TCS feed, names Infosys
        rise = by("TCS shares rise as AI revenue")
        self.assertEqual(rise["touches"], ["TCS"])

    def test_earnings_headlines_tagged(self):
        items = news.tag(news.parse_rss(raw("yahoo-INFY.xml"), "INFY"), {"INFY": "Infosys Limited"})
        q2 = [i for i in items if i["title"] == "Infosys Q2 Earnings Call Highlights"][0]
        self.assertIn("earnings", q2["tags"])

    def test_day_trading_tags(self):
        items = [{"id": "x", "source": "Yahoo Finance", "kind": "headline", "ticker": "IDEA",
                  "title": "Vodafone Idea hits upper circuit after SEBI nod; FIIs buy as Nifty rises",
                  "summary": "", "link": "", "time": None}]
        tags = news.tag(items, {"IDEA": None})[0]["tags"]
        for t in ("halt", "regulatory", "macro"):
            self.assertIn(t, tags)

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_rss(raw("yahoo-INFY.xml"), "INFY")
        b = news.parse_rss(raw("yahoo-INFY.xml"), "TCS")
        items = news.dedupe(news.tag(a + b, {"INFY": None, "TCS": None}))
        self.assertEqual(len(items), 14)
        self.assertEqual(items[0]["touches"], ["INFY", "TCS"])


class Meetings(unittest.TestCase):
    def items(self):
        names = news.parse_equity_list(raw("EQUITY_L.csv"))
        by_name = {news.norm_name(names[s]): s for s in ("IDEA", "NAVA", "SBIN")}
        return news.nse_items(ann() + board(), by_name)

    def test_results_due_within_a_week(self):
        state = {}
        news.remember_meetings(self.items(), state, datetime.date(2026, 11, 3))
        self.assertEqual(sorted(state["meetings"]), ["IDEA", "NAVA"])
        self.assertTrue(state["meetings"]["IDEA"][0]["results"])
        watch = {"IDEA": None, "NAVA": None, "SBIN": None}
        self.assertEqual(news.results_due(state, watch, datetime.date(2026, 11, 3)), [])  # 9 days out
        due = news.results_due(state, watch, datetime.date(2026, 11, 5))
        self.assertEqual([(d["symbol"], d["date"]) for d in due],
                         [("IDEA", "2026-11-12"), ("NAVA", "2026-11-12")])
        self.assertEqual(news.render_due(due[:1]),
                         ["RESULTS DUE  IDEA on Thu 2026-11-12: a day trade is out before the close."])
        # past meetings are forgotten
        news.remember_meetings([], state, datetime.date(2026, 11, 13))
        self.assertEqual(state["meetings"], {})


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", os.path.join(self.tmp.name, "none.json"),
                     "--watchlist", os.path.join(self.tmp.name, "none.json"),
                     "--plan", os.path.join(self.tmp.name, "none.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "SBIN", "IDEA", "--hours", "24", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        # 5 SBIN filings, the IDEA intimation (its board-feed twin shares the link) and the results PDF
        self.assertEqual(len(data["items"]), 7)
        self.assertEqual(data["notes"], [])
        code, out, _ = run(["--tickers", "SBIN", "IDEA", "--hours", "24"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_the_text_brief(self):
        code, out, err = run(["--tickers", "sbin.ns", "IDEA", "M&M", "INFY", "--hours", "24",
                              "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("! 2026-11-03 13:47 IST  SBIN        NSE           State Bank of India: Outcome of Board Meeting", out)
        self.assertIn("2026-11-03 16:07 IST  M&M", out)
        self.assertIn("tags: results, board meeting", out)
        self.assertRegex(out, r"INFY\s+1,148\.10 -1\.11% vs yesterday's close 1,161\.00 at 15:29 IST")
        self.assertIn("Checked 2026-11-03 16:30 IST", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn("RESULTS DUE", out)   # IDEA's results are 9 days out
        self.assertNotIn(chr(0x2014), out)
        self.assertNotIn(" ET", out)

    def test_results_due_is_remembered_across_checks(self):
        run(["--tickers", "IDEA", "--hours", "24"] + self.base)
        args = list(self.base)
        args[args.index("--now") + 1] = "2026-11-06 09:20"
        code, out, err = run(["--tickers", "IDEA", "--hours", "24", "--no-moves", "--json"] + args)
        data = json.loads(out)
        self.assertEqual(data["results_due"][0]["symbol"], "IDEA")
        self.assertEqual(data["results_due"][0]["date"], "2026-11-12")

    def test_headlines_window(self):
        code, out, err = run(["--tickers", "INFY", "--hours", "72", "--no-nse", "--no-moves",
                              "--now", "2026-10-23 20:00"] + self.base[:4])
        self.assertEqual(code, 0, err)
        self.assertIn("2026-10-23 19:56 IST  INFY", out)   # 14:26 UTC
        self.assertIn("Infosys shares fall as revenue misses estimates and guidance disappoints", out)
        self.assertNotIn("2026-10-26", out)               # after the pinned clock
        line = [l for l in out.splitlines() if "revenue misses estimates" in l][0]
        tags = out.splitlines()[out.splitlines().index(line) + 1]
        self.assertIn("earnings", tags)

    def test_tickers_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [{"symbol": "SBIN", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "trader-exit", "symbol": "RELIANCE.NS"}, fh)
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, plan)), ["RELIANCE", "SBIN"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run", "--hours", "24"] + args)
        self.assertEqual(code, 0, err)
        tickers = {t for i in json.loads(out)["items"] for t in i["touches"]}
        self.assertEqual(tickers, {"SBIN"})
        self.assertFalse(os.path.exists(self.state))

    def test_bad_symbols_and_no_symbols(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no symbols", err)
        code, _, err = run(["--tickers", "RELIANCE.BO"] + self.base)
        self.assertEqual(code, 2)
        self.assertIn("NSE only", err)

    def test_symbol_not_on_the_list(self):
        code, out, err = run(["--tickers", "ZZZZ", "--hours", "24", "--dry-run"] + self.base)
        self.assertIn("note: ZZZZ is not on NSE's equity list", out)

    def test_moves(self):
        m = news.parse_chart(raw("chart-RELIANCE.json"), "RELIANCE")
        self.assertEqual(m["prev_close"], 1300.0)
        self.assertEqual(m["pct"], 0.61)
        self.assertEqual(news.to_ist(m["time"]).strftime("%H:%M"), "15:29")
        self.assertNotIn("MOVING", news.render_moves([m])[0])
        self.assertIn("MOVING", news.render_moves([m], move_pct=0.5)[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")
        # no chart file offline: no move line, no error note
        code, out, _ = run(["--tickers", "SBIN", "--hours", "24", "--dry-run"] + self.base)
        self.assertNotIn("vs yesterday's close", out)
        self.assertNotIn("note:", out)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-09-01T00:00:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


if __name__ == "__main__":
    unittest.main()
