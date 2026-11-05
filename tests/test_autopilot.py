import datetime
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import ROOT, load

ap = load("trader-autopilot", "autopilot")

DAY = "2026-11-04"  # a Wednesday, an NSE trading day


def at(hhmm, date=DAY):
    return datetime.datetime.strptime("%s %s" % (date, hhmm), "%Y-%m-%d %H:%M")


def bar_before(now_text, minutes_ago):
    """The bar_start of a 5-minute bar that closed `minutes_ago` minutes before now."""
    now = datetime.datetime.strptime(now_text, "%Y-%m-%d %H:%M")
    return (now - datetime.timedelta(minutes=minutes_ago + 5)).strftime("%H:%M")


class FakeRunner:
    """Stands in for the sibling tools: no network, no subprocess. Writes the files they would,
    for the symbol that actually triggered. Refuses any tool outside the autopilot's allowlist."""

    PRICES = {"CARTRADE": 2344.2, "AEGISLOG": 1124.0, "RELIANCE": 1174.3}

    def __init__(self, project, entry=None, alerts_out="", size_code=0, risk="PASS", add_code=0,
                 news_code=0, age=0, ready=True, quotes=None, rings=None, auto_help=True, quote_age=0,
                 approve_code=0, result_code=0, bars=None, vol=None, hist=None, due=None, scan_code=0):
        self.project, self.calls = project, []
        self.vol, self.hist, self.due, self.scan_code = vol or [], hist or {}, due or [], scan_code
        self.bars = bars or {}              # symbol -> [{"start": "YYYY-MM-DD HH:MM", "high", "low", "close"}]
        self.auto_help, self.quote_age = auto_help, quote_age
        self.approve_code, self.result_code = approve_code, result_code
        self.ready, self.quotes, self.rings = ready, quotes or {}, rings or []
        self.entry = entry or {}            # symbol -> "triggered" / "waiting" / "expired" / "error"
        self.alerts_out, self.size_code, self.risk, self.add_code = alerts_out, size_code, risk, add_code
        self.news_code, self.age = news_code, age

    def __call__(self, tool, args, cwd):
        assert tool in ap.AUTO_PAPER_TOOLS, "the autopilot ran %s" % tool
        if tool == ap.TICKET:
            assert ap.ticket_args_ok(args), "the autopilot ran ticket.py %s" % args
        self.calls.append((tool, list(args)))
        now = args[args.index("--now") + 1] if "--now" in args else "%s 09:50" % DAY
        tr = os.path.join(cwd, "trader")
        if tool == "trader-scanner/scan.py":
            if self.scan_code:
                return self.scan_code, "scan.py: Yahoo answered HTTP 429"
            out = os.path.join(cwd, args[args.index("--out") + 1])
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w") as fh:
                json.dump({"mode": "volatile", "hits": [{"symbol": k, "atr_pct": v} for k, v in self.vol]}, fh)
            return 0, "Wrote %d hits" % len(self.vol)
        if tool == "trader-timemachine/timemachine.py" and args[0] == "history":
            syms = [a for a in args[1:] if not a.startswith("-") and a.upper() == a and not a[:1].isdigit()]
            return 0, json.dumps({"history": {k: v for k, v in self.hist.items() if k in syms}, "errors": []})
        if tool == "trader-news/news.py" and "--json" in args:
            return 0, json.dumps({"results_due": self.due, "items": [], "moves": [], "notes": []})
        if tool == "trader-news/news.py":
            if self.news_code == 2:
                return 2, "Nothing new.\n  note: headlines for CARTRADE: HTTP 429\n  note: NSE announcements feed: HTTP 503\n"
            return self.news_code, "AEGISLOG +7.00%"
        if tool == "trader-entry/entry.py":
            sym, path = args[1], os.path.join(cwd, args[args.index("--write") + 1])
            st = self.entry.get(sym, "waiting")
            if st == "error":
                return 1, "entry.py: Yahoo answered HTTP 429"
            doc = {"symbol": sym, "date": now[:10], "status": st, "reason": "no breakout"}
            if st == "triggered":
                bs = bar_before(now, self.age)
                close = (datetime.datetime.strptime(bs, "%H:%M") + datetime.timedelta(minutes=5)).strftime("%H:%M")
                doc.update(price=self.PRICES.get(sym, 100.0), bar_start=bs, time=close, reason="closed above the range")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                json.dump(doc, fh)
            return (0 if st == "triggered" else 1), "%s %s" % (sym, st)
        if tool == "trader-exit/exit.py":
            entry = json.load(open(os.path.join(cwd, args[args.index("--from") + 1])))
            px = entry["price"]
            with open(os.path.join(tr, "plan.json"), "w") as fh:
                json.dump({"symbol": entry["symbol"], "entry": px, "stop": round(px * 0.975, 1),
                           "target": round(px * 1.05, 1)}, fh)
            return 0, "Wrote trader/plan.json."
        if tool == "trader-timemachine/timemachine.py":
            return 0, "Strongly toward the stop (target first 4.4 % vs stop first 22.4 %)."
        if tool == "trader-budget/size.py":
            plan = json.load(open(os.path.join(tr, "plan.json")))
            if self.size_code == 0:
                with open(os.path.join(tr, "orders.json"), "w") as fh:
                    created = (datetime.datetime.strptime(now, "%Y-%m-%d %H:%M") - ap.IST).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")
                    json.dump({"created_at": created, "orders": [{"symbol": plan["symbol"], "qty": "15", "side": "buy"}]},
                              fh)
            return self.size_code, "Size: 15 shares" if self.size_code == 0 else "size.py: zero shares"
        if tool == "trader-risk/check.py":
            orders = json.load(open(os.path.join(tr, "orders.json")))["orders"]
            if self.risk == "crash":
                return 1, "Traceback: boom"
            os.makedirs(os.path.join(tr, "state"), exist_ok=True)
            with open(os.path.join(tr, "state", "risk.json"), "w") as fh:
                json.dump({"date": now[:10], "results": [{"symbol": o["symbol"], "verdict": self.risk}
                                                         for o in orders]}, fh)
            return (0 if self.risk == "PASS" else 1), self.risk
        if tool == "trader-alerts/alerts.py":
            if args[0] == "add":
                return self.add_code, "Added 4 alerts"
            if args[0] == "bars":
                syms = [a for a in args[1:] if not a.startswith("-") and a.upper() == a and not a[:4].isdigit()]
                return 0, json.dumps({"date": now[:10], "interval": "5m", "errors": [],
                                      "bars": {k: v for k, v in self.bars.items() if k in syms}})
            if args[0] == "quote":
                t = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                     - datetime.timedelta(minutes=self.quote_age)).strftime("%Y-%m-%dT%H:%M:%SZ")
                syms = [a for a in args[1:] if not a.startswith("-") and a.upper() == a and not a[:4].isdigit()]
                return 0, json.dumps({"quotes": {k: {"price": v, "time": t} for k, v in self.quotes.items()
                                                 if k in syms}, "errors": []})
            if "--json" in args:
                return (0 if self.rings else 1), json.dumps({"rings": self.rings, "quotes": self.quotes})
            return (0, self.alerts_out) if self.alerts_out else (1, "nothing rang")
        if tool == ap.TICKET:
            return self.ticket(args, tr, now)
        return 0, ""

    def ticket(self, args, tr, now):
        """A paper broker: show / approve --auto-paper / result --status paper / flatten."""
        sub = args[0]
        orders = json.load(open(os.path.join(tr, "orders.json")))["orders"] if os.path.exists(
            os.path.join(tr, "orders.json")) else []
        if sub == "show":
            return 0, json.dumps({"index": 1, "ready": self.ready, "status": "READY" if self.ready else "NOT READY",
                                  "fingerprint": "ab" * 32})
        if args == ["approve", "--help"]:
            return 0, "usage: ticket.py approve ... [--auto-paper]" if self.auto_help else "usage: ticket.py approve"
        if sub == "approve":
            if self.approve_code == 0:
                self.record(tr, now, orders[0], "approved")
            return self.approve_code, "Recorded the auto-paper approval" if self.approve_code == 0 else "refused"
        if sub == "result":
            if self.result_code:
                return self.result_code, "result refused"
            o = orders[0]
            price, qty, sym = float(args[args.index("--filled-price") + 1]), int(o["qty"]), o["symbol"]
            pf_path = os.path.join(tr, "portfolio.json")
            pf = json.load(open(pf_path)) if os.path.exists(pf_path) else {"cash": 100000, "positions": []}
            pos = [p for p in pf["positions"] if p["ticker"] == sym]
            if not pos:
                pos = [{"ticker": sym, "shares": 0}]
                pf["positions"].append(pos[0])
            pos[0]["shares"] += qty if o["side"] == "buy" else -qty
            json.dump(pf, open(pf_path, "w"))
            st = os.path.join(tr, "state")
            os.makedirs(st, exist_ok=True)
            bpath = os.path.join(st, "broker.json")
            doc = json.load(open(bpath)) if os.path.exists(bpath) else {"orders": []}
            at = now.replace(" ", "T") + ":00+05:30"
            doc["orders"] += [{"at": at, "date": now[:10], "symbol": sym, "qty": str(qty), "status": "placed",
                               "side": o["side"], "fingerprint": "ab" * 32},
                              {"at": at, "date": now[:10], "symbol": sym, "qty": str(qty), "status": "filled",
                               "side": o["side"], "price": price}]
            json.dump(doc, open(bpath, "w"))
            return 0, "Recorded order 1 as placed (paper, simulated)."
        if sub == "flatten":
            sym, qty = args[args.index("--symbol") + 1], args[args.index("--qty") + 1]
            created = (datetime.datetime.strptime(now, "%Y-%m-%d %H:%M") - ap.IST).strftime("%Y-%m-%dT%H:%M:%SZ")
            json.dump({"created_at": created, "orders": [{"symbol": sym, "qty": qty, "side": "sell"}]},
                      open(os.path.join(tr, "orders.json"), "w"))
            return 0, "Wrote the flatten ticket"
        return 2, "unexpected"

    def record(self, tr, now, o, status):
        st = os.path.join(tr, "state")
        os.makedirs(st, exist_ok=True)
        bpath = os.path.join(st, "broker.json")
        doc = json.load(open(bpath)) if os.path.exists(bpath) else {"orders": []}
        doc["orders"].append({"at": now.replace(" ", "T") + ":00+05:30", "date": now[:10], "symbol": o["symbol"],
                              "side": o.get("side"), "status": status, "fingerprint": "ab" * 32})
        json.dump(doc, open(bpath, "w"))

    def tools(self):
        return [c[0] for c in self.calls]

    def args_of(self, tool, sub=None):
        return [c[1] for c in self.calls if c[0] == tool and (sub is None or c[1][:1] == [sub])]


