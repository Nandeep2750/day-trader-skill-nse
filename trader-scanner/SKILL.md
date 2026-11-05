---
name: trader-scanner
description: >-
  Chart Scanner for NSE. Scans thousands of Indian stock charts for setups:
  opening-range breakouts, flags, oversold bounces. Builds the list of NSE
  stocks in the EQ series from NSE's own equity list (or the NIFTY 500 with
  --index nifty500), runs a pre-market scan of daily closes (20-day
  closing-high breakout, bull flag, reclaimed the 20-day average, oversold,
  oversold bounce) and an intraday opening-range breakout scan on 5-minute
  bars in IST, then writes trader/watchlist.json for trader-news and
  trader-entry. Free Yahoo data (SYMBOL.NS), bar by bar. Use when the user
  says "scan the market", "scan NSE", "find me setups", "what is breaking
  out", "opening range breakouts", "scan for bull flags", "oversold stocks",
  "pre-market scan", "build my watchlist", "scan the NIFTY 500", "what should
  I look at today", "high volatility stocks", "most volatile stocks for
  tomorrow", "what moves the most", or at the start of a trading day before
  /trader-news. Also ranks the most volatile liquid stocks by ATR % for
  tomorrow's watchlist.
argument-hint: "[universe | daily | orb | volatile] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade on NSE. It reads thousands
of free charts and lists the ones that match a rule. That is all it does. A
scan is a list of charts that match a rule, not a list of stocks to buy, and
a setup on a chart is not a prediction. Most people who day trade lose money;
SEBI's own studies of intraday traders say the same. The scanner does not
change that. It only saves you from flipping through 2,000 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                          # -> trader/universe.txt (NSE, EQ series)
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe --index nifty500         # or just the NIFTY 500
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                             # before the open -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,INFY,M&M # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                               # after 9:35 AM IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 09:45                    # cut the clock at 9:45 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --down                        # breakdowns: the short side, F&O scrips only
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
python3 "${CLAUDE_SKILL_DIR}/scan.py" volatile --index nifty500         # after the close: most volatile liquid stocks
python3 "${CLAUDE_SKILL_DIR}/scan.py" volatile --symbols CARTRADE,AEGISLOG --top 5
```

Flags: `--min-price 50` (rupees) and `--min-volume` (daily 5,00,000 shares,
orb 0), `--or-minutes 15`, `--max-confirm 200` (orb stage 2),
`volatile`: `--min-turnover-cr 100`, `--min-avg-turnover-cr 150`,
`--max-confirm 30`, `--top 15`, `--index all|nifty500`.
`--now "2026-10-29 09:45"` (IST) to pin the clock, `--offline PATH` to read
saved files, `--no-write`, `--show N`, `--json`. Exit code 0 when the scan
found something, 1 when it found nothing or could not run (the reason is one
plain line).

Symbols are plain NSE symbols: `RELIANCE`, `M&M`, `BAJAJ-AUTO`, `360ONE`. A
trailing `.NS` is dropped (`reliance.ns` works); a BSE `.BO` symbol is
refused, because the whole pack trades NSE only. On the shell, quote a list
with `&` in it: `--symbols "M&M,RELIANCE"`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line, read by `daily` and `orb` |
| `trader/watchlist.json` | written by `daily`, `orb` and `volatile`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. trader-news and trader-entry read `hits[].symbol` |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 14:45) |

## The universe

`universe` downloads NSE's list of listed equities,
`nsearchives.nseindia.com/content/equities/EQUITY_L.csv` (about 2,600 rows),
and keeps a row when:

1. The symbol is a plain NSE symbol (letters, digits, `&` and `-`).
2. The series is `EQ`. `BE` and `BZ` are trade-to-trade: every trade must be
   taken for delivery, so they cannot be day traded at all. Any other series
   is out too.

On the Nov 3 2026 file that keeps 2,391 of 2,623 (199 BE, 33 BZ dropped).
`--index nifty500` reads `content/indices/ind_nifty500list.csv` instead: the
500 largest, most traded names, a good default universe for intraday (a
NIFTY 500 scan takes 25 spark requests instead of 120). The tool asks NSE
only for these public archive files, with an honest user agent.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request. Liquidity
comes from the spark meta: last price at least `--min-price` (Rs 50) and the
last session's volume at least `--min-volume` (5,00,000 shares). Run it
before the 9:15 AM open (the 9:00 to 9:08 pre-open is fine) or after the
close: during the session the volume is the day's so far, and today's daily
bar is left out until 3:30 PM. NSE's official close is the weighted average
price of the last 30 minutes, so Yahoo's last bar can still move a little for
a few minutes after 3:30.

The setups are computed from daily closes only (spark has no highs or lows):

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

A closing-high breakout fires more often than an intraday-high one. Hits are
listed most traded first (price times volume, in rupees).

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 9:15,
9:20 and 9:25 five-minute bars, so 9:15 to 9:30. A hit is the first completed
5-minute bar to CLOSE above the range high, at that bar's close time. The
first bar that can break out is the 9:30 bar, done at 9:35 AM.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
   On NSE, spark leaves out any bar whose close did not change from the bar
   before (common in a Rs 15 stock with 1 paisa ticks), so a closes-only
   chart carries the last close forward through those gaps. It still needs
   its 9:15 bar.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. On a strong morning most charts are candidates, so stage 2
   checks at most `--max-confirm` (200) of them, the most traded first (price
   times volume so far), and prints PARTIAL with the count it skipped.
   `--max-confirm 0` checks them all.

A v8 chart is usable only when every 5-minute bar from 9:15 to the cutoff is
there, up to 3:15 PM; a missing bar means a halt or a thin stock. The three
closing-session bars are never required: Yahoo's 3:20 PM bar is empty on
every NSE day, and the 3:15 and 3:25 bars are often thin or missing. Null
closes are dropped, a bar off the 5-minute grid (the live tick, like 9:46:07)
is dropped, and a bar counts only once it is complete (start + 5 minutes).
Before 9:35 AM, on a weekend or an NSE holiday, after the 3:30 PM close, or
after `no_entries_after` (an expired setup) it prints one plain line and
writes nothing.

## The volatility scan (after the close, for tomorrow)

For "which stocks move the most?". Stage 1 reads a month of daily closes
(spark, 20 symbols a request) and works out each stock's average absolute
daily move over the last 14 completed sessions, the latest session's move and
its turnover (price x volume, in crore). Floors: last price at least
`--min-price` (Rs 50) and at least `--min-turnover-cr` (Rs 100 crore) traded
in the latest session. Stage 2 fetches a 1-month daily v8 chart (real highs
and lows) for the `--max-confirm` (30) biggest average movers:

- ATR % = the simple average of the last 14 true ranges (high to low, or the
  gap from the previous close when bigger; not Wilder's smoothing), divided by
  the last completed close.
- The latest session's high-to-low range %.
- Average turnover over the last 20 completed sessions, floor
  `--min-avg-turnover-cr` (Rs 150 crore): a stock that only traded big once is out.

Ranked by ATR %, the top `--top` (15) are printed and written to
`trader/watchlist.json` (mode `volatile`). A session move of 5 % or more gets
"check news": something moved it, and it can gap again at the open. Run it
after the 3:30 PM close; during the session today's bar is left out of the
ATR and averages, and its move and range are labelled "so far".

Say plainly what volatile means for a day trade: a wide daily range means a
wide opening range, a stop further away and, at the same max loss, fewer
shares. It is not a list of stocks to buy.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars are
  5-minute bars, bar by bar, never tick by tick. query2 first, query1 on a 429
  or 5xx, one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 2,310 of 2,391 charts (81
  failed or had no data)". Never round a partial scan up to "the whole market".
- NSE equity trading holidays for 2026 are in the calendar. NSE publishes the
  2027 list in December; until it is added, a 2027 holiday looks like a
  trading day to this tool. Special sessions (Muhurat trading on Sunday
  Nov 8 2026, budget-day Sundays) are not modelled: weekends stay closed.
- Times are IST (UTC+5:30, no daylight saving).

## Steps

1. Once a week or so: `scan.py universe` (or `--index nifty500`).
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup.
3. After 9:35 AM IST (9:45 is a good first look): `scan.py orb`. Tell the
   user how many usable charts broke their range and when, and that a later
   hit is closer to the price now.
4. For tomorrow's list (after the close): `scan.py volatile --index
   nifty500`. Show the table, point at any "check news", and say that a
   volatile stock means a wider stop and fewer shares.
5. The user picks one chart. Hand it on: `/trader-news SYMBOL` (what is
   moving it), then `/trader-entry SYMBOL` (the entry bar by bar). Never pick
   for them.

## Example: the README walkthrough

Thursday Oct 29 2026, scan at 9:45 AM IST over nine real NSE charts
(`tests/fixtures/trader-scanner/wall5m-2026-10-29.json.gz`, real Yahoo v8
charts cut to that day):

```
python3 scan.py orb --offline wall5m-2026-10-29.json.gz --now "2026-10-29 09:45" --min-price 0
Opening-range breakout scan, Thu 2026-10-29 9:45 AM IST: 15-minute range (9:15 to 9:30 AM), 5-minute bars, Yahoo.
Fetched 9 of 9 charts; 9 usable; 5 candidates, 5 confirmed with real highs and lows.
2 of 9 charts closed above their opening-range high by 9:45 AM:
   9:35 AM  HDFCBANK        ₹682.50  range 678.30 to 682.45  +0.01% over
   9:40 AM  RELIANCE      ₹1,174.30  range 1,156.20 to 1,170.50  +0.32% over
