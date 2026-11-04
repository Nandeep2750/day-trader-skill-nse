---
name: trader-exit
description: >-
  Exit Planner for NSE stocks. Sets your stop and your target before you
  enter, so you know when to get out. Reads the entry from trader-entry (or a
  price you give), puts the stop at the opening-range midpoint by default (or
  the range low, your price, a percent, or a multiple of the ATR), puts every
  price on the NSE tick for its price band without ever growing the risk, and
  sets the target at 2R (or 1R, or your price). Refuses a stop at or above
  the entry, a target at or below it, or a stop too tight to survive the
  spread, and says why. Flat by 15:10 IST by default, before Zerodha's and
  Angel One's intraday auto square-off. Writes trader/plan.json for the Time
  Machine, the Budget Manager and Price Alerts. Long side only. Use when the
  user says "where's my stop", "set my stop and target", "plan the exit",
  "when do I get out", "stop loss", "what's my R", "risk reward", or right
  after /trader-entry triggers, before /trader-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X]"
---

# trader-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap or a fast market can fill past it. Day trading loses most people money,
and a neat plan does not change that.

NSE cash market only, prices in rupees, times in IST. Symbols are plain NSE
symbols (`RELIANCE`, `M&M`); a trailing `.NS` is dropped, `.BO` is refused.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --entry 1174.30 --or-high 1170.50 --or-low 1156.20
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

Other flags: `--flat-by 15:10`, `--json`, `--now "2026-10-29 09:41"` (IST),
`--entry PRICE` with a waiting entry.json to plan ahead, `--setup level`,
`--offline FILE` (a saved Yahoo chart JSON for an ATR stop).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from trader-entry: symbol, entry price, time, bar, opening range |
| `trader/config.json` | `flat_by` (15:10), `min_reward_risk` (1.0: a note here, trader-risk enforces it) and `broker` (`zerodha` or `angelone`, for the square-off warning) |
| Yahoo chart API | only for an `atr:K` stop: the session's bars for `SYMBOL.NS` up to the entry bar, free and unofficial |
| `trader/plan.json` | with `--write`: symbol, exchange (NSE), entry, stop and its rule, target and its R, risk a share, reward:risk, flat by. trader-timemachine, trader-budget and trader-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **NSE ticks.** Every price the plan writes is on the tick for its price
  band, so the broker will take it: below ₹250 the tick is ₹0.01, below
  ₹1,000 ₹0.05, below ₹5,000 ₹0.10, below ₹10,000 ₹0.50, below ₹20,000 ₹1.00,
  above that ₹5.00. NSE sets each stock's tick from last month's close, so
  within 10% of a band edge (₹225 to ₹275, ₹900 to ₹1,100, ₹4,500 to ₹5,500,
  ₹9,000 to ₹11,000, ₹18,000 to ₹22,000) the plan uses the coarser tick: it is
  valid in either band.
- **Rounding never grows the risk.** A long's stop rounds UP to the tick
  (toward the entry), the target rounds DOWN. Risk = entry minus stop.
  Target = entry + R x risk. `2R` by default; `1R`, `1.5R` or `price:X` also
  work.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under 0.05% of the price: too tight to survive the
  spread, normal wiggle would stop you out. An entry.json that has not
  triggered (unless you give `--entry`). A short: v2 plans longs only.
- **Flat by.** 15:10 IST by default (or `flat_by` in the config). Out by
  then, win or lose, no overnight hold. Brokers square off intraday (MIS)
  positions themselves near the close, at the market and with a fee:
  Zerodha from 15:20, Angel One from 15:15. A flat-by at or after your
  broker's square-off gets a warning. A flat-by at or after the 15:30 close,
  or an entry at or after the flat-by time, is refused.

## Steps

1. Run it right after `/trader-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/trader-timemachine` to replay the plan, then `/trader-budget`
   to size it to the max loss per trade. Offer `/trader-alerts` for the stop,
   the target and the flat-by time. Claude can keep `entry.py watch` running
   in the background or on a `/loop` until a setup triggers, then come here.

## Example: the walkthrough

RELIANCE, Thursday 2026-10-29. The entry is 1174.30 at 9:40 AM (the 9:35 bar,
bar 4) and the opening range is 1156.20 to 1170.50. `exit.py plan --stop
or-mid --target 1R --write`:

```
Exit plan: RELIANCE (NSE) long, Thu 2026-10-29, opening-range breakout on 5-minute bars
  Entry         ₹1,174.30 at 9:40 AM (bar 4)
  Stop          ₹1,163.40  opening-range midpoint: (₹1,170.50 + ₹1,156.20) / 2 = ₹1163.35, rounded up to the ₹0.10 tick
  Target        ₹1,185.20  1R (one times the risk)
  Risk a share  ₹10.90 (0.93% of the entry)
  Reward:risk   1.0
  Flat by       15:10 IST, win or lose: no overnight hold
```

The default 2R target would be 1174.30 + 2 x 10.90 = 1196.10. trader-budget
then sizes it to your max loss per trade, estimated charges included.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk. It
  never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Long side only. v2 plans longs only.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, futures, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys (Kite Connect, Angel One SmartAPI) live in environment variables or the Claude Desktop
  config, never in the chat.
