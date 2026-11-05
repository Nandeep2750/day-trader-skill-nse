---
name: trader-alerts
description: >-
  Price Alerts for NSE. Rings when a price, a stop, the flat-by time, the
  broker's auto square-off or the opening bell hits. Turns the trade plan
  into four alerts in one go (the stop, the target, "flat by close" at your
  flat-by time and a warning 5 minutes before Zerodha's or Angel One's MIS
  auto square-off), plus above / below / stop / target levels, percent moves
  from the previous close, any time of day, and the NSE opening bell, each
  ringing once so you are not spammed. Knows NSE hours (9:15 to 15:30 IST)
  with the 2026 trading holidays, works out IST itself, fetches free delayed
  quotes (Yahoo chart JSON, SYMBOL.NS) one symbol at a time, and writes
  trader/prices.json for sizing and the risk check. Use when the user says
  "alert me when", "tell me if RELIANCE hits", "watch my stop", "watch my
  trade", "remind me to close by 3:10", "is the market open", "when does
  NSE open", "get me a price", or right after /trader-exit writes a plan.
argument-hint: "[symbol] [price]"
---

# trader-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when an alert rings,
nothing is bought or sold by this skill. In this pack the protective stop is
an SL-M order that trader-broker places at Zerodha or Angel One right after
the entry fills. The target is NOT a resting order (a resting target next to
a resting stop could open a short if both filled), so when the target alert
rings, the user takes profit with `/trader-broker flatten`, which cancels the
stop and then sells.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open (IST)
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by, square-off from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1295.50
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --target 1304.50
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:10 --label "flat by close" --symbol RELIANCE
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --square-off --symbol RELIANCE --broker angelone
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol INFY --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE --write         # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" bars RELIANCE --interval 1m --json  # today's 1- or 5-minute highs and lows, read-only
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

`--now "2026-11-03 15:10"` pins the clock (IST); `--offline DIR` reads saved
quote files (`yahoo-<SYMBOL>.json`, and `bars-<SYMBOL>.json` for `bars`) instead of the network.