```

RELIANCE's range is the 9:15 to 9:25 bars, high 1,170.50 (the 9:25 bar), low
1,156.20 (the 9:15 bar). The 9:30 bar closed at 1,169.50, still inside; the
9:35 bar closed at 1,174.30, 0.32 % over the high, done at 9:40 AM. The user
picks RELIANCE and Claude hands it to `/trader-news RELIANCE`, then
`/trader-entry RELIANCE`. By 2:45 PM (the default last entry) six of the nine
had broken out: HDFCBANK, RELIANCE, ICICIBANK (9:55), SBIN (10:10), INFY
(11:40) and M&M (1:00 PM).

The pre-market scan before Tuesday Nov 3 (closes through Mon Nov 2, a real
19-symbol spark answer) tags RELIANCE a 20-day breakout and a reclaim of the
20-day average, BHARTIARTL, SBIN, LT, MARUTI and NTPC 20-day breakouts, TCS a
bull flag, M&M and TATASTEEL a reclaim, and YESBANK oversold and an oversold
bounce. With the default floors (Rs 50, 5,00,000 shares) IDEA and YESBANK
drop out on price, BAJAJ-AUTO and MARUTI on volume.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data (IST).
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Neither Zerodha nor Angel One offers a paper account, so
  paper mode means trader-broker records the order locally and calls no broker tool. Going live is the
  user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin (MIS leverage), short selling or delivery trades unless the config allows it and the
  user confirms.
- Broker keys (Kite Connect, SmartAPI, TOTP secrets) live in environment variables or the Claude Desktop
  config, never in the chat.
