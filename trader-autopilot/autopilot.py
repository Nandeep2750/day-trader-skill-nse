#!/usr/bin/env python3
"""Paper-trading autopilot for trader-autopilot. Standard library only.

Runs the read-only steps of an NSE day trade on a timer and stops at the
ticket. It never approves, fills, flattens or calls a broker: the user's
explicit yes stays with the user, in Claude. It can only run the tools in
ALLOWED_TOOLS below, and the broker's is not one of them.

    python3 autopilot.py tick                              # one pass (what launchd runs every 2 minutes)
    python3 autopilot.py tick --now "2026-11-03 09:50"     # replay a moment (IST) in a separate folder
    python3 autopilot.py install                           # write a LaunchAgent plist, print the launchctl lines
    python3 autopilot.py status                            # today's notes and state
    python3 autopilot.py log --lines 60                    # the tail of today's full log

One tick, by the IST clock, on NSE trading days only (weekends and the 2026
NSE holidays are skipped; outside 8:55 to 15:35 it does nothing):

  news_at (09:00) to 09:15   trader-news for the symbols, until it succeeds once
  entry_from (09:30) to no_entries_after (14:45)
                             trader-entry ORB check for every symbol not yet
                             done. A FRESH trigger (its bar closed at most 10
                             minutes ago) runs trader-exit, the time machine,
                             trader-budget and trader-risk and notifies "show
                             ticket". An old trigger is noted, not prepared.
                             While a ticket is pending nothing else is prepared.
  after the paper fill       trader-alerts add --from-plan with the plan that
                             was prepared (a snapshot), once
  09:15 to 15:30             trader-alerts check; each ring is notified once

Symbols, first found wins: trader/config.json "autopilot": {"symbols": [...]},
then trader/universe.txt, then the hits in trader/watchlist.json if the scan is
from today or the previous trading day. A bad symbol is skipped and logged.

Settings in trader/config.json (all optional):
  "autopilot": {"symbols": ["CARTRADE", "AEGISLOG"], "stop": "or-mid",
                "target": "2R", "news_at": "09:00", "entry_from": "09:30"}

With "paper": false in the config the tick refuses unless --live-ok is given,
and even then it only prepares and notifies; it never approves.

Files (in the project's trader/ folder):
  autopilot/today.md            the day's notes
  autopilot/logs/YYYY-MM-DD.log every command it ran, with its output
  autopilot/plan-SYMBOL.json    the plan as prepared (the alerts use this copy)
  autopilot/replay-YYYY-MM-DD/  where `tick --now` runs, away from the live files
  state/autopilot-YYYY-MM-DD.json
                                what happened today (one IST date per file)

Exit code 0 on a normal tick (including "nothing to do"), 1 on a refusal or error.
"""

import argparse
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.dirname(HERE)  # the folder holding trader-entry/, trader-exit/ ... (repo or .claude/skills)
IST = datetime.timedelta(hours=5, minutes=30)   # India Standard Time, no daylight saving
NSE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
LABEL_BASE = "com.daytrader.autopilot"
INTERVAL = 120            # seconds between ticks (launchd StartInterval)
CALL_TIMEOUT = 90         # seconds per tool call: shorter than the interval
NOTIFY_TIMEOUT = 10
BAR_MINUTES = 5
STALE_MINUTES = 2 * BAR_MINUTES   # a trigger whose bar closed longer ago than this is not prepared
TITLE = "Day trader autopilot"

# The only tools a tick may run. The broker's ticket tool is deliberately absent.
ALLOWED_TOOLS = ("trader-news/news.py", "trader-entry/entry.py", "trader-exit/exit.py",
                 "trader-timemachine/timemachine.py", "trader-budget/size.py", "trader-risk/check.py",
                 "trader-alerts/alerts.py", "trader-scanner/scan.py")
TICKET = "trader-broker/ticket.py"
# Auto-paper mode only: the broker's ticket tool joins, for the calls ticket_args_ok allows.
AUTO_PAPER_TOOLS = ALLOWED_TOOLS + (TICKET,)
# Tools that take --now (checked against each tool's --help). alerts.py add does not.
TAKES_NOW = {("trader-alerts/alerts.py", "quote"), ("trader-alerts/alerts.py", "bars"), (TICKET, "show"), (TICKET, "approve"), (TICKET, "result"), (TICKET, "flatten"), (TICKET, "log"),
("trader-news/news.py", None), ("trader-entry/entry.py", "check"), ("trader-exit/exit.py", "plan"),
             ("trader-timemachine/timemachine.py", "run"), ("trader-budget/size.py", None),
             ("trader-risk/check.py", None), ("trader-alerts/alerts.py", "check"),
             ("trader-scanner/scan.py", "volatile"), ("trader-timemachine/timemachine.py", "history")}
PICK_TIMEOUT = 300        # seconds for one morning-pick tool call (a NIFTY 500 scan); ticks meanwhile skip (the lock)
PICK_DEFAULTS = {"index": "nifty500", "candidates": 20, "top": 4, "at": "08:55", "until": "09:25",
                 "min_avg_r": 0.0, "min_trades": 15}

