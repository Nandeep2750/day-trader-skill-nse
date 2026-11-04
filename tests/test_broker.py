import datetime
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
CFG = {"budget": 100000, "broker": "zerodha", "paper": False, "product": "MIS", "flat_by": "15:10",
       "no_entries_after": "14:45"}
BATCH = ["RELIANCE", "TCS", "INFY", "M&M", "360ONE"]
PLAIN = ("BUY 65 RELIANCE on NSE, market, intraday (MIS), good for today / "
         "stop-loss 1215.40 (SL-M, placed after the entry fills) / "
         "target 1240.20 (a resting LIMIT order, placed after the stop; when one fills, cancel the other) / "
         "loss about ₹806 if the stop fills at 1215.40 (it can fill worse in a fast market) / "
         "plus about ₹61.17 estimated charges (brokerage, STT, exchange, SEBI, stamp, GST): "
         "max loss about ₹867.17 all in")


def rel(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def W(**over):
    return ticket.canonical(rel(**over), CFG)


FP = ticket.fingerprint(rel(), CFG)
KITE_ENTRY = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
              "quantity": 65, "product": "MIS", "order_type": "MARKET", "validity": "DAY", "tag": "dts" + FP[:8]}


def utc_z(ist_text):
    return ticket.parse_now(ist_text).strftime("%Y-%m-%dT%H:%M:%SZ")


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


class Canonical(unittest.TestCase):
    def test_case_is_normalised_and_a_limit_stays_a_limit(self):
        w = W(type="LIMIT", limit_price="1226.50", side="BUY", product="mis", exchange="nse")
        self.assertEqual((w["type"], w["side"], w["product"], w["exchange"]), ("limit", "buy", "MIS", "NSE"))
        self.assertTrue(ticket.plain(w).startswith("BUY 65 RELIANCE on NSE, limit 1226.50,"))
        args = ticket.entry_calls(w, "zerodha", FP)[0][1]
        self.assertEqual((args["order_type"], args["price"]), ("LIMIT", 1226.5))
        angel = ticket.entry_calls(w, "angelone", FP)[1][1]
        self.assertEqual((angel["ordertype"], angel["price"]), ("LIMIT", "1226.50"))

    def test_anything_not_canonical_is_refused(self):
        for over in ({"type": "stop"}, {"type": "Market Order"}, {"side": "short"}, {"product": "NRML"},
                     {"exchange": "BSE"}, {"time_in_force": "gtc"}, {"qty": "2.5"}, {"qty": "0"},
                     {"type": "limit"}, {"order_class": "oco"}, {"stop_loss_stop_price": "x"}):
            with self.assertRaises(ticket.TicketError, msg=str(over)):
                W(**over)

    def test_paper_is_live_only_when_literally_false(self):
        self.assertTrue(ticket.is_paper({"paper": None}))
        self.assertTrue(ticket.is_paper({"paper": 0}))
        self.assertTrue(ticket.is_paper({}))
        self.assertFalse(ticket.is_paper({"paper": False}))
        self.assertEqual(ticket.fingerprint(rel(), dict(CFG, paper=None)),
                         ticket.fingerprint(rel(), dict(CFG, paper=True)))

    def test_cancel_ids_are_sorted_into_the_fingerprint(self):
        a = rel(cancel_stop={"broker_order_ids": ["12", "11"]})
        b = rel(cancel_stop={"broker_order_id": " 11, 12 ,"})
        self.assertEqual(ticket.cancel_ids(b), ["11", "12"])
        self.assertEqual(ticket.fingerprint(a, CFG), ticket.fingerprint(b, CFG))
        self.assertEqual(ticket.fingerprint(rel(cancel_stop={"broker_order_id": ""}), CFG), FP)

    def test_fingerprint_contract(self):

        self.assertEqual(len(FP), 64)
        self.assertEqual(ticket.fingerprint(rel(type="MARKET", side=" Buy ", symbol="reliance.ns"), CFG), FP)
        self.assertEqual(ticket.fingerprint(rel(why="other words"), CFG), FP)
        for over in ({"product": "CNC"}, {"qty": "66"}, {"plan": dict(rel()["plan"], stop=1215.5)},
                     {"cancel_stop": {"broker_order_id": "1"}}):
            self.assertNotEqual(ticket.fingerprint(rel(**over), CFG), FP, over)
        for cfg in (dict(CFG, paper=True), dict(CFG, broker="angelone"), dict(CFG, product="CNC")):
            self.assertNotEqual(ticket.fingerprint(dict(rel(), product=None), cfg),
                                ticket.fingerprint(dict(rel(), product=None), CFG), cfg)
        self.assertNotEqual(ticket.fingerprint(rel(), dict(CFG, paper=True)), FP)


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, w=None, batch=BATCH):
        return ticket.parse_yes(reply, w or W(), batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes RELIANCE", "yes 65 RELIANCE",
                  "yes buy 65 shares of RELIANCE", "yes reliance.ns", "yes please",
                  "yes go ahead", "yes confirm", "yes, place order for 65", "yes; RELIANCE",
                  "yes - do it now", "yes @ 65", "yes max loss 867.17", "yes max loss 806",
                  "yes max loss ₹867.17", "yes ₹867.17"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "haan", "go", "do it", "yeah", "yep", "send it",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both",
                  "yes TCS", "yes M&M", "yes 70", "yes but make it 50", "yes?", "no", "not yes",
                  "yes, wait", "yes if it is under 1230", "yes, stop 1215.40", "yes, abort",
                  "yes after lunch", "yes for INFY", "yes sell it", "yes max loss 900", "yes max",
                  "yes RELIANCE 66", "yes yes", "yes ₹900", "yes 1215.40", "yes at 1215.40",
                  "yes 867.17", "yes target 1240.20", "yes 806", "yes market"):
            self.assertFalse(self.yes(r), r)

    def test_a_limit_price_is_a_bare_number_too(self):
        w = W(type="limit", limit_price="1226.50")
        self.assertTrue(self.yes("yes 65 at 1226.50", w))
        self.assertTrue(self.yes("yes ₹1226.50", w))
        self.assertFalse(self.yes("yes 1227.80", w))

    def test_digits_in_a_symbol_are_not_an_amount(self):
        o = ticket.canonical({"symbol": "360ONE", "exchange": "NSE", "product": "MIS", "side": "sell", "qty": "3",
                              "type": "market", "time_in_force": "day"}, CFG)
        self.assertTrue(self.yes("yes sell 3 360ONE", o))
        self.assertFalse(self.yes("yes sell 4 360ONE", o))
        self.assertFalse(self.yes("yes 360ONE", W()))