Symbols are plain NSE symbols: `RELIANCE`, `M&M`, `BAJAJ-AUTO`, `360ONE`. A
trailing `.NS` is dropped; a `.BO` (BSE) symbol is refused, because this pack
trades NSE only.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` |
| `trader/config.json` | `broker` (`zerodha` or `angelone`, for the square-off time) and `flat_by` when the plan has none |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes, merged in, for trader-budget and trader-risk |
| `trader/portfolio.json` | the symbols held, for `quote --holdings` and the square-off alert |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the IST clock passed `at` ("15:10") on a trading day | once per trading day |
| `square_off` | 5 minutes before the broker's MIS auto square-off (Zerodha 15:25, so 15:20; Angel One 15:10, so 15:05), if `trader/portfolio.json` still holds the symbol (or there is no portfolio file) | once per trading day |
| `opening_bell` | NSE is open (from 9:15 IST) | once per trading day |

A rule with `"date"` is watched on that IST date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today. The pack's
default flat-by time is 15:00, before every broker's auto square-off: a broker
square-off is a market order at whatever price, plus a fee.

## Quotes

- Yahoo: `query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS` (so `M&M`
  is asked as `M%26M.NS`), an unofficial free JSON endpoint, one request per
  symbol, half a second apart. On a 429 it waits and tries once more; on a
  429 or a 5xx after that it tries `query1`. It identifies itself as
  `day-trader-skill-nse/1.0`.
- Stooq's free CSV does not serve NSE symbols (404 for `reliance.in` and
  `reliance.ns` on 2026-11-03), so it is not offered.
- Free quotes are unofficial, delayed and can lag. They are fine for alerts
  and for checking a size, not for timing an order. The broker's price is the
  one that fills. Each price in `trader/prices.json` keeps its quote time, and
  trader-risk only uses one under 15 minutes old.
- If Yahoo is down, read the price from the broker connector (Kite
  `get_ltp`, Angel One `get_ltp`) and write it into `trader/prices.json` as
  `{"as_of": "...Z", "prices": {"RELIANCE": 1307.9}}`.

## The NSE clock

Regular session 9:15 a.m. to 3:30 p.m. IST (pre-open 9:00 to 9:08 is not
counted as open), closed on weekends and on the 2026 equity segment trading
holidays: Jan 26 (Republic Day), Mar 3 (Holi), Mar 26 (Shri Ram Navami),
Mar 31 (Shri Mahavir Jayanti), Apr 3 (Good Friday), Apr 14 (Dr. Baba Saheb
Ambedkar Jayanti), May 1 (Maharashtra Day), May 28 (Bakri Id), Jun 26
(Muharram), Sep 14 (Ganesh Chaturthi), Oct 2 (Mahatma Gandhi Jayanti), Oct 20
(Dussehra), Nov 10 (Diwali Balipratipada), Nov 24 (Prakash Gurpurb Sri Guru
Nanak Dev), Dec 25 (Christmas).

India has no daylight saving time, so IST is UTC+5:30 all year; the tool
needs no time zone files and ignores the computer's own zone. NSE has no
scheduled early closes. Special sessions (Muhurat trading on Sunday
2026-11-08, a budget-day Sunday) are not modelled: weekends stay closed. For
any year other than 2026 the clock says the holiday list is unknown; NSE
publishes next year's list in December.

## Steps

1. Right after trader-exit writes the plan, run `add --from-plan`. Confirm
   the four alerts back to the user: the stop, the target, the flat-by time
   and the square-off warning (with the broker and its time).
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the symbols in today's
   rules and rings each alert once. A check reads the latest delayed quote;
   it is not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each:
   - stop: the SL-M stop at the broker has probably triggered. Check the
     positions (Kite `get_positions`, Angel One positions) and record the
     fill with trader-broker.
   - target: nothing sits at the broker for the target. To take the profit,
     `/trader-broker flatten` (cancels the stop, then sells), then
     trader-risk, then the user's yes.
   - flat by close / square-off: close anything still open the same way.
   Never act for them.

## Example

A test plan on RELIANCE for Tuesday 2026-11-03 (the opening range is that
day's real 9:15 to 9:30 range, 1284.30 to 1306.60; the entry is made up):
entry 1300.00, stop 1295.50 (the range midpoint), target 1304.50, flat by
15:10, broker Zerodha. `add --from-plan` writes `reliance-plan-stop`,
`reliance-plan-target`, `reliance-flat-by` and `reliance-square-off` for that
day. A `check` reading the real 15:29 quote, 1307.90:

```
NSE OPEN (2026-11-03 15:20 IST, regular session).
ALERT RELIANCE is ₹1,307.90: at or above your target ₹1,304.50 (plan target: 1R (one times the risk)).
If the target rests at the broker (a Robo bracket or a resting LIMIT), check get_orders: filled, record it (trader-broker fill --target-hit, which prints the cancel for the stop). If it is only watched, run /trader-broker flatten (cancels the stop, then sells).
ALERT Square-off in 5 minutes: Zerodha auto squares off intraday (MIS) positions at 15:25 IST and
charges for it. RELIANCE is still open: trader-broker flatten --symbol RELIANCE builds the ticket
(cancel the stop, then sell), then trader-risk and your yes.
```

Each rings once. The next morning the plan's alerts are no longer watched;
with `add --opening-bell`, the bell rings at 9:15.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: symbol, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, and never predicts where a price is going.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`): neither Zerodha nor Angel One has a paper account, so
  paper means trader-broker records a simulated order and no broker tool is called. Going live is the
  user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin (MIS leverage), short selling or delivery (CNC) unless the config allows it and the
  user confirms.
- Broker keys (Kite Connect API key and secret, Angel One SmartAPI key, MPIN and TOTP secret) live in
  environment variables or the Claude Desktop config, never in the chat.