# Fallback only: the scanner's NSE_HOLIDAYS is read first, so the pack keeps one list.
NSE_HOLIDAYS_2026 = {  # NSE equity segment trading holidays, 2026 (weekdays only)
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
# Special sessions (Muhurat trading, budget-day Sundays) are not modelled: weekends stay closed.

DEFAULTS = {"stop": "or-mid", "target": "2R", "news_at": "09:00", "entry_from": "09:30"}
STOP_RULE = re.compile(r"^(or-mid|or-low|price:\d+(\.\d+)?|pct:\d+(\.\d+)?|atr:\d+(\.\d+)?)$")
TARGET_RULE = re.compile(r"^(\d+(\.\d+)?R|price:\d+(\.\d+)?)$")
ACTIVE_FROM = datetime.time(8, 55)
ACTIVE_UNTIL = datetime.time(15, 35)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
DECIDED = ("approved", "declined", "placed", "denied", "failed")   # ticket.py's entry statuses


class AutopilotError(Exception):
    pass


def ticket_args_ok(args):
    """The only ticket.py calls the autopilot may make, and only in auto-paper mode: show, log,
    flatten (builds a sell ticket), approve with --auto-paper, result with --status paper."""
    args = list(args)
    if not args:
        return False
    sub = args[0]
    if args == ["approve", "--help"]:
        return True  # read-only: does this ticket.py know --auto-paper?
    if sub in ("show", "log", "flatten"):
        return True
    if sub == "approve":
        return "--auto-paper" in args and "--reply" not in args
    if sub == "result":
        i = args.index("--status") if "--status" in args else -1
        return 0 < i < len(args) - 1 and args[i + 1] == "paper"
    return False


# ---------------------------------------------------------------- time and calendar

def scan_args_ok(args):
    """scan.py only as the morning pick runs it: volatile, an index, a top N, an --out under trader/autopilot/,
    --quiet, and --now in a replay. Never universe or daily, which would overwrite the symbol lists."""
    a = list(args)
    if "--now" in a:
        i = a.index("--now")
        a = a[:i] + a[i + 2:]
    return (len(a) == 8 and a[0] == "volatile" and a[1] == "--index" and a[2] in ("nifty500", "all")
            and a[3] == "--top" and a[4].isdigit() and a[5] == "--out"
            and a[6].startswith(os.path.join("trader", "autopilot", "pick-volatile-")) and ".." not in a[6]
            and a[7] == "--quiet")


def now_ist():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + IST


def parse_now(text):
    """'2026-11-03 09:50' (IST) -> naive IST datetime."""
    try:
        return datetime.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        raise AutopilotError('--now wants an IST time like "2026-11-03 09:50"')


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text if text is not None else ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise AutopilotError("%s wants a time like 09:30, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def utc_iso_to_ist_date(text):
    try:
        dt = datetime.datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return (dt + IST).date() if str(text).endswith("Z") else dt.date()


def load_holidays(skills=SKILLS):
    """(holidays, years covered). The scanner's list if it loads, else this file's copy."""
    path = os.path.join(skills, "trader-scanner", "scan.py")
    try:
        spec = importlib.util.spec_from_file_location("autopilot_scan", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        hol = dict(mod.NSE_HOLIDAYS)
        years = tuple(getattr(mod, "CALENDAR_YEARS", ())) or tuple(sorted({int(d[:4]) for d in hol}))
        return hol, years
    except Exception:
        return dict(NSE_HOLIDAYS_2026), (2026,)


# SEBI's Closing Auction Session, mirrored from the scanner so the pack keeps one copy.
CAS_FNO_FROM = datetime.date(2026, 8, 3)       # F&O scrips' continuous close moved to 15:15
CAS_ALL_DAY_FROM = datetime.date(2026, 12, 7)  # continuous close back to 15:30 for every scrip
FO_CACHE = "fo_mktlots.csv"        # under trader/cache/
FO_CACHE_MAX_AGE = 20 * 3600       # NSE revises the F&O list monthly; a day old is fine


def load_fno(skills=SKILLS, project=None):
    """The symbols with F&O contracts (frozenset), via the scanner, cached for the day under
    trader/cache/ so a tick does not ask NSE every two minutes.

    An empty set when the list cannot be read at all: every scrip is then treated as non-F&O,
    which squares a position off earlier than it needs to rather than later than it may."""
    cache = os.path.join(project, "trader", "cache", FO_CACHE) if project else None
    if cache and os.path.exists(cache):
        fresh = time.time() - os.path.getmtime(cache) < FO_CACHE_MAX_AGE
        got = _fno_from(skills, os.path.dirname(cache))
        if got and fresh:
            return got
    path = os.path.join(skills, "trader-scanner", "scan.py")
    try:
        spec = importlib.util.spec_from_file_location("autopilot_scan_fno", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        text = mod.http_get(mod.NSE_URL + mod.FO_LOTS[1], timeout=60)
        syms = mod.fno_symbols_from(text)
    except Exception:
        return got if (cache and os.path.exists(cache) and got) else frozenset()
    if cache:
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
    return syms


def _fno_from(skills, offline_dir):
    """The cached list parsed by the scanner, or an empty set if it will not read."""
    path = os.path.join(skills, "trader-scanner", "scan.py")
    try:
        spec = importlib.util.spec_from_file_location("autopilot_scan_fno_cache", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.fno_symbols(offline_dir)
    except Exception:
        return frozenset()


def trading_day(d, hol):
    return d.weekday() < 5 and d.isoformat() not in hol


def previous_trading_day(d, hol):
    d -= datetime.timedelta(days=1)
    while not trading_day(d, hol):
        d -= datetime.timedelta(days=1)
    return d


# ---------------------------------------------------------------- files

def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def nse_symbol(text):
    sym = str(text if text is not None else "").strip().upper()
    if sym.endswith(".NS"):
        sym = sym[:-3]
    if sym.endswith(".BO"):
        raise AutopilotError("%s is a BSE symbol: this pack covers NSE only" % text)
    if not NSE_SYMBOL.match(sym):
        raise AutopilotError("%r is not an NSE symbol (like RELIANCE, M&M, BAJAJ-AUTO)" % (text,))
    return sym


def load_config(project):
    path = os.path.join(project, "trader", "config.json")
    if not os.path.exists(path):
        return {}
    doc = read_json(path)
    if not isinstance(doc, dict):
        raise AutopilotError("%s is not valid JSON" % path)
    return doc


def is_paper(cfg):
    """Same rule as the order tools: paper unless "paper" is literally false."""
    return cfg.get("paper", True) is not False


def settings(cfg):
    """Validated settings. Every value is a string checked against what trader-exit accepts."""
    auto = cfg.get("autopilot") or {}
    if not isinstance(auto, dict):
        raise AutopilotError('"autopilot" in trader/config.json must be an object')
    s = dict(DEFAULTS)
    for k in DEFAULTS:
        if auto.get(k) not in (None, ""):
            s[k] = str(auto[k]).strip()
    if not STOP_RULE.match(s["stop"]):
        raise AutopilotError('autopilot.stop must be or-mid, or-low, price:X, pct:X or atr:K, not %r' % s["stop"])
    if not TARGET_RULE.match(s["target"]):
        raise AutopilotError('autopilot.target must be like "2R", "1.5R" or "price:X", not %r' % s["target"])
    if "auto_paper" in auto and not isinstance(auto["auto_paper"], bool):
        raise AutopilotError("autopilot.auto_paper must be true or false, not %r" % (auto["auto_paper"],))
    s["auto_paper"] = auto.get("auto_paper") is True
    try:
        s["max_trades"] = int(cfg.get("max_trades_per_day", 3))
    except (TypeError, ValueError):
        raise AutopilotError("max_trades_per_day must be a whole number")
    s["pick"] = pick_settings(auto.get("auto_pick"))
    s["news_at_t"] = parse_hhmm(s["news_at"], "autopilot.news_at")
    s["entry_from_t"] = parse_hhmm(s["entry_from"], "autopilot.entry_from")
    s["flat_by_t"] = parse_hhmm(cfg.get("flat_by") or "15:00", "flat_by")
    until = cfg.get("no_entries_after", "14:45")
    # missing = 14:45; blank or null = off, as in trader-risk and trader-broker. Entries still stop at
    # flat_by: a position opened after it would have to close at once.
    s["entry_until_t"] = s["flat_by_t"] if until in (None, "") else parse_hhmm(until, "no_entries_after")
    if s["entry_until_t"] > s["flat_by_t"]:
        s["entry_until_t"] = s["flat_by_t"]
    s["warnings"] = []
    if s["news_at_t"] >= OPEN:
        s["warnings"].append("autopilot.news_at %s is at or after the 9:15 open: the news check never runs"
                             % s["news_at"])
    if s["entry_from_t"] > s["entry_until_t"]:
        s["warnings"].append("autopilot.entry_from %s is after no_entries_after: no entry check runs"
                             % s["entry_from"])
    return s


def pick_settings(raw):
    """autopilot.auto_pick: true (the defaults), false / missing (off), or an object overriding them."""
    if raw in (None, False):
        return None
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        raise AutopilotError("autopilot.auto_pick must be true, false or an object, not %r" % (raw,))
    if raw.get("enabled") is False:
        return None
    p = dict(PICK_DEFAULTS)
    p.update({k: v for k, v in raw.items() if k in PICK_DEFAULTS and v not in (None, "")})
    if p["index"] not in ("nifty500", "all"):
        raise AutopilotError('autopilot.auto_pick.index must be "nifty500" or "all"')
    for k, lo, hi in (("candidates", 1, 40), ("top", 1, 10), ("min_trades", 0, 60)):
        if isinstance(p[k], bool) or not isinstance(p[k], int) or not lo <= p[k] <= hi:
            raise AutopilotError("autopilot.auto_pick.%s must be a whole number from %d to %d" % (k, lo, hi))
    try:
        p["min_avg_r"] = float(p["min_avg_r"])
    except (TypeError, ValueError):
        raise AutopilotError("autopilot.auto_pick.min_avg_r must be a number")
    p["at_t"] = parse_hhmm(p["at"], "autopilot.auto_pick.at")
    p["until_t"] = parse_hhmm(p["until"], "autopilot.auto_pick.until")
    if p["until_t"] < p["at_t"]:
        raise AutopilotError("autopilot.auto_pick.until must not be before its at")
    if p["top"] > p["candidates"]:
        raise AutopilotError("autopilot.auto_pick.top cannot be more than its candidates")
    return p


def symbols(project, cfg, today=None, hol=None):
    """(symbols, where from, skipped notes). First found wins: config, universe.txt, a recent watchlist."""
    skipped = []
    auto = cfg.get("autopilot") or {}
    raw = auto.get("symbols") if isinstance(auto, dict) else None
    source = 'config "autopilot.symbols"'
    if raw is not None and not isinstance(raw, list):
        raw = [raw]
    if not raw:
        uni = os.path.join(project, "trader", "universe.txt")
        if os.path.exists(uni):
            with open(uni, encoding="utf-8") as fh:
                raw = [ln.split("#")[0].strip() for ln in fh if ln.split("#")[0].strip()]
            source = "trader/universe.txt"
    if not raw:
        wl = read_json(os.path.join(project, "trader", "watchlist.json"))
        if isinstance(wl, dict) and wl.get("hits"):
            made = utc_iso_to_ist_date(wl.get("created_at") or "") if wl.get("created_at") else None
            if made is None and wl.get("as_of"):
                try:
                    made = datetime.datetime.strptime(str(wl["as_of"])[:10], "%Y-%m-%d").date()
                except ValueError:
                    made = None
            fresh = today is None or (made is not None and
                                      made >= previous_trading_day(today, hol or NSE_HOLIDAYS_2026))
            if fresh:
                raw = [h.get("symbol") for h in wl["hits"] if isinstance(h, dict) and h.get("symbol")]
                source = "trader/watchlist.json hits"
            else:
                skipped.append("trader/watchlist.json is from %s, too old to use" % (made or "an unknown date"))
    out = []
    for r in raw or []:
        try:
            s = nse_symbol(r)
        except AutopilotError as e:
            skipped.append("skipped %s" % e)
            continue
        if s not in out:
            out.append(s)
    if not out:
        raise AutopilotError('no usable symbols: set "autopilot": {"symbols": [...]} in trader/config.json, '
                             "or write trader/universe.txt, or run /trader-scanner today%s"
                             % ((" (" + "; ".join(skipped) + ")") if skipped else ""))
    return out, source, skipped


# ---------------------------------------------------------------- running tools and notifying

class SubprocessRunner:
    """Runs a sibling skill tool from ALLOWED_TOOLS. Tests swap in a fake with the same call signature."""

    def __init__(self, skills=SKILLS, python=sys.executable, timeout=CALL_TIMEOUT, auto_paper=False):
        self.skills, self.python, self.timeout = skills, python, timeout
        self.allowed = AUTO_PAPER_TOOLS if auto_paper else ALLOWED_TOOLS

    def __call__(self, tool, args, cwd):
        if tool not in self.allowed:
            raise AutopilotError("refusing to run %s: not a step of the autopilot in this mode" % tool)
        if tool == TICKET and not ticket_args_ok(args):
            raise AutopilotError("refusing ticket.py %s: only show, log, flatten, approve --auto-paper and "
                                 "result --status paper are allowed" % " ".join(args))
        if tool == "trader-scanner/scan.py" and not scan_args_ok(args):
            raise AutopilotError("refusing scan.py %s: only the morning pick's volatile scan is allowed" % " ".join(args))
        cmd = [self.python, os.path.join(self.skills, tool)] + list(args)
        try:
            p = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=self.timeout)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "timed out after %d s" % self.timeout
        except OSError as e:
            return 127, "could not run %s: %s" % (tool, e)


def mac_notify(title, text, sound=False, run=subprocess.run, which=shutil.which):
    """A macOS notification. The text goes in as an argument, never inside the script, so no
    character (a rupee sign, a quote) can break it. Returns True only if osascript said so."""
    osa = which("osascript")
    if not osa:
        print("NOTIFY: %s" % text)
        return False
    script = ["-e", "on run argv",
              "-e", "display notification (item 1 of argv) with title (item 2 of argv)%s"
              % (' sound name "Glass"' if sound else ""),
              "-e", "end run"]
    try:
        p = run([osa] + script + [text[:230], title], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=NOTIFY_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return p.returncode == 0


def print_notify(title, text, sound=False):
    print("NOTIFY: %s" % text)
    return True


# ---------------------------------------------------------------- one day

class Day:
    def __init__(self, project, now, runner, notifier, test_now=None):
        self.project, self.now, self.runner, self.notifier = project, now, runner, notifier
        self.test_now = test_now
        self.auto_paper = False
        self.cfg = {}
        self.skills = SKILLS
        self._fno = None
        self.last_stdout = self.last_text = ""
        self.date = now.date().isoformat()
        self.trader = os.path.join(project, "trader")
        self.log_path = os.path.join(self.trader, "autopilot", "logs", self.date + ".log")
        self.state_path = os.path.join(self.trader, "state", "autopilot-%s.json" % self.date)
        self.summary_path = os.path.join(self.trader, "autopilot", "today.md")
        self._started = now_ist()
        st = read_json(self.state_path)
        self.state = st if isinstance(st, dict) and st.get("date") == self.date else {
            "date": self.date, "news": False, "done": {}, "active": None, "alerts": False,
            "events": [], "notes": [], "paper": True}

    def clock(self):
        """The time now: the tick's time plus however long the tick has run so far."""
        return self.now + (now_ist() - self._started)

    @property
    def fno(self):
        """The symbols with F&O contracts, read once a tick. Before the earlier of the two
        cutoffs no scrip can be closed by either, so the list is not fetched at all."""
        if self.clock().time() < AUTO_SQUARE_OFF_FNO:
            return frozenset()
        if self._fno is None:
            self._fno = load_fno(self.skills, self.project)
        return self._fno

    def save(self):
        write_json(self.state_path, self.state)

    def log(self, text):
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(text)

    def run(self, tool, *args):
        args = list(args)
        if tool == "trader-scanner/scan.py" and not scan_args_ok(args):
            raise AutopilotError("refusing scan.py %s: only the morning pick's volatile scan is allowed" % " ".join(args))
        if tool == TICKET and not (self.auto_paper and ticket_args_ok(args)):
            raise AutopilotError("refusing ticket.py %s outside auto-paper mode or its allowed calls"
                                 % " ".join(args))
        sub = args[0] if args and not args[0].startswith("-") else None
        if self.test_now and (tool, sub) in TAKES_NOW and args != ["approve", "--help"]:
            args += ["--now", self.test_now]
        got = self.runner(tool, args, self.project)
        code, stdout = got[0], got[1] or ""
        stderr = (got[2] or "") if len(got) > 2 else ""
        self.last_stdout = stdout   # JSON is parsed from stdout only
        out = stdout + (("\n" if stdout and not stdout.endswith("\n") else "") + stderr if stderr else "")
        self.last_text = out
        self.log("\n[%s IST] $ %s %s (exit %s)\n%s%s" % (self.now.strftime("%H:%M"), tool,
                                                       " ".join(shlex.quote(a) for a in args),
                                                       code, out, "" if out.endswith("\n") else "\n"))
        return code, out

    def run_json(self, tool, *args):
        """(exit code, parsed stdout or None). Messages on stderr never break the JSON."""
        code, _ = self.run(tool, *args)
        try:
            return code, json.loads(self.last_stdout)
        except ValueError:
            return code, None

    def write_summary(self):
        os.makedirs(os.path.dirname(self.summary_path), exist_ok=True)
        with open(self.summary_path, "w", encoding="utf-8") as fh:
            fh.write("# Autopilot %s (%s)\n\n" % (self.date, "paper" if self.state.get("paper", True) else "LIVE"))
            fh.write("\n".join("- " + n for n in self.state["notes"]) + "\n")
            if self.state.get("trades"):
                fh.write("\n".join(journal_lines(journal_doc(self))) + "\n")

    def note(self, key, text, sound=False):
        """Record and notify one event, once a day per key. A failed notification is recorded
        in today.md and retried on the next tick."""
        if key in self.state["events"]:
            return False
        title = TITLE if self.state.get("paper", True) else TITLE + " (LIVE)"
        delivered = self.notifier(title, text, sound) is not False
        stamp = "%s  %s" % (self.now.strftime("%H:%M"), text)
        outbox = self.state.setdefault("outbox", [])
        if delivered:
            self.state["events"].append(key)
            self.state["notes"].append(stamp)
            self.state["outbox"] = [o for o in outbox if o[0] != key]
        else:
            if not any(o[0] == key for o in outbox):
                outbox.append([key, text, bool(sound)])
                self.state["notes"].append(stamp + "  (notification failed, retrying)")
        self.write_summary()
        return delivered

    def flush(self):
        """Retry notifications that did not go out on an earlier tick."""
        for key, text, sound in list(self.state.get("outbox", [])):
            self.note(key, text, sound)


def last_line(out):
    lines = [ln.strip() for ln in (out or "").strip().splitlines() if ln.strip()]
    return lines[-1][:140] if lines else "no output"


def broker_records(project, date):
    doc = read_json(os.path.join(project, "trader", "state", "broker.json")) or {}
    recs = doc.get("orders", []) if isinstance(doc, dict) else doc if isinstance(doc, list) else []
    return [r for r in recs if isinstance(r, dict) and str(r.get("date", ""))[:10] == date]


def ist_stamp(text):
    """'2026-11-03T09:51:00+05:30' or '...Z' -> '2026-11-03T09:51' in IST, comparable as text."""
    t = str(text or "")
    try:
        dt = datetime.datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return ""
    if t.endswith("Z"):
        dt += IST
    return dt.strftime("%Y-%m-%dT%H:%M")


def records_since(project, date, sym, since, statuses):
    """trader-broker records for this symbol today, at or after `since` (IST 'YYYY-MM-DDTHH:MM'),
    with one of these statuses. ticket.py's records carry symbol, date, at and status (a decline
    has no qty), so they are matched on those."""
    return [r for r in broker_records(project, date)
            if str(r.get("symbol", "")).upper() == sym and r.get("status") in statuses
            and ist_stamp(r.get("at")) >= (since or "")]


def pending_ticket(project, date):
    """The symbol of an order in trader/orders.json from today that nobody has decided on since it
    was written, or None."""
    doc = read_json(os.path.join(project, "trader", "orders.json"))
    if not isinstance(doc, dict) or not doc.get("orders"):
        return None
    made = utc_iso_to_ist_date(doc.get("created_at") or "")
    if made is None or made.isoformat() != date:
        return None
    since = ist_stamp(doc.get("created_at"))
    for o in doc["orders"]:
        sym = str(o.get("symbol", "")).upper()
        if sym and not records_since(project, date, sym, since, DECIDED):
            return sym
    return None


def shares_held(project, sym):
    pf = read_json(os.path.join(project, "trader", "portfolio.json")) or {}
    total = 0.0
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        try:
            if str(p.get("ticker", p.get("symbol", ""))).upper() == sym:
                total += float(p.get("shares", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    return total


def filled_since(project, date, sym, prepared_at, shares_before):
    """True once the prepared trade's buy has filled: a buy fill logged at or after the preparation,
    or more shares than were held at the preparation."""
    if any(r.get("side") == "buy" for r in records_since(project, date, sym, prepared_at, ("filled",))):
        return True
    return shares_held(project, sym) > (shares_before or 0)


def trigger_age(res, now):
    """Minutes since the trigger bar closed, or None if the entry result does not say."""
    bar = res.get("bar_start")
    if not bar:
        return None
    try:
        start = datetime.datetime.strptime("%s %s" % (res.get("date") or now.date().isoformat(), bar),
                                           "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    closed = start + datetime.timedelta(minutes=BAR_MINUTES)
    return int((now - closed).total_seconds() // 60)


ODDS = re.compile(r"target first\s+([\d.]+)\s*%\s+vs\s+stop first\s+([\d.]+)\s*%")


def risk_verdict(project, date, sym):
    """PASS / BLOCK from trader/state/risk.json for today's order on this symbol, or None if missing."""
    doc = read_json(os.path.join(project, "trader", "state", "risk.json"))
    if not isinstance(doc, dict) or doc.get("date") != date:
        return None
    for r in doc.get("results", []):
        if str(r.get("symbol", "")).upper() == sym:
            return r.get("verdict")
    return None


def prepare_core(day, sym, res, s):
    """Plan, replay, size and risk-check one fresh trigger. Returns a dict (plan, orders, verdict,
    odds, prep_start, price, when) or None after noting why it stopped; exceptions propagate."""
    price, when = res.get("price"), res.get("time") or res.get("bar_start")
    prep_start = day.clock().strftime("%Y-%m-%dT%H:%M")
    entry_path = os.path.join("trader", "autopilot", "entry-%s.json" % sym)
    code, out = day.run("trader-exit/exit.py", "plan", "--from", entry_path, "--stop", s["stop"],
                        "--target", s["target"], "--write")
    plan = read_json(os.path.join(day.project, "trader", "plan.json")) or {}
    if code != 0 or str(plan.get("symbol", "")).upper() != sym:
        day.note("plan-failed:" + sym, "%s triggered at %s (%s) but the exit plan failed: %s"
                 % (sym, price, when, last_line(out)), True)
        return None
    snap =os.path.join(day.project, "trader", "autopilot", "plan-%s.json" % sym)
    write_json(snap, plan)
    _, tm = day.run("trader-timemachine/timemachine.py", "run", "--write")
    m = ODDS.search(tm or "")
    odds = (" Time machine: target first %s %% vs stop first %s %%." % m.groups()) if m else ""
    code, out = day.run("trader-budget/size.py", "--write")
    if day.auto_paper:
        own_order(day)  # this orders.json is the autopilot's: never a human's pending ticket
    orders = (read_json(os.path.join(day.project, "trader", "orders.json")) or {}).get("orders") or []
    if code != 0 or not orders or str(orders[0].get("symbol", "")).upper() != sym:
        day.note("size-refused:" + sym, "%s triggered at %s (%s), but trader-budget wrote no order: %s"
                 % (sym, price, when, last_line(out)), True)
        return None
    risk_path = os.path.join(day.project, "trader", "state", "risk.json")
    if os.path.exists(risk_path):
        os.remove(risk_path)  # the verdict read below must come from this check, not an earlier one
    day.run("trader-risk/check.py")
    verdict = risk_verdict(day.project, day.date, sym)
    return {"plan": plan, "orders": orders, "verdict": verdict, "odds": odds, "prep_start": prep_start,
            "price": price, "when": when}


def prepare_trade(day, sym, res, s):
    """Human mode: prepare the ticket and notify; the user says yes or no in Claude."""
    p = prepare_core(day, sym, res, s)
    if p is None:
        return True
    plan, orders, verdict, odds = p["plan"], p["orders"], p["verdict"], p["odds"]
    prep_start, price, when = p["prep_start"], p["price"], p["when"]
    day.state["active"] = sym
    day.state["prepared_at"] = prep_start
    day.state["shares_before"] = shares_held(day.project, sym)
    day.note("ticket:" + sym, "%s triggered at %s (bar closed %s). Stop %s, target %s, %s shares. %s In Claude, in this "
             "project, say: show ticket.%s" % (
                 sym, price, when, plan.get("stop"), plan.get("target"), orders[0].get("qty", "?"),
                 ("Risk %s." % verdict) if verdict else "Risk check failed (see the log): do not trade it.",
                 odds), True)
    return True


def check_entry(day, sym, s):
    rel = os.path.join("trader", "autopilot", "entry-%s.json" % sym)
    path = os.path.join(day.project, rel)
    if os.path.exists(path):
        os.remove(path)  # never read a stale result if this check fails
    code, out = day.run("trader-entry/entry.py", "check", sym, "--setup", "orb", "--write", rel)
    res = read_json(path) or {}
    if res.get("date") != day.date:
        day.log("  (no fresh result for %s: %s)\n" % (sym, last_line(out)))
        return
    status = res.get("status")
    if status == "expired":
        day.state["done"][sym] = "expired"
        day.note("expired:" + sym, "%s: no breakout (%s). No more checks today." % (sym, res.get("reason", "")))
        return
    if status != "triggered":
        return
    when = res.get("time") or res.get("bar_start")
    age = trigger_age(res, day.clock())
    if age is None or age > STALE_MINUTES:
        day.state["done"][sym] = "stale"
        day.note("stale:" + sym, "%s triggered at %s, %s min ago: stale, not prepared (the autopilot only "
                 "prepares a trigger within %d minutes)." % (sym, when, "?" if age is None else age, STALE_MINUTES))
        return
    if day.auto_paper:
        auto_entry_gate(day, sym, res, s, when)
        return
    pending = pending_ticket(day.project, day.date)
    if day.state["active"] or pending:
        day.note("also:" + sym, "%s also triggered at %s (%s), but a ticket for %s is pending. Finish or decline "
                 "that ticket first; nothing was prepared for %s." % (sym, res.get("price"), when,
                                                                    day.state["active"] or pending, sym), True)
        return
    try:
        prepare_trade(day, sym, res, s)
    except Exception as e:  # leave a clear note; done stays unset so the next tick can retry
        day.log("  error preparing %s: %s\n%s" % (sym, e, traceback.format_exc()))
        day.note("prepare-error:" + sym, "%s triggered at %s but preparing it failed (%s). It retries next tick; "
                 "see the log." % (sym, when, e), True)
        return
    day.state["done"][sym] = "triggered"


# ---------------------------------------------------------------- auto-paper

PRICE_FRESH = datetime.timedelta(minutes=15)    # the same freshness rule as trader-broker's fresh_price
# Any trade still open now is closed, whatever its plan. Two times, because a scrip's
# broker square-off and its continuous close both depend on whether it has F&O contracts:
# an F&O scrip is squared off by Angel One at 15:10 and stops continuous trading at 15:15
# inside the first CAS window, so the autopilot must be out before 15:10, not 15:15.
AUTO_SQUARE_OFF = datetime.time(15, 10)          # a scrip with no F&O contracts
AUTO_SQUARE_OFF_FNO = datetime.time(15, 5)       # a scrip with F&O contracts, inside the CAS window


def auto_square_off(sym, day_date, fno):
    """The latest the autopilot will hold this scrip today."""
    if sym in fno and CAS_FNO_FROM <= day_date < CAS_ALL_DAY_FROM:
        return AUTO_SQUARE_OFF_FNO
    return AUTO_SQUARE_OFF
TRADE_LIVE = ("entering", "open", "exiting")     # a trade in one of these still needs the autopilot


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def parse_utc(text):
    t = str(text or "")
    try:
        dt = datetime.datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    if t.endswith("Z"):
        return dt
    m = re.search(r"([+-])(\d\d):(\d\d)$", t)
    if m:
        off = datetime.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return dt - off if m.group(1) == "+" else dt + off
    return dt - IST  # no offset: IST, as everywhere in the pack


def auto_tools_ok(day):
    """ticket.py must know --auto-paper (checked once a day). Without it: no auto action, loudly."""
    if day.state.get("auto_ok") is None:
        code, out = day.run(TICKET, "approve", "--help")
        day.state["auto_ok"] = "--auto-paper" in out
    if not day.state["auto_ok"]:
        day.note("auto-missing", "AUTO-PAPER IS OFF: trader-broker's ticket.py has no --auto-paper here. Nothing "
                 "is traded automatically today; update the skills.", True)
    return day.state["auto_ok"]


def quotes_for(day, syms):
    """{SYM: price} of FRESH quotes: alerts.py quote (read-only), then trader/prices.json if it is at
    most 15 minutes old. A replay (--now) uses whatever the quote is: its P&L is not reliable."""
    syms = [s for s in syms if s]
    out = {}
    if not syms:
        return out
    code, doc = day.run_json("trader-alerts/alerts.py", "quote", *(syms + ["--json"]))
    now = utc_now()
    for sym, q in ((doc or {}).get("quotes") or {}).items():
        try:
            price = float(q.get("price"))
        except (TypeError, ValueError, AttributeError):
            continue
        at = parse_utc(q.get("time"))
        if day.test_now or (at is not None and datetime.timedelta(minutes=-1) <= now - at <= PRICE_FRESH):
            out[str(sym).upper()] = price
    missing = [s for s in syms if s not in out]
    if missing:
        pdoc = read_json(os.path.join(day.project, "trader", "prices.json")) or {}
        prices = pdoc.get("prices") or {}
        for sym in missing:
            p = prices.get(sym)
            d = (pdoc.get("detail") or {}).get(sym) or {}
            at = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or parse_utc(pdoc.get("as_of"))
            if p is not None and at is not None and datetime.timedelta(0) <= now - at <= PRICE_FRESH:
                out[sym] = float(p)
    return out


def bars_for(day, syms):
    """{SYM: today's 1-minute bars} from alerts.py bars (read-only). Missing or failed symbols are left out:
    their exits fall back to the quote alone."""
    syms = [s for s in syms if s]
    if not syms:
        return {}
    code, doc = day.run_json("trader-alerts/alerts.py", "bars", *(syms + ["--interval", "1m", "--json"]))
    if not isinstance(doc, dict) or doc.get("date") != day.date:
        return {}
    return {str(k).upper(): v for k, v in (doc.get("bars") or {}).items() if isinstance(v, list)}


def bar_touch(trade, bars, date):
    """(reason, price, bar HH:MM) for the first 1-minute bar after the entry whose low reached the stop or
    whose high reached the target, else (None, None, None). A tick every 2 minutes misses a wick between
    two checks; the bars do not. A bar that reached both counts as the stop: the order inside a bar is
    unknown, and the stop is the cautious answer (the Time Machine's rule too). Bars start the minute after
    the fill's minute, so nothing from before the buy counts (that minute is left to the quote rules). Bars
    from flat-by on are left to the flat-by exit. A stop fills at the stop (an SL-M), a target at the target
    (a limit there would fill)."""
    if trade.get("carried_from") or not trade.get("entry_time"):
        return None, None, None
    try:
        stop = None if trade.get("stop") is None else float(trade["stop"])
        target = None if trade.get("target") is None else float(trade["target"])
        entered = datetime.datetime.strptime("%s %s" % (date, trade["entry_time"]), "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None, None, None
    try:
        flat_t = parse_hhmm(trade.get("flat_by") or "15:00", "flat_by")
    except AutopilotError:
        flat_t = datetime.time(15, 0)    # exit_reason's fallback too
    first = (entered + datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    flat = "%s %s" % (date, flat_t.strftime("%H:%M"))
    for b in sorted((b for b in bars if isinstance(b, dict)), key=lambda b: str(b.get("start"))):
        start = str(b.get("start") or "")
        if start[:10] != date or start < first or start >= flat:
            continue
        try:
            hi, lo = float(b["high"]), float(b["low"])
        except (KeyError, TypeError, ValueError):
            continue
        if stop is not None and lo <= stop:
            return "stop", stop, start[11:]
        if target is not None and hi >= target:
            return "target", target, start[11:]
    return None, None, None


def own_order(day):
    """Record that trader/orders.json, as it is now, was written for the autopilot."""
    doc = read_json(os.path.join(day.project, "trader", "orders.json")) or {}
    if doc.get("created_at"):
        own = day.state.setdefault("own_orders", [])
        if doc["created_at"] not in own:
            own.append(doc["created_at"])


def human_pending(day):
    """An undecided ticket in trader/orders.json that the autopilot did NOT write, or None."""
    doc = read_json(os.path.join(day.project, "trader", "orders.json")) or {}
    if doc.get("created_at") in day.state.get("own_orders", []):
        return None
    return pending_ticket(day.project, day.date)


def latest_record(day, fp):
    """The last trader-broker record for this exact ticket (by fingerprint), or None."""
    if not fp:
        return None
    recs = [r for r in broker_records(day.project, day.date) if r.get("fingerprint") == fp]
    return recs[-1] if recs else None


def auto_entry_gate(day, sym, res, s, when):
    """A fresh trigger in auto-paper mode: one trade per symbol, max_trades_per_day in all (0 means no
    limit, as in trader-risk), and never over someone else's undecided ticket."""
    trades = day.state.setdefault("trades", {})
    if sym in trades:
        day.state["done"][sym] = "traded"
        return
    taken = [t for t in trades.values() if t.get("status") in TRADE_LIVE + ("closed",)]
    if s["max_trades"] > 0 and len(taken) >= s["max_trades"]:
        day.state["done"][sym] = "max-trades"
        day.note("max-trades:" + sym, "AUTO-PAPER: %s triggered at %s, but max_trades_per_day (%d) is reached: "
                 "no trade." % (sym, when, s["max_trades"]))
        return
    pending = human_pending(day)
    if pending:
        day.note("pending:%s:%s" % (sym, pending), "AUTO-PAPER: %s triggered at %s, but an undecided ticket for "
                 "%s (not the autopilot's) is in trader/orders.json: not touching it." % (sym, when, pending), True)
        return
    try:
        auto_enter(day, sym, res, s)
    except Exception as e:  # noted; the symbol is done for today so a broken step does not repeat
        day.log("  error auto-entering %s: %s\n%s" % (sym, e, traceback.format_exc()))
        day.note("auto-error:" + sym, "AUTO-PAPER: %s triggered at %s but the paper entry failed (%s); see the log."
                 % (sym, when, e), True)
    day.state["done"][sym] = "triggered"


def ticket_ready(day, sym, side):
    """show --json for order 1: (fingerprint, None) when it is READY and is this symbol and side,
    else (None, why)."""
    code, t = day.run_json(TICKET, "show", "--index", "1", "--json")
    if isinstance(t, list):
        t = t[0] if t else {}
    if not isinstance(t, dict):
        return None, "ticket.py show gave no JSON: %s" % last_line(day.last_text)
    orders = (read_json(os.path.join(day.project, "trader", "orders.json")) or {}).get("orders") or []
    o = orders[0] if orders else {}
    if str(o.get("symbol", "")).upper() != sym or str(o.get("side", "")).lower() != side:
        return None, "order 1 is %s %s, not %s %s" % (o.get("side"), o.get("symbol"), side, sym)
    if not t.get("ready") or not t.get("fingerprint"):
        return None, t.get("status") or "not ready"
    return t["fingerprint"], None


def paper_result(day, price):
    code, out = day.run(TICKET, "result", "--index", "1", "--status", "paper", "--filled-price", "%.2f" % price)
    return code == 0, last_line(out)


def charges_for(cfg, buy_value, sell_value, skills=SKILLS):
    """trader-budget's own estimate (same formula, same config overrides), or None if it will not load."""
    try:
        spec = importlib.util.spec_from_file_location("autopilot_size",
                                                      os.path.join(skills, "trader-budget", "size.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.round_trip_charges(cfg, buy_value, sell_value)["total"]
    except Exception:
        return None


def auto_enter(day, sym, res, s):
    """Prepare; on a risk PASS approve and fill on paper at the current quote (else the trigger close)."""
    p = prepare_core(day, sym, res, s)
    if p is None:
        return
    if p["verdict"] != "PASS":
        day.note("auto-block:" + sym, "AUTO-PAPER: %s triggered at %s, risk %s: no trade.%s"
                 % (sym, p["price"], p["verdict"] or "check failed", p["odds"]), True)
        return
    plan = p["plan"]
    stop, target = plan.get("stop"), plan.get("target")
    q = quotes_for(day, [sym]).get(sym)
    price = float(q) if q is not None else float(p["price"])
    if (stop is not None and price <= float(stop)) or (target is not None and price >= float(target)):
        day.note("auto-moved:" + sym, "AUTO-PAPER: %s triggered at %s but the price is already %.2f (stop %s, "
                 "target %s): skipped." % (sym, p["price"], price, stop, target), True)
        return
    fp, why = ticket_ready(day, sym, "buy")
    if not fp:
        day.note("auto-notready:" + sym, "AUTO-PAPER: %s passed risk but the ticket is not ready (%s): no trade."
                 % (sym, why), True)
        return
    qty = int(float(p["orders"][0].get("qty", 0)))
    tr = {"symbol": sym, "qty": qty, "planned_price": round(price, 2), "stop": stop, "target": target,
          "flat_by": plan.get("flat_by"), "product": p["orders"][0].get("product", "MIS"),
          "status": "entering", "entry_fp": fp, "odds": p["odds"], "trigger_time": p["when"]}
    day.state.setdefault("trades", {})[sym] = tr
    day.save()  # a crash between here and the fill is recovered next tick (resolve_trades)
    code, out = day.run(TICKET, "approve", "--index", "1", "--fingerprint", fp[:8], "--auto-paper")
    if code != 0:
        tr.update(status="not-entered", why="approve refused: %s" % last_line(out))
        day.note("auto-approve-failed:" + sym, "AUTO-PAPER: %s buy was not approved (%s): no trade."
                 % (sym, last_line(out)), True)
        return
    finish_entry(day, tr, price)


def finish_entry(day, tr, price):
    sym = tr["symbol"]
    ok, why = paper_result(day, price)
    if not ok:
        day.note("auto-fill-retry:" + sym, "AUTO-PAPER: %s buy approved but the fill was not recorded (%s); it "
                 "retries next tick." % (sym, why), True)
        return
    opened(day, tr, price)


def opened(day, tr, price):
    sym = tr["symbol"]
    tr.update(status="open", entry_price=round(float(price), 2), entry_time=day.clock().strftime("%H:%M"))
    snap = os.path.join("trader", "autopilot", "plan-%s.json" % sym)
    if os.path.exists(os.path.join(day.project, snap)):
        day.run("trader-alerts/alerts.py", "add", "--from-plan", "--plan", snap)
    write_journal(day)
    day.note("auto-buy:" + sym, "AUTO-PAPER BUY %s %s at %.2f (trigger bar closed %s). Stop %s, target %s.%s"
             % (tr["qty"], sym, float(price), tr.get("trigger_time"), tr.get("stop"), tr.get("target"),
                tr.get("odds") or ""), True)


def resolve_trades(day):
    """Finish what an earlier tick left half done: an approved ticket with no result gets its paper
    result; a recorded fill moves the trade on. Matched by the ticket's fingerprint."""
    for sym, tr in sorted((day.state.get("trades") or {}).items()):
        if tr.get("status") == "entering":
            rec = latest_record(day, tr.get("entry_fp"))
            st = (rec or {}).get("status")
            if st == "approved":
                finish_entry(day, tr, tr["planned_price"])
            elif st == "placed":
                opened(day, tr, tr["planned_price"])
            elif rec is None:
                tr.update(status="not-entered", why="never approved")
                day.note("auto-lost:" + sym, "AUTO-PAPER: the %s entry was never approved: no trade." % sym)
        elif tr.get("status") == "exiting":
            rec = latest_record(day, tr.get("exit_fp"))
            st = (rec or {}).get("status")
            if st == "approved":
                finish_exit(day, tr, tr["exit_planned"], tr["exit_reason"], tr["exit_qty"])
            elif st == "placed":
                closed(day, tr, tr["exit_planned"], tr["exit_reason"], tr["exit_qty"])
            elif rec is None:
                tr["status"] = "open"  # the close was never approved: the next tick tries again


def adopt_fills(day, syms):
    """A paper buy filled today for a watched symbol with no trade record (a crash, or a fill made by
    hand): adopt it so it gets exits, with the stop and target from the plan snapshot."""
    trades = day.state.setdefault("trades", {})
    for sym in syms:
        if sym in trades:
            continue
        fills = [r for r in records_since(day.project, day.date, sym, "", ("filled",)) if r.get("side") == "buy"]
        if not fills or shares_held(day.project, sym) <= 0:
            continue
        plan = read_json(os.path.join(day.project, "trader", "autopilot", "plan-%s.json" % sym)) or {}
        f = fills[-1]
        trades[sym] = {"symbol": sym, "qty": int(float(f.get("qty", 0))), "status": "open",
                       "entry_price": float(f.get("price") or plan.get("entry") or 0),
                       "entry_time": ist_stamp(f.get("at"))[-5:], "stop": plan.get("stop"),
                       "target": plan.get("target"), "flat_by": plan.get("flat_by"), "adopted": True}
        write_journal(day)
        day.note("adopted:" + sym, "AUTO-PAPER: found a %s paper buy with no trade record; it is managed now "
                 "(stop %s, target %s)." % (sym, plan.get("stop"), plan.get("target")), True)


def exit_reason(trade, quote, clock, cutoff=None):
    """(reason, price) to exit at, or (None, None). The stop exits at the stop price (an SL-M is
    assumed to fill at its trigger; slippage is not modelled); everything else at the quote.
    cutoff is this scrip's hard close (see auto_square_off); it defaults to the non-F&O one."""
    if trade.get("carried_from"):
        return "carried over", quote
    stop, target = trade.get("stop"), trade.get("target")
    if stop is not None and quote is not None and quote <= float(stop):
        return "stop", float(stop)
    if quote is not None and target is not None and quote >= float(target):
        return "target", quote
    try:
        flat_t = parse_hhmm(trade.get("flat_by") or "15:00", "flat_by")
    except AutopilotError:
        flat_t = datetime.time(15, 0)
    if clock.time() >= flat_t:
        return "flat-by", quote
    # The backstop, for a trade whose flat_by is missing or later than this scrip can safely hold.
    if clock.time() >= (cutoff or AUTO_SQUARE_OFF):
        return "auto square-off", quote
    return None, None


def auto_exits(day):
    """Every open auto-paper trade: a fresh quote each tick, and close what hit, one ticket at a time."""
    trades = day.state.get("trades") or {}
    open_syms = [k for k in sorted(trades) if trades[k].get("status") == "open"]
    if not open_syms:
        return
    quotes = quotes_for(day, open_syms)
    bars = bars_for(day, [s for s in open_syms if not trades[s].get("carried_from")])
    clock = day.clock()
    fno = day.fno
    for sym in open_syms:
        tr = trades[sym]
        cutoff = auto_square_off(sym, day.now.date(), fno)
        reason, price, bar = bar_touch(tr, bars.get(sym) or [], day.date)
        if reason:
            tr["touch_bar"] = bar
        else:
            tr.pop("touch_bar", None)
            reason, price = exit_reason(tr, quotes.get(sym), clock, cutoff)
        if not reason:
            continue
        if price is None:
            loud = clock.time() >= cutoff
            day.note("auto-noquote:%s:%s" % (sym, "eod" if loud else reason),
                     ("AUTO-PAPER ALARM: %s is still open at %s and there is no fresh quote to close it. It retries "
                      "every tick and carries over to the next trading day." if loud else
                      "AUTO-PAPER: %s should close (%s) but there is no fresh quote; it retries next tick.")
                     % ((sym, clock.strftime("%H:%M")) if loud else (sym, reason)), True)
            continue
        try:
            auto_exit(day, sym, tr, reason, float(price))
        except Exception as e:  # retried next tick
            day.log("  error auto-exiting %s: %s\n%s" % (sym, e, traceback.format_exc()))
            day.note("auto-exit-error:%s:%s" % (sym, e), "AUTO-PAPER: closing %s failed (%s); it retries next "
                     "tick." % (sym, e), True)


def auto_exit(day, sym, tr, reason, price):
    pending = human_pending(day)
    if pending:
        day.note("exit-wait:%s:%s" % (sym, pending), "AUTO-PAPER: %s should close (%s) but your undecided ticket "
                 "for %s is in trader/orders.json; the autopilot will not overwrite it. Decide it, and the close "
                 "follows on the next tick." % (sym, reason, pending), True)
        return
    held = int(shares_held(day.project, sym))
    qty = min(held, int(tr.get("qty") or held))
    if qty <= 0:
        tr.update(status="closed", exit_reason="no shares held", exit_time=day.clock().strftime("%H:%M"))
        write_journal(day)
        return
    code, out = day.run(TICKET, "flatten", "--symbol", sym, "--qty", str(qty))
    if code != 0:
        raise AutopilotError("flatten refused: %s" % last_line(out))
    own_order(day)
    risk_path = os.path.join(day.project, "trader", "state", "risk.json")
    if os.path.exists(risk_path):
        os.remove(risk_path)
    day.run("trader-risk/check.py")
    if risk_verdict(day.project, day.date, sym) != "PASS":
        raise AutopilotError("trader-risk did not pass the closing sell")
    fp, why = ticket_ready(day, sym, "sell")
    if not fp:
        raise AutopilotError("the closing ticket is not ready: %s" % why)
    tr.update(status="exiting", exit_fp=fp, exit_planned=round(price, 2), exit_reason=reason, exit_qty=qty)
    day.save()
    code, out = day.run(TICKET, "approve", "--index", "1", "--fingerprint", fp[:8], "--auto-paper")
    if code != 0:
        tr["status"] = "open"
        raise AutopilotError("approve refused: %s" % last_line(out))
    finish_exit(day, tr, price, reason, qty)


def finish_exit(day, tr, price, reason, qty):
    ok, why = paper_result(day, price)
    if not ok:
        day.note("auto-exit-retry:" + tr["symbol"], "AUTO-PAPER: %s close approved but the fill was not recorded "
                 "(%s); it retries next tick." % (tr["symbol"], why), True)
        return
    closed(day, tr, price, reason, qty)


def closed(day, tr, price, reason, qty):
    sym = tr["symbol"]
    entry = float(tr["entry_price"])
    gross = round((price - entry) * qty, 2)
    charges = charges_for(day.cfg, entry * qty, price * qty, day.skills)
    booked = day.clock().strftime("%H:%M")
    touched = tr.get("touch_bar") if reason in ("stop", "target") else None
    tr.update(status="closed", exit_time=touched or booked, booked_at=booked, exit_price=round(price, 2),
              exit_reason=reason, closed_qty=qty, gross=gross, charges=charges,
              net=round(gross - charges, 2) if charges is not None else None)
    write_journal(day)
    loud = reason in ("auto square-off", "carried over")
    touched = (", touched at %s, booked at %s" % (touched, booked)) if touched else ""
    day.note("auto-sell:" + sym, "AUTO-PAPER SELL %d %s at %.2f (%s%s). P&L %+.2f gross, %s net after estimated "
             "charges." % (qty, sym, price, reason, ": closed by the autopilot's 15:15 safety net" if
                           reason == "auto square-off" else touched, gross,
                           ("%+.2f" % tr["net"]) if tr["net"] is not None else "?"), loud or True)


def carry_over(day):
    """Trades still open (or half done) on the last day the autopilot ran come into today, to be closed
    at the first fresh quote. MIS positions should never stay overnight; this is the paper safety net."""
    if day.state.get("carried_checked"):
        return
    day.state["carried_checked"] = True
    folder = os.path.join(day.trader, "state")
    try:
        older = sorted(f for f in os.listdir(folder) if f.startswith("autopilot-") and f.endswith(".json")
                       and f[len("autopilot-"):-5] < day.date and len(f) == len("autopilot-YYYY-MM-DD.json"))
    except OSError:
        return
    if not older:
        return
    prev = read_json(os.path.join(folder, older[-1])) or {}
    for sym, tr in (prev.get("trades") or {}).items():
        if tr.get("status") in ("open", "exiting") and sym not in day.state.setdefault("trades", {}):
            day.state["trades"][sym] = dict(tr, status="open", carried_from=prev.get("date"))
            day.note("carried:" + sym, "AUTO-PAPER ALARM: %s was still open at the end of %s. It is closed at the "
                     "first fresh quote today." % (sym, prev.get("date")), True)


def journal_doc(day):
    trades = [t for _, t in sorted((day.state.get("trades") or {}).items(),
                                   key=lambda kv: kv[1].get("entry_time") or "")
              if t.get("entry_price") is not None]
    done = [t for t in trades if t.get("status") == "closed" and t.get("gross") is not None]
    tot = {"trades": len(trades), "closed": len(done),
           "gross": round(sum(t["gross"] for t in done), 2),
           "charges": round(sum(t.get("charges") or 0 for t in done), 2)}
    tot["net"] = round(tot["gross"] - tot["charges"], 2)
    return {"date": day.date, "paper": True, "trades": trades, "totals": tot,
            "note": "Paper trades: fills simulated at a fresh quote (else the trigger close), the stop price, or the "
                    "latest fresh quote. Slippage is not modelled; charges are trader-budget's estimate."}


def _money(x):
    return "" if x is None else "%+.2f" % float(x)


def journal_lines(doc):
    if not doc or not doc.get("trades"):
        return []
    lines = ["", "## Paper trades", "",
             "| symbol | qty | entry | exit | reason | gross | charges | net |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for t in doc["trades"]:
        lines.append("| %s | %s | %s %.2f | %s | %s | %s | %s | %s |" % (
            t["symbol"], t.get("closed_qty") or t.get("qty"), t.get("entry_time"), float(t["entry_price"]),
            ("%s %.2f" % (t.get("exit_time"), t["exit_price"])) if t.get("exit_price") is not None else "open",
            t.get("exit_reason", ""), _money(t.get("gross")),
            "" if t.get("charges") is None else "%.2f" % t["charges"], _money(t.get("net"))))
    tot = doc["totals"]
    lines.append("| day | %d trades, %d closed | | | | %s | %.2f | %s |" % (
        tot["trades"], tot["closed"], _money(tot["gross"]), tot["charges"], _money(tot["net"])))
    return lines


def write_journal(day):
    write_json(os.path.join(day.trader, "autopilot", "journal-%s.json" % day.date), journal_doc(day))
    day.write_summary()


def release_ended_ticket(day):
    """A prepared ticket the user declined (or the broker denied, or that failed) frees the
    autopilot to prepare the next fresh trigger."""
    active = day.state["active"]
    if not active or day.state["alerts"]:
        return
    ended = records_since(day.project, day.date, active, day.state.get("prepared_at"),
                          ("declined", "denied", "failed"))
    if ended:
        day.state["active"] = None
        day.note("ended:%s:%s" % (active, day.state.get("prepared_at")),
                 "The %s ticket was %s: the autopilot will prepare the next fresh trigger."
                 % (active, ended[-1].get("status")))


def pick_rule(s):
    """The history scoring rule from the autopilot's own stop and target (or-mid and 2R by default).
    Rules the history cannot replay (price:, atr:) are scored as or-mid, and the pick says so."""
    stop = {"or-mid": ["--stop-rule", "or_mid"], "or-low": ["--stop-rule", "or_low"]}.get(s["stop"])
    if stop is None and s["stop"].startswith("pct:"):
        stop = ["--stop-rule", "pct", "--stop-pct", s["stop"][4:]]
    target = (s["target"][:-1] if s["target"].endswith("R") else "2")
    note = "" if stop and s["target"].endswith("R") else " (scored with or-mid and 2R: %s / %s cannot be replayed)" % (
        s["stop"], s["target"])
    return ((stop or ["--stop-rule", "or_mid"]) + ["--target-r", target, "--until", s["flat_by_t"].strftime("%H:%M")],
            note)


def morning_pick(day, s):
    """Pick today's symbols before the open: the most volatile liquid stocks (trader-scanner volatile),
    ranked by how the autopilot's own breakout rule did on each one's last ~60 sessions (trader-timemachine
    history), minus any with results today (trader-news). Writes day.state["pick"]; True when done.
    A failed step retries next tick until auto_pick.until; the config's symbols are the fallback."""
    p = s["pick"]
    runner = day.runner
    old = getattr(runner, "timeout", None)
    if old is not None:
        runner.timeout = max(old, PICK_TIMEOUT)
    try:
        out = os.path.join("trader", "autopilot", "pick-volatile-%s.json" % day.date)
        doc = read_json(os.path.join(day.project, out)) if day.state.get("pick_scanned") else None
        code = 0
        if not (doc or {}).get("hits"):    # a retry reuses this morning's scan; only a new day scans again
            code, _ = day.run("trader-scanner/scan.py", "volatile", "--index", p["index"], "--top",
                              str(p["candidates"]), "--out", out, "--quiet")
            doc = read_json(os.path.join(day.project, out))
            if (doc or {}).get("hits"):
                day.state["pick_scanned"] = True
                day.save()
        hits = [h for h in (doc or {}).get("hits") or [] if isinstance(h, dict) and h.get("symbol")]
        if not hits:
            return False, "the volatile scan found nothing (exit %s)" % code
        cands = []
        for h in hits:
            try:
                cands.append(nse_symbol(h["symbol"]))
            except AutopilotError:
                continue
        atr = {nse_symbol(h["symbol"]): h.get("atr_pct") for h in hits if h.get("symbol") in cands}
        rule, rule_note = pick_rule(s)
        code, hist = day.run_json("trader-timemachine/timemachine.py", "history", *(cands + rule + ["--json"]))
        history = (hist or {}).get("history") or {}
        if not history:
            return False, "the history replay returned nothing (exit %s)" % code
        code, news = day.run_json("trader-news/news.py", "--tickers", ",".join(cands), "--hours", "18",
                                  "--json", "--dry-run", "--no-moves")
        results_today = sorted({d.get("symbol") for d in (news or {}).get("results_due") or []
                                if isinstance(d, dict) and d.get("date") == day.date})
        news_note = "" if news is not None and code != 2 else " (the news check failed: results days not filtered)"
    finally:
        if old is not None:
            runner.timeout = old
    rows = []
    for sym in cands:
        h = history.get(sym)
        if not isinstance(h, dict):
            continue
        avg, n = h.get("avg_R"), h.get("triggered") or 0
        why = None
        if sym in results_today:
            why = "results today"
        elif avg is None or n < p["min_trades"]:
            why = "only %d past breakouts" % n
        elif avg < p["min_avg_r"]:
            why = "average R %+.3f under %+.2f" % (avg, p["min_avg_r"])
        rows.append({"symbol": sym, "avg_R": avg, "trades": n, "target": h.get("target"), "stop": h.get("stop"),
                     "atr_pct": round(atr[sym], 2) if isinstance(atr.get(sym), (int, float)) else None, "skip": why})
    keep = sorted((r for r in rows if not r["skip"]), key=lambda r: (-r["avg_R"], -(r["atr_pct"] or 0)))
    picked = [r["symbol"] for r in keep[:p["top"]]]
    day.state["pick"] = {"at": day.clock().strftime("%H:%M"), "symbols": picked, "rows": rows,
                         "rule": " ".join(rule), "index": p["index"]}
    if picked:
        day.note("pick", "Picked for today: %s.%s%s Skipped: %s." % (
            ", ".join("%s (%+.2fR over %d%s)" % (r["symbol"], r["avg_R"], r["trades"],
                                                 "" if r["atr_pct"] is None else ", ATR %s%%" % r["atr_pct"])
                      for r in keep[:p["top"]]), rule_note, news_note,
            ", ".join("%s (%s)" % (r["symbol"], r["skip"]) for r in rows if r["skip"])[:200] or "none"), True)
    else:
        day.note("pick", "No stock cleared the pick today (%d checked: %s).%s%s" % (
            len(rows), ", ".join("%s %s" % (r["symbol"], r["skip"]) for r in rows)[:200], rule_note, news_note), True)
    return True, None


def todays_symbols(day, s, project, cfg, now, hol, t):
    """(symbols, skipped notes): the morning pick when auto_pick is on, else the configured symbols."""
    p = s["pick"]
    if p is None:
        syms, _, skipped = symbols(project, cfg, now.date(), hol)
        return syms, skipped
    pick = day.state.get("pick")
    if pick is None and p["at_t"] <= t <= p["until_t"]:
        try:
            done, why = morning_pick(day, s)
        except Exception as e:  # a crash in the pick never stops the tick: it retries, then falls back
            done, why = False, "%s: %s" % (type(e).__name__, e)
            day.log("  morning pick crashed: %s\n%s" % (e, traceback.format_exc()))
        if not done:
            day.note("pick-retry:" + hashlib.sha1(str(why).encode("utf-8")).hexdigest()[:8],
                     "Morning pick failed (%s); it retries until %s." % (why, p["until"]))
        pick = day.state.get("pick")
    if pick is not None:
        return list(pick.get("symbols") or []), []
    if t <= p["until_t"]:
        return [], []   # the pick has not run yet: nothing to watch until it does
    day.state["pick"] = {"at": None, "symbols": [], "rows": [], "failed": True}
    try:
        syms, _, skipped = symbols(project, cfg, now.date(), hol)
    except AutopilotError:
        day.note("pick-none", "The morning pick did not run and no symbols are configured: nothing to watch today.",
                 True)
        return [], []
    day.state["pick"]["symbols"] = list(syms)
    day.note("pick-fallback", "The morning pick did not finish by %s: watching the configured symbols instead (%s)."
             % (p["until"], ", ".join(syms)), True)
    return syms, skipped


def tick(project, now=None, runner=None, notifier=None, live_ok=False, test_now=None, skills=SKILLS):
    """One pass. Returns a short status string; raises AutopilotError on a refusal."""
    now = now or now_ist()
    notifier = notifier or mac_notify
    hol, years = load_holidays(skills)
    if now.weekday() >= 5:
        return "weekend: nothing to do"
    if now.date().isoformat() in hol:
        return "NSE holiday (%s): nothing to do" % hol[now.date().isoformat()]
    t = now.time()
    if t < ACTIVE_FROM or t > ACTIVE_UNTIL:
        return "outside 8:55 to 15:35 IST: nothing to do"
    cfg = load_config(project)
    paper = is_paper(cfg)
    if not paper and not live_ok:
        raise AutopilotError('trader/config.json has "paper": false. The autopilot runs on paper by default; '
                             "pass --live-ok to let it prepare live tickets (it still never approves one)")
    s = settings(cfg)
    auto = s["auto_paper"]
    if auto and not paper:
        raise AutopilotError('"auto_paper" needs paper trading, and trader/config.json has "paper": false. '
                             "The autopilot never approves a live order, --live-ok or not.")
    runner = runner or SubprocessRunner(skills, auto_paper=auto)
    if s["pick"] is None:
        syms, _, skipped = symbols(project, cfg, now.date(), hol)
    day = Day(project, now, runner, notifier, test_now)
    day.auto_paper, day.cfg, day.skills = auto, cfg, skills
    day.state["paper"] = paper
    day.state["auto_paper"] = auto
    try:
        day.flush()
        if s["pick"] is not None:
            syms, skipped = todays_symbols(day, s, project, cfg, now, hol, t)
            # an open trade is always watched, picked today or not
            syms = syms + [k for k, v in sorted((day.state.get("trades") or {}).items())
                           if v.get("status") in ("open", "entering", "exiting") and k not in syms]
        for msg in skipped + s["warnings"]:
            day.note("setting:" + hashlib.sha1(msg.encode("utf-8")).hexdigest()[:10], msg)
        if now.year not in years:
            day.note("calendar-year", "The NSE holiday list covers %s, not %d: a holiday this year will look like "
                     "a trading day. Add NSE_HOLIDAYS for %d to trader-scanner/scan.py."
                     % (", ".join(str(y) for y in years), now.year, now.year))

        news_until = max(OPEN, s["entry_from_t"]) if s["pick"] else OPEN   # a late pick still gets its news
        if not day.state["news"] and syms and s["news_at_t"] <= t < news_until:
            args = ["--tickers", ",".join(syms), "--hours", "18"] + (["--dry-run"] if test_now else [])
            code, out = day.run("trader-news/news.py", *args)
            if code in (0, 1):
                day.state["news"] = True
                flag = [ln.strip() for ln in out.splitlines() if "RESULTS DUE" in ln or ln.strip().startswith("!")]
                day.note("news", "News for %s: %s" % (", ".join(syms), (" | ".join(flag)[:160]) if flag else
                                                     "nothing flagged (full text in the log)."), bool(flag))
            else:
                why = " | ".join(ln.strip() for ln in out.splitlines() if ln.strip().startswith("note:"))[:160]
                day.note("news-failed", "The news check failed (%s); it retries until 9:15."
                         % (why or last_line(out)))

        auto_on = False
        if auto:
            carry_over(day)
            auto_on = auto_tools_ok(day)
            if auto_on:
                resolve_trades(day)
                adopt_fills(day, syms)
                if OPEN <= t <= CLOSE:
                    auto_exits(day)  # exits first: closing a position matters more than opening one
        if not auto:
            release_ended_ticket(day)
        if (auto_on or not auto) and s["entry_from_t"] <= t <= s["entry_until_t"]:
            for sym in syms:
                if day.state["done"].get(sym):
                    continue
                try:
                    check_entry(day, sym, s)
                except Exception as e:  # one bad symbol never stops the others
                    day.log("  error checking %s: %s\n" % (sym, e))

        active = None if auto else day.state["active"]
        if active and not day.state["alerts"] and filled_since(project, day.date, active,
                                                              day.state.get("prepared_at"),
                                                              day.state.get("shares_before")):
            snap = os.path.join("trader", "autopilot", "plan-%s.json" % active)
            code, out = day.run("trader-alerts/alerts.py", "add", "--from-plan", "--plan", snap)
            if code == 0:
                day.state["alerts"] = True
                day.note("alerts-on", "%s position seen: stop, target, flat-by and square-off alerts are on "
                         "(from the plan prepared at the trigger)." % active)
            else:
                day.note("alerts-failed", "Could not add alerts for %s (%s). It retries each tick; or in Claude: "
                         "add alerts from the plan." % (active, last_line(out)), True)
        if not auto and day.state["alerts"] and OPEN <= t <= CLOSE:
            code, out = day.run("trader-alerts/alerts.py", "check")
            if code == 0:
                for ln in out.splitlines():
                    ln = ln.strip()
                    if ln.startswith("ALERT"):
                        key = "ring:" + hashlib.sha1(ln.encode("utf-8")).hexdigest()[:12]
                        day.note(key, ln[:230], True)
    finally:
        day.save()
    return "ticked %s IST (%d symbols)" % (now.strftime("%Y-%m-%d %H:%M"), len(syms))


def report_failure(project, now, message, notifier):
    """A refusal or crash during market hours: notify once per day and write it to today.md."""
    if now.weekday() >= 5 or not (ACTIVE_FROM <= now.time() <= ACTIVE_UNTIL):
        return
    path = os.path.join(project, "trader", "state", "autopilot-errors-%s.json" % now.date().isoformat())
    seen = read_json(path) or []
    key = hashlib.sha1(message.encode("utf-8")).hexdigest()[:12]
    if key in seen:
        return
    if notifier(TITLE, "Autopilot stopped: %s" % message[:200], True) is not False:
        seen.append(key)
        write_json(path, seen)
    summary = os.path.join(project, "trader", "autopilot", "today.md")
    os.makedirs(os.path.dirname(summary), exist_ok=True)
    with open(summary, "a", encoding="utf-8") as fh:
        fh.write("- %s  Autopilot stopped: %s\n" % (now.strftime("%H:%M"), message))


# ---------------------------------------------------------------- replay, install, status, log

def replay_project(project, now):
    """`tick --now` runs in trader/autopilot/replay-DATE/, a copy of the config and symbol lists,
    so a replay never overwrites the live plan, orders, risk verdicts or notes."""
    dest = os.path.join(project, "trader", "autopilot", "replay-%s" % now.date().isoformat())
    os.makedirs(os.path.join(dest, "trader"), exist_ok=True)
    for name in ("config.json", "universe.txt", "watchlist.json"):
        src, dst = os.path.join(project, "trader", name), os.path.join(dest, "trader", name)
        if os.path.exists(src):
            shutil.copyfile(src, dst)
        elif os.path.exists(dst):
            os.remove(dst)
    return dest


def label_for(project):
    return "%s.%s" % (LABEL_BASE, hashlib.sha1(os.path.abspath(project).encode("utf-8")).hexdigest()[:8])


def stable_python():
    """An interpreter path that survives upgrades when there is one (Homebrew's unversioned
    python3 link), else this interpreter."""
    which = shutil.which("python3")
    if which and os.path.islink(which) and os.path.realpath(which) == os.path.realpath(sys.executable):
        return which
    return sys.executable


def plist_doc(project, python=None, script=None):
    project = os.path.abspath(project)
    logs = os.path.join(project, "trader", "autopilot", "logs")
    return {"Label": label_for(project),
            "ProgramArguments": [python or stable_python(), script or os.path.abspath(__file__),
                                 "tick", "--project", project],
            "WorkingDirectory": project, "StartInterval": INTERVAL, "RunAtLoad": False,
            "StandardOutPath": os.path.join(logs, "launchd.log"),
            "StandardErrorPath": os.path.join(logs, "launchd.log")}


def auto_line(project):
    try:
        auto = settings(load_config(project))["auto_paper"]
    except AutopilotError:
        auto = False
    if auto:
        return ("AUTO-PAPER is on in trader/config.json: it approves and fills its own PAPER tickets (never a live "
                "one) and logs every trade to trader/autopilot/journal-DATE.json for you to review.")
    return "It never approves an order: when a ticket is ready you open Claude and say yes or no yourself."


def install(project, out=None):
    """Write the plist and return the lines to show. Never runs launchctl."""
    project = os.path.abspath(project)
    label = label_for(project)
    out = os.path.abspath(out or os.path.join(project, "trader", "autopilot", label + ".plist"))
    agents = os.path.realpath(os.path.expanduser(os.path.join("~", "Library", "LaunchAgents")))
    if os.path.realpath(os.path.dirname(out)).startswith(agents):
        raise AutopilotError("not writing into ~/Library/LaunchAgents: write it elsewhere and copy it yourself "
                             "with the printed command")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    os.makedirs(os.path.join(project, "trader", "autopilot", "logs"), exist_ok=True)
    doc = plist_doc(project)
    with open(out, "wb") as fh:
        plistlib.dump(doc, fh)
    agent = "~/Library/LaunchAgents/%s.plist" % label
    return ["Wrote %s" % out,
            "It runs `autopilot.py tick` every %d s with %s; on weekdays it only acts 8:55 to 15:35 IST."
            % (INTERVAL, doc["ProgramArguments"][0]),
            "Nothing is running yet. To turn it on, run these yourself:",
            "  cp %s %s" % (shlex.quote(out), agent),
            "  launchctl bootstrap gui/$(id -u) %s" % agent,
            "To turn it off:",
            "  launchctl bootout gui/$(id -u)/%s" % label,
            "Your Mac must be awake and logged in during market hours, or ticks are skipped.",
            auto_line(project)]


def status(project, now=None, check=False):
    now = now or now_ist()
    date = now.date().isoformat()
    lines = ["Autopilot, %s IST" % now.strftime("%Y-%m-%d %H:%M")]
    st = read_json(os.path.join(project, "trader", "state", "autopilot-%s.json" % date))
    if st:
        lines.append("  done: %s" % (", ".join("%s %s" % kv for kv in sorted(st.get("done", {}).items())) or "none"))
        lines.append("  prepared: %s, alerts on: %s" % (st.get("active") or "none", "yes" if st.get("alerts") else "no"))
        pick = st.get("pick")
        if isinstance(pick, dict):
            lines.append("  picked%s: %s" % ((" at %s" % pick["at"]) if pick.get("at") else "",
                                             ", ".join(pick.get("symbols") or []) or "none"))
        lines += ["  " + n for n in st.get("notes", [])] or ["  no notes yet today"]
        lines += journal_lines(read_json(os.path.join(project, "trader", "autopilot", "journal-%s.json" % date)))
    else:
        lines.append("  no tick has run today")
    label = label_for(project)
    local = os.path.join(project, "trader", "autopilot", label + ".plist")
    agent = os.path.expanduser(os.path.join("~", "Library", "LaunchAgents", label + ".plist"))
    lines.append("  plist: %s" % ("file present at " + agent if os.path.exists(agent) else
                                  ("written, not copied to LaunchAgents: " + local) if os.path.exists(local) else
                                  "none (run autopilot.py install)"))
    if check:
        p = subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        lines.append("  launchd: %s" % ("loaded" if p.returncode == 0 else "not loaded"))
    return lines


def log_tail(project, now=None, lines=60):
    now = now or now_ist()
    path = os.path.join(project, "trader", "autopilot", "logs", now.date().isoformat() + ".log")
    if not os.path.exists(path):
        return ["no log for %s yet" % now.date().isoformat()]
    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines()[-lines:]


# ---------------------------------------------------------------- lock and CLI

def acquire_lock(project):
    """An exclusive flock held for the tick; the OS drops it if the process dies."""
    path = os.path.join(project, "trader", "state", "autopilot.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main(argv=None):
    ap = argparse.ArgumentParser(description="Paper-trading autopilot (trader-autopilot). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")
    for name, helptext in (("tick", "one pass (what launchd runs)"), ("install", "write the LaunchAgent plist"),
                           ("status", "today's notes and state"), ("log", "the tail of today's log")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--project", default=os.getcwd(), help="the folder holding trader/ (default: here)")
        if name in ("tick", "status", "log"):
            p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-03 09:50"')
    sub.choices["tick"].add_argument("--live-ok", action="store_true",
                                     help='run even with "paper": false (still never approves)')
    sub.choices["tick"].add_argument("--notify", action="store_true", help="notify even with --now")
    sub.choices["install"].add_argument("--out", help="where to write the plist (not ~/Library/LaunchAgents)")
    sub.choices["status"].add_argument("--check", action="store_true", help="also ask launchd (read-only)")
    sub.choices["log"].add_argument("--lines", type=int, default=60)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    project = os.path.abspath(a.project)
    now = None
    try:
        now = parse_now(a.now) if getattr(a, "now", None) else None
        if a.cmd == "install":
            print("\n".join(install(project, a.out)))
            return 0
        if a.cmd == "status":
            print("\n".join(status(project, now, a.check)))
            return 0
        if a.cmd == "log":
            print("\n".join(log_tail(project, now, a.lines)))
            return 0
    except AutopilotError as e:
        print("autopilot: %s" % e, file=sys.stderr)
        return 1
    notifier = print_notify if (a.now and not a.notify) else mac_notify
    if a.now:
        project = replay_project(project, now)
        print("Replay in %s (the live files are not touched)." % project)
    lock = acquire_lock(project)
    if not lock:
        print("autopilot: another tick is still running; skipped.")
        return 0
    try:
        print(tick(project, now=now, notifier=notifier, live_ok=a.live_ok, test_now=a.now))
        return 0
    except AutopilotError as e:
        print("autopilot: %s" % e, file=sys.stderr)
        report_failure(project, now or now_ist(), str(e), notifier)
        return 1
    except Exception as e:  # never die silently under launchd
        traceback.print_exc()
        report_failure(project, now or now_ist(), "%s: %s" % (type(e).__name__, e), notifier)
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
