---
name: trader-budget
description: >-
  Budget Manager. Sizes every NSE position to your budget and your max loss
  per trade, in rupees. Reads the trade plan from trader-exit (entry, stop,
  target), puts the prices on NSE's tick, finds the whole shares that fit
  inside four limits (max loss per trade including estimated Zerodha or
  Angel One charges, position cap, max order, cash after your buffer), says
  which limit decided it, and writes ONE bracket order (an MIS entry, a
  stop-loss, a target) to trader/orders.json for trader-risk and
  trader-broker. A stop too far away for your max loss is a plain refusal,
  not a smaller order. Nothing is placed. Use when the user says "how many
  shares", "size this trade", "how much should I buy", "I have 1 lakh", "I
  only want to risk 1,000 rupees a trade", "set my rules", "keep 10% in
  cash", "what are the charges", or after /trader-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# trader-budget

The Budget Manager turns a plan ("buy RELIANCE at 1174.30, stop 1163.40")
into an exact order ("buy 63 shares, MIS"), inside rules the user sets once.
The stop decides the size: the further away the stop, the fewer shares. It
writes a proposal only. trader-risk checks it and trader-broker asks the user
yes or no. Day trading loses most people money (SEBI's own studies found
that most individual intraday traders lose). Sizing to a max loss keeps each
loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                          # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                  # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write          # a limit order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1174.30 --json   # a limit order at 1174.30
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights RELIANCE=60,ITC=40 --prices trader/prices.json --config split.json
```

`--now "2026-10-29 09:40"` pins the clock (IST). A plan for another day is
refused: make a new one with trader-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, setup, flat-by time (trader-exit writes it) |
| `trader/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (trader-alerts writes it) |
| `trader/portfolio.json` | optional: its cash caps the size (trader-broker keeps it) |
| `trader/orders.json` | the one proposed bracket order it writes with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each value in plain words. Every
amount is in rupees.

- **budget**: how much money this is for (example 1,00,000). Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills at the stop, charges
  included (default 1,000).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest position (75% of the
  budget), the biggest order (80,000) and the smallest order (500).
- The day limits trader-risk enforces: **max_daily_loss_inr** (3,000), **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:30" / "14:45") and **flat_by** ("15:10"), all IST. Keep flat_by before the broker's MIS auto
  square-off (Zerodha 15:20, Angel One 15:15).
- **broker**: "zerodha" or "angelone" (sets the brokerage in the charges estimate and where
  trader-broker sends orders). **product**: "MIS" (intraday, the default) or "CNC" (delivery).
- **include_charges** (true): count the estimated round-trip charges inside the max loss.
- **allow_margin** (false) and **mis_margin_pct** (20): with margin on, an MIS trade needs only that
  share of its value in cash because the broker lends the rest. Margin magnifies losses: leave it off
  unless the user asks.
- **blocked**: NSE symbols they never want to trade.
- Keep **paper** true until they have watched it work. Neither Zerodha nor Angel One has a paper
  account, so paper means trader-broker records the trade locally and sends nothing.

A config from the US pack (with `max_loss_per_trade_usd` and the like) is refused with a message:
rename the keys to `_inr` and set the amounts in rupees. Nothing is converted silently.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

First the prices go on NSE's tick, rounded so the risk never grows: the stop
UP, the target DOWN, a buy limit DOWN. The tick is 0.01 under Rs 250, 0.05 to
Rs 1,000, 0.10 to Rs 5,000, 0.50 to Rs 10,000, 1.00 to Rs 20,000 and 5.00
above. NSE sets each stock's tick from last month's close, so within 10% of a
band edge (225 to 275, 900 to 1,100, 4,500 to 5,500, 9,000 to 11,000, 18,000 to
22,000) the coarser tick is used: it is valid in either band.

Whole shares only: NSE has no fractional shares. The size is the largest
whole number that fits all four limits:

| limit | shares |
| --- | --- |
| max loss | shares x (entry minus stop) + estimated charges stays within `max_loss_per_trade_inr` |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) / entry, never more than the cash in `trader/portfolio.json` (with `allow_margin`, divided by `mis_margin_pct` of the entry) |

