import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")

# The order is RELIANCE's opening-range breakout on Mon 2026-10-19 (real 5-minute
# bars: range 1207.30 to 1223.40, the 9:45 bar closed 1227.80), sized to the pack's
# default limits. The risk verdicts are written here, so these tests do not depend
# on trader-risk; FingerprintParity checks the two tools agree.
ORDERS_PATH = os.path.join(FIX, "trader-broker", "orders-reliance.json")
PLAN_PATH = os.path.join(FIX, "trader-broker", "plan-reliance.json")
NOW = "2026-10-19 09:50"
TODAY = "2026-10-19"
CFG = {"budget": 100000, "broker": "zerodha", "paper": False, "product": "MIS", "flat_by": "15:10"}
BATCH = ["RELIANCE", "TCS", "INFY", "M&M", "360ONE"]
PLAIN = ("BUY 65 RELIANCE on NSE, market, intraday (MIS), good for today / "
         "stop-loss 1215.40 (SL-M, placed after the entry fills) / "
         "target 1240.20 (watched by trader-alerts, not a resting order) / "
         "loss about ₹806 if the stop fills at 1215.40 (it can fill worse in a fast market) / "
         "plus about ₹61.17 estimated charges (brokerage, STT, exchange, SEBI, stamp, GST): "
         "max loss about ₹867.17 all in")


def rel(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


KITE_ENTRY = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
              "quantity": 65, "product": "MIS", "order_type": "MARKET", "validity": "DAY",
              "tag": "dts" + ticket.fingerprint(rel())[:8]}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


def json_after(text, marker):
    """The first JSON object printed after a marker line."""
    start = text.index("{", text.index(marker))
    depth = 0
    for i in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return json.loads(text[start:i + 1])
    raise AssertionError("no JSON after " + marker)


class Money(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(ticket.inr(100000), "₹1,00,000")
        self.assertEqual(ticket.inr(1234567.5), "₹12,34,567.50")
        self.assertEqual(ticket.inr(806), "₹806")
        self.assertEqual(ticket.inr(-1500.25), "-₹1,500.25")

    def test_symbols(self):
        self.assertEqual(ticket.norm_symbol("reliance.ns"), "RELIANCE")
        self.assertEqual(ticket.norm_symbol("M&M"), "M&M")
        self.assertEqual(ticket.norm_symbol("bajaj-auto"), "BAJAJ-AUTO")
        for bad in ("RELIANCE.BO", "", "RELI ANCE", "-X"):
            with self.assertRaises(ticket.TicketError):
                ticket.norm_symbol(bad)


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=None, batch=BATCH):
        return ticket.parse_yes(reply, order or rel(), batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes RELIANCE", "yes 65 RELIANCE",
                  "yes 1215.40", "yes max loss 806", "yes target 1240.20", "yes reliance.ns"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "haan", "go", "do it", "yeah", "yep", "send it",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both",
                  "yes TCS", "yes M&M", "yes 70", "yes but make it 50", "yes?", "no", "not yes",
                  "yes, wait", "yes if it is under 1230", "yes, stop 1215.40"):
            self.assertFalse(self.yes(r), r)

    def test_digits_in_a_symbol_are_not_an_amount(self):
        o = {"symbol": "360ONE", "exchange": "NSE", "product": "MIS", "side": "sell", "qty": "3",
             "type": "market", "time_in_force": "day"}
        self.assertTrue(self.yes("yes sell 3 360ONE", o))
        self.assertFalse(self.yes("yes sell 4 360ONE", o))
        self.assertFalse(self.yes("yes 360ONE", rel()))


