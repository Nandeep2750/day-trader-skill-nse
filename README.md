# The Day Trader skill, NSE edition

10 Claude skills for day trading on NSE: scan thousands of charts, track breaking news, time every entry and exit, and replay every outcome before you risk a rupee.

A port of [github.com/Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT) from the US market to the National Stock Exchange of India: IST, the NSE
holiday calendar, the 9:15 to 15:30 session, NSE symbols, rupees, NSE tick
sizes, intraday (MIS) charges, and orders through **Zerodha** (Kite MCP) or
**Angel One** (angel-one-mcp).

Free, no signup, no data API key. The tools are plain Python that read free
public data: Yahoo Finance charts (`SYMBOL.NS`) and NSE's public archive
files and RSS feeds (`nsearchives.nseindia.com`).

Nothing in this pack can place an order on its own. Paper trading is the
default, and neither Zerodha nor Angel One has a paper account, so paper here
means the pack simulates the fill locally and Claude calls no broker tool.

## Install

As a plugin, in Claude Code:

```
/plugin marketplace add Nandeep2750/day-trader-skill-nse
/plugin install day-trader-skill-nse@day-trader-skill-nse
```

Installed as a plugin the skills show up as `/day-trader-skill-nse:trader-scanner`
and so on. Copy the folders instead if you want plain `/trader-scanner`:

```bash
git clone https://github.com/Nandeep2750/day-trader-skill-nse.git
cp -r day-trader-skill-nse/trader-* ~/.claude/skills/
```

The tools need Python 3.8 or newer. Nothing to pip install.

## The ten

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans the NSE EQ universe (or NIFTY 500) for setups: opening-range breakouts, flags, oversold bounces, and the most volatile liquid stocks (ATR %). |
| `/trader-news` | News Monitor | Yahoo headlines plus NSE corporate announcements and board meetings; flags results due this week. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and target on the NSE tick before you enter, and the flat-by time. |
| `/trader-timemachine` | Time Machine | Replays the rest of the session 10,000 times plus every past day the setup fired. |
| `/trader-risk` | Risk Manager | Checks every order against your limits, ticks, charges and the NSE session. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and max loss, charges included. |
| `/trader-broker` | Order Executor | Zerodha or Angel One tickets, paper by default, never without your yes. |
| `/trader-alerts` | Price Alerts | Rings on the stop, the target, flat-by and before your broker's auto square-off. |
| `/trader-autopilot` | Paper Autopilot | Runs news, entry checks, plan, sizing, risk and alerts every 2 minutes on a Mac (launchd); stops at the ticket for your yes. Optional morning auto-pick: the NIFTY 500's most volatile stocks, ranked by the breakout rule's own history. A live dashboard (`dashboard.py`). |