class Base(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.project, "trader"))
        self.notes = []
        self.config({"paper": True, "broker": "angelone", "autopilot": {"symbols": ["CARTRADE", "AEGISLOG"]}})

    def tearDown(self):
        shutil.rmtree(self.project)

    def config(self, cfg):
        with open(os.path.join(self.project, "trader", "config.json"), "w") as fh:
            json.dump(cfg, fh)

    def notify(self, title, text, sound=False):
        self.notes.append(text)
        return True

    def tick(self, hhmm, runner, date=DAY, **kw):
        return ap.tick(self.project, now=at(hhmm, date), runner=runner, notifier=self.notify,
                       test_now="%s %s" % (date, hhmm), **kw)

    def fill(self, sym, shares=15, date=DAY, when="09:51"):
        """What ticket.py result --status paper leaves behind."""
        st = os.path.join(self.project, "trader", "state")
        os.makedirs(st, exist_ok=True)
        with open(os.path.join(st, "broker.json"), "w") as fh:
            at = "%sT%s:00+05:30" % (date, when)
            json.dump({"orders": [{"at": at, "date": date, "symbol": sym, "qty": str(shares), "status": "approved"},
                                  {"at": at, "date": date, "symbol": sym, "qty": str(shares), "status": "placed"},
                                  {"at": at, "date": date, "symbol": sym, "qty": str(shares), "side": "buy",
                                   "status": "filled"}]}, fh)
        with open(os.path.join(self.project, "trader", "portfolio.json"), "w") as fh:
            json.dump({"cash": 50000, "positions": [{"ticker": sym, "shares": shares}]}, fh)

    def state(self, date=DAY):
        return json.load(open(os.path.join(self.project, "trader", "state", "autopilot-%s.json" % date)))


class Calendar(Base):
    def test_weekend_holiday_and_off_hours_do_nothing(self):
        r = FakeRunner(self.project)
        self.assertIn("weekend", self.tick("10:00", r, date="2026-11-07"))
        self.assertIn("Diwali", self.tick("10:00", r, date="2026-11-10"))
        self.assertIn("nothing to do", self.tick("08:30", r))
        self.assertIn("nothing to do", self.tick("15:40", r))
        self.assertEqual(r.calls, [])
        self.assertEqual(self.notes, [])

    def test_scanner_holiday_list_is_used(self):
        hol, years = ap.load_holidays(ap.SKILLS)
        self.assertEqual(hol["2026-11-24"], "Prakash Gurpurb Sri Guru Nanak Dev")
        self.assertIn(2026, years)

    def test_fallback_holiday_list_without_the_scanner(self):
        hol, years = ap.load_holidays("/nonexistent")
        self.assertEqual(hol, ap.NSE_HOLIDAYS_2026)
        self.assertEqual(years, (2026,))

    def test_year_outside_the_list_warns_once(self):
        r = FakeRunner(self.project)
        self.tick("10:00", r, date="2027-01-04")
        self.tick("10:02", r, date="2027-01-04")
        self.assertEqual(sum("not 2027" in n for n in self.notes), 1)


class News(Base):
    def test_news_runs_once_in_its_window(self):
        r = FakeRunner(self.project)
        self.tick("08:56", r)
        self.assertNotIn("trader-news/news.py", r.tools())
        self.tick("09:00", r)
        self.tick("09:02", r)
        news = r.args_of("trader-news/news.py")
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0][:2], ["--tickers", "CARTRADE,AEGISLOG"])
        self.assertIn("--now", news[0])
        self.assertIn("--dry-run", news[0])  # a replay never marks headlines as seen
        self.assertEqual(len(self.notes), 1)

    def test_news_window_closes_at_the_open(self):
        r = FakeRunner(self.project)
        self.tick("09:16", r)
        self.assertNotIn("trader-news/news.py", r.tools())

    def test_failed_news_is_noted_once_and_retried(self):
        r = FakeRunner(self.project, news_code=2)
        self.tick("09:00", r)
        self.tick("09:02", r)
        self.assertEqual(len(r.args_of("trader-news/news.py")), 2)
        self.assertEqual(sum("news check failed" in n for n in self.notes), 1)
        self.assertFalse(self.state()["news"])
        r.news_code = 0
        self.tick("09:04", r)
        self.assertTrue(self.state()["news"])

    def test_late_news_setting_warns(self):
        self.config({"paper": True, "autopilot": {"symbols": ["CARTRADE"], "news_at": "09:20"}})
        self.tick("09:30", FakeRunner(self.project))
        self.assertTrue(any("never runs" in n for n in self.notes))