class BrokerCalls(unittest.TestCase):
    def test_zerodha_entry_and_stop(self):
        self.assertEqual(ticket.entry_calls(W(), "zerodha", FP), [("place_order", KITE_ENTRY)])
        self.assertLessEqual(len(KITE_ENTRY["tag"]), 20)
        self.assertTrue(KITE_ENTRY["tag"].isalnum())
        tool, args = ticket.stop_call(W(), "zerodha", 60, FP)
        self.assertEqual(args, {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                                "transaction_type": "SELL", "quantity": 60, "product": "MIS",
                                "order_type": "SL-M", "trigger_price": 1215.4, "validity": "DAY",
                                "tag": KITE_ENTRY["tag"] + "sl"})

    def test_angel_searches_then_places_with_price_zero(self):
        calls = ticket.entry_calls(W(), "angelone", FP)
        self.assertEqual(calls[0], ("search_scrip", {"exchange": "NSE", "searchscrip": "RELIANCE"}))
        tool, args = calls[1]
        self.assertIn("exactly RELIANCE-EQ", args["symboltoken"])
        self.assertEqual({k: v for k, v in args.items() if k != "symboltoken"},
                         {"variety": "NORMAL", "tradingsymbol": "RELIANCE-EQ", "transactiontype": "BUY",
                          "exchange": "NSE", "ordertype": "MARKET", "producttype": "INTRADAY",
                          "duration": "DAY", "quantity": "65", "price": "0"})
        self.assertNotIn("force", args)
        tool, stop = ticket.stop_call(W(), "angelone", 65, FP)
        self.assertEqual((stop["variety"], stop["ordertype"], stop["triggerprice"], stop["transactiontype"],
                          stop["quantity"], stop["price"]), ("STOPLOSS", "STOPLOSS_MARKET", "1215.40", "SELL",
                                                             "65", "0"))

    def test_modify_calls(self):
        self.assertEqual(ticket.modify_stop_call(W(), "zerodha", "77", 60),
                         ("modify_order", {"variety": "regular", "order_id": "77", "order_type": "SL-M",
                                           "quantity": 60, "trigger_price": 1215.4}))
        tool, a = ticket.modify_stop_call(W(), "angelone", "77", 60)
        for k in ("variety", "orderid", "ordertype", "producttype", "duration", "price", "quantity",
                  "tradingsymbol", "symboltoken", "exchange", "triggerprice"):
            self.assertIn(k, a)

    def test_flatten_cancels_every_stop_first(self):
        o = ticket.canonical({"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "sell",
                              "qty": "65", "type": "market", "time_in_force": "day",
                              "cancel_stop": {"broker_order_id": "11,12", "broker_order_ids": ["11", "12"]}}, CFG)
        k = ticket.entry_calls(o, "zerodha", FP)
        self.assertEqual(k[:2], [("cancel_order", {"variety": "regular", "order_id": "11"}),
                                 ("cancel_order", {"variety": "regular", "order_id": "12"})])
        self.assertEqual(k[2][1]["transaction_type"], "SELL")
        a = ticket.entry_calls(o, "angelone", FP)
        self.assertEqual(a[1], ("cancel_order", {"variety": "STOPLOSS", "orderid": "12"}))

    def test_plain_english(self):
        self.assertEqual(ticket.plain(W()), PLAIN)
        watched = ticket.canonical(rel(), dict(CFG, target_order="watched"))
        self.assertIn("target 1240.20 (watched by trader-alerts, not a resting order)", ticket.plain(watched))
        self.assertEqual(ticket.plain(ticket.canonical({"symbol": "TCS", "side": "sell", "qty": "1", "type": "limit",
                                                        "limit_price": "3050", "time_in_force": "day",
                                                        "product": "MIS"})),
                         "SELL 1 share of TCS on NSE, limit order at ₹3,050, intraday (MIS), good for today")
        self.assertEqual(ticket.max_loss(W(type="limit", limit_price="1226.50")), (721.5, 1215.4))
        self.assertEqual(ticket.max_loss(W()), (867.17, 1215.4))

    def test_unknown_broker_is_refused(self):
        with self.assertRaises(ticket.TicketError):
            ticket.broker_of({"broker": "alpaca"})
        self.assertEqual(ticket.broker_of({"broker": "Angel One"}), "angelone")
        self.assertEqual(ticket.broker_of({}), "zerodha")