Dropped from the original: `trader-analyst` (read SEC EDGAR filings) and
`trader-pelosi` (read US House trade disclosures). Neither has an NSE source
the tools can read without scraping.

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by 15:10, square-off warning                        (while it is open)
news                                                                           (daily, on a schedule)
autopilot: all of the above every 2 minutes on a Mac, stopping at your yes    (paper)
```

An example on real data: RELIANCE, Thursday October 29 2026.

1. **`/trader-scanner`** at 9:45 AM IST: RELIANCE's 15-minute opening range
   (9:15 to 9:30) is 1,156.20 to 1,170.50, and the 9:35 bar closed at
   1,174.30 at 9:40 AM, 0.32 % over the top.
2. **`/trader-news`**: the morning's headlines in IST, NSE filings for the
   company, and a RESULTS DUE line if a board meeting on results is within 7 days.
3. **`/trader-entry`**: at 9:37 it said waiting (the 9:35 bar was still
   forming); at 9:40 it said triggered at ₹1,174.30.
4. **`/trader-exit`**: stop at the range midpoint 1,163.35, rounded UP to the
   ₹0.10 tick = ₹1,163.40, so ₹10.90 a share at risk; target 1R = ₹1,185.20;
   flat by 15:10 IST, before Zerodha's (15:20) and Angel One's (15:15) MIS auto square-off.
5. **`/trader-timemachine`**: 10,000 replays from 9:40 to 15:10, each 5-minute
   candle drawn from RELIANCE's own candles at that time of day over the 55
   complete sessions before (seed 29): target first 24.2 %, stop first 31.7 %,
   neither 44.1 %. The same rule on those real sessions fired 26 times: 12
   target, 11 stop, 3 neither. Verdict: leans toward the stop.
6. **`/trader-budget`** with a ₹1,00,000 account and a ₹1,000 max loss: the
   75 % position cap binds at 63 shares (₹73,980.90). Loss at the stop ₹686.70
   plus ₹73.07 estimated Zerodha charges = ₹759.77 all in. One bracket: buy 63
   RELIANCE, market, MIS; then an SL-M stop at 1,163.40; then a resting target.
7. **`/trader-risk`**: PASS, with the max loss, daily loss, trades per day,
   reward:risk, entry window, position size, cash, tick and product checks.
8. **`/trader-broker`** shows the ticket and the exact Kite or Angel One tool
   call. You type yes for that one order; Claude's own "Allow once / Deny" card
   asks again. After the fill it places the SL-M stop for the filled quantity,
   then the LIMIT target (on Angel One the default sends all three as one Robo
   bracket instead).
9. **`/trader-alerts`** rings on the stop, the target (the other leg is then
   cancelled at once), at 15:10, and at 15:15 if a Zerodha position is still open.

## Brokers

**Zerodha.** Official [zerodha/kite-mcp-server](https://github.com/zerodha/kite-mcp-server).
The hosted `mcp.kite.trade` leaves out the order tools, so placing orders
needs the self-hosted server with your own Kite Connect app (API key and
secret in the MCP config, never in chat). Kite sessions expire daily: run
`login` each morning.

**Angel One.** Angel One has no official MCP server. The pack targets the
community [angel-one-mcp](https://github.com/ameernoufil/angel-one-mcp)
(MIT, not made or endorsed by Angel One). It needs your SmartAPI key, client
ID, MPIN and TOTP secret: together those are a full login to your account, so
keep them only in the local MCP config file. Set its `HARD_MAX_ORDER_VALUE`
to your `max_order_inr`.

The target is set by `"target_order"` in the config. On Angel One the default
is `robo`: entry, target and stop go as one Robo bracket, and Angel cancels the
other leg when one fills. On Zerodha (no intraday bracket) the default is
`resting`: entry, then an SL-M stop, then a LIMIT target; when one fills the
other is cancelled at once (the broker does not link them, so if both fill
first you are briefly short and buy back). `watched` keeps only the stop at
the broker and has trader-alerts watch the target. Setup details are in `trader-broker/SKILL.md`,
with config examples in `trader-broker/claude_desktop_config.example.json`
and `trader-broker/mcp.example.json`.

## The tools

```bash
python3 trader-scanner/scan.py universe                                # NSE EQ series, from EQUITY_L.csv
python3 trader-scanner/scan.py universe --index nifty500               # or the NIFTY 500
python3 trader-scanner/scan.py daily                                   # pre-open setups
python3 trader-scanner/scan.py orb --at 09:45                          # opening-range breakouts so far
python3 trader-scanner/scan.py volatile --index nifty500 --top 15      # most volatile liquid stocks (ATR %)
python3 trader-news/news.py --tickers RELIANCE,INFY --hours 12         # headlines, NSE filings, results due
python3 trader-entry/entry.py check RELIANCE --setup orb --write       # waiting / triggered / expired
python3 trader-exit/exit.py plan --from trader/entry.json --stop or-mid --target 1R --write
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-timemachine/timemachine.py history BHEL SWIGGY          # the breakout rule's past record, no plan
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat-by, square-off
python3 trader-alerts/alerts.py check                                  # rings what hit, once
python3 trader-autopilot/autopilot.py install --project ~/paper-test   # writes a launchd plist, prints the command to load it
python3 trader-autopilot/autopilot.py status                           # what the autopilot did today
python3 trader-autopilot/dashboard.py --project ~/paper-test --open    # live dashboard on 127.0.0.1:8765, past days at /?date=YYYY-MM-DD
```

`--now "2026-10-29 09:40"` pins any tool's clock (IST). Config lives in
`trader/config.json`; start from `trader-budget/config.example.json`
(budget, `max_loss_per_trade_inr`, `broker`: `zerodha` or `angelone`, `product`: `MIS`, `paper`: true).

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline on fixtures cut from real data: RELIANCE's 5-minute
bars for the 60 sessions to November 3 2026, the October 29 scan, NSE's
EQUITY_L and NIFTY 500 lists, and NSE announcement and board-meeting feeds.
They reproduce every number in the example above.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
SEBI's studies found that most individual intraday traders in equity cash
lose money. You can lose money. Decide for yourself, and talk to a
SEBI-registered adviser if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat. Set your broker's order tools to "Needs approval" in
Claude, never "Always allow". No options, futures, margin, short selling or
delivery (CNC) unless you turn them on.

**Paper first.** `"paper": true` is the default. Keys live in the MCP config
file or environment variables, never in the chat.

**Charges are estimates.** Brokerage, STT, exchange, SEBI, stamp duty and GST
are computed from published rates for equity intraday and can change. Check
your broker's charge calculator; override any rate with `charges` in the config.

**Ticks and circuits.** Prices are rounded to NSE's price-band tick sizes
(reviewed monthly by NSE). The pack does not know a stock's circuit limit: a
stock locked in a circuit may not let your stop fill at all.

**Calendar.** The 2026 NSE holiday list is built in. NSE publishes 2027's in
December: add it to each tool's `NSE_HOLIDAYS` then. Special sessions (Muhurat
trading, budget-day Sundays) are not modelled.

**Auto square-off.** Brokers close open MIS positions near the close (Zerodha
about 15:20, Angel One about 15:15) and may charge for it. The pack's flat-by
is 15:10 by default.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. Results, a circuit, or a headline
can do things those days never did.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API: it can lag, change or rate-limit you, and it often leaves NSE's
last bars (15:20, 15:25) empty. Check anything that matters against your broker.

**Taxes.** Intraday equity gains are speculative business income in India.
Nothing here is tax advice.

## Files

```
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json, mcp.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-autopilot/   SKILL.md, autopilot.py, dashboard.py
tests/              unittest suite + offline fixtures
```

MIT. Original pack by [Jake Schincariol](https://opusjake.ai).
