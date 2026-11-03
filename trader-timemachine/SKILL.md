---
name: trader-timemachine
description: >-
  Time Machine. Replays every possible outcome of an NSE day trade before
  you risk a rupee: 10,000 simulated sessions plus every past time the setup
  fired. Takes the plan from trader-exit (entry, stop, target, flat-by),
  rebuilds the rest of the day 10,000 times from the stock's own 5-minute
  NSE bars over the last 60 days (IST, 9:15 to 15:30), and re-runs the same
  rule on every past session. Prints the odds of target first, stop first,
  or neither by the flat-by time (default 15:10, before the MIS auto
  square-off), where the price sits then, the average R, the history, and
  one honest verdict, then hands off to /trader-budget. Use when the user says "run the time machine",
  "replay this trade", "what are the odds", "simulate this trade", "monte
  carlo", "backtest this setup", "how often does this hit the target", "is
  this a coin flip", or after /trader-exit writes a plan, before
  /trader-budget.
argument-hint: "[path to plan.json, or NSE SYMBOL]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the stock's own last 60
NSE sessions, how often does this plan hit the target first, the stop first,
or neither before you have to be out? It answers with 10,000 replays and with the real record of the same
rule on every past session. It is a simulation built from the stock's own
recent bars, not a prediction. Day trading loses most people money, and a
good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 14:30      # end each replay at another time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-10-29 \
    --entry 1174.30 --stop 1163.40 --target 1185.20 --entry-time "9:40 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--offline FILE` (a saved Yahoo chart JSON, `.json` or `.json.gz`),
`--now "2026-10-29 09:45"` (IST) to pin the clock.

Symbols are plain NSE symbols: `RELIANCE`, `M&M`, `BAJAJ-AUTO`. A trailing
`.NS` is fine; BSE (`.BO`) is refused. The data is Yahoo's `SYMBOL.NS`.
Times are IST (UTC+5:30, no daylight saving).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, exchange (NSE), date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range, flat_by |
| Yahoo chart API | one request: 60 days of 5-minute bars for `SYMBOL.NS` (`range=60d&interval=5m`), free and unofficial, can lag |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the close |

## How it works

1. **The pool.** The NSE session is 9:15 to 15:30 IST, 75 five-minute bars.
   It keeps the COMPLETE sessions before the plan's date: every bar from
   9:15 up to `--until`. Yahoo leaves NSE's 15:20 bar empty on every day
   (often 15:25 too), so a day only has to be complete up to `--until`, not
   to the close; a replay to 15:30 is refused for that reason. Days with a
   gap before `--until`, and today, are left out and listed. Every bar
   becomes four ratios to the bar before it: open, high, low and close over
   the previous close.
2. **One replay.** Start at the entry price. For each bar left in the day,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default: the plan's
   `flat_by`, else 3:10 PM, before Angel One's 3:15 PM and Zerodha's
   3:20 PM MIS auto square-off). Green, which means target
   first or above the entry at `--until`.
   R is +1 at a 1R target, -1 at the stop, and otherwise the distance from
   the entry at `--until` divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse.
4. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on every one of those past sessions. That is
   what really happened, not a simulation. Its stops and targets sit on the
   NSE tick (0.01 below 250, 0.05 to 1,000, 0.10 to 5,000, 0.50 to 10,000,
   1.00 to 20,000, 5.00 above), both rounded toward the entry so the risk
   never grows.
5. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them. So a 2R plan with target first
   24.1 % and stop first 50.5 % is close to break-even (it needs 33 % of
   the 74.6 % that hit a side, and got 32 %), not a loser.
6. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean. Those can only point the
   other way because of the replays that hit neither.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 complete sessions, when the entry
bar is not in the data (wrong date, not closed yet, a future day), when the
date is a weekend or an NSE holiday (the 2026 list is built in; add 2027
when NSE publishes it), or when the plan does not make sense (a stop above a buy's entry, an entry off the 5-minute
grid). A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. It ends each replay at the plan's
   `flat_by`, so "neither" means still open at the time you would get out.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the flat-by time, the average R, the history line, then the
   verdict line and any line under it exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   Results, news and circuit limits can do things those 60 days never did.
   Check `/trader-news` (board meetings, results) for the stock before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

## Example: RELIANCE, Thursday 2026-10-29 (real data)

The 15-minute opening range is 1156.20 to 1170.50. The 9:35 bar closes above
it: entry 1174.30 at 9:40 AM (bar 4), stop 1163.40 (the range midpoint
1163.35, rounded up to the 0.10 tick), target 1185.20 (1R, risk 10.90 a
share), flat by 15:10. Seed 29.

```
10,000 replays of the rest of the session, 9:40 AM to 3:10 PM (66 five-minute bars), seed 29.
  target first   24.2 %
  stop first     31.7 %
  neither        44.1 %   still open at 3:10 PM
  green          44.0 %   target first, or above the entry at 3:10 PM
  Price at 3:10 PM: median 1172.72, 90 % of replays between 1156.23 and 1189.76.
  Average R: -0.101
History: the same rule on each of those 55 real sessions fired 26 times:
12 target, 11 stop, 3 neither. Average R -0.020.
Leans toward the stop: target first 24.2 % vs stop first 31.7 %.
44.1 % of the replays hit neither by 3:10 PM; the price at 3:10 PM decides those.
```

At 1R the target has to come first in half of the replays that hit one
side. It did in 43.3 %. The replays lean toward the stop, and the real
history was close to even, slightly negative. That is the honest answer for
this setup, and it is what the user should hear before `/trader-budget`
sizes anything.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag; it leaves NSE's last bars empty. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin (MIS leverage), short selling or delivery (CNC) unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