class FingerprintParity(unittest.TestCase):
    def test_same_function_as_trader_risk(self):
        check = load("trader-risk", "check")
        for o in (rel(), rel(type="LIMIT", limit_price="1226.50"),
                  {"symbol": "RELIANCE", "side": "sell", "qty": "65", "type": "market",
                   "cancel_stop": {"broker_order_id": "11,12"}}):
            for cfg in (CFG, dict(CFG, paper=True, broker="angelone"), dict(CFG, paper=None)):
                self.assertEqual(ticket.fingerprint(o, cfg), check.fingerprint(o, cfg))


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
        self.cfg_doc = c
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def orders_doc(self):
        with open(self.orders) as fh:
            return json.load(fh)["orders"]

    def fp(self, i=1):
        return ticket.fingerprint(self.orders_doc()[i - 1], self.cfg_doc)

    def pass_risk(self, verdict="PASS", date=TODAY, reasons=(), kind=None, at=NOW, max_loss=867.17):
        """What trader-risk writes: a verdict per order fingerprint, with checked_at and the all-in max loss."""
        results = []
        for o in self.orders_doc():
            results.append({"symbol": o["symbol"], "fingerprint": ticket.fingerprint(o, self.cfg_doc),
                            "verdict": verdict, "reasons": list(reasons), "warnings": [],
                            "kind": kind or ("entry" if o["side"] == "buy" else "exit"),
                            "max_loss": max_loss if o["side"] == "buy" else None})
        os.makedirs(os.path.dirname(self.risk), exist_ok=True)
        with open(self.risk, "w") as fh:
            json.dump({"date": date, "checked_at": utc_z(at), "results": results}, fh)
        with open(self.risk) as fh:
            doc = json.load(fh)
        for r in doc["results"]:
            r["checked_at"] = utc_z(at)
        with open(self.risk, "w") as fh:
            json.dump(doc, fh)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def approve(self, reply="yes", now=NOW, fp=None):
        return run(self.args("approve", "--index", "1", "--fingerprint", (fp or self.fp())[:8], "--reply", reply,
                             now=now))

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
        self.assertIn("No broker call", text)
        self.assertNotIn("place_order", text)
        code, _, err = self.approve()
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_live_zerodha_flow_with_the_stop(self):
        self.pass_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, LIVE ACCOUNT, REAL MONEY, Zerodha (Kite MCP)", text)
        for part in PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("fingerprint: " + FP[:8], text)
        self.assertIn("READY: trader-risk PASS", text)
        self.assertEqual(json_after(text, "Step 1"), KITE_ENTRY)
        self.assertEqual(json_after(text, "Step 2")["order_type"], "SL-M")
        self.assertEqual(json_after(text, "Step 3")["order_type"], "LIMIT")
        self.assertIn("If both fill first you are short", text)
        self.assertNotIn('"plan"', text)
        self.assertNotIn('"why"', text)

        code, text, _ = self.approve("sure")
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))
        code, _, err = self.approve(fp="00000000")
        self.assertEqual(code, 1)
        self.assertIn("not the ticket you showed", err)

        code, text, _ = self.approve()
        self.assertEqual(code, 0)
        self.assertEqual(json_after(text, "place_order"), KITE_ENTRY)
        code, _, err = self.approve()
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)
        code, text, _ = run(self.args("show", "--index", "1", "--json", now="2026-10-19 09:53"))
        self.assertEqual(json.loads(text)["calls"][0]["input"], KITE_ENTRY)   # approved and fresh: shown
        code, text, _ = run(self.args("show", "--index", "1", "--json", now="2026-10-19 09:58"))
        self.assertEqual(json.loads(text)["calls"], [])                       # a yes counts 5 minutes
        self.assertIn("UNRESOLVED", json.loads(text)["status"])
        self.assertIn("dts" + FP[:8], json.loads(text)["status"])

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
        self.assertEqual((plc["broker_order_id"], plc["max_loss"], plc["filled_qty"]),
                         ("261019000101", 867.17, "65"))
        pf = self.portfolio()
        self.assertEqual((pf["cash"], pf["positions"][0]["shares"], pf["positions"][0]["product"]),
                         (20173.5, 65, "MIS"))

        # step 2: the stop for the filled qty
        code, text, _ = run(self.args("stop", "--index", "1", now="2026-10-19 09:51"))
        self.assertEqual(code, 0)
        stop = json_after(text, "place_order")
        self.assertEqual((stop["order_type"], stop["trigger_price"], stop["quantity"], stop["transaction_type"]),
                         ("SL-M", 1215.4, 65, "SELL"))
        self.assertEqual(run(self.args("stop", "--index", "1", "--status", "placed"))[0], 1)  # needs the id
        code, text, _ = run(self.args("stop", "--index", "1", "--status", "placed", "--broker-id", "261019000102"))
        self.assertEqual(code, 0)
        # a second stop is refused: modify the open one instead
        code, text, _ = run(self.args("stop", "--index", "1", "--filled-qty", "60"))
        self.assertEqual(code, 1)
        self.assertIn("already open (broker id 261019000102)", text)
        self.assertEqual(json_after(text, "modify_order"),
                         {"variety": "regular", "order_id": "261019000102", "order_type": "SL-M", "quantity": 60,
                          "trigger_price": 1215.4})
        self.assertEqual(run(self.args("stop", "--index", "1", "--status", "placed", "--broker-id", "9"))[0], 1)
        self.assertEqual([s["broker_order_id"] for s in ticket.open_stops(self.state_doc(), "RELIANCE", TODAY)],
                         ["261019000102"])

        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)
        self.assertNotIn("place_order", text)
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
        self.assertIn("cancels every open stop and target first (broker id 261019000102)", text)
        self.assertIn("do NOT send the sell", text)
        flat = self.orders_doc()[0]
        self.assertEqual(flat["cancel_stop"]["broker_order_ids"], ["261019000102"])
        self.pass_risk(kind="exit", at=late)
        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 0)        # an exit is not held to no_entries_after
        self.assertLess(text.index("cancel_order"), text.index("place_order"))
        self.assertIn('"order_id": "261019000102"', text)
        self.assertIn("do NOT send the sell", text)
        self.assertEqual(self.approve("yes sell 65 RELIANCE", now=late)[0], 0)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "261019000150",
                                   "--filled-qty", "65", "--filled-price", "1236.00", "--portfolio", self.pf,
                                   now="2026-10-19 15:11"))
        self.assertEqual(code, 0)
        pf = self.portfolio()
        self.assertEqual((pf["cash"], pf["positions"]), (100513.5, []))
        self.assertEqual(ticket.open_stops(self.state_doc(), "RELIANCE", TODAY), [])
        code, _, err = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def stops_state(self, *ids):
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w") as fh:
            json.dump({"orders": [{"date": TODAY, "symbol": "RELIANCE", "status": "stop_placed",
                                   "broker_order_id": i} for i in ids]}, fh)

    def flatten(self, now="2026-10-19 15:00"):
        return run(["flatten", "--symbol", "RELIANCE", "--qty", "65", "--orders", self.orders,
                    "--config", self.cfg, "--state", self.state, "--now", now])

    def test_each_cancel_is_recorded_as_it_happens(self):
        T = "2026-10-19 15:00"
        self.stops_state("11", "13")
        self.flatten(T)
        self.pass_risk(kind="exit", at=T)
        self.assertEqual(self.approve(now=T)[0], 0)
        code, text, _ = run(self.args("result", "--index", "1", "--cancelled", "11", now=T))
        self.assertEqual(code, 0)
        self.assertEqual([s["broker_order_id"] for s in ticket.open_stops(self.state_doc(), "RELIANCE", TODAY)],
                         ["13"])
        self.assertEqual(run(self.args("result", "--index", "1", "--cancelled", "99", now=T))[0], 1)
        # a fresh flatten after the partial cancel only cancels what is still open
        self.flatten("2026-10-19 15:01")
        self.assertEqual(ticket.cancel_ids(self.orders_doc()[0]), ["13"])

    def test_a_cancel_that_failed_because_the_stop_executed_stops_the_sell(self):
        T = "2026-10-19 15:00"
        self.stops_state("11")
        self.flatten(T)
        self.pass_risk(kind="exit", at=T)
        self.approve(now=T)
        code, text, _ = run(self.args("result", "--index", "1", "--cancel-failed", "11", now=T))
        self.assertEqual(code, 1)          # a reason is required
        code, text, _ = run(self.args("result", "--index", "1", "--cancel-failed", "11", "--reason", "executed",
                                      now=T))
        self.assertEqual(code, 1)
        self.assertIn("Do NOT send the sell", text)
        self.assertEqual(ticket.open_stops(self.state_doc(), "RELIANCE", TODAY), [])
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "7", now=T))
        self.assertEqual(code, 1)
        self.assertIn("executed", err)

    def test_an_already_cancelled_stop_lets_the_sell_go(self):
        T = "2026-10-19 15:00"
        self.stops_state("11")
        self.flatten(T)
        self.pass_risk(kind="exit", at=T)
        self.approve(now=T)
        code, text, _ = run(self.args("result", "--index", "1", "--cancel-failed", "11", "--reason",
                                      "already-cancelled", now=T))
        self.assertEqual(code, 0)
        self.assertIn("the sell can go", text)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "7", now=T))
        self.assertEqual(code, 0)
        self.assertEqual(sum(1 for r in self.state_doc()["orders"] if r["status"] == "stop_cancelled"), 1)

    def test_declined_or_failed_reopens_with_a_new_pass(self):
        self.pass_risk()
        run(self.args("decline", "--index", "1", "--reply", "no"))
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 09:51"))
        self.assertIn("DONE: declined", text)
        self.pass_risk(at="2026-10-19 09:52")
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 09:52"))
        self.assertEqual(code, 0)
        self.assertIn("READY", text)
        self.assertEqual(self.approve(now="2026-10-19 09:52")[0], 0)
        run(self.args("result", "--index", "1", "--status", "failed", "--note", "RMS", now="2026-10-19 09:53"))
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 09:53"))
        self.assertIn("DONE: failed", text)
        self.pass_risk(at="2026-10-19 09:54")
        self.assertEqual(self.approve(now="2026-10-19 09:54")[0], 0)

    def test_a_late_result_is_recorded_and_an_open_yes_never_turns_ready(self):
        self.pass_risk()
        self.approve()
        # 6 minutes on, no result yet: unresolved, no calls, even with a newer PASS
        self.pass_risk(at="2026-10-19 09:56")
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 09:56"))
        self.assertEqual(code, 1)
        self.assertIn("UNRESOLVED", text)
        self.assertIn("Check get_orders (Kite tag dts%s / Angel order book)" % self.fp()[:8], text)
        self.assertNotIn("place_order", text)
        self.assertEqual(self.approve(now="2026-10-19 09:56")[0], 1)
        # the broker did take it: the late result is recorded, then the stop
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "1",
                                     "--filled-qty", "65", "--filled-price", "1228.10", "--portfolio", self.pf,
                                     now="2026-10-19 09:58"))
        self.assertEqual(code, 0, err)
        code, text, _ = run(self.args("stop", "--index", "1", now="2026-10-19 09:58"))
        self.assertEqual(code, 0)
        self.assertEqual(json_after(text, "place_order")["quantity"], 65)
        # an entry approved at 14:44 loses its calls at 14:46, inside the 5 minutes
        with open(self.state, "w") as fh:
            json.dump({"orders": []}, fh)
        self.pass_risk(at="2026-10-19 14:44")
        self.assertEqual(self.approve(now="2026-10-19 14:44")[0], 0)
        code, text, _ = run(self.args("show", now="2026-10-19 14:46"))
        self.assertIn("UNRESOLVED: the yes can no longer be acted on (no new entries after 14:45", text)
        self.assertNotIn("place_order", text)
        # never sent: record it denied, and a new PASS re-opens it the next day's way
        self.assertEqual(run(self.args("result", "--index", "1", "--status", "denied", now="2026-10-19 14:47"))[0], 0)
        code, text, _ = run(self.args("show", now="2026-10-19 14:47"))
        self.assertIn("DONE: denied", text)

    def test_a_failed_flatten_after_its_cancels_warns_no_stop(self):
        T = "2026-10-19 15:00"
        state = {"orders": [{"date": TODAY, "symbol": "RELIANCE", "status": "stop_placed",
                             "broker_order_id": "11", "stop": 1215.4, "qty": "65"}]}
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w") as fh:
            json.dump(state, fh)
        self.flatten(T)
        self.pass_risk(kind="exit", at=T)
        self.approve(now=T)
        run(self.args("result", "--index", "1", "--cancelled", "11", now=T))
        code, text, _ = run(self.args("result", "--index", "1", "--status", "failed", "--note", "RMS", now=T))
        self.assertEqual(code, 1)
        self.assertIn("NO STOP AT THE EXCHANGE for RELIANCE", text)
        self.assertIn("Re-flatten now", text)
        call = json_after(text, "place_order")
        self.assertEqual((call["order_type"], call["trigger_price"], call["quantity"], call["transaction_type"]),
                         ("SL-M", 1215.4, 65, "SELL"))
        self.assertEqual(run(self.args("result", "--index", "1", "--restop-id", "21", now=T))[0], 0)
        self.assertEqual([s["broker_order_id"] for s in ticket.open_stops(self.state_doc(), "RELIANCE", TODAY)],
                         ["21"])

    def test_a_failed_paper_flatten_puts_back_a_simulated_stop(self):
        T = "2026-10-19 15:00"
        self.config(**dict(CFG, paper=True))
        state = {"orders": [{"date": TODAY, "symbol": "RELIANCE", "status": "stop_placed", "paper": True,
                             "broker_order_id": "paper-x", "stop": 1215.4}]}
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w") as fh:
            json.dump(state, fh)
        self.flatten(T)
        self.pass_risk(kind="exit", at=T)
        self.approve(now=T)
        run(self.args("result", "--index", "1", "--cancelled", "paper-x", now=T))
        code, text, _ = run(self.args("result", "--index", "1", "--status", "denied", now=T))
        self.assertIn("NO STOP AT THE EXCHANGE for RELIANCE", text)
        self.assertIn("simulated SL-M SELL 65 RELIANCE, trigger 1215.40", text)
        self.assertEqual(len(ticket.open_stops(self.state_doc(), "RELIANCE", TODAY)), 1)

    def test_a_blank_no_entries_after_is_off(self):
        for value in ("", None):
            self.config(**dict(CFG, no_entries_after=value))
            self.pass_risk(at="2026-10-19 15:00")
            code, text, _ = run(self.args("show", now="2026-10-19 15:00"))
            self.assertEqual(code, 0, value)
        cfg = dict(CFG)
        cfg.pop("no_entries_after")
        self.config(**cfg)
        self.pass_risk(at="2026-10-19 15:00")
        code, text, _ = run(self.args("show", now="2026-10-19 15:00"))
        self.assertIn("no new entries after 14:45", text)        # missing: the 14:45 default

    def test_a_paper_flatten_fills_at_a_fresh_price(self):
        T = "2026-10-19 15:00"
        self.config(**dict(CFG, paper=True))
        with open(self.pf, "w") as fh:
            json.dump({"cash": 20000, "positions": [{"ticker": "RELIANCE", "shares": 65, "product": "MIS",
                                                     "lots": [{"date": TODAY, "shares": 65, "cost": 79807.0}]}]}, fh)
        run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
             "--config", self.cfg, "--state", self.state, "--now", T])
        self.pass_risk(kind="exit", at=T)
        code, text, _ = run(self.args("show", now=T))
        self.assertIn("pass --filled-price P", text)
        self.approve(now=T)
        prices = os.path.join(self.d, "prices.json")
        with open(prices, "w") as fh:
            json.dump({"as_of": utc_z("2026-10-19 14:40"), "prices": {"RELIANCE": 1236.5}}, fh)
        code, _, err = run(self.args("result", "--index", "1", "--status", "paper", "--portfolio", self.pf,
                                     "--prices", prices, now=T))
        self.assertEqual(code, 1)                                   # 20 minutes old
        self.assertIn("--filled-price", err)
        with open(prices, "w") as fh:
            json.dump({"as_of": utc_z("2026-10-19 14:55"), "prices": {"RELIANCE": 1236.5}}, fh)
        code, _, err = run(self.args("result", "--index", "1", "--status", "paper", "--portfolio", self.pf,
                                     "--prices", prices, now=T))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.portfolio()["cash"], 20000 + 65 * 1236.5)

    def test_a_later_block_does_not_hide_an_approval(self):
        self.pass_risk()
        self.approve()
        self.pass_risk(verdict="BLOCK", reasons=["duplicate: already approved"], at="2026-10-19 09:51")
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 09:51"))
        self.assertIn("APPROVED by you", text)
        self.assertIn("place_order", text)

    def test_no_entries_after_is_by_the_minute(self):
        self.pass_risk(at="2026-10-19 14:45:59")
        code, text, _ = run(self.args("show", now="2026-10-19 14:45:59"))
        self.assertEqual(code, 0)
        self.pass_risk(at="2026-10-19 14:46:00")
        code, text, _ = run(self.args("show", now="2026-10-19 14:46:00"))
        self.assertIn("no new entries after 14:45", text)

    def test_a_yes_older_than_its_pass_does_not_count(self):
        self.pass_risk(at="2026-10-19 09:51")
        code, _, err = self.approve(now="2026-10-19 09:50")
        self.assertEqual(code, 1)
        self.assertIn("stale", err)

    def test_a_live_entry_without_an_exchange_stop_is_refused(self):
        with open(self.orders) as fh:
            doc = json.load(fh)
        for k in ("order_class", "stop_loss_stop_price", "take_profit_limit_price"):
            doc["orders"][0].pop(k)
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        self.pass_risk()
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("no exchange stop: use a bracket order", text)
        self.assertNotIn("place_order", text)

    def test_paper_null_is_paper(self):
        self.config(**dict(CFG, paper=None))
        self.pass_risk()
        code, text, _ = run(self.args("show"))
        self.assertIn("PAPER (SIMULATED", text)
        self.assertNotIn("place_order", text)

    def auto(self, now=NOW, fp=None, *extra):
        return run(self.args("approve", "--index", "1", "--fingerprint", (fp or self.fp())[:8], "--auto-paper",
                             *extra, now=now))

    def test_auto_paper_is_refused_unless_every_guard_holds(self):
        cases = ((dict(CFG, autopilot={"auto_paper": True}), "impossible with a live config"),   # paper false
                 (dict(CFG, paper=True), "needs \"autopilot\""),
                 (dict(CFG, paper=True, autopilot={}), "needs \"autopilot\""),
                 (dict(CFG, paper=True, autopilot={"auto_paper": "true"}), "the JSON value true"),
                 (dict(CFG, paper=True, autopilot={"auto_paper": 1}), "the JSON value true"))
        for cfg, why in cases:
            self.config(**cfg)
            self.pass_risk()
            code, _, err = self.auto()
            self.assertEqual(code, 1, cfg)
            self.assertIn(why, err, cfg)
            self.assertFalse(os.path.exists(self.state), cfg)
        self.config(**dict(CFG, paper=True, autopilot={"auto_paper": True}))
        self.pass_risk(at="2026-10-19 09:44")
        code, _, err = self.auto()
        self.assertIn("stale", err)
        self.pass_risk()
        code, _, err = self.auto(NOW, "00000000")
        self.assertIn("not the ticket you showed", err)
        code, _, err = self.auto(NOW, None, "--reply", "yes")
        self.assertIn("takes no --reply", err)
        self.pass_risk(at="2026-10-19 14:46")
        code, _, err = self.auto("2026-10-19 14:46")
        self.assertIn("no new entries after 14:45", err)
        code, _, err = run(self.args("approve", "--index", "1", "--fingerprint", self.fp()[:8]))
        self.assertIn("needs --reply", err)

    def test_auto_paper_trades_a_whole_paper_day(self):
        self.config(**dict(CFG, paper=True, autopilot={"auto_paper": True}))
        self.pass_risk()
        code, text, _ = self.auto()
        self.assertEqual(code, 0)
        self.assertIn("AUTO-PAPER", text)
        rec = self.state_doc()["orders"][0]
        self.assertEqual((rec["status"], rec["auto"], rec["paper"], rec["reply"], rec["max_loss"]),
                         ("approved", True, True, ticket.AUTO_REPLY, 867.17))
        code, _, _ = run(self.args("result", "--index", "1", "--status", "paper", "--filled-price", "1228.00",
                                   "--portfolio", self.pf, now="2026-10-19 09:51"))
        self.assertEqual(code, 0)
        placed = [r for r in self.state_doc()["orders"] if r["status"] == "placed"][0]
        self.assertTrue(placed["auto"] and placed["paper"])
        # the exit: flatten, a fresh PASS, an automatic paper approval
        T = "2026-10-19 15:10"
        run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
             "--config", self.cfg, "--state", self.state, "--now", T])
        self.pass_risk(kind="exit", at=T)
        self.assertEqual(self.auto(T)[0], 0)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "paper", "--filled-price", "1236.00",
                                   "--portfolio", self.pf, now=T))
        self.assertEqual(code, 0)
        self.assertEqual(self.portfolio()["positions"], [])
        code, text, _ = run(["log", "--state", self.state, "--now", T])
        self.assertIn("AUTO-PAPER BUY 65 RELIANCE", text)
        self.assertIn("2 placed today, 2 of them AUTO-PAPER (no human yes)", text)

    def test_a_live_config_never_auto_approves_even_with_auto_paper_true(self):
        self.config(**dict(CFG, paper=False, autopilot={"auto_paper": True}))
        self.pass_risk()
        code, _, err = self.auto()
        self.assertEqual(code, 1)
        self.assertIn("impossible with a live config", err)
        self.assertFalse(os.path.exists(self.state))

    def test_flatten_cancels_every_open_stop(self):
        state = {"orders": [
            {"date": TODAY, "symbol": "RELIANCE", "status": "stop_placed", "broker_order_id": "11"},
            {"date": TODAY, "symbol": "RELIANCE", "status": "stop_placed", "broker_order_id": "12"},
            {"date": TODAY, "symbol": "RELIANCE", "status": "stop_placed", "broker_order_id": "13"},
            {"date": TODAY, "symbol": "RELIANCE", "status": "stop_cancelled", "broker_order_id": "12"}]}
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w") as fh:
            json.dump(state, fh)
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--qty", "65", "--orders", self.orders,
                             "--config", self.cfg, "--state", self.state, "--now", "2026-10-19 15:00"])
        self.assertEqual(code, 0)
        self.assertEqual(self.orders_doc()[0]["cancel_stop"]["broker_order_ids"], ["11", "13"])
        self.pass_risk(kind="exit", at="2026-10-19 15:00")
        code, text, _ = run(self.args("show", now="2026-10-19 15:00"))
        self.assertEqual(text.count("cancel_order"), 2)

    def test_flatten_takes_the_product_of_the_position(self):
        with open(self.pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"ticker": "RELIANCE", "shares": 5, "product": "CNC",
                                                 "lots": []}]}, fh)
        run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
             "--config", self.cfg, "--state", self.state, "--now", NOW])
        self.assertEqual(self.orders_doc()[0]["product"], "CNC")

    def test_a_stale_pass_is_not_ready(self):
        self.pass_risk(at="2026-10-19 09:44")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("PASS is stale: re-run trader-risk", text)
        self.assertNotIn("place_order", text)
        code, text, _ = run(self.args("show", "--json"))
        self.assertEqual(json.loads(text)[0]["calls"], [])
        code, _, err = self.approve()
        self.assertEqual(code, 1)
        self.assertIn("stale", err)
        self.pass_risk(at=NOW)
        self.assertEqual(self.approve()[0], 0)

    def test_approve_rechecks_the_clock(self):
        self.pass_risk(at=NOW)
        code, _, err = self.approve(now="2026-10-19 09:56")      # 6 minutes later
        self.assertIn("stale", err)
        self.pass_risk(at="2026-10-19 14:46")
        code, text, _ = run(self.args("show", now="2026-10-19 14:46"))
        self.assertIn("no new entries after 14:45 IST", text)
        self.assertEqual(self.approve(now="2026-10-19 14:46")[0], 1)
        self.pass_risk(at="2026-10-20 10:00", date="2026-10-20")
        code, text, _ = run(self.args("show", now="2026-10-20 10:00"))
        self.assertIn("NSE holiday: Dussehra", text)
        self.pass_risk(at="2026-10-19 09:10")
        code, text, _ = run(self.args("show", now="2026-10-19 09:10"))
        self.assertIn("the market is closed", text)

    def test_a_config_change_voids_the_yes(self):
        self.pass_risk()
        self.assertEqual(self.approve()[0], 0)
        self.config(**dict(CFG, paper=True))
        code, _, err = run(self.args("result", "--index", "1", "--status", "paper", "--portfolio", self.pf))
        self.assertEqual(code, 1)
        self.assertIn("config changed since your yes: re-check and re-approve", err)
        self.assertFalse(os.path.exists(self.pf))

    def test_a_failed_stop_is_loud(self):
        self.pass_risk()
        self.approve()
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "1", "--filled-qty", "65",
                      "--filled-price", "1228.10", "--portfolio", self.pf))
        code, text, _ = run(self.args("stop", "--index", "1", "--status", "failed", "--note", "RMS rejected"))
        self.assertEqual(code, 1)
        self.assertIn("WARNING: the stop was NOT placed", text)
        self.assertIn("flatten --symbol RELIANCE --qty 65", text)
        self.assertEqual(ticket.open_stops(self.state_doc(), "RELIANCE", TODAY), [])

    def test_the_stop_waits_for_the_entry(self):
        self.pass_risk()
        code, _, err = run(self.args("stop", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("comes after the fill", err)

    def test_a_fill_that_cannot_apply_writes_nothing(self):
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0].update(side="sell")
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        self.pass_risk(kind="entry")
        self.assertEqual(self.approve("yes")[0], 0)
        before = self.state_doc()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "5",
                                     "--filled-qty", "65", "--filled-price", "1228", "--portfolio", self.pf))
        self.assertEqual(code, 1)
        self.assertIn("short sales are not recorded", err)
        self.assertEqual(self.state_doc(), before)
        self.assertFalse(os.path.exists(self.pf))

    def test_angel_one_ticket(self):
        self.config(**dict(CFG, broker="angelone", target_order="watched"))
        self.pass_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("LIVE ACCOUNT, REAL MONEY, Angel One (angel-one-mcp)", text)
        self.assertEqual(json_after(text, "1. search_scrip"), {"exchange": "NSE", "searchscrip": "RELIANCE"})
        self.assertEqual(json_after(text, "2. place_order")["price"], "0")
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
        code, text, _ = self.approve()
        self.assertEqual(code, 0)
        self.assertNotIn("place_order", text)
        self.assertIn("--status paper", text)
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("record --status paper", err)
        code, text, _ = run(self.args("result", "--index", "1", "--status", "paper", "--portfolio", self.pf))
        self.assertEqual(code, 0)
        self.assertIn("Simulated protective stop at 1215.40 and target at 1240.20", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]],
                         ["approved", "placed", "filled", "stop_placed", "target_placed"])
        self.assertTrue(all(r.get("paper") for r in st["orders"][1:]))
        self.assertEqual(st["orders"][2]["price"], 1227.8)          # the plan's entry
        self.assertEqual(self.portfolio()["cash"], 20193.0)
        self.assertTrue(ticket.open_stops(st, "RELIANCE", TODAY))

        # the paper flatten cancels the simulated stop with the simulated sell
        run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
             "--plan", PLAN_PATH, "--config", self.cfg, "--state", self.state, "--now", "2026-10-19 11:00"])
        self.pass_risk(kind="exit", at="2026-10-19 11:00")
        self.assertEqual(self.approve(now="2026-10-19 11:00")[0], 0)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "paper", "--filled-price", "1240.20",
                                   "--portfolio", self.pf, now="2026-10-19 11:00"))
        self.assertEqual(code, 0)
        self.assertEqual(self.portfolio()["cash"], 100806.0)
        self.assertEqual(ticket.open_stops(self.state_doc(), "RELIANCE", TODAY), [])

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
        code, _, err = self.approve(fp=FP)
        self.assertIn("not the ticket you showed", err)

    def test_an_invalid_order_is_refused(self):
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["type"] = "stop"
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        code, _, err = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("order 1 is not valid", err)

    def test_yesterdays_check_does_not_count(self):
        self.pass_risk(date="2026-10-16")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("not today", text)

    def test_blocked_orders_are_never_ready_and_show_no_calls(self):
        self.pass_risk(verdict="BLOCK", reasons=["window: no new entries before 09:30"])
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("BLOCKED by trader-risk: window: no new entries before 09:30", text)
        self.assertNotIn("place_order", text)
        code, text, _ = run(self.args("show", "--index", "1", "--json"))
        self.assertEqual((json.loads(text)["calls"], json.loads(text).get("stop_call")), ([], None))
        self.assertEqual(self.approve()[0], 1)

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
        self.assertEqual(len(self.state_doc()["orders"]), 1)
        code, _, err = run(["fill", "--symbol", "TCS", "--side", "sell", "--qty", "0.5", "--price", "3050",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertIn("whole shares", err)


class TargetOrders(unittest.TestCase):
    """The live target: a Robo bracket on Angel One, a resting LIMIT on Zerodha (and on request), or watched."""

    setUp, tearDown, config, orders_doc, fp = Flow.setUp, Flow.tearDown, Flow.config, Flow.orders_doc, Flow.fp
    pass_risk, args, approve, state_doc, portfolio = (Flow.pass_risk, Flow.args, Flow.approve, Flow.state_doc,
                                                      Flow.portfolio)

    def edit(self, **plan):
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["plan"].update(plan)
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)

    def robo(self):
        """Angel One with the plan's entry dated today: a market Robo goes at that (fresh) entry."""
        self.config(**dict(CFG, broker="angelone"))
        self.edit(date=TODAY)

    def placed(self, broker_id="9001", qty="65"):
        self.pass_risk()
        self.assertEqual(self.approve("yes")[0], 0)
        return run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", broker_id,
                             "--filled-qty", qty, "--filled-price", "1228", "--portfolio", self.pf))

    def test_modes(self):
        self.assertEqual(W()["target_mode"], "resting")                       # Zerodha's default
        self.assertEqual(ticket.canonical(rel(), dict(CFG, broker="angelone"))["target_mode"], "robo")
        self.assertEqual(ticket.canonical(rel(product="CNC"), dict(CFG, broker="angelone"))["target_mode"], "resting")
        self.assertEqual(ticket.canonical(rel(), dict(CFG, target_order="Watched"))["target_mode"], "watched")
        with self.assertRaises(ticket.TicketError):
            ticket.canonical(rel(), dict(CFG, target_order="robo"))            # Zerodha has no Robo
        with self.assertRaises(ticket.TicketError):
            ticket.canonical(rel(), dict(CFG, target_order="oco"))
        no_target = rel()
        no_target.pop("take_profit_limit_price")
        self.assertEqual(ticket.canonical(no_target, CFG)["target_mode"], "watched")
        # the fingerprint covers target_order, the same in trader-risk
        check = load("trader-risk", "check")
        a, b = ticket.fingerprint(rel(), CFG), ticket.fingerprint(rel(), dict(CFG, target_order="watched"))
        self.assertNotEqual(a, b)
        self.assertEqual(a, ticket.fingerprint(rel(), dict(CFG, target_order="")))
        self.assertEqual(check.fingerprint(rel(), dict(CFG, target_order="watched")), b)

    def test_robo_ticket_and_flow(self):
        self.robo()
        self.pass_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("Angel One Robo bracket, LIMIT entry 1227.80", text)
        robo = json_after(text, "2. place_order")
        self.assertEqual((robo["variety"], robo["producttype"], robo["ordertype"], robo["price"], robo["quantity"]),
                         ("ROBO", "BO", "LIMIT", "1227.80", "65"))
        self.assertEqual((robo["squareoff"], robo["stoploss"]), ("12.40", "12.40"))   # 1240.20 - 1227.80, 1227.80 - 1215.40
        self.assertNotIn("Step 2", text)
        self.assertIn("Robo bracket: Angel One holds the target and the stop", text)
        self.assertEqual(self.approve("yes")[0], 0)
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "65",
                                     "--filled-price", "1227.8", "--portfolio", self.pf))
        self.assertEqual(code, 1)
        self.assertIn("needs --broker-id for a Robo bracket", err)
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "R1",
                                      "--filled-qty", "65", "--filled-price", "1227.8", "--portfolio", self.pf))
        self.assertEqual(code, 0)
        self.assertIn("no step 2 or 3", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]][-1], "robo_placed")
        self.assertEqual(run(self.args("stop", "--index", "1"))[0], 1)
        code, _, err = run(self.args("target", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("Robo bracket", err)
        # flatten cancels the Robo (variety ROBO), then sells only what is still held
        run(["flatten", "--symbol", "RELIANCE", "--qty", "65", "--orders", self.orders, "--portfolio", self.pf,
             "--plan", PLAN_PATH, "--config", self.cfg, "--state", self.state, "--now", "2026-10-19 11:00"])
        doc = json.load(open(self.orders))
        self.assertEqual(doc["orders"][0]["cancel_stop"]["robo_ids"], ["R1"])
        self.pass_risk(kind="exit", at="2026-10-19 11:00")
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 11:00"))
        self.assertEqual(json_after(text, "1. cancel_order"), {"variety": "ROBO", "orderid": "R1"})
        self.assertIn("Send the sell only for the quantity still held", text)

    def test_robo_leg_fill_needs_no_cancel(self):
        self.robo()
        self.pass_risk()
        self.approve("yes")
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "R1", "--filled-qty", "65",
                      "--filled-price", "1227.8", "--portfolio", self.pf))
        code, text, _ = run(["fill", "--symbol", "RELIANCE", "--side", "sell", "--qty", "65", "--price", "1240.2",
                             "--target-hit", "--portfolio", self.pf, "--config", self.cfg, "--state", self.state,
                             "--now", "2026-10-19 10:30"])
        self.assertEqual(code, 0)
        self.assertIn("Angel cancels the other leg itself", text)
        self.assertNotIn("cancel_order", text)
        self.assertEqual(ticket.open_legs(self.state_doc(), "RELIANCE", TODAY, "robo"), [])

    def test_resting_target_flow_on_zerodha(self):
        code, text, _ = self.placed()
        self.assertEqual(code, 0)
        self.assertIn("ticket.py target --index 1", text)
        code, _, err = run(self.args("target", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("protective stop first", err)                     # never a target without the stop
        self.assertEqual(run(self.args("stop", "--index", "1", "--status", "placed", "--broker-id", "S1"))[0], 0)
        code, text, _ = run(self.args("target", "--index", "1"))
        self.assertEqual(code, 0)
        call = json_after(text, "1. place_order")
        self.assertEqual((call["order_type"], call["price"], call["transaction_type"], call["quantity"], call["tag"]),
                         ("LIMIT", 1240.2, "SELL", 65, "dts" + self.fp()[:8] + "tg"))
        code, _, err = run(self.args("target", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("needs --broker-id", err)
        self.assertEqual(run(self.args("target", "--index", "1", "--status", "placed", "--broker-id", "T1"))[0], 0)
        self.assertEqual(run(self.args("target", "--index", "1"))[0], 1)   # never a second target
        # the target fills: cancel the stop at once, then the short check
        code, text, _ = run(["fill", "--symbol", "RELIANCE", "--side", "sell", "--qty", "65", "--price", "1240.2",
                             "--target-hit", "--portfolio", self.pf, "--config", self.cfg, "--state", self.state,
                             "--now", "2026-10-19 10:30"])
        self.assertEqual(code, 0)
        self.assertIn("NOW cancel the protective stop S1", text)
        self.assertEqual(json_after(text, "1. cancel_order"), {"variety": "regular", "order_id": "S1"})
        self.assertIn("NEGATIVE quantity", text)
        st = self.state_doc()
        self.assertEqual(ticket.open_legs(st, "RELIANCE", TODAY, "target"), [])
        self.assertEqual(len(ticket.open_stops(st, "RELIANCE", TODAY)), 1)
        code, _, _ = run(["cancelled", "--symbol", "RELIANCE", "--broker-id", "S1", "--config", self.cfg,
                          "--state", self.state, "--now", "2026-10-19 10:31"])
        self.assertEqual(code, 0)
        self.assertEqual(ticket.open_stops(self.state_doc(), "RELIANCE", TODAY), [])
        self.assertEqual(run(["cancelled", "--symbol", "RELIANCE", "--broker-id", "S1", "--config", self.cfg,
                              "--state", self.state, "--now", "2026-10-19 10:32"])[0], 1)   # nothing open now

    def test_stop_fill_cancels_the_target_and_executed_cancel(self):
        self.placed()
        run(self.args("stop", "--index", "1", "--status", "placed", "--broker-id", "S1"))
        run(self.args("target", "--index", "1", "--status", "placed", "--broker-id", "T1"))
        code, text, _ = run(["fill", "--symbol", "RELIANCE", "--side", "sell", "--qty", "65", "--price", "1215.4",
                             "--stop-hit", "--portfolio", self.pf, "--config", self.cfg, "--state", self.state,
                             "--now", "2026-10-19 10:30"])
        self.assertIn("NOW cancel the resting target T1", text)
        code, text, _ = run(["cancelled", "--symbol", "RELIANCE", "--broker-id", "T1", "--failed", "executed",
                             "--config", self.cfg, "--state", self.state, "--now", "2026-10-19 10:31"])
        self.assertEqual(code, 1)
        self.assertIn("you are short", text)

    def test_robo_needs_a_fresh_entry_and_the_right_sides(self):
        self.config(**dict(CFG, broker="angelone"))           # the fixture's plan entry is undated
        self.pass_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: a Robo bracket goes in as a LIMIT at the plan's entry 1227.80, and that entry "
                      "is undated", text)
        self.assertNotIn("place_order", text)
        self.assertEqual(self.approve("yes")[0], 1)            # nothing is recorded for an order that cannot go
        self.assertFalse(os.path.exists(self.state))
        self.edit(date=TODAY, entry_time="9:20 AM")             # 30 minutes old at 9:50
        self.pass_risk()
        self.assertIn("older than 15 minutes", run(self.args("show", "--index", "1"))[1])
        self.edit(entry_time="9:50 AM", entry=1245.0)           # an entry above the target
        self.pass_risk()
        self.assertIn("stop below the entry and the target above it", run(self.args("show", "--index", "1"))[1])

    def test_partial_leg_fill_is_refused(self):
        self.placed()
        run(self.args("stop", "--index", "1", "--status", "placed", "--broker-id", "S1"))
        run(self.args("target", "--index", "1", "--status", "placed", "--broker-id", "T1"))
        code, _, err = run(["fill", "--symbol", "RELIANCE", "--side", "sell", "--qty", "30", "--price", "1240.2",
                            "--target-hit", "--portfolio", self.pf, "--config", self.cfg, "--state", self.state,
                            "--now", "2026-10-19 10:30"])
        self.assertEqual(code, 1)
        self.assertIn("a partial fill: 30 of the target's 65 shares", err)
        st = self.state_doc()
        self.assertEqual((len(ticket.open_stops(st, "RELIANCE", TODAY)),
                          len(ticket.open_legs(st, "RELIANCE", TODAY, "target"))), (1, 1))

    def test_no_second_target_after_the_first_filled(self):
        self.placed()
        run(self.args("stop", "--index", "1", "--status", "placed", "--broker-id", "S1"))
        run(self.args("target", "--index", "1", "--status", "placed", "--broker-id", "T1"))
        run(["fill", "--symbol", "RELIANCE", "--side", "sell", "--qty", "65", "--price", "1240.2", "--target-hit",
             "--portfolio", self.pf, "--config", self.cfg, "--state", self.state, "--now", "2026-10-19 10:30"])
        code, text, _ = run(self.args("target", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("target_filled", text)
        self.assertNotIn("place_order", text)

    def test_target_needs_this_orders_own_live_stop(self):
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w") as fh:     # a leftover stop from another order, and a paper one
            json.dump({"orders": [{"at": "2026-10-19T09:20:00+05:30", "date": TODAY, "symbol": "RELIANCE",
                                   "status": "stop_placed", "broker_order_id": "OLD1", "fingerprint": "x" * 64},
                                  {"at": "2026-10-19T09:21:00+05:30", "date": TODAY, "symbol": "RELIANCE",
                                   "status": "stop_placed", "broker_order_id": "paper-1", "paper": True,
                                   "fingerprint": self.fp()}]}, fh)
        self.placed()
        code, _, err = run(self.args("target", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("this order's protective stop first", err)

    def test_paper_fill_cancels_the_simulated_other_leg_in_the_log(self):
        self.config(**dict(CFG, paper=True))
        self.pass_risk()
        self.approve("yes")
        run(self.args("result", "--index", "1", "--status", "paper", "--portfolio", self.pf))
        code, text, _ = run(["fill", "--symbol", "RELIANCE", "--side", "sell", "--qty", "65", "--price", "1240.2",
                             "--target-hit", "--portfolio", self.pf, "--config", self.cfg, "--state", self.state,
                             "--now", "2026-10-19 10:30"])
        self.assertEqual(code, 0)
        st = self.state_doc()
        self.assertEqual((ticket.open_stops(st, "RELIANCE", TODAY), ticket.open_legs(st, "RELIANCE", TODAY, "target")),
                         ([], []))
        self.assertNotIn("cancel_order", text)

    def test_failed_robo_flatten_warns_and_can_restop(self):
        self.robo()
        self.pass_risk()
        self.approve("yes")
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "R1", "--filled-qty", "65",
                      "--filled-price", "1227.8", "--portfolio", self.pf))
        run(["flatten", "--symbol", "RELIANCE", "--qty", "65", "--orders", self.orders, "--portfolio", self.pf,
             "--plan", PLAN_PATH, "--config", self.cfg, "--state", self.state, "--now", "2026-10-19 11:00"])
        self.pass_risk(kind="exit", at="2026-10-19 11:00")
        self.approve("yes", now="2026-10-19 11:00")
        code, text, _ = run(self.args("result", "--index", "1", "--cancelled", "R1", "--status", "failed",
                                      now="2026-10-19 11:00"))
        self.assertIn("NO STOP AT THE EXCHANGE", text)
        self.assertIn("Check get_positions first", text)
        code, _, err = run(self.args("result", "--index", "1", "--restop-id", "RS1", now="2026-10-19 11:01"))
        self.assertEqual(code, 0, err)

    def test_flatten_cancels_stop_and_target_with_the_right_variety(self):
        self.config(**dict(CFG, broker="angelone", target_order="resting"))
        self.placed()
        run(self.args("stop", "--index", "1", "--status", "placed", "--broker-id", "S1"))
        run(self.args("target", "--index", "1", "--status", "placed", "--broker-id", "T1"))
        run(["flatten", "--symbol", "RELIANCE", "--qty", "65", "--orders", self.orders, "--portfolio", self.pf,
             "--plan", PLAN_PATH, "--config", self.cfg, "--state", self.state, "--now", "2026-10-19 11:00"])
        cs = json.load(open(self.orders))["orders"][0]["cancel_stop"]
        self.assertEqual((cs["broker_order_ids"], cs["target_ids"]), (["S1", "T1"], ["T1"]))
        self.pass_risk(kind="exit", at="2026-10-19 11:00")
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 11:00"))
        self.assertEqual(json_after(text, "1. cancel_order"), {"variety": "STOPLOSS", "orderid": "S1"})
        self.assertEqual(json_after(text, "2. cancel_order"), {"variety": "NORMAL", "orderid": "T1"})
        self.assertEqual(self.approve("yes", now="2026-10-19 11:00")[0], 0)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "X9",
                                   "--cancelled", "S1", "--cancelled", "T1", "--filled-qty", "65",
                                   "--filled-price", "1230", "--portfolio", self.pf, now="2026-10-19 11:00"))
        self.assertEqual(code, 0)
        st = self.state_doc()
        self.assertEqual((ticket.open_stops(st, "RELIANCE", TODAY), ticket.open_legs(st, "RELIANCE", TODAY, "target")),
                         ([], []))


if __name__ == "__main__":
    unittest.main()
