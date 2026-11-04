---
name: trader-news
description: >-
  News Monitor for NSE stocks. Tracks breaking news on every stock you watch
  and flags what moves it: Yahoo Finance headlines per NSE symbol, NSE's own
  corporate announcements and board-meeting filings (results, dividends,
  bonus, split, buyback, fund raising, order wins, mergers, resignations,
  exchange queries on a spurt in volume) and how far each stock is from
  yesterday's close. Warns when a company's results are due within the week.
  Shows only what is new since the last check, newest first, in India time
  (IST), tags each item and never repeats one. Use when the user says "any
  news on RELIANCE", "why is this moving", "what's the news on my watchlist",
  "morning briefing", "pre-market news", "monitor the news", "did anything
  break", "check the NSE filings", "when are results", or after
  /trader-scanner finds setups.
argument-hint: "[NSE symbols]"
---

# trader-news

A chart can look perfect and still be about to gap on a results day, a
board decision or an exchange query. Before a setup becomes a trade, this
skill reads the feeds that carry those first, for every stock on the
watchlist, and tells you only what is new. It also prints how far each stock
has moved today, so a filing and a move sit side by side.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                         # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE INFY M\&M --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-nse --json
```

Symbols are plain NSE symbols (`INFY`, `M&M`, `BAJAJ-AUTO`); `INFY.NS` works
too, a BSE `.BO` symbol is refused. Quote `M&M` in a shell.

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (trader-scanner) |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (trader-broker records fills there) |
| Yahoo Finance headline RSS | `https://feeds.finance.yahoo.com/rss/2.0/headline?s=INFY.NS&region=IN&lang=en-IN`, one request per symbol; some NSE symbols get an empty feed |
| NSE announcements RSS | `https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml`, once per run |
| NSE board meetings RSS | `https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml`, once per run |
| NSE equity list | `EQUITY_L.csv`, to match a symbol to the company name the feeds use; cached in `trader/cache/` for a day |
| Yahoo Finance chart endpoint | the last price against yesterday's close (unofficial, can lag) |
| `trader/state/news.json` | what has been shown (kept 30 days) and upcoming board meetings |

The NSE feeds hold about the last day of filings, so run the check at least
daily: every board meeting it sees is remembered until its date passes.

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story or the filing and read it. Quote the source and
  link it. Never summarise a paywalled story you could not read: say so.
- **Filings beat headlines.** If an NSE filing and a headline disagree, the
  filing is what the company told the exchange.
- **Say what it touches.** Every item lists the symbols it touches and its
  tags. Headlines: earnings, analyst, legal, deal, capital, people, risk,
  halt (circuits, suspensions), regulatory (SEBI, RBI, USFDA...), macro (RBI
  policy, inflation, FIIs, crude, the rupee). NSE filings: results, board
  meeting, dividend, bonus, split, buyback, fund raising, order win, merger,
  resignation, people, rating, record date, legal, exchange query, investor
  meet, press release. Filings marked `!` carry results, a dividend, bonus,
  split or buyback, fund raising, a merger, a resignation, a credit rating,
  regulatory action, or an exchange query (a spurt in volume or a news
  verification: the exchange asked the company to explain).
- **RESULTS DUE** heads the brief when a watched company has a board meeting
  to consider financial results within 7 days. Say it plainly: a day trade
  is out before the close, never held into the results.
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no headline is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new; 2 means every fetch failed (no news is not good news: say so). `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: RESULTS DUE first, then every `!` filing,
   anything tagged halt, legal, regulatory or risk, and every MOVING symbol.
3. Write a short brief, per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "results
   are on Thursday: an overnight gap can jump past any stop").
4. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`: results inside the trade, an exchange query, a fund
   raising. Never act on a headline alone.

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before the 9:15 AM IST open.

## Example: Tue 2026-11-03, after the close

Watching SBIN, IDEA, M&M and INFY, checked at 4:30 PM IST (real NSE feeds):

```
INFY       1,148.10 -1.11% vs yesterday's close 1,161.00 at 15:29 IST
  2026-11-03 16:07 IST  M&M         NSE           Mahindra & Mahindra Limited: Updates
! 2026-11-03 15:48 IST  IDEA        NSE           Vodafone Idea Limited: Board Meeting Intimation
      ... Board Meeting to be held on 12-Nov-2026 to inter-alia consider and approve the Unaudited Financial results ...
! 2026-11-03 13:47 IST  SBIN        NSE           State Bank of India: Outcome of Board Meeting
      State Bank Of India has submitted to the Exchange, the financial results for the period ended September 30, 2026.
```

The brief: "SBIN filed its September-quarter results at 1:47 PM: read them
before trading it tomorrow. Vodafone Idea's board meets on Thu Nov 12 for
results; from Thu Nov 5 the check heads with RESULTS DUE. M&M's filing is a
registrar address change: nothing for the trade. No new INFY headline today."
When a symbol has no news, say so rather than padding the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one headline request per symbol, the two NSE feeds
  once per run, a pause between calls. It reads only NSE's public archive
  files and RSS, never the nseindia.com website's internal API.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