**Estimated charges** are an NSE intraday round trip, buy at the entry and
sell at the stop: brokerage (Zerodha 0.03% capped at Rs 20 an order; Angel
One 0.1%, at least Rs 5 and at most Rs 20 an order), STT 0.025% of the sell,
NSE transaction charge 0.00297% of both sides, SEBI fee Rs 10 a crore, stamp
duty 0.003% of the buy, GST 18% on brokerage, exchange and SEBI. Say they are
an estimate and the broker's contract note is the real number. A `charges`
object in the config can override any rate (`brokerage_pct`, `brokerage_cap`,
`brokerage_min`, `stt_sell_pct`, `exchange_pct`, `sebi_pct`, `stamp_buy_pct`,
`gst_pct`).

The entry it sizes at: the limit price with `--limit`. For a market order, the
plan's entry, or the price in `trader/prices.json` if it is fresh (under 15
minutes old) and higher. A price that ran up since the plan means a bigger
loss at the same stop, so fewer shares. A lower price never shrinks the risk.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk is under `min_reward_risk`, the stop is not
below the entry, the target is not above it (the move already happened), the
plan is for another day, the symbol is blocked, not an NSE symbol or a BSE
(.BO) one, or the plan is a short. Say the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees, the
loss at the stop, the estimated charges and the all-in max loss. Say plainly
that a stop is an order, not a promise: in a fast market, a gap or a circuit
it can fill below the stop. Then hand over to `/trader-risk`.

## Example: RELIANCE, Thursday 2026-10-29

Real 5-minute bars. Opening range 9:15 to 9:30: 1156.20 to 1170.50. The 9:35
bar closed at 1174.30, the first close above the range. Stop at the range's
midpoint, 1163.35, which goes UP to the 0.10 tick: 1163.40. Target 1R: 1185.20.
Risk 10.90 a share. The example config: Rs 1,00,000 budget, 10% cash buffer,
Rs 1,000 max loss, Zerodha.

| limit | the math | shares |
| --- | --- | --- |
| max loss | Rs 1,000 / 10.90, charges included | 84 |
| position cap | 75% of Rs 1,00,000 = Rs 75,000 / 1174.30 | **63 (decides it)** |
| max order | Rs 80,000 / 1174.30 | 68 |
| cash | Rs 1,00,000 minus Rs 10,000 = Rs 90,000 / 1174.30 | 76 |

63 shares = Rs 73,980.90 (MIS). Loss at the stop: 63 x 10.90 = Rs 686.70.
Estimated Zerodha charges for the round trip: Rs 73.07. All in: Rs 759.77. It writes:

```json
{"symbol": "RELIANCE", "exchange": "NSE", "side": "buy", "qty": "63", "type": "market",
 "time_in_force": "day", "product": "MIS", "order_class": "bracket",
 "take_profit_limit_price": "1185.20", "stop_loss_stop_price": "1163.40",
 "why": "opening-range breakout on RELIANCE: 63 shares, bound by your position cap (...)",
 "plan": {"entry": 1174.3, "stop": 1163.4, "target": 1185.2, "risk_per_share": 10.9,
          "loss_at_stop": 686.7, "charges": 73.07, "max_loss": 759.77, "broker": "zerodha",
          "setup": "orb", "entry_time": "9:40 AM", "flat_by": "15:10"}}
```

`why` and `plan` are notes for trader-risk and trader-broker. They are never
sent to the broker. "bracket" here is the pack's own: trader-broker places
the entry, then a stop-loss (SL-M) once it fills, and trader-alerts watches the
target; neither broker has a native bracket for equity MIS. With a Rs 10 max
loss, one share would already risk more: no order. At 9:52 the quote was
1176.48, so the same plan risks 13.08 a share and reward:risk falls to 0.67:
refused, because the move it was waiting for has mostly happened.

## Weights mode (kept from v1)

`--weights RELIANCE=30,HDFCBANK=25` or `--targets FILE` (`targets.example.json`
here: five NIFTY 50 names, an example, not a recommendation) splits a budget
across a list by weight in whole shares, so it needs prices (`--prices
trader/prices.json`): the cash buffer comes off the top, weights are
renormalised to 100, any stock over `max_position_pct` is pinned at the cap
and its excess shared out, slices under the minimum order (or a share dearer
than its slice) are dropped, and the rest is reported as left idle. The
orders are plain CNC (delivery) buys with no stop: trader-risk blocks them
unless the config has `"allow_delivery": true` and `"require_stop": false`.
Use a separate config (`--config`) for a long-term split.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag. The broker's price is the one that fills.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes options,
futures, margin or short positions unless the config allows it.

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
