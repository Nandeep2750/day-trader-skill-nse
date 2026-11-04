---
name: trader-risk
description: >-
  Risk Manager. Checks every NSE trade against your limits and stops
  anything over them, before it can reach Zerodha or Angel One. For a day
  trade: a stop is required, the loss at the stop plus the estimated charges
  must fit your max loss per trade (measured from the higher of the plan's
  entry and a fresh price), reward:risk must meet your minimum, the day's
  total risk must fit your daily loss limit, no more than your trades per
  day, and no new entries outside your entry window (IST) or when NSE is
  closed. It also keeps the basics: NSE symbols only, whole shares, prices on
  NSE's tick, MIS not CNC unless allowed, blocked symbols, order size,
  position cap, cash buffer, margin, short sales, duplicates and a daily
  order limit, and warns when flat_by is later than the broker's auto
  square-off. Getting out is never blocked by a day limit. Prints PASS or
  BLOCK with the reason and records the verdicts so trader-broker only shows
  an order that passed. Use when the user says "check my order", "is this
  safe", "risk check", "does this break my rules", "can I take this trade",
  "how much have I risked today", or before /trader-broker, always.
argument-hint: "[path to orders.json]"
---

# trader-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by trader-budget or trader-broker's flatten passes through it. A
BLOCK is final for that order: it does not go to the user for a yes, and it
does not go to the broker. Most people who day trade lose money. These limits
keep each loss and each day's losses small. They do not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-29 09:40"  # pin the clock (IST)
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget, or trader-broker's flatten) |
| `trader/config.json` | the limits, in rupees (trader-budget ships `config.example.json`) |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | a fresh price raises the entry it measures risk from; also values holdings |
| `trader/state/broker.json` | what was placed today: duplicates, trades per day, daily loss |
| `trader/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

## Entries and exits

An **entry** opens or adds to a position: a buy, or a sell that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, asset and short sales
only. Every other rule (day limits, entry window, loss, reward:risk, blocked
list, product, size, duplicates) turns into a warning for an exit, never a BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no symbol; side not buy or sell; no whole-share `qty` (NSE has no fractional shares, and a rupee `notional` is refused); type not market or limit; a limit order with no limit price; `time_in_force` not day; `exchange` not NSE; `product` not MIS or CNC; a price off NSE's tick (near a band edge, off the coarser tick); a bracket that misses its stop-loss or target. Case never matters: "LIMIT" is checked as "limit" |
| asset | anything but a plain NSE symbol (RELIANCE, M&M, BAJAJ-AUTO): a `.BO` (BSE) or `.NS` (Yahoo) suffix, or a symbol that does not look like NSE's |
| short | a sell of more than you hold. Short selling is not supported end to end yet (trader-broker cannot protect a short), so this blocks even with `allow_short` true |
| blocked | (entries) the symbol is on the `blocked` list |
| product | (entries) CNC (delivery) while `allow_delivery` is false: entries are MIS (intraday) |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy would leave less than the cash buffer, or need margin (`allow_margin` false). With `allow_margin` true an MIS buy needs `mis_margin_pct` (20) of its value |
| stop | (entries) no stop while `require_stop` is true (a bracket `stop_loss_stop_price`, or the order's plan stop); a stop on the wrong side of the entry; a fresh price already at or below the stop; on a live account, a stop only in the plan (no exchange stop: use a bracket order) |
| price | (entries) a market order with neither a fresh price (under 15 minutes) nor a fresh plan entry. The plan's entry counts only while it is under 15 minutes old, timed by the OLDER of the plan's `created_at` and its date and entry time (the entry bar's time is the price's time, so a plan written at 10:00 from a 9:40 trigger is as old as 9:40) |
| loss | (entries) qty x (entry minus stop), plus estimated round-trip charges (`include_charges`), over `max_loss_per_trade_inr` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| daily loss | (entries) the max losses (charges included) of today's entries, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) NSE is closed (always, even with the bounds blank), or before `no_entries_before` or after `no_entries_after` (IST) |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed (or approved and not yet resolved) today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a limit order. For a
  market order, the HIGHER of the plan's entry and a fresh price from
  `trader/prices.json` (under 15 minutes old). A plan written at 9:40 cannot
  understate the risk at 10:00 after the price ran up. An older price is not
  used, and it says so.
- **NSE's tick**: 0.01 under Rs 250, 0.05 to Rs 1,000, 0.10 to Rs 5,000, 0.50
  to Rs 10,000, 1.00 to Rs 20,000, 5.00 above. The exchange rejects a price
  off the tick, so the check blocks it first and names the two nearest valid
  prices. NSE sets each stock's tick from last month's close, which the pack
  does not know, so within 10% of a band edge (225 to 275, 900 to 1,100, 4,500
  to 5,500, 9,000 to 11,000, 18,000 to 22,000) only the coarser tick passes: it
  is valid in either band. trader-exit and trader-budget already round that way.
- **Estimated charges** (same formula as trader-budget): brokerage at the
  config's broker (Zerodha 0.03% capped at Rs 20 an order; Angel One 0.1%, Rs 5
  to Rs 20), STT 0.025% of the sell, NSE transaction charge, SEBI fee, stamp
  duty 0.003% of the buy, GST 18%. An estimate: the contract note is the truth.
- **Daily loss** counts the full max loss of every entry placed today, win or
  lose, as trader-broker logged it (charges included), and of every entry the
  user said yes to that has not come back placed, denied or failed yet. A
  logged entry with no max loss counts as a full `max_loss_per_trade_inr`.
  The order being checked never counts against itself: re-checking an order
  already approved or placed leaves its own records out (matched by
  fingerprint). Three full-size trades of about Rs 1,000 use up a Rs 3,000 day.
- **Paper or live**: live only when the config says `"paper": false`, literally.
  Anything else (missing, null, "false" as text) is paper.
- **The window** is IST (UTC+5:30, no daylight saving), worked out in the tool,
  never from the computer's clock zone. The NSE session is 9:15 to 15:30.
  Weekends and NSE holidays are closed. The holiday list covers 2026; for
  another year it warns you to check nseindia.com. Muhurat trading and other
  special sessions are not modelled. Setting both `no_entries_*` to null
  widens the window to the whole session; it never lets an entry through
  while NSE is closed.
- **The fingerprint** in `trader/state/risk.json` covers the order's fields
  (case-normalised), its plan's entry, stop and target, the stop a flatten
  cancels, and the config's product, broker and paper flag. Each verdict also
  carries `checked_at` and the all-in `max_loss`. trader-broker refuses an
  entry whose PASS is more than 5 minutes old or past `no_entries_after`, so
  check again right before asking the user.

Warnings do not block, but say them to the user: a LIVE account, a plan entry
too old to use, a flat_by at
or after the broker's MIS auto square-off (Zerodha 15:20, Angel One 15:15; the
broker would close the position at the market and charge a fee), an order
with no exchange or product (taken as NSE and the config's product), a stop
that lives only in the plan and not at the broker (paper only; live, it
blocks), a price too old to use, a date outside the holiday list, a logged
entry with no max loss (counted in full).

## Steps

1. If the price may have moved since the plan, refresh it first
   (trader-alerts: `alerts.py quote RELIANCE --write`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop with the charges, reward:risk, and how much of today's limits it
   uses. For a BLOCK, give the reason and the fix (fewer shares, a closer stop
   from trader-exit, wait for the window, or skip the trade). Never change a
   limit yourself.
4. If the order changes in any way, run the check again. trader-broker matches
   each order to its verdict by fingerprint, so an edited order is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example: RELIANCE, Thursday 2026-10-29

The RELIANCE bracket from trader-budget at 9:40 AM IST, on a fresh
Rs 1,00,000 paper account with the example config:

```
PASS  #1 buy RELIANCE 63 shares (MIS), entry
          entry 1174.30 (the plan's entry), stop 1163.40, target 1185.20: risk 10.90 a share
          max loss ₹759.77 (₹686.70 at the stop + ₹73.07 estimated charges) (limit ₹1,000), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, ₹0 of ₹3,000 daily loss

1 passed, 0 blocked at 2026-10-29 09:40 IST. Cash after the passed orders: ₹26,019.10 (paper account).
```

One more share and the position cap blocks it:

```
BLOCK #1 buy RELIANCE 64 shares (MIS), entry
          ...
        - position: RELIANCE would be ₹75,155.20, 75.2% of the ₹1,00,000 account, over your 75% cap
```

The same 63 shares checked at 10:00 with the 9:52 quote (1176.48) in
`trader/prices.json` are blocked: the risk is 13.08 a share and reward:risk
falls to 0.67. At 9:25 they are blocked by the window (no new entries before
9:30). The 15:10 sell that closes the position passes, even though it is
after the 14:45 entry cutoff: it is an exit.

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices or the broker log.
- Free Yahoo prices are unofficial and can lag; the broker's price is the one that fills, and a
  stop can fill worse than its price in a fast market, a gap or a circuit.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

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