class BrokerCalls(unittest.TestCase):
    def test_zerodha_entry_and_stop(self):
        calls = ticket.entry_calls(rel(), "zerodha")
        self.assertEqual(calls, [("place_order", KITE_ENTRY)])
        self.assertLessEqual(len(KITE_ENTRY["tag"]), 20)
        self.assertTrue(KITE_ENTRY["tag"].isalnum())
        tool, args = ticket.stop_call(rel(), "zerodha", 60)
        self.assertEqual(args, {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                                "transaction_type": "SELL", "quantity": 60, "product": "MIS",
                                "order_type": "SL-M", "trigger_price": 1215.4, "validity": "DAY",
                                "tag": KITE_ENTRY["tag"] + "sl"})

    def test_zerodha_limit(self):
        args = ticket.entry_calls(rel(type="limit", limit_price="1226.50"), "zerodha")[0][1]
        self.assertEqual((args["order_type"], args["price"]), ("LIMIT", 1226.5))

    def test_angel_searches_then_places(self):
        calls = ticket.entry_calls(rel(), "angelone")
        self.assertEqual(calls[0], ("search_scrip", {"exchange": "NSE", "searchscrip": "RELIANCE"}))
        tool, args = calls[1]
        self.assertEqual(tool, "place_order")
        self.assertEqual(args["tradingsymbol"], "RELIANCE-EQ")
        self.assertIn("exactly RELIANCE-EQ", args["symboltoken"])
        self.assertEqual({k: v for k, v in args.items() if k != "symboltoken"},
                         {"variety": "NORMAL", "tradingsymbol": "RELIANCE-EQ", "transactiontype": "BUY",
                          "exchange": "NSE", "ordertype": "MARKET", "producttype": "INTRADAY",
                          "duration": "DAY", "quantity": "65"})
        self.assertNotIn("force", args)
        tool, stop = ticket.stop_call(rel(), "angelone", 65)
        self.assertEqual((stop["variety"], stop["ordertype"], stop["triggerprice"], stop["transactiontype"],
                          stop["quantity"]), ("STOPLOSS", "STOPLOSS_MARKET", "1215.40", "SELL", "65"))

    def test_flatten_cancels_the_stop_first(self):
        o = {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "sell", "qty": "65",
             "type": "market", "time_in_force": "day", "cancel_stop": {"broker_order_id": "261019000123"}}
        k = ticket.entry_calls(o, "zerodha")
        self.assertEqual(k[0], ("cancel_order", {"variety": "regular", "order_id": "261019000123"}))
        self.assertEqual(k[1][1]["transaction_type"], "SELL")
        a = ticket.entry_calls(o, "angelone")
        self.assertEqual(a[0], ("cancel_order", {"variety": "STOPLOSS", "orderid": "261019000123"}))

    def test_plain_english(self):
        self.assertEqual(ticket.plain(rel()), PLAIN)
        self.assertEqual(ticket.plain({"symbol": "TCS", "side": "sell", "qty": "1", "type": "limit",
                                       "limit_price": "3050", "time_in_force": "day", "product": "MIS"}),
                         "SELL 1 share of TCS on NSE, limit order at ₹3,050, intraday (MIS), good for today")
        lim = rel(type="limit", limit_price="1226.50")
        self.assertEqual(ticket.max_loss(lim), (721.5, 1215.4))

    def test_unknown_broker_is_refused(self):
        with self.assertRaises(ticket.TicketError):
            ticket.broker_of({"broker": "alpaca"})
        self.assertEqual(ticket.broker_of({"broker": "Angel One"}), "angelone")
        self.assertEqual(ticket.broker_of({}), "zerodha")


class FingerprintParity(unittest.TestCase):
    def test_same_fields_as_trader_risk(self):
        check = load("trader-risk", "check")
        o = rel()
        self.assertEqual(ticket.fingerprint(o), check.fingerprint(o))
        self.assertNotEqual(ticket.fingerprint(o), ticket.fingerprint(rel(product="CNC")))
        self.assertEqual(ticket.fingerprint(o), ticket.fingerprint(rel(why="other words")))


