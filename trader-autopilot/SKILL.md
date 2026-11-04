---
name: trader-autopilot
description: >-
  Paper-trading Autopilot for NSE. Runs the read-only steps of an
  opening-range-breakout day trade on a timer and stops at the ticket: news
  at 9:00 IST, an ORB entry check on each of your symbols every 2 minutes
  from 9:30 to 14:45, and on a fresh trigger the exit plan (stop and target
  from your config), the 10,000-replay time machine, position sizing and the
  risk check, then a macOS notification with the trigger time and price,
  stop, target, shares, PASS or BLOCK and the odds. One ticket at a time: a
  second trigger, or a stale one, is noted but not prepared. After you
  record a paper fill it turns on the stop, target, flat-by and square-off
  alerts from the plan it prepared and notifies each ring once. Never
  approves, fills, flattens or calls a broker: your yes stays manual, in
  Claude. Writes a LaunchAgent plist and prints the launchctl lines for the
  user to run; skips weekends and NSE holidays. Use when the user says "run
  it automatically", "start on its own tomorrow", "watch these stocks for
  me", "autopilot", "notify me when it triggers", "schedule the morning
  routine", or asks what the autopilot did today. With "auto_paper": true
  (paper only) it also takes the trades itself on paper: approves its own
  tickets with --auto-paper, simulates the fills, closes at the stop,
  target, flat-by or square-off, and keeps a P&L journal for later review.
argument-hint: "[tick | install | status | log]"
---

# trader-autopilot

The autopilot does the waiting so the user does not have to watch the clock.
Every 2 minutes on an NSE trading day it runs the same tools the user would,
in the same order, and stops exactly where the user's decision starts: the
ticket. It never approves, fills, flattens or calls Zerodha or Angel One; it
can only run the read-only tools in its own allowlist, and trader-broker is
not on it. When a ticket is ready it sends a notification, and the user opens
Claude in the project and says "show ticket", then yes or no.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
The tool finds its sibling skills (`../trader-entry/` and the rest) next to it,
so it works from the repo and from a project's `.claude/skills/`.

```bash
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" install                 # write the LaunchAgent plist, print the launchctl lines
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" status                  # today's notes, what triggered, plist file present or not
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" status --check          # also ask launchd whether it is loaded (read-only)
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" log --lines 80          # every command it ran today, with output
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" tick                    # one pass by hand (what launchd runs)
python3 "${CLAUDE_SKILL_DIR}/autopilot.py" tick --now "2026-11-03 09:50"   # replay a moment (IST)
```

Run every command from the project folder (the one holding `trader/`), or pass
`--project PATH`. A `tick --now` replay runs in
`trader/autopilot/replay-YYYY-MM-DD/` (a copy of the config and symbol lists),
prints instead of notifying, and passes `--dry-run` to the news check, so it
never touches the live plan, orders, risk verdicts, news state or notes.

## What one tick does (IST)

| when | what | notification |
| --- | --- | --- |
| `auto_pick.at` (08:55) to `auto_pick.until` (09:25), once, only with `auto_pick` on | the morning pick: `trader-scanner volatile --index nifty500 --top 20` (works before the open, on past sessions), `trader-timemachine history` on those 20 with the autopilot's own stop and target, `trader-news --json` to drop any with results today; keeps the best `top` (4) by average R that have at least `min_trades` (15) past breakouts and an average R of at least `min_avg_r` (0.0). Up to 300 s a step; later ticks wait. A failed step retries next tick; past `until`, the configured symbols are watched instead | yes: the picks with their average R, trade count and ATR %, and every skip with its reason |
| `news_at` (09:00) to 09:15 | `trader-news` for the symbols; if every source failed (exit 2) it retries until 9:15 and says why | yes; flags RESULTS DUE and important NSE filings |
| `entry_from` (09:30) to `no_entries_after` (14:45) | `trader-entry check SYMBOL --setup orb` for every symbol not yet done | no, until something happens |
| a fresh trigger (its bar closed at most 10 minutes ago) and no ticket pending | `trader-exit plan` (config stop / target), a copy of the plan to `trader/autopilot/plan-SYMBOL.json`, `trader-timemachine run`, `trader-budget`, `trader-risk` (an old `trader/state/risk.json` is removed first, then the new verdict is read from it) | yes: time, price, stop, target, shares, PASS / BLOCK (or "risk check failed"), odds, "show ticket" |
| a stale trigger (the first breakout was earlier) | nothing is prepared | yes: "triggered at 9:50, 25 min ago: stale, not prepared" |
| a trigger while a ticket is pending (prepared today, or any order in `trader/orders.json` from today with no yes / no / result recorded since it was written) | nothing is prepared, the pending ticket's files are untouched; the symbol is checked again next tick | yes: "finish or decline that ticket first" |
| the pending ticket is declined, denied or failed (a trader-broker record after the preparation) | the autopilot is free again; a held-back trigger is re-checked (by then usually stale) | yes |
| expired | no more checks for that symbol today | noted |
| after the prepared buy fills (a buy fill logged after the preparation, or more shares than at preparation) | `trader-alerts add --from-plan --plan trader/autopilot/plan-SYMBOL.json`, once (retried if it fails) | yes |
| 09:15 to 15:30, once alerts are on | `trader-alerts check` | each ring once: stop, target, flat-by 15:10, square-off warning |