class Entry(Base):
    def test_no_entry_checks_before_entry_from(self):
        r = FakeRunner(self.project)
        self.tick("09:28", r)
        self.assertNotIn("trader-entry/entry.py", r.tools())

    def test_fresh_trigger_runs_the_chain_and_stops_at_the_ticket(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        chain = ["trader-exit/exit.py", "trader-timemachine/timemachine.py", "trader-budget/size.py",
                 "trader-risk/check.py"]
        self.assertEqual([t for t in r.tools() if t in chain], chain)
        plan = r.args_of("trader-exit/exit.py")[0]
        self.assertEqual(plan[:7], ["plan", "--from", os.path.join("trader", "autopilot", "entry-CARTRADE.json"),
                                    "--stop", "or-mid", "--target", "2R"])
        msg = [n for n in self.notes if "CARTRADE triggered" in n][0]
        for bit in ("2344.2", "(bar closed 09:50)", "Stop 2285.6", "target 2461.4", "15 shares", "Risk PASS.",
                    "show ticket", "target first 4.4 % vs stop first 22.4 %"):
            self.assertIn(bit, msg)
        snap = json.load(open(os.path.join(self.project, "trader", "autopilot", "plan-CARTRADE.json")))
        self.assertEqual(snap["symbol"], "CARTRADE")

    def test_stale_trigger_is_noted_not_prepared(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, age=25)
        self.tick("10:30", r)
        self.assertNotIn("trader-exit/exit.py", r.tools())
        self.assertTrue(any("25 min ago: stale, not prepared" in n for n in self.notes))
        self.assertEqual(self.state()["done"]["CARTRADE"], "stale")

    def test_trigger_just_inside_the_freshness_window_is_prepared(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, age=ap.STALE_MINUTES)
        self.tick("10:30", r)
        self.assertIn("trader-exit/exit.py", r.tools())

    def test_stop_and_target_come_from_the_config_and_are_validated(self):
        self.config({"paper": True, "autopilot": {"symbols": ["CARTRADE"], "stop": "or-low", "target": "1R"}})
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.assertEqual(r.args_of("trader-exit/exit.py")[0][3:7], ["--stop", "or-low", "--target", "1R"])
        for bad in ({"target": 2}, {"stop": "midpoint"}, {"target": "two R"}):
            cfg = {"autopilot": dict({"symbols": ["CARTRADE"]}, **bad)}
            if bad == {"target": 2}:
                with self.assertRaises(ap.AutopilotError):
                    ap.settings(cfg)  # 2 becomes "2", which is not "2R"
            else:
                with self.assertRaises(ap.AutopilotError):
                    ap.settings(cfg)

    def test_triggered_symbol_is_not_checked_again(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.tick("09:52", r)
        checks = [a[1] for a in r.args_of("trader-entry/entry.py")]
        self.assertEqual(checks.count("CARTRADE"), 1)
        self.assertEqual(checks.count("AEGISLOG"), 2)
        self.assertEqual(r.tools().count("trader-exit/exit.py"), 1)

    def test_second_trigger_is_not_prepared_and_says_finish_the_pending_ticket(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        r.entry["AEGISLOG"] = "triggered"
        self.tick("11:00", r)
        self.assertEqual(r.tools().count("trader-exit/exit.py"), 1)
        msg = [n for n in self.notes if "AEGISLOG also triggered" in n][0]
        self.assertIn("Finish or decline that ticket first", msg)
        self.assertNotIn("plan AEGISLOG", msg)
        plan = json.load(open(os.path.join(self.project, "trader", "plan.json")))
        self.assertEqual(plan["symbol"], "CARTRADE")  # the pending ticket's plan is untouched

    def test_a_pending_ticket_from_an_earlier_run_blocks_preparation(self):
        with open(os.path.join(self.project, "trader", "orders.json"), "w") as fh:
            json.dump({"created_at": "2026-11-04T04:00:00Z", "orders": [{"symbol": "INFY", "qty": "10"}]}, fh)
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.assertNotIn("trader-exit/exit.py", r.tools())
        self.assertTrue(any("a ticket for INFY is pending" in n for n in self.notes))

    def test_a_decided_or_old_ticket_does_not_block(self):
        with open(os.path.join(self.project, "trader", "orders.json"), "w") as fh:
            json.dump({"created_at": "2026-11-03T04:00:00Z", "orders": [{"symbol": "INFY", "qty": "10"}]}, fh)
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.assertIn("trader-exit/exit.py", r.tools())

    def test_expired_stops_checks(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "expired", "AEGISLOG": "waiting"})
        self.tick("14:40", r)
        self.tick("14:42", r)
        checks = [a[1] for a in r.args_of("trader-entry/entry.py")]
        self.assertEqual(checks.count("CARTRADE"), 1)
        self.assertEqual(sum("CARTRADE: no breakout" in n for n in self.notes), 1)

    def test_no_entry_checks_after_no_entries_after(self):
        r = FakeRunner(self.project)
        self.tick("14:50", r)
        self.assertNotIn("trader-entry/entry.py", r.tools())

    def test_a_failing_symbol_does_not_stop_the_others_or_reuse_a_stale_file(self):
        stale = os.path.join(self.project, "trader", "autopilot", "entry-CARTRADE.json")
        os.makedirs(os.path.dirname(stale))
        with open(stale, "w") as fh:
            json.dump({"symbol": "CARTRADE", "date": DAY, "status": "triggered", "price": 1, "bar_start": "09:45"}, fh)
        r = FakeRunner(self.project, entry={"CARTRADE": "error", "AEGISLOG": "triggered"})
        self.tick("09:50", r)
        self.assertFalse(any("CARTRADE triggered" in n for n in self.notes))
        self.assertTrue(any("AEGISLOG triggered" in n for n in self.notes))

    def test_size_refusal_is_reported_and_no_risk_check(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, size_code=1)
        self.tick("09:50", r)
        self.assertNotIn("trader-risk/check.py", r.tools())
        self.assertTrue(any("wrote no order" in n for n in self.notes))

    def test_risk_block_and_risk_crash_are_told_apart(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, risk="BLOCK")
        self.tick("09:50", r)
        self.assertTrue(any("Risk BLOCK." in n for n in self.notes))
        self.tearDown()
        self.setUp()
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, risk="crash")
        self.tick("09:50", r)
        self.assertTrue(any("Risk check failed" in n for n in self.notes))

    def test_an_exception_while_preparing_is_noted_and_retried(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        with mock.patch.object(ap, "prepare_trade", side_effect=TypeError("bad value")):
            self.tick("09:50", r)
        self.assertTrue(any("preparing it failed (bad value)" in n for n in self.notes))
        self.assertNotIn("CARTRADE", self.state()["done"])
        self.tick("09:52", r)
        self.assertIn("trader-exit/exit.py", r.tools())


class Alerts(Base):
    def prepared(self, **kw):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, **kw)
        self.tick("09:50", r)
        return r

    def test_alerts_wait_for_the_paper_fill_and_use_the_snapshot(self):
        r = self.prepared()
        self.tick("09:52", r)
        self.assertEqual(r.args_of("trader-alerts/alerts.py", "add"), [])
        self.fill("CARTRADE")
        self.tick("09:54", r)
        self.tick("09:56", r)
        adds = r.args_of("trader-alerts/alerts.py", "add")
        self.assertEqual(adds, [["add", "--from-plan", "--plan", os.path.join("trader", "autopilot",
                                                                              "plan-CARTRADE.json")]])

    def test_an_old_position_is_not_mistaken_for_the_fill(self):
        with open(os.path.join(self.project, "trader", "portfolio.json"), "w") as fh:
            json.dump({"cash": 1, "positions": [{"ticker": "CARTRADE", "shares": 10}]}, fh)
        r = self.prepared()
        self.tick("09:52", r)
        self.assertEqual(r.args_of("trader-alerts/alerts.py", "add"), [])
        with open(os.path.join(self.project, "trader", "portfolio.json"), "w") as fh:
            json.dump({"cash": 1, "positions": [{"ticker": "CARTRADE", "shares": 25}]}, fh)
        self.tick("09:54", r)
        self.assertEqual(len(r.args_of("trader-alerts/alerts.py", "add")), 1)

    def test_add_never_gets_now_but_check_does(self):
        r = self.prepared()
        self.fill("CARTRADE")
        self.tick("09:54", r)
        self.assertNotIn("--now", r.args_of("trader-alerts/alerts.py", "add")[0])
        self.assertIn("--now", r.args_of("trader-alerts/alerts.py", "check")[0])

    def test_failed_add_is_retried_and_noted_once(self):
        r = self.prepared(add_code=1)
        self.fill("CARTRADE")
        self.tick("09:54", r)
        self.tick("09:56", r)
        self.assertEqual(len(r.args_of("trader-alerts/alerts.py", "add")), 2)
        self.assertEqual(sum("Could not add alerts" in n for n in self.notes), 1)

    def test_a_ring_is_notified_once(self):
        r = self.prepared(alerts_out="NSE OPEN (regular session).\nALERT CARTRADE is 2,463.20: at or above your target\n")
        self.fill("CARTRADE")
        self.tick("10:30", r)
        self.tick("10:32", r)
        self.assertEqual([n for n in self.notes if n.startswith("ALERT")],
                         ["ALERT CARTRADE is 2,463.20: at or above your target"])


class Lifecycle(Base):
    """Round 2: the pending ticket, the fill and the risk verdict all belong to this preparation."""

    def decline(self, sym, when="09:52", fingerprint="0da68f3a687e25d9"):
        """The record ticket.py writes for a no: it has no qty."""
        st = os.path.join(self.project, "trader", "state")
        os.makedirs(st, exist_ok=True)
        path = os.path.join(st, "broker.json")
        doc = json.load(open(path)) if os.path.exists(path) else {"orders": []}
        doc["orders"].append({"at": "%sT%s:00+05:30" % (DAY, when), "date": DAY, "index": 1,
                              "fingerprint": fingerprint, "symbol": sym, "side": "buy",
                              "plain": "BUY 15 %s on NSE, market, intraday (MIS)" % sym, "status": "declined",
                              "reply": "no", "paper": True, "broker": "angelone"})
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def test_a_declined_ticket_frees_the_autopilot(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.assertEqual(self.state()["active"], "CARTRADE")
        self.decline("CARTRADE")
        self.assertIsNone(ap.pending_ticket(self.project, DAY))
        r.entry["AEGISLOG"] = "triggered"
        self.tick("09:54", r)
        self.assertTrue(any("The CARTRADE ticket was declined" in n for n in self.notes))
        self.assertTrue(any("AEGISLOG triggered at 1124.0" in n for n in self.notes))
        self.assertEqual(self.state()["active"], "AEGISLOG")

    def test_a_held_back_symbol_is_re_evaluated_after_the_decline(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        r.entry["AEGISLOG"] = "triggered"
        self.tick("09:52", r)
        self.assertNotIn("AEGISLOG", self.state()["done"])
        self.decline("CARTRADE", when="10:20")
        r.age = 30  # by now AEGISLOG's breakout is old
        self.tick("10:22", r)
        self.assertTrue(any("AEGISLOG triggered" in n and "stale, not prepared" in n for n in self.notes))
        self.assertEqual(r.tools().count("trader-exit/exit.py"), 1)

    def test_a_decline_before_the_preparation_does_not_count(self):
        self.decline("CARTRADE", when="09:30")
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.tick("09:52", r)
        self.assertEqual(self.state()["active"], "CARTRADE")

    def test_a_fill_before_the_preparation_does_not_turn_on_alerts(self):
        self.fill("CARTRADE", when="09:20")
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.tick("09:52", r)
        self.assertEqual(r.args_of("trader-alerts/alerts.py", "add"), [])
        self.fill("CARTRADE", shares=15, when="09:53")
        with open(os.path.join(self.project, "trader", "portfolio.json"), "w") as fh:
            json.dump({"cash": 1, "positions": [{"ticker": "CARTRADE", "shares": 15}]}, fh)
        self.tick("09:54", r)
        self.assertEqual(len(r.args_of("trader-alerts/alerts.py", "add")), 1)

    def test_an_old_risk_verdict_is_never_read(self):
        st = os.path.join(self.project, "trader", "state")
        os.makedirs(st)
        with open(os.path.join(st, "risk.json"), "w") as fh:
            json.dump({"date": DAY, "results": [{"symbol": "CARTRADE", "verdict": "PASS"}]}, fh)
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, risk="crash")
        self.tick("09:50", r)
        self.assertTrue(any("Risk check failed" in n for n in self.notes))
        self.assertFalse(any("Risk PASS" in n for n in self.notes))

    def test_trigger_age_uses_the_clock_at_the_check(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, age=ap.STALE_MINUTES)
        real = [datetime.datetime(2026, 11, 4, 4, 20)]

        def slow_clock():  # every call is 3 minutes later: the tick has been running a while
            real[0] += datetime.timedelta(minutes=3)
            return real[0]
        with mock.patch.object(ap, "now_ist", side_effect=slow_clock):
            self.tick("10:30", r)
        self.assertNotIn("trader-exit/exit.py", r.tools())
        self.assertTrue(any("stale" in n for n in self.notes))

    def test_news_exit_2_is_a_failure_with_its_notes(self):
        r = FakeRunner(self.project, news_code=2)
        self.tick("09:00", r)
        self.assertFalse(self.state()["news"])
        msg = [n for n in self.notes if "news check failed" in n][0]
        self.assertIn("headlines for CARTRADE: HTTP 429", msg)
        self.assertIn("retries until 9:15", msg)


class Notify(Base):
    def test_osascript_gets_the_text_as_an_argument(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        ok = ap.mac_notify("Day trader autopilot", 'CARTRADE at ₹2,344.20 "quoted"', run=fake_run,
                           which=lambda name: "/usr/bin/osascript")
        self.assertTrue(ok)
        argv = seen["argv"]
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertEqual(argv[1:7], ["-e", "on run argv", "-e",
                                     "display notification (item 1 of argv) with title (item 2 of argv)",
                                     "-e", "end run"])
        self.assertEqual(argv[7:], ['CARTRADE at ₹2,344.20 "quoted"', "Day trader autopilot"])
        self.assertEqual(seen["kw"]["timeout"], ap.NOTIFY_TIMEOUT)

    def test_a_failed_notification_is_not_marked_delivered(self):
        fail = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"error")
        self.assertFalse(ap.mac_notify("t", "x", run=fail, which=lambda n: "/usr/bin/osascript"))
        self.assertFalse(ap.mac_notify("t", "x", which=lambda n: None))

    def test_undelivered_events_are_retried_and_written_down(self):
        calls = []

        def flaky(title, text, sound=False):
            calls.append(text)
            return len(calls) > 1
        r = FakeRunner(self.project)
        ap.tick(self.project, now=at("09:00"), runner=r, notifier=flaky, test_now="%s 09:00" % DAY)
        today = open(os.path.join(self.project, "trader", "autopilot", "today.md")).read()
        self.assertIn("notification failed, retrying", today)
        ap.tick(self.project, now=at("09:02"), runner=r, notifier=flaky, test_now="%s 09:02" % DAY)
        self.assertEqual(sum("News for" in c for c in calls), 2)
        self.assertIn("news", self.state()["events"])


class Safety(Base):
    def test_live_config_is_refused_without_live_ok(self):
        self.config({"paper": False, "autopilot": {"symbols": ["CARTRADE"]}})
        with self.assertRaises(ap.AutopilotError):
            self.tick("09:50", FakeRunner(self.project))
        for still_paper in (0, "", None, "no"):  # only a literal false is live, as in the order tools
            self.config({"paper": still_paper, "autopilot": {"symbols": ["CARTRADE"]}})
            self.tick("09:50", FakeRunner(self.project))

    def test_paper_rule_matches_the_order_tools(self):
        for value, paper in ((True, True), (False, False), (0, True), ("", True), (None, True), ("no", True)):
            self.assertIs(ap.is_paper({"paper": value}), paper, value)
        self.assertTrue(ap.is_paper({}))

    def test_live_ok_prepares_but_only_runs_allowed_tools(self):
        self.config({"paper": False, "autopilot": {"symbols": ["CARTRADE"]}})
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r, live_ok=True)
        self.assertIn("trader-risk/check.py", r.tools())
        self.assertTrue(all(t in ap.ALLOWED_TOOLS for t in r.tools()))
        self.assertTrue(self.notes and not self.state()["paper"])

    def test_the_runner_refuses_the_broker(self):
        self.assertNotIn("trader-broker/ticket.py", ap.ALLOWED_TOOLS)
        with self.assertRaises(ap.AutopilotError):
            ap.SubprocessRunner(ROOT)("trader-broker/ticket.py", ["approve", "--index", "1"], self.project)

    def test_refusal_during_market_hours_notifies_once(self):
        self.config({"paper": True, "autopilot": {"symbols": ["CARTRADE"], "target": "lots"}})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ap, "now_ist", return_value=at("10:00")), \
                mock.patch.object(ap, "mac_notify", side_effect=self.notify), \
                redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(ap.main(["tick", "--project", self.project]), 1)
            self.assertEqual(ap.main(["tick", "--project", self.project]), 1)
        self.assertEqual(sum("Autopilot stopped" in n for n in self.notes), 1)
        self.assertIn("Autopilot stopped", open(os.path.join(self.project, "trader", "autopilot", "today.md")).read())