class ConfigExamples(unittest.TestCase):
    def test_placeholders_only(self):
        with open(os.path.join(ROOT, "trader-broker", "claude_desktop_config.example.json")) as fh:
            servers = json.load(fh)["mcpServers"]
        self.assertEqual(servers["kite"]["env"]["APP_MODE"], "stdio")
        self.assertEqual(servers["angel-one"]["args"], ["-y", "angel-one-mcp"])
        for name, keys in (("kite", ("KITE_API_KEY", "KITE_API_SECRET")),
                           ("angel-one", ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD",
                                          "ANGEL_TOTP_SECRET"))):
            for k in keys:
                self.assertIn("NEVER_IN_CHAT", servers[name]["env"][k])
        self.assertEqual(servers["angel-one"]["env"]["HARD_MAX_ORDER_VALUE"], "80000")
        with open(os.path.join(ROOT, "trader-broker", "mcp.example.json")) as fh:
            code = json.load(fh)["mcpServers"]
        self.assertEqual(code["kite"]["env"]["KITE_API_KEY"], "${KITE_API_KEY}")


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        self.config(**CFG)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def config(self, **c):
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def pass_risk(self, verdict="PASS", date=TODAY, reasons=(), kind=None):
        """What trader-risk writes: a verdict per order fingerprint."""
        with open(self.orders) as fh:
            orders = json.load(fh)["orders"]
        os.makedirs(os.path.dirname(self.risk), exist_ok=True)
        results = []
        for o in orders:
            r = {"symbol": o["symbol"], "fingerprint": ticket.fingerprint(o), "verdict": verdict,
                 "reasons": list(reasons), "warnings": []}
            r["kind"] = kind or ("entry" if o["side"] == "buy" else "exit")
            results.append(r)
        with open(self.risk, "w") as fh:
            json.dump({"date": date, "results": results}, fh)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def state_doc(self):
        with open(self.state) as fh:
            return json.load(fh)

    def portfolio(self):
        with open(self.pf) as fh:
            return json.load(fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_live_zerodha_flow_with_the_stop(self):
        self.pass_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, LIVE ACCOUNT, REAL MONEY, Zerodha (Kite MCP)", text)
        for part in PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: trader-risk PASS", text)
        self.assertEqual(json_after(text, "Step 1"), KITE_ENTRY)
        self.assertEqual(json_after(text, "Step 2")["order_type"], "SL-M")
        self.assertIn("could both fill and leave you short", text)
        self.assertNotIn('"plan"', text)
        self.assertNotIn('"why"', text)
        self.assertIn("Allow once", text)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertEqual(json_after(text, "place_order"), KITE_ENTRY)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)

        code, _, err = run(self.args("result", "--index", "1", "--status", "paper"))
        self.assertEqual(code, 1)
        self.assertIn("paper mode only", err)

        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "261019000101",
                                      "--filled-qty", "65", "--filled-price", "1228.10", "--portfolio", self.pf,
                                      now="2026-10-19 09:51"))
        self.assertEqual(code, 0)
        self.assertIn("ticket.py stop --index 1", text)
        self.assertIn("NO stop", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        appr, plc = st["orders"][0], st["orders"][1]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["date"], appr["broker"]),
                         ("yes", "entry", 867.17, TODAY, "zerodha"))
        self.assertEqual(appr["at"], "2026-10-19T09:50:00+05:30")
        self.assertEqual((plc["broker_order_id"], plc["max_loss"], plc["filled_qty"]), ("261019000101", 867.17, "65"))
        pf = self.portfolio()
        self.assertEqual(pf["cash"], 20173.5)
        self.assertEqual(pf["positions"][0]["shares"], 65)

        # step 2: the stop for the filled qty
        code, text, _ = run(self.args("stop", "--index", "1", now="2026-10-19 09:51"))
        self.assertEqual(code, 0)
        stop = json_after(text, "place_order")
        self.assertEqual((stop["order_type"], stop["trigger_price"], stop["quantity"], stop["transaction_type"]),
                         ("SL-M", 1215.4, 65, "SELL"))
        self.assertEqual(run(self.args("stop", "--index", "1", "--status", "placed"))[0], 1)  # needs the id
        code, text, _ = run(self.args("stop", "--index", "1", "--status", "placed", "--broker-id", "261019000102"))
        self.assertEqual(code, 0)
        self.assertEqual(ticket.open_stop(self.state_doc(), "RELIANCE", TODAY)["broker_order_id"], "261019000102")

        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)
        code, text, _ = run(["log", "--state", self.state, "--now", NOW])
        self.assertIn("1 placed today", text)
        self.assertIn("max loss ₹867.17", text)
        self.assertIn("stop_placed", text)

        # flat by 15:10: cancel the stop, then sell
        late = "2026-10-19 15:10"
        code, text, _ = run(["flatten", "--symbol", "reliance", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--state", self.state, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("SELL 65 shares of RELIANCE on NSE, market order, intraday (MIS), good for today "
                      "(flat by 15:10 IST)", text)
        self.assertIn("replaced 1 earlier proposal", text)
        self.assertIn("cancels the open stop (broker id 261019000102) first", text)
        with open(self.orders) as fh:
            flat = json.load(fh)["orders"][0]
        self.assertEqual(flat["cancel_stop"]["broker_order_id"], "261019000102")
        self.pass_risk(kind="exit")
        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 0)
        self.assertLess(text.index("cancel_order"), text.index("place_order"))
        self.assertIn('"order_id": "261019000102"', text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 65 RELIANCE", now=late))[0], 0)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "261019000150",
                                   "--filled-qty", "65", "--filled-price", "1236.00", "--portfolio", self.pf,
                                   now="2026-10-19 15:11"))
        self.assertEqual(code, 0)
        pf = self.portfolio()
        self.assertEqual((pf["cash"], pf["positions"]), (100513.5, []))
        self.assertIsNone(ticket.open_stop(self.state_doc(), "RELIANCE", TODAY))
        code, _, err = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_a_failed_stop_is_loud(self):
        self.pass_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "1", "--filled-qty", "65",
                      "--filled-price", "1228.10", "--portfolio", self.pf))
        code, text, _ = run(self.args("stop", "--index", "1", "--status", "failed", "--note", "RMS rejected"))
        self.assertEqual(code, 1)
        self.assertIn("WARNING: the stop was NOT placed", text)
        self.assertIn("flatten --symbol RELIANCE --qty 65", text)
        self.assertIsNone(ticket.open_stop(self.state_doc(), "RELIANCE", TODAY))

    def test_the_stop_waits_for_the_entry(self):
        self.pass_risk()
        code, _, err = run(self.args("stop", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("comes after the fill", err)

    def test_angel_one_ticket(self):
        self.config(**dict(CFG, broker="angelone"))
        self.pass_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("LIVE ACCOUNT, REAL MONEY, Angel One (angel-one-mcp)", text)
        self.assertEqual(json_after(text, "1. search_scrip"), {"exchange": "NSE", "searchscrip": "RELIANCE"})
        self.assertEqual(json_after(text, "2. place_order")["tradingsymbol"], "RELIANCE-EQ")
        self.assertIn("Never pass force: true", text)
        self.assertEqual(json_after(text, "Step 2")["ordertype"], "STOPLOSS_MARKET")

    def test_paper_mode_calls_no_broker(self):
        self.config(**dict(CFG, paper=True))
        self.pass_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER (SIMULATED: no order goes to Zerodha)", text)
        self.assertIn("Claude calls no broker tool", text)
        self.assertNotIn("place_order", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertNotIn("place_order", text)
        self.assertIn("--status paper", text)
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("record --status paper", err)
        code, text, _ = run(self.args("result", "--index", "1", "--status", "paper", "--portfolio", self.pf))
        self.assertEqual(code, 0)
        self.assertIn("Simulated protective stop at 1215.40", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled", "stop_placed"])
        self.assertTrue(all(r.get("paper") for r in st["orders"][1:]))
        self.assertEqual(st["orders"][2]["price"], 1227.8)          # the plan's entry
        self.assertEqual(self.portfolio()["cash"], 20193.0)
        self.assertTrue(ticket.open_stop(st, "RELIANCE", TODAY))

        # the paper flatten cancels the simulated stop with the simulated sell
        run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
             "--plan", PLAN_PATH, "--config", self.cfg, "--state", self.state, "--now", "2026-10-19 11:00"])
        self.pass_risk(kind="exit")
        run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 11:00"))
        code, _, _ = run(self.args("result", "--index", "1", "--status", "paper", "--filled-price", "1240.20",
                                   "--portfolio", self.pf, now="2026-10-19 11:00"))
        self.assertEqual(code, 0)
        self.assertEqual(self.portfolio()["cash"], 100806.0)
        self.assertIsNone(ticket.open_stop(self.state_doc(), "RELIANCE", TODAY))

    def test_flatten_with_the_broker_qty(self):
        code, text, _ = run(["flatten", "--symbol", "reliance.ns", "--qty", "65", "--orders", self.orders,
                             "--portfolio", self.none, "--plan", self.none, "--config", self.cfg,
                             "--state", self.state, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertNotIn("Check it against get_positions", text)
        self.assertIn("No open stop is recorded for RELIANCE", text)
        self.assertEqual(run(["flatten", "--symbol", "RELIANCE", "--qty", "0", "--orders", self.orders])[0], 1)
        self.assertEqual(run(["flatten", "--symbol", "RELIANCE", "--qty", "2.5", "--orders", self.orders])[0], 1)

    def test_an_edited_order_is_not_ready(self):
        self.pass_risk()
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["qty"] = "66"
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.pass_risk(date="2026-10-16")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.pass_risk(verdict="BLOCK", reasons=["window: no new entries before 09:30"])
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("BLOCKED by trader-risk: window: no new entries before 09:30", text)
        code, _, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.pass_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)

    def test_fill_sell_relieves_the_oldest_lot(self):
        pf = {"cash": 0, "positions": [{"ticker": "TCS", "shares": 3, "lots": [
            {"id": "TCS-1", "date": "2025-01-02", "shares": 2, "cost": 6000.0},
            {"id": "TCS-2", "date": "2026-01-02", "shares": 1, "cost": 3100.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["fill", "--symbol", "tcs", "--side", "sell", "--qty", "2", "--price", "3050",
                             "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 0)
        self.assertIn("Cash now ₹6,100", text)
        pf = self.portfolio()
        self.assertEqual(pf["cash"], 6100.0)
        self.assertEqual([l["id"] for l in pf["positions"][0]["lots"]], ["TCS-2"])
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "filled")
        code, _, err = run(["fill", "--symbol", "TCS", "--side", "sell", "--qty", "5", "--price", "3050",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        code, _, err = run(["fill", "--symbol", "TCS", "--side", "sell", "--qty", "0.5", "--price", "3050",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertIn("whole shares", err)


if __name__ == "__main__":
    unittest.main()