Outside 8:55 to 15:35, on weekends and on the 2026 NSE holidays (read from
trader-scanner) a tick does nothing. In a year the holiday list does not
cover, it warns once that a holiday will look like a trading day.

If a notification does not go out (osascript fails), the event is written to
`today.md` as "notification failed, retrying" and sent again on the next tick.
A refusal or crash during market hours (a bad setting, invalid JSON, a live
config without `--live-ok`) is notified once a day and written to `today.md`,
so it is never silent.

## Settings

Symbols: with `"auto_pick"` on, the morning pick (above), plus any open paper
trade. Otherwise (or as the pick's fallback), first found wins:

1. `trader/config.json`: `"autopilot": {"symbols": ["CARTRADE", "AEGISLOG"]}`
2. `trader/universe.txt`: one NSE symbol per line
3. the hits in `trader/watchlist.json` (from `/trader-scanner`), only if the scan
   is from today or the previous trading day

A bad symbol is skipped and noted; the others still run.

`"auto_pick": true` turns the morning pick on with its defaults; an object
overrides them: `{"top": 4, "candidates": 20, "index": "nifty500" | "all",
"min_avg_r": 0.0, "min_trades": 15, "at": "08:55", "until": "09:25"}`;
`{"enabled": false}` or `false` turns it off. The history is about 30 past
breakouts a stock: a small sample, a filter, not a forecast. The pick is in
`status` and `trader/state/autopilot-YYYY-MM-DD.json` ("pick": every
candidate's average R, trades, ATR % and why it was skipped).

Optional, in the same `"autopilot"` block: `"stop"` (`or-mid`, `or-low`,
`price:X`, `pct:X`, `atr:K`; default `or-mid`), `"target"` (`2R`, `1.5R`,
`price:X`; default `2R`), `"news_at"` (`09:00`), `"entry_from"` (`09:30`). The
entry window ends at the config's `no_entries_after`. Invalid values stop the
tick with a clear message (notified once).

`"paper"` follows the same rule as the order tools: only a literal `false` is
live. Live, a tick refuses unless `--live-ok` is passed, and even then it only
prepares tickets and notifies.

## Auto-paper: fully automatic paper trading

For a day the user wants to run unattended on paper and review later:

```json
"paper": true,
"autopilot": {"symbols": ["CARTRADE", "AEGISLOG"], "auto_paper": true}
```

`auto_paper` must be the JSON value `true`, and only works with paper trading:
with `"paper": false` it is a hard refusal (notified once), `--live-ok` or not.
The autopilot never approves a live order. In this mode, and only in it, it may
run trader-broker's `ticket.py`, and only these calls (checked before every run):
`show`, `log`, `flatten`, `approve --auto-paper` (never `--reply`) and
`result --status paper`.

| when | what happens |
| --- | --- |
| first tick of the day | checks that trader-broker's `ticket.py approve --help` knows `--auto-paper`; if not, no automatic action all day and one loud notification |
| a fresh trigger, risk PASS | a fresh quote (at most 15 minutes old) is the fill price, else the trigger close; if that quote is already at or through the stop or the target the trade is skipped. Then `ticket.py show --json` (READY, same symbol and side), the trade is saved as "entering", `approve --auto-paper`, `result --status paper`, alerts from the plan snapshot. Notified as "AUTO-PAPER BUY". |
| risk BLOCK or ticket not READY | no trade, notified. That order in `trader/orders.json` is recorded as the autopilot's own, so it never blocks a later trigger |
| every tick while a paper trade is open | a fresh quote for it with `trader-alerts quote` (alert rules are not needed), and today's 1-minute bars with `trader-alerts bars --interval 1m` |
| a 1-minute bar after the entry reached the stop or the target | closes at the stop price or the target price, even if the price was back by the next tick: a 2-minute check misses a wick between two checks, the bars do not. The first such bar decides; a bar that reached both counts as the stop (the order inside a bar is unknown; the Time Machine's rule too). Bars start the minute after the fill's minute, so nothing from before the buy counts; bars from `flat_by` on are left to the flat-by exit. The note says when ("touched at 10:11, booked at 10:30"); the journal's exit time is the touch time, `booked_at` the tick that booked it. If the bars cannot be read, the quote rules below decide alone |
| quote at or under the stop | closes at the stop price (an SL-M is assumed to fill at its trigger; slippage is not modelled); retried every tick while the price stays there |
| quote at or over the target | closes at the quote |
| `flat_by` (15:10) | closes at the latest fresh quote |
| 15:15, anything still open | "auto square-off" at the latest fresh quote (the safety net before the brokers' MIS square-off) |
| no fresh quote | never a made-up price: it retries every tick; at 15:15 and later it raises a loud alarm, and an unclosed trade carries over to the next trading day and is closed at its first fresh quote |
| a close | `ticket.py flatten` for the trade's own quantity (not everything held), a fresh trader-risk PASS, `show --json`, saved as "exiting", `approve --auto-paper`, `result --status paper`. Notified as "AUTO-PAPER SELL ... P&L". |
| a step left half done (a crash, a refused result) | next tick: an approved ticket with no result gets its paper result (matched by fingerprint, never approved twice); a paper buy filled today with no trade record is adopted, with the stop and target from the plan snapshot |

Limits: one trade per symbol per day, at most `max_trades_per_day` in all (0
means no limit, as in trader-risk), several positions at once, one ticket at a
time (`trader/orders.json` holds one). It never touches or overwrites an
undecided ticket a person left in `trader/orders.json`: entries wait, and a
close waits with a notification until that ticket is decided.
Missing `no_entries_after` means 14:45; blank or null means off, but entries
still stop at `flat_by`.

A replay (`tick --now`) takes the quote as it is now, not as it was at the
replayed moment, so replay exits and P&L are not reliable; use it to check the
flow, not the result.

The day's trades go to `trader/autopilot/journal-YYYY-MM-DD.json` and as a
table in `today.md` (and `status`): symbol, quantity, entry time and price, exit
time, price and reason, gross P&L, estimated charges (trader-budget's own
formula and config) and net, with day totals. Paper fills are simulations:
real fills can be worse.

## Turn it on and off (macOS)

1. `autopilot.py install` writes `trader/autopilot/com.daytrader.autopilot.<id>.plist`
   (the id is a short hash of the project path, so two projects never clash).
   It runs `autopilot.py tick` every 120 seconds with the printed Python (the
   unversioned Homebrew `python3` link when that is the one running, so a
   Python upgrade does not break it) and prints the lines below. It never runs
   launchctl itself and refuses to write into `~/Library/LaunchAgents`:
   installing a background job is the user's call.
2. The user turns it on with the two printed lines (`cp ...` then
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`).
3. And off: `launchctl bootout gui/$(id -u)/<label>` (also printed).

The Mac must be awake and logged in during market hours: a sleeping Mac skips
ticks (keep it plugged in, sleep off, lid open or an external display).
Keep the project in your home folder, not in Desktop, Documents or Downloads:
macOS privacy controls can stop a launchd job from reading those folders.
Notifications come from `osascript`; if they do not show, allow notifications
for Script Editor in System Settings > Notifications.

## Where things go

| file | what |
| --- | --- |
| `trader/autopilot/today.md` | the day's notes, one line per event |
| `trader/autopilot/logs/YYYY-MM-DD.log` | every command it ran, with its exit code and output |
| `trader/autopilot/entry-SYMBOL.json` | the latest entry check per symbol |
| `trader/autopilot/plan-SYMBOL.json` | the plan as prepared at the trigger (the alerts use this copy) |
| `trader/state/autopilot-YYYY-MM-DD.json` | the day's state: news done, done symbols, prepared trade, alerts on, events notified |
| `trader/plan.json`, `trader/orders.json`, `trader/state/risk.json` | written by trader-exit, trader-budget, trader-risk as usual |

## Answering the user

- "What did the autopilot do?" Run `status`, then read the relevant part of `log`.
- A ticket notification arrived: run `/trader-broker` `ticket.py show`, show the
  ticket, and wait for the user's own yes for that one order. Never treat the
  notification as a yes.
- An alert rang for the target or flat-by: offer `/trader-broker flatten`; it
  still needs trader-risk and the user's yes.

## Safety

- Education and tooling, not financial advice. A trigger is a rule firing, not a recommendation.
- Paper trading by default. Without `auto_paper` the autopilot never approves, fills,
  flattens or calls a broker tool; every order needs trader-risk's PASS and the user's
  explicit yes in the chat, and for a live order the "Allow once" card.
- With `auto_paper` (paper only) the user's yes is delegated in the config for
  simulated orders only; every order still needs a fresh trader-risk PASS, and
  nothing ever reaches Zerodha or Angel One.
- It installs nothing on its own: the user runs the launchctl lines.
- Free Yahoo data lags and can be missing; a 2-minute tick can be late by a bar.