class Symbols(Base):
    def test_precedence_config_then_universe_then_watchlist(self):
        with open(os.path.join(self.project, "trader", "universe.txt"), "w") as fh:
            fh.write("RELIANCE\n# comment\nm&m\n")
        with open(os.path.join(self.project, "trader", "watchlist.json"), "w") as fh:
            json.dump({"created_at": "2026-11-04T03:30:00Z", "hits": [{"symbol": "INFY"}]}, fh)
        today = datetime.date(2026, 11, 4)
        self.assertEqual(ap.symbols(self.project, {"autopilot": {"symbols": ["cartrade.ns"]}}, today)[0], ["CARTRADE"])
        self.assertEqual(ap.symbols(self.project, {}, today)[0], ["RELIANCE", "M&M"])
        os.remove(os.path.join(self.project, "trader", "universe.txt"))
        self.assertEqual(ap.symbols(self.project, {}, today)[0], ["INFY"])
        os.remove(os.path.join(self.project, "trader", "watchlist.json"))
        with self.assertRaises(ap.AutopilotError):
            ap.symbols(self.project, {}, today)

    def test_an_old_watchlist_is_skipped(self):
        with open(os.path.join(self.project, "trader", "watchlist.json"), "w") as fh:
            json.dump({"created_at": "2026-10-20T03:30:00Z", "hits": [{"symbol": "INFY"}]}, fh)
        with self.assertRaises(ap.AutopilotError) as cm:
            ap.symbols(self.project, {}, datetime.date(2026, 11, 4))
        self.assertIn("too old", str(cm.exception))
        # Monday may use Friday's scan
        with open(os.path.join(self.project, "trader", "watchlist.json"), "w") as fh:
            json.dump({"created_at": "2026-11-06T10:00:00Z", "hits": [{"symbol": "INFY"}]}, fh)
        self.assertEqual(ap.symbols(self.project, {}, datetime.date(2026, 11, 9))[0], ["INFY"])

    def test_bad_symbols_are_skipped_one_by_one(self):
        syms, _, skipped = ap.symbols(self.project, {"autopilot": {"symbols": ["CARTRADE", "RELIANCE.BO", "x;rm"]}})
        self.assertEqual(syms, ["CARTRADE"])
        self.assertEqual(len(skipped), 2)
        with self.assertRaises(ap.AutopilotError):
            ap.symbols(self.project, {"autopilot": {"symbols": ["RELIANCE.BO"]}})

    def test_a_bad_symbol_does_not_stop_the_tick(self):
        self.config({"paper": True, "autopilot": {"symbols": ["CARTRADE", "BAD SYM"]}})
        r = FakeRunner(self.project)
        self.tick("09:50", r)
        self.assertEqual([a[1] for a in r.args_of("trader-entry/entry.py")], ["CARTRADE"])
        self.assertTrue(any("skipped" in n for n in self.notes))


class Install(Base):
    def test_install_writes_a_plist_and_never_runs_launchctl(self):
        with mock.patch.object(ap.subprocess, "run") as run, mock.patch.object(ap.os, "system") as system:
            lines = ap.install(self.project)
            run.assert_not_called()
            system.assert_not_called()
        label = ap.label_for(self.project)
        self.assertRegex(label, r"^com\.daytrader\.autopilot\.[0-9a-f]{8}$")
        with open(os.path.join(self.project, "trader", "autopilot", label + ".plist"), "rb") as fh:
            doc = plistlib.load(fh)
        self.assertEqual(doc["Label"], label)
        self.assertIn(doc["ProgramArguments"][0], (sys.executable, ap.stable_python()))
        self.assertTrue(os.path.realpath(doc["ProgramArguments"][0]) == os.path.realpath(sys.executable))
        self.assertEqual(doc["ProgramArguments"][2:], ["tick", "--project", os.path.abspath(self.project)])
        self.assertLess(ap.CALL_TIMEOUT, doc["StartInterval"])
        text = "\n".join(lines)
        self.assertIn("launchctl bootstrap gui/$(id -u)", text)
        self.assertIn("launchctl bootout gui/$(id -u)/%s" % label, text)
        self.assertIn(doc["ProgramArguments"][0], text)

    def test_install_refuses_to_write_into_launchagents(self):
        with self.assertRaises(ap.AutopilotError):
            ap.install(self.project, os.path.expanduser("~/Library/LaunchAgents/x.plist"))

    def test_two_projects_get_two_labels(self):
        other = tempfile.mkdtemp()
        try:
            self.assertNotEqual(ap.label_for(self.project), ap.label_for(other))
        finally:
            shutil.rmtree(other)


class Cli(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ap.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_status_before_any_tick(self):
        code, out, _ = self.run_cli("status", "--project", self.project, "--now", "2026-11-04 09:00")
        self.assertEqual(code, 0)
        self.assertIn("no tick has run today", out)
        self.assertNotIn("installed", out)

    def test_replay_runs_in_its_own_folder(self):
        for name, body in (("plan.json", '{"symbol": "LIVE"}'), ("orders.json", '{"orders": []}')):
            with open(os.path.join(self.project, "trader", name), "w") as fh:
                fh.write(body)
        code, out, _ = self.run_cli("tick", "--project", self.project, "--now", "2026-11-07 10:00")
        self.assertEqual(code, 0)
        replay = os.path.join(self.project, "trader", "autopilot", "replay-2026-11-07")
        self.assertIn(replay, out)
        self.assertTrue(os.path.exists(os.path.join(replay, "trader", "config.json")))
        self.assertEqual(json.load(open(os.path.join(self.project, "trader", "plan.json"))), {"symbol": "LIVE"})
        self.assertFalse(os.path.exists(os.path.join(self.project, "trader", "autopilot", "today.md")))

    def test_replay_recopies_the_config_every_time(self):
        self.run_cli("tick", "--project", self.project, "--now", "2026-11-07 10:00")
        self.config({"paper": True, "autopilot": {"symbols": ["INFY"]}})
        self.run_cli("tick", "--project", self.project, "--now", "2026-11-07 10:02")
        cfg = json.load(open(os.path.join(self.project, "trader", "autopilot", "replay-2026-11-07", "trader",
                                          "config.json")))
        self.assertEqual(cfg["autopilot"]["symbols"], ["INFY"])

    def test_live_refusal_exits_1(self):
        self.config({"paper": False, "autopilot": {"symbols": ["CARTRADE"]}})
        code, _, err = self.run_cli("tick", "--project", self.project, "--now", "2026-11-04 10:00")
        self.assertEqual(code, 1)
        self.assertIn("--live-ok", err)

    def test_lock_blocks_a_second_tick(self):
        lock = ap.acquire_lock(self.project)
        try:
            self.assertIsNone(ap.acquire_lock(self.project))
        finally:
            lock.close()
        again = ap.acquire_lock(self.project)
        self.assertIsNotNone(again)
        again.close()

    def test_state_is_one_file_per_ist_date(self):
        r = FakeRunner(self.project)
        self.tick("09:00", r)
        self.tick("09:00", r, date="2026-11-05")
        st = os.listdir(os.path.join(self.project, "trader", "state"))
        self.assertIn("autopilot-2026-11-04.json", st)
        self.assertIn("autopilot-2026-11-05.json", st)


class AutoPaper(Base):
    """Fully automatic paper trading: config "autopilot": {"auto_paper": true}."""

    def setUp(self):
        super().setUp()
        self.config({"paper": True, "broker": "angelone", "max_trades_per_day": 3,
                     "autopilot": {"symbols": ["CARTRADE", "AEGISLOG"], "auto_paper": True}})

    def ticket_calls(self, r, sub):
        return [a for a in r.args_of(ap.TICKET, sub) if a != ["approve", "--help"]]

    def test_entry_approves_and_fills_on_paper(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.assertEqual(self.ticket_calls(r, "approve")[0][:5],
                         ["approve", "--index", "1", "--fingerprint", "abababab"])
        self.assertIn("--auto-paper", self.ticket_calls(r, "approve")[0])
        res = self.ticket_calls(r, "result")[0]
        self.assertEqual(res[:6], ["result", "--index", "1", "--status", "paper", "--filled-price"])
        self.assertEqual(res[6], "2344.20")
        self.assertEqual(r.args_of("trader-alerts/alerts.py", "add")[0][-1],
                         os.path.join("trader", "autopilot", "plan-CARTRADE.json"))
        self.assertTrue(any(n.startswith("AUTO-PAPER BUY 15 CARTRADE at 2344.20") for n in self.notes))
        tr = self.state()["trades"]["CARTRADE"]
        self.assertEqual((tr["qty"], tr["entry_price"], tr["status"]), (15, 2344.2, "open"))

    def test_risk_block_means_no_trade(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, risk="BLOCK")
        self.tick("09:50", r)
        self.assertEqual(self.ticket_calls(r, "approve"), [])
        self.assertTrue(any("risk BLOCK: no trade" in n for n in self.notes))

    def test_a_ticket_that_is_not_ready_is_not_approved(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, ready=False)
        self.tick("09:50", r)
        self.assertEqual(self.ticket_calls(r, "approve"), [])
        self.assertTrue(any("not ready" in n for n in self.notes))

    def test_target_exit_journal_and_pnl(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        target = self.state()["trades"]["CARTRADE"]["target"]
        r.quotes = {"CARTRADE": target + 1}
        self.tick("10:30", r)
        self.assertEqual(self.ticket_calls(r, "flatten")[0][:5], ["flatten", "--symbol", "CARTRADE", "--qty", "15"])
        self.assertEqual(self.ticket_calls(r, "result")[1][6], "%.2f" % (target + 1))
        tr = self.state()["trades"]["CARTRADE"]
        self.assertEqual((tr["status"], tr["exit_reason"]), ("closed", "target"))
        self.assertAlmostEqual(tr["gross"], round((target + 1 - 2344.2) * 15, 2))
        self.assertGreater(tr["charges"], 0)  # trader-budget's real formula
        self.assertAlmostEqual(tr["net"], round(tr["gross"] - tr["charges"], 2))
        journal = json.load(open(os.path.join(self.project, "trader", "autopilot", "journal-%s.json" % DAY)))
        self.assertEqual(journal["totals"]["closed"], 1)
        self.assertAlmostEqual(journal["totals"]["net"], tr["net"])
        today = open(os.path.join(self.project, "trader", "autopilot", "today.md")).read()
        self.assertIn("| CARTRADE | 15 | 09:50 2344.20 | 10:30 %.2f | target |" % (target + 1), today)
        self.assertTrue(any(n.startswith("AUTO-PAPER SELL 15 CARTRADE") for n in self.notes))
        self.tick("10:32", r)
        self.assertEqual(len(self.ticket_calls(r, "flatten")), 1)  # closed stays closed

    def bar(self, hhmm, high, low):
        return {"start": "%s %s" % (DAY, hhmm), "high": high, "low": low, "close": (high + low) / 2}

    def test_a_target_touched_between_ticks_exits_at_the_target(self):
        """The SWIGGY case: a wick reaches the target between two 2-minute checks, then falls back."""
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        tr = self.state()["trades"]["CARTRADE"]
        target, stop = tr["target"], tr["stop"]
        r.quotes = {"CARTRADE": target - 30}
        r.bars = {"CARTRADE": [self.bar("09:40", target + 50, stop - 50),       # before the entry's bar: ignored
                               self.bar("09:50", 2350.0, 2340.0),
                               self.bar("10:10", target + 2, 2350.0),
                               self.bar("10:15", target - 25, target - 35)]}
        self.tick("10:16", r)
        tr = self.state()["trades"]["CARTRADE"]
        self.assertEqual((tr["status"], tr["exit_reason"], tr["exit_price"], tr["touch_bar"]),
                         ("closed", "target", target, "10:10"))
        self.assertEqual(self.ticket_calls(r, "result")[1][6], "%.2f" % target)
        self.assertTrue(any("(target, touched at 10:10, booked at 10:16)" in n for n in self.notes))
        self.assertEqual((tr["exit_time"], tr["booked_at"]), ("10:10", "10:16"))

    def test_a_bar_that_reached_both_counts_as_the_stop(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        tr = self.state()["trades"]["CARTRADE"]
        r.quotes = {"CARTRADE": 2350.0}
        r.bars = {"CARTRADE": [self.bar("10:05", tr["target"] + 1, tr["stop"] - 1)]}
        self.tick("10:12", r)
        tr = self.state()["trades"]["CARTRADE"]
        self.assertEqual((tr["exit_reason"], tr["exit_price"]), ("stop", tr["stop"]))

    def test_no_touch_keeps_the_quote_rules(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        tr = self.state()["trades"]["CARTRADE"]
        r.quotes = {"CARTRADE": 2350.0}
        r.bars = {"CARTRADE": [self.bar("10:05", tr["target"] - 1, tr["stop"] + 1)]}
        self.tick("10:12", r)
        self.assertEqual(self.state()["trades"]["CARTRADE"]["status"], "open")
        r.bars = {}   # alerts.py bars failed: the quote alone decides, as before
        r.quotes = {"CARTRADE": tr["target"] + 1}
        self.tick("10:14", r)
        tr = self.state()["trades"]["CARTRADE"]
        self.assertEqual((tr["exit_reason"], tr["exit_price"], tr.get("touch_bar")), ("target", tr["target"] + 1, None))

    def test_bar_touch_rules(self):
        tr = {"entry_time": "09:52", "stop": 100.0, "target": 110.0, "flat_by": "15:10"}
        b = lambda hhmm, hi, lo, date=DAY: {"start": "%s %s" % (date, hhmm), "high": hi, "low": lo}
        self.assertEqual(ap.bar_touch(tr, [b("09:53", 110.0, 105.0)], DAY), ("target", 110.0, "09:53"))  # next minute
        self.assertEqual(ap.bar_touch(tr, [b("09:52", 111, 99)], DAY), (None, None, None))     # the fill's minute
        self.assertEqual(ap.bar_touch(tr, [b("09:50", 111, 99)], DAY), (None, None, None))     # before the fill
        self.assertEqual(ap.bar_touch(dict(tr, flat_by="bad"), [b("15:12", 111, 99)], DAY), (None, None, None))
        self.assertEqual(ap.bar_touch(dict(tr, stop="abc"), [b("10:00", 111, 99)], DAY), (None, None, None))
        self.assertEqual(ap.bar_touch(tr, [b("15:10", 111, 99)], DAY), (None, None, None))           # flat-by's job
        self.assertEqual(ap.bar_touch(tr, [b("10:00", 111, 99, "2026-11-03")], DAY), (None, None, None))
        self.assertEqual(ap.bar_touch(tr, [b("10:05", 105, 100.0), b("10:00", 110.5, 104)], DAY),
                         ("target", 110.0, "10:00"))                                                  # earliest first
        self.assertEqual(ap.bar_touch(tr, [{"start": "%s 10:00" % DAY, "high": None}, b("10:05", 106, 99.9)], DAY),
                         ("stop", 100.0, "10:05"))                                                    # bad bar skipped
        self.assertEqual(ap.bar_touch(dict(tr, carried_from="2026-11-03"), [b("10:00", 111, 99)], DAY),
                         (None, None, None))
        self.assertEqual(ap.bar_touch(dict(tr, entry_time="bad"), [b("10:00", 111, 99)], DAY), (None, None, None))
        self.assertEqual(ap.bar_touch(dict(tr, target=None), [b("10:00", 999, 101)], DAY), (None, None, None))

    def test_stop_exits_at_the_stop_price(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        stop = self.state()["trades"]["CARTRADE"]["stop"]
        r.quotes = {"CARTRADE": stop - 20}
        self.tick("10:30", r)
        self.assertEqual(self.ticket_calls(r, "result")[1][6], "%.2f" % stop)
        self.assertEqual(self.state()["trades"]["CARTRADE"]["exit_reason"], "stop")

    def test_flat_by_exits_at_the_quote(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        r.quotes = {"CARTRADE": 2350.0}
        self.tick("14:58", r)
        self.assertEqual(self.ticket_calls(r, "flatten"), [])
        self.tick("15:00", r)
        tr = self.state()["trades"]["CARTRADE"]
        self.assertEqual((tr["exit_reason"], tr["exit_price"]), ("flat-by", 2350.0))

    def test_no_quote_means_retry_not_a_made_up_price(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.tick("15:12", r)
        self.assertEqual(self.ticket_calls(r, "flatten"), [])
        self.assertTrue(any("no fresh quote" in n for n in self.notes))

    def test_two_positions_at_once(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered", "AEGISLOG": "triggered"})
        self.tick("09:50", r)
        self.assertEqual(sorted(self.state()["trades"]), ["AEGISLOG", "CARTRADE"])
        r.quotes = {"CARTRADE": 2350.0, "AEGISLOG": 1130.0}
        self.tick("15:10", r)
        self.assertEqual(sorted(a[2] for a in self.ticket_calls(r, "flatten")), ["AEGISLOG", "CARTRADE"])
        journal = json.load(open(os.path.join(self.project, "trader", "autopilot", "journal-%s.json" % DAY)))
        self.assertEqual(journal["totals"]["closed"], 2)

    def test_max_trades_per_day(self):
        self.config({"paper": True, "max_trades_per_day": 1,
                     "autopilot": {"symbols": ["CARTRADE", "AEGISLOG"], "auto_paper": True}})
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered", "AEGISLOG": "triggered"})
        self.tick("09:50", r)
        self.assertEqual(list(self.state()["trades"]), ["CARTRADE"])
        self.assertTrue(any("max_trades_per_day (1) is reached" in n for n in self.notes))

    def test_one_trade_per_symbol(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.assertEqual(self.state()["done"]["CARTRADE"], "triggered")
        self.tick("09:52", r)
        self.assertEqual(len(self.ticket_calls(r, "approve")), 1)

    def test_auto_paper_with_a_live_config_is_refused_even_with_live_ok(self):
        self.config({"paper": False, "autopilot": {"symbols": ["CARTRADE"], "auto_paper": True}})
        for kw in ({}, {"live_ok": True}):
            with self.assertRaises(ap.AutopilotError):
                self.tick("09:50", FakeRunner(self.project), **kw)

    def test_auto_paper_must_be_a_real_true(self):
        for bad in ("true", 1, "yes"):
            with self.assertRaises(ap.AutopilotError):
                ap.settings({"autopilot": {"auto_paper": bad}})
        self.assertFalse(ap.settings({"autopilot": {"auto_paper": False}})["auto_paper"])

    def test_status_prints_the_journal(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        text = "\n".join(ap.status(self.project, at("10:00")))
        self.assertIn("| CARTRADE | 15 | 09:50 2344.20 | open |", text)


    def test_no_entries_after_blank_is_off_but_flat_by_still_stops_entries(self):
        base = {"symbols": ["CARTRADE"]}
        self.assertEqual(ap.settings({"autopilot": base})["entry_until_t"], datetime.time(14, 45))
        for off in (None, ""):
            s = ap.settings({"no_entries_after": off, "flat_by": "15:05", "autopilot": base})
            self.assertEqual(s["entry_until_t"], datetime.time(15, 5))
        s = ap.settings({"no_entries_after": "15:20", "autopilot": base})
        self.assertEqual(s["entry_until_t"], datetime.time(15, 0))


class AutoPaperHardening(Base):
    """Round 4: it runs unattended, so every half-finished step must recover on its own."""

    def setUp(self):
        Base.setUp(self)
        self.config({"paper": True, "broker": "angelone", "max_trades_per_day": 3,
                     "autopilot": {"symbols": ["CARTRADE", "AEGISLOG"], "auto_paper": True}})

    ticket_calls = AutoPaper.ticket_calls

    def test_b1_a_failed_entry_does_not_block_later_triggers(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, risk="BLOCK")
        self.tick("09:50", r)
        self.assertIsNotNone(ap.pending_ticket(self.project, DAY))  # the BLOCKed order is still "undecided"...
        r.risk, r.entry["AEGISLOG"] = "PASS", "triggered"
        r.quotes = {}
        self.tick("09:52", r)
        self.assertIn("AEGISLOG", self.state()["trades"])  # ...but it is the autopilot's own, so it does not block
        self.assertFalse(any("not the autopilot's" in n for n in self.notes))

    def test_b1_a_human_ticket_still_blocks(self):
        with open(os.path.join(self.project, "trader", "orders.json"), "w") as fh:
            json.dump({"created_at": "2026-11-04T04:00:00Z", "orders": [{"symbol": "INFY", "qty": "10"}]}, fh)
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.assertEqual(self.ticket_calls(r, "approve"), [])
        self.assertTrue(any("not the autopilot's" in n for n in self.notes))

    def test_b2_no_auto_paper_in_ticket_py_means_no_auto_actions(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, auto_help=False)
        self.tick("09:50", r)
        self.tick("09:52", r)
        self.assertNotIn("trader-entry/entry.py", r.tools())
        self.assertEqual(r.args_of(ap.TICKET, "approve"), [["approve", "--help"]])  # checked once a day
        self.assertEqual(sum("AUTO-PAPER IS OFF" in n for n in self.notes), 1)

    def test_h1_approved_entry_without_a_result_is_finished_next_tick(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, result_code=1)
        self.tick("09:50", r)
        self.assertEqual(self.state()["trades"]["CARTRADE"]["status"], "entering")
        r.result_code = 0
        self.tick("09:52", r)
        self.assertEqual(self.state()["trades"]["CARTRADE"]["status"], "open")
        self.assertEqual(len(self.ticket_calls(r, "approve")), 1)  # not approved twice

    def test_h1_approved_exit_without_a_result_is_finished_next_tick(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        r.quotes, r.result_code = {"CARTRADE": 2500.0}, 1
        self.tick("10:30", r)
        tr = self.state()["trades"]["CARTRADE"]
        self.assertEqual((tr["status"], tr["exit_planned"]), ("exiting", 2500.0))
        r.result_code = 0
        self.tick("10:32", r)
        self.assertEqual(self.state()["trades"]["CARTRADE"]["status"], "closed")
        self.assertEqual(len(self.ticket_calls(r, "flatten")), 1)

    def test_h2_the_trade_is_saved_before_approval(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        seen = {}
        real = r.ticket

        def spy(args, tr, now):
            if args[0] == "approve" and args != ["approve", "--help"]:
                seen["state"] = self.state()["trades"]["CARTRADE"]["status"]
            return real(args, tr, now)
        r.ticket = spy
        self.tick("09:50", r)
        self.assertEqual(seen["state"], "entering")

    def test_h2_an_orphan_paper_fill_is_adopted(self):
        os.makedirs(os.path.join(self.project, "trader", "autopilot"))
        with open(os.path.join(self.project, "trader", "autopilot", "plan-CARTRADE.json"), "w") as fh:
            json.dump({"symbol": "CARTRADE", "entry": 2344.2, "stop": 2300.0, "target": 2400.0}, fh)
        self.fill("CARTRADE", when="09:51")
        r = FakeRunner(self.project, quotes={"CARTRADE": 2290.0})
        self.tick("09:55", r)
        tr = self.state()["trades"]["CARTRADE"]
        self.assertTrue(tr["adopted"])
        self.assertEqual((tr["status"], tr["exit_reason"]), ("closed", "stop"))

    def test_h3_quotes_are_fetched_without_any_alert_rules(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, add_code=1)
        self.tick("09:50", r)
        r.quotes = {"CARTRADE": 2500.0}
        self.tick("10:30", r)
        self.assertEqual(self.state()["trades"]["CARTRADE"]["exit_reason"], "target")
        self.assertTrue(any(a[0] == "quote" for a in r.args_of("trader-alerts/alerts.py")))

    def test_m1_stale_quotes_are_not_used_live(self):
        r = FakeRunner(self.project, quote_age=30, quotes={"CARTRADE": 1.0})
        day = ap.Day(self.project, at("10:00"), r, self.notify)  # live mode: no test_now
        self.assertEqual(ap.quotes_for(day, ["CARTRADE"]), {})
        r.quote_age = 5
        self.assertEqual(ap.quotes_for(day, ["CARTRADE"]), {"CARTRADE": 1.0})

    def test_m1_prices_json_only_when_fresh(self):
        r = FakeRunner(self.project)
        day = ap.Day(self.project, at("10:00"), r, self.notify)
        for age, want in ((30, {}), (5, {"CARTRADE": 2350.0})):
            t = (ap.utc_now() - datetime.timedelta(minutes=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(os.path.join(self.project, "trader", "prices.json"), "w") as fh:
                json.dump({"as_of": t, "prices": {"CARTRADE": 2350.0}}, fh)
            self.assertEqual(ap.quotes_for(day, ["CARTRADE"]), want)

    def test_m2_entry_fills_at_the_quote_and_skips_if_already_through(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, quotes={"CARTRADE": 2350.0})
        self.tick("09:50", r)
        self.assertEqual(self.ticket_calls(r, "result")[0][6], "2350.00")
        self.tearDown()
        self.setUp()
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"}, quotes={"CARTRADE": 2500.0})
        self.tick("09:50", r)
        self.assertEqual(self.ticket_calls(r, "approve"), [])
        self.assertTrue(any("already 2500.00" in n for n in self.notes))

    def test_m3_exit_qty_is_the_trade_qty_not_everything_held(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        pf_path = os.path.join(self.project, "trader", "portfolio.json")
        pf = json.load(open(pf_path))
        pf["positions"][0]["shares"] = 40  # 25 more held from elsewhere
        json.dump(pf, open(pf_path, "w"))
        r.quotes = {"CARTRADE": 2500.0}
        self.tick("10:30", r)
        self.assertEqual(self.ticket_calls(r, "flatten")[0][4], "15")
        self.assertAlmostEqual(self.state()["trades"]["CARTRADE"]["gross"], round((2500.0 - 2344.2) * 15, 2))

    def test_m4_safety_net_square_off_at_15_15(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.state_set("CARTRADE", flat_by="15:25")
        r.quotes = {"CARTRADE": 2350.0}
        self.tick("15:15", r)
        self.assertEqual(self.state()["trades"]["CARTRADE"]["exit_reason"], "auto square-off")
        self.assertTrue(any("15:15 safety net" in n for n in self.notes))

    def test_m4_no_quote_at_the_end_alarms_and_carries_over(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        self.tick("15:20", r)
        self.assertTrue(any("AUTO-PAPER ALARM: CARTRADE is still open" in n for n in self.notes))
        r.quotes = {"CARTRADE": 2330.0}
        self.tick("09:16", r, date="2026-11-05")
        tr = self.state("2026-11-05")["trades"]["CARTRADE"]
        self.assertEqual((tr["status"], tr["exit_reason"], tr["carried_from"]), ("closed", "carried over", DAY))

    def state_set(self, sym, **kw):
        path = os.path.join(self.project, "trader", "state", "autopilot-%s.json" % DAY)
        st = json.load(open(path))
        st["trades"][sym].update(kw)
        json.dump(st, open(path, "w"))

    def test_stop_exit_retries_while_price_stays_under(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        stop = self.state()["trades"]["CARTRADE"]["stop"]
        r.quotes, r.approve_code = {"CARTRADE": stop - 5}, 1
        self.tick("10:30", r)
        self.assertEqual(self.state()["trades"]["CARTRADE"]["status"], "open")
        r.approve_code = 0
        self.tick("10:32", r)
        self.assertEqual(self.state()["trades"]["CARTRADE"]["exit_reason"], "stop")

    def test_max_trades_zero_means_no_limit(self):
        self.config({"paper": True, "max_trades_per_day": 0,
                     "autopilot": {"symbols": ["CARTRADE", "AEGISLOG"], "auto_paper": True}})
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered", "AEGISLOG": "triggered"})
        self.tick("09:50", r)
        self.assertEqual(sorted(self.state()["trades"]), ["AEGISLOG", "CARTRADE"])

    def test_exit_never_overwrites_a_human_ticket(self):
        r = FakeRunner(self.project, entry={"CARTRADE": "triggered"})
        self.tick("09:50", r)
        with open(os.path.join(self.project, "trader", "orders.json"), "w") as fh:
            json.dump({"created_at": "2026-11-04T04:40:00Z", "orders": [{"symbol": "INFY", "qty": "10"}]}, fh)
        r.quotes = {"CARTRADE": 2500.0}
        self.tick("10:30", r)
        self.assertEqual(self.ticket_calls(r, "flatten"), [])
        self.assertTrue(any("will not overwrite it" in n for n in self.notes))

    def test_json_is_read_from_stdout_only(self):
        def noisy(tool, args, cwd):
            return 0, json.dumps({"quotes": {"CARTRADE": {"price": 9.0, "time": "x"}}}), "warning: slow\n"
        day = ap.Day(self.project, at("10:00"), noisy, self.notify, test_now="%s 10:00" % DAY)
        self.assertEqual(ap.quotes_for(day, ["CARTRADE"]), {"CARTRADE": 9.0})

    def test_install_says_auto_paper(self):
        self.assertIn("AUTO-PAPER is on", "\n".join(ap.install(self.project)))


class TicketGuard(Base):
    def test_ticket_args_allowlist(self):
        ok = (["show", "--index", "1", "--json"], ["log"], ["flatten", "--symbol", "X", "--qty", "1"],
              ["approve", "--index", "1", "--fingerprint", "ab", "--auto-paper"],
              ["result", "--index", "1", "--status", "paper", "--filled-price", "1"])
        bad = (["approve", "--index", "1", "--fingerprint", "ab", "--reply", "yes"],
               ["approve", "--index", "1", "--fingerprint", "ab", "--auto-paper", "--reply", "yes"],
               ["result", "--index", "1", "--status", "placed"], ["result", "--status"], ["stop", "--index", "1"],
               ["decline", "--index", "1"], [])
        for a in ok:
            self.assertTrue(ap.ticket_args_ok(a), a)
        for a in bad:
            self.assertFalse(ap.ticket_args_ok(a), a)

    def test_human_mode_never_runs_the_ticket_tool(self):
        with self.assertRaises(ap.AutopilotError):
            ap.SubprocessRunner(ROOT)(ap.TICKET, ["show"], self.project)
        day = ap.Day(self.project, at("09:50"), FakeRunner(self.project), self.notify)
        with self.assertRaises(ap.AutopilotError):
            day.run(ap.TICKET, "show", "--index", "1", "--json")

    def test_auto_runner_still_refuses_a_human_approval(self):
        runner = ap.SubprocessRunner(ROOT, auto_paper=True)
        for args in (["approve", "--index", "1", "--fingerprint", "ab", "--reply", "yes"],
                     ["result", "--index", "1", "--status", "placed"]):
            with self.assertRaises(ap.AutopilotError):
                runner(ap.TICKET, args, self.project)


class AutoPaperIntegration(unittest.TestCase):
    """The real sibling tools, offline, on RELIANCE 2026-10-29: entry at the 9:40 trigger, auto
    approval, paper fill, then a target, a stop and a flat-by exit, and the journal."""

    FX = os.path.join(ROOT, "tests", "fixtures")
    OFF = {"trader-entry/entry.py": os.path.join(FX, "trader-entry", "RELIANCE-2026-10-29-5m.json"),
           "trader-timemachine/timemachine.py": os.path.join(FX, "trader-timemachine", "RELIANCE-60d5m.json.gz")}

    def setUp(self):
        with open(os.path.join(ROOT, "trader-broker", "ticket.py"), encoding="utf-8") as fh:
            if "--auto-paper" not in fh.read():
                self.skipTest("ticket.py has no --auto-paper yet")
        self.project = tempfile.mkdtemp()
        self.qdir = os.path.join(self.project, "quotes")
        os.makedirs(self.qdir)
        os.makedirs(os.path.join(self.project, "trader"))
        with open(os.path.join(self.project, "trader", "config.json"), "w") as fh:
            json.dump({"paper": True, "broker": "angelone", "budget": 100000, "max_loss_per_trade_inr": 1000,
                       "autopilot": {"symbols": ["RELIANCE"], "target": "1R", "auto_paper": True}}, fh)
        with open(os.path.join(self.FX, "trader-alerts", "yahoo-RELIANCE.json")) as fh:
            self.chart = json.load(fh)
        self.real = ap.SubprocessRunner(ROOT, auto_paper=True)
        self.notes = []

    def tearDown(self):
        shutil.rmtree(self.project)

    def quote(self, price):
        doc = json.loads(json.dumps(self.chart))
        doc["chart"]["result"][0]["meta"]["regularMarketPrice"] = price
        with open(os.path.join(self.qdir, "yahoo-RELIANCE.json"), "w") as fh:
            json.dump(doc, fh)

    fail_next = ()   # (tool, subcommand) pairs to fail once, as if the process died there

    def runner(self, tool, args, cwd):
        args = list(args)
        key = (tool, args[0] if args else None)
        if key in self.fail_next:
            self.fail_next = tuple(k for k in self.fail_next if k != key)
            return 1, "", "simulated crash"
        if tool in self.OFF:
            args += ["--offline", self.OFF[tool]]
        if tool == "trader-alerts/alerts.py" and args[0] in ("check", "quote", "bars"):
            args += ["--offline", self.qdir]
        return self.real(tool, args, cwd)

    def tick(self, hhmm):
        when = "2026-10-29 %s" % hhmm
        return ap.tick(self.project, now=at(hhmm, "2026-10-29"), runner=self.runner,
                       notifier=lambda t, x, s=False: self.notes.append(x) or True, test_now=when)

    def trade(self):
        st = json.load(open(os.path.join(self.project, "trader", "state", "autopilot-2026-10-29.json")))
        return st["trades"]["RELIANCE"]

    def enter(self):
        self.quote(1174.3)
        self.tick("09:40")
        tr = self.trade()
        self.assertEqual((tr["qty"], tr["entry_price"], tr["stop"], tr["target"]), (63, 1174.3, 1163.4, 1185.2))
        self.assertTrue(any(n.startswith("AUTO-PAPER BUY 63 RELIANCE at 1174.30") for n in self.notes))

    def test_hardening_with_the_real_tools(self):
        """M2 fill at the fresh quote, H3 exits with no alert rules at all, H1 an approved entry whose
        result never got recorded is finished on the next tick."""
        self.quote(1176.0)
        self.fail_next = ((ap.TICKET, "result"), ("trader-alerts/alerts.py", "add"))
        self.tick("09:40")
        tr = self.trade()
        self.assertEqual(tr["status"], "entering")
        self.tick("09:42")
        tr = self.trade()
        self.assertEqual((tr["status"], tr["entry_price"], tr["qty"]), ("open", 1176.0, 63))
        broker = json.load(open(os.path.join(self.project, "trader", "state", "broker.json")))["orders"]
        self.assertEqual([r["status"] for r in broker if r.get("side") == "buy" and r.get("status") != "filled"],
                         ["approved", "placed"])  # one approval, one result: never approved twice
        self.assertFalse(os.path.exists(os.path.join(self.project, "trader", "alerts.json")))
        self.quote(1190.0)
        self.tick("10:30")
        tr = self.trade()
        self.assertEqual((tr["exit_reason"], tr["exit_price"], tr["gross"]), ("target", 1190.0, round(14 * 63, 2)))

    def test_b1_a_blocked_entry_is_the_autopilots_own_order(self):
        with open(os.path.join(self.project, "trader", "config.json")) as fh:
            cfg = json.load(fh)
        cfg["max_daily_loss_inr"] = 500   # trader-budget sizes it, trader-risk BLOCKs the daily loss
        with open(os.path.join(self.project, "trader", "config.json"), "w") as fh:
            json.dump(cfg, fh)
        self.quote(1174.3)
        self.tick("09:40")
        self.assertTrue(any("no trade" in n for n in self.notes), self.notes)
        self.assertEqual(ap.pending_ticket(self.project, "2026-10-29"), "RELIANCE")
        day = ap.Day(self.project, at("09:42", "2026-10-29"), self.runner, lambda *a: True)
        self.assertIsNone(ap.human_pending(day))

    def test_target(self):
        self.enter()
        self.quote(1180.0)
        self.tick("10:00")
        self.assertEqual(self.trade()["status"], "open")
        self.quote(1186.0)
        self.tick("10:20")
        tr = self.trade()
        self.assertEqual((tr["exit_reason"], tr["exit_price"], tr["gross"]), ("target", 1186.0, 737.1))
        self.assertAlmostEqual(tr["net"], round(737.1 - tr["charges"], 2))
        pf = json.load(open(os.path.join(self.project, "trader", "portfolio.json")))
        self.assertEqual(sum(p.get("shares", 0) for p in pf["positions"] if p.get("ticker") == "RELIANCE"), 0)

    def test_stop(self):
        self.enter()
        self.quote(1150.0)
        self.tick("10:00")
        tr = self.trade()
        self.assertEqual((tr["exit_reason"], tr["exit_price"], tr["gross"]), ("stop", 1163.4, -686.7))

    def test_touch_between_ticks_on_real_bars(self):
        """RELIANCE's real 5-minute bars with one wick raised to 1,186 in the 10:05 bar: the 10:12 quote is
        back at 1,176, and the target (1,185.20) still fills, at the target, from the bar's high."""
        with open(os.path.join(self.FX, "trader-entry", "RELIANCE-2026-10-29-5m.json")) as fh:
            chart = json.load(fh)
        res = chart["chart"]["result"][0]
        with open(os.path.join(self.qdir, "bars-RELIANCE.json"), "w") as fh:
            json.dump(chart, fh)
        self.enter()
        self.quote(1176.0)
        self.tick("10:12")
        self.assertEqual(self.trade()["status"], "open")       # the real bars never reach 1,185.20 by then
        hi = res["indicators"]["quote"][0]["high"]
        for i, t in enumerate(res["timestamp"]):
            if (datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=t) + ap.IST).strftime(
                    "%Y-%m-%d %H:%M") == "2026-10-29 10:05":
                hi[i] = 1186.0
        with open(os.path.join(self.qdir, "bars-RELIANCE.json"), "w") as fh:
            json.dump(chart, fh)
        self.tick("10:14")
        tr = self.trade()
        self.assertEqual((tr["exit_reason"], tr["exit_price"], tr["touch_bar"], tr["gross"]),
                         ("target", 1185.2, "10:05", round((1185.2 - 1174.3) * 63, 2)))

    def test_flat_by(self):
        self.enter()
        self.quote(1178.0)
        self.tick("15:10")
        tr = self.trade()
        self.assertEqual((tr["exit_reason"], tr["exit_price"]), ("flat-by", 1178.0))
        journal = json.load(open(os.path.join(self.project, "trader", "autopilot", "journal-2026-10-29.json")))
        self.assertEqual(journal["totals"]["closed"], 1)
        self.assertAlmostEqual(journal["totals"]["gross"], round((1178.0 - 1174.3) * 63, 2))


class MorningPick(Base):
    """autopilot.auto_pick: the NIFTY 500's most volatile, ranked by the breakout rule's own history."""

    VOL = [("CARTRADE", 7.8), ("AEGISLOG", 7.5), ("BHEL", 3.9), ("GVT&D", 4.0), ("SWIGGY", 5.8), ("TINY", 6.0),
           ("RESULT", 5.0)]
    HIST = {"CARTRADE": {"avg_R": 0.05, "triggered": 30, "target": 8, "stop": 14},
            "AEGISLOG": {"avg_R": -0.10, "triggered": 31, "target": 6, "stop": 16},
            "BHEL": {"avg_R": 0.218, "triggered": 33, "target": 9, "stop": 15},
            "GVT&D": {"avg_R": 0.262, "triggered": 32, "target": 6, "stop": 10},
            "SWIGGY": {"avg_R": -0.061, "triggered": 32, "target": 7, "stop": 15},
            "TINY": {"avg_R": 0.9, "triggered": 8, "target": 4, "stop": 2},
            "RESULT": {"avg_R": 0.5, "triggered": 30, "target": 12, "stop": 10}}

    def setUp(self):
        super().setUp()
        self.config({"paper": True, "broker": "angelone",
                     "autopilot": {"symbols": ["CARTRADE"], "auto_paper": True, "auto_pick": {"top": 3}}})

    def runner(self, **kw):
        return FakeRunner(self.project, vol=self.VOL, hist=self.HIST,
                          due=[{"symbol": "RESULT", "date": DAY, "link": "x"}], **kw)

    def test_pick_ranks_by_history_and_drops_results_and_thin_records(self):
        r = self.runner()
        self.tick("08:56", r)
        pick = self.state()["pick"]
        self.assertEqual(pick["symbols"], ["GVT&D", "BHEL", "CARTRADE"])     # AEGISLOG, SWIGGY under 0.0
        skip = {x["symbol"]: x["skip"] for x in pick["rows"]}
        self.assertEqual((skip["RESULT"], skip["TINY"]), ("results today", "only 8 past breakouts"))
        self.assertIn("under +0.00", skip["SWIGGY"])
        self.assertTrue(any(n.startswith("Picked for today: GVT&D (+0.26R over 32, ATR 4.0%)") for n in self.notes))
        hist = [a for t, a in r.calls if t == "trader-timemachine/timemachine.py"][0]
        self.assertEqual(hist[hist.index("--stop-rule") + 1:hist.index("--stop-rule") + 2], ["or_mid"])
        self.assertEqual(hist[hist.index("--target-r") + 1], "2")
        # the pick runs once; news and entries use it
        self.tick("09:01", r)
        news = [a for t, a in r.calls if t == "trader-news/news.py" and "--json" not in a][0]
        self.assertEqual(news[news.index("--tickers") + 1], "GVT&D,BHEL,CARTRADE")
        self.assertEqual(sum(1 for t, a in r.calls if t == "trader-scanner/scan.py"), 1)
        self.tick("09:40", r)
        checked = [a[1] for t, a in r.calls if t == "trader-entry/entry.py"]
        self.assertEqual(checked, ["GVT&D", "BHEL", "CARTRADE"])

    def test_failed_pick_retries_then_falls_back_to_the_config(self):
        r = self.runner(scan_code=1)
        self.tick("08:56", r)
        self.assertNotIn("pick", self.state())
        self.assertTrue(any("Morning pick failed" in n for n in self.notes))
        self.tick("09:00", r)
        self.assertEqual([a for t, a in r.calls if t == "trader-news/news.py"], [])   # nothing to watch yet
        self.tick("09:26", r)
        self.assertEqual(self.state()["pick"]["symbols"], ["CARTRADE"])
        self.assertTrue(any("watching the configured symbols instead (CARTRADE)" in n for n in self.notes))
        self.tick("09:40", r)
        self.assertEqual([a[1] for t, a in r.calls if t == "trader-entry/entry.py"], ["CARTRADE"])

    def test_nothing_cleared(self):
        self.config({"paper": True, "autopilot": {"auto_pick": {"min_avg_r": 5}}})
        r = self.runner()
        self.tick("08:56", r)
        self.assertEqual(self.state()["pick"]["symbols"], [])
        self.assertTrue(any(n.startswith("No stock cleared the pick today") for n in self.notes))
        self.tick("09:40", r)
        self.assertEqual([a for t, a in r.calls if t == "trader-entry/entry.py"], [])

    def test_an_open_trade_is_watched_even_if_not_picked(self):
        os.makedirs(os.path.join(self.project, "trader", "state"), exist_ok=True)
        with open(os.path.join(self.project, "trader", "state", "autopilot-%s.json" % DAY), "w") as fh:
            json.dump({"date": DAY, "news": True, "done": {"SWIGGY": "triggered"}, "active": None, "alerts": False,
                       "events": [], "notes": [], "paper": True,
                       "pick": {"at": "08:56", "symbols": ["BHEL"], "rows": []},
                       "trades": {"SWIGGY": {"symbol": "SWIGGY", "status": "open", "qty": 10, "entry_price": 178.4,
                                             "entry_time": "09:35", "stop": 176.43, "target": 182.04}}}, fh)
        r = self.runner()
        r.quotes = {"SWIGGY": 179.0}
        status = self.tick("10:00", r)
        self.assertIn("(2 symbols)", status)

    def test_pick_settings(self):
        self.assertIsNone(ap.pick_settings(None))
        self.assertIsNone(ap.pick_settings({"enabled": False}))
        self.assertEqual(ap.pick_settings(True)["top"], 4)
        for bad in ({"top": 0}, {"top": 5, "candidates": 4}, {"index": "nifty50"}, {"min_avg_r": "x"},
                    {"at": "9am"}, "yes", {"candidates": True}):
            with self.assertRaises(ap.AutopilotError):
                ap.pick_settings(bad)
        s = ap.settings({"autopilot": {"stop": "pct:1.5", "target": "1.5R"}})
        self.assertEqual(ap.pick_rule(s), (["--stop-rule", "pct", "--stop-pct", "1.5", "--target-r", "1.5",
                                            "--until", "15:00"], ""))
        with self.assertRaises(ap.AutopilotError):
            ap.pick_settings({"at": "09:20", "until": "09:00"})

    def test_scan_args_are_guarded(self):
        good = ["volatile", "--index", "nifty500", "--top", "20", "--out",
                os.path.join("trader", "autopilot", "pick-volatile-%s.json" % DAY), "--quiet"]
        self.assertTrue(ap.scan_args_ok(good))
        self.assertTrue(ap.scan_args_ok(good + ["--now", "%s 08:56" % DAY]))
        for bad in (["universe", "--index", "nifty500"], ["daily"], good[:-1],
                    good[:6] + ["trader/universe.txt", "--quiet"],
                    good[:6] + [os.path.join("trader", "autopilot", "pick-volatile-../../x.json"), "--quiet"]):
            self.assertFalse(ap.scan_args_ok(bad), bad)
        day = ap.Day(self.project, at("08:56"), lambda *a: (0, ""), self.notify)
        with self.assertRaises(ap.AutopilotError):
            day.run("trader-scanner/scan.py", "universe", "--index", "nifty500")

    def test_news_failure_is_flagged_and_retry_reuses_the_scan(self):
        r = self.runner()
        r.due = []
        real = r.__call__

        def failing(tool, args, cwd):
            if tool == "trader-news/news.py" and "--json" in args:
                r.calls.append((tool, list(args)))
                return 2, json.dumps({"results_due": [], "items": [], "notes": ["HTTP 429"]})
            return real(tool, args, cwd)
        self.tick("08:56", failing)
        self.assertTrue(any("results days not filtered" in n for n in self.notes))
        self.assertEqual(self.state()["pick"]["symbols"][0], "RESULT")   # unfiltered, and said so
        # a retry after a later step failed does not scan the NIFTY 500 again
        st = self.state()
        del st["pick"]
        with open(os.path.join(self.project, "trader", "state", "autopilot-%s.json" % DAY), "w") as fh:
            json.dump(st, fh)
        self.tick("08:58", r)
        self.assertEqual(sum(1 for t, a in r.calls if t == "trader-scanner/scan.py"), 1)

    def test_fallback_list_is_not_the_saved_pick(self):
        os.makedirs(os.path.join(self.project, "trader", "state"), exist_ok=True)
        with open(os.path.join(self.project, "trader", "state", "autopilot-%s.json" % DAY), "w") as fh:
            json.dump({"date": DAY, "news": True, "done": {}, "active": None, "alerts": False, "events": [],
                       "notes": [], "paper": True,
                       "trades": {"SWIGGY": {"symbol": "SWIGGY", "status": "open", "qty": 10, "entry_price": 178.4,
                                             "entry_time": "09:35", "stop": 176.43, "target": 182.04}}}, fh)
        r = self.runner(scan_code=1)
        r.quotes = {"SWIGGY": 179.0}
        self.tick("09:26", r)
        self.assertEqual(self.state()["pick"]["symbols"], ["CARTRADE"])
        s = ap.settings({"autopilot": {"stop": "atr:1", "target": "price:200"}})
        self.assertIn("cannot be replayed", ap.pick_rule(s)[1])


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-autopilot", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-autopilot\n"))
        low = " ".join(text.lower().split())
        for bit in ("not financial advice", "yes", "awake", "launchctl bootout", "trader/autopilot/logs",
                    "documents", "stale", "pending"):
            self.assertIn(bit, low)
        self.assertNotIn("\u2014", text)


if __name__ == "__main__":
    unittest.main()


class FnoAwareSquareOff(unittest.TestCase):
    """A scrip with F&O contracts has to be out earlier: Angel One squares it off at 15:10
    and, until the revised CAS starts, its continuous trading ends at 15:15."""

    FNO = frozenset({"ATHERENERG", "POLICYBZR"})

    def test_the_cutoff_depends_on_the_scrip_and_the_date(self):
        inside, after = datetime.date(2026, 11, 5), datetime.date(2026, 12, 7)
        self.assertEqual(ap.auto_square_off("ATHERENERG", inside, self.FNO), ap.AUTO_SQUARE_OFF_FNO)
        self.assertEqual(ap.auto_square_off("CARTRADE", inside, self.FNO), ap.AUTO_SQUARE_OFF)
        self.assertEqual(ap.auto_square_off("ATHERENERG", after, self.FNO), ap.AUTO_SQUARE_OFF)

    def test_flat_by_is_read_before_the_backstop(self):
        def reason(hhmm, flat, cutoff):
            h, m = (int(x) for x in hhmm.split(":"))
            clock = datetime.datetime(2026, 11, 5, h, m)
            trade = {"stop": 1.0, "target": 9e9, "flat_by": flat}
            return ap.exit_reason(trade, 5.0, clock, cutoff)[0]

        early = datetime.time(15, 5)
        self.assertIsNone(reason("15:04", "15:10", early))
        self.assertEqual(reason("15:05", "15:10", early), "auto square-off")   # the backstop is sooner
        self.assertEqual(reason("15:00", "15:00", early), "flat-by")           # the plan's own time wins
        # a flat_by later than the scrip can safely hold still gets closed by the backstop
        self.assertEqual(reason("15:10", "15:25", datetime.time(15, 10)), "auto square-off")

    def test_the_list_is_cached_and_never_fetched_twice(self):
        proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proj)
        cache = os.path.join(proj, "trader", "cache", ap.FO_CACHE)
        os.makedirs(os.path.dirname(cache))
        with open(cache, "w", encoding="utf-8") as fh:
            fh.write("UNDERLYING,SYMBOL,NOV-26\nAther Energy Limited,ATHERENERG,900\n")
        with mock.patch.object(ap, "_fno_from", wraps=ap._fno_from) as spy:
            syms = ap.load_fno(project=proj)
        self.assertEqual(syms, frozenset({"ATHERENERG"}))
        self.assertEqual(spy.call_count, 1)        # read from disk, no request

    def test_an_unreadable_list_treats_every_scrip_as_non_fno(self):
        proj = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, proj)
        self.assertEqual(ap.load_fno(skills=proj, project=proj), frozenset())
        self.assertEqual(ap.auto_square_off("ATHERENERG", datetime.date(2026, 11, 5), frozenset()),
                         ap.AUTO_SQUARE_OFF)
