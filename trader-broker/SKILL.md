---
name: trader-broker
description: >-
  Order Executor for NSE. Sends approved orders to Zerodha (Kite MCP) or
  Angel One (angel-one-mcp), paper trading by default, never without your
  yes. Shows each order as a plain ticket (a bracket shows its stop-loss,
  its target and the max loss at the stop in rupees) plus the exact
  place_order call for your broker, records your yes word for word, then
  Claude's own Allow once / Deny card asks again. Places the protective SL-M
  stop after the entry fills, builds the ticket that cancels the stop and
  closes a day trade before the broker's MIS auto square-off, records fills,
  and logs each trade's max loss so trader-risk can hold the daily limit.
  Covers setting up Zerodha's kite-mcp-server (self-hosted, Kite Connect) and
  the community angel-one-mcp server in Claude Desktop or Claude Code (keys
  in the local config, never in chat) and setting order tools to "Needs
  approval". Use when the user says "place the order", "buy it", "execute",
  "send it to Zerodha", "send it to Angel", "close my position", "flatten",
  "square off", "get me out", "connect Kite", "connect Angel One", "set up
  paper trading", or after /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches NSE only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order, under this exact config, in the last 5 minutes**
   (matched by fingerprint; an entry also needs the market open and the time before
   `no_entries_after`). An older PASS is stale: re-run `/trader-risk`.
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the broker's order tools are set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --fingerprint 1a2b3c4d --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 20 --filled-price 1228.10
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status paper          # paper mode
python3 "${CLAUDE_SKILL_DIR}/ticket.py" stop --index 1                           # step 2: the stop call
python3 "${CLAUDE_SKILL_DIR}/ticket.py" stop --index 1 --status placed --broker-id ID
python3 "${CLAUDE_SKILL_DIR}/ticket.py" target --index 1                         # step 3 (resting): the target call
python3 "${CLAUDE_SKILL_DIR}/ticket.py" target --index 1 --status placed --broker-id ID
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --qty 20 --price 1214.40 --stop-hit
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --qty 20 --price 1240.40 --target-hit
python3 "${CLAUDE_SKILL_DIR}/ticket.py" cancelled --symbol RELIANCE --broker-id ID   # the other leg, cancelled
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE --qty 20
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to a broker. It renders tickets and the exact tool
calls, checks that a reply is a real yes for that order, builds the stop and
flatten tickets, and keeps the log in `trader/state/broker.json`.
`--now "2026-11-02 09:45"` pins the clock (IST). Symbols are plain NSE
symbols (RELIANCE, M&M, BAJAJ-AUTO); a `.NS` suffix is stripped, `.BO` is refused.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `flatten` replaces it) |
| `trader/config.json` | `broker` ("zerodha" or "angelone"), `paper`, `product` ("MIS"), `flat_by`, `budget` |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, failed, filled and stop, with each entry's max loss |
| `trader/portfolio.json` | cash and shares, updated by `fill` and by `result --filled-qty` |
| `trader/plan.json` | the flat-by time for `flatten` |

## How a bracket works on NSE

How the target goes in is `"target_order"` in `trader/config.json`. The
default depends on the broker; the ticket says which one applies.

**`robo`, Angel One's default: one Robo bracket.** The entry, the target and
the stop go as ONE `place_order` (variety "ROBO", producttype "BO", ordertype
"LIMIT", `price` the entry, `squareoff` the target distance and `stoploss`
the stop distance, both in rupees from the entry). Angel One holds both legs
and cancels the other when one fills, so they can never both fill. There is
no step 2 or 3. Robo is intraday only, needs a LIMIT entry (a market order is
sent at the plan's entry price, so it may not fill if the price has run), and
Angel refuses it on low-volume stocks: if it is refused, say so and offer
`"target_order": "resting"`. Record it with `result --status placed
--broker-id <the Robo's order id>`; check `get_orders` that both legs are open.

**`resting`, Zerodha's default (Angel One on request): three steps.**

1. **The entry**: a MARKET (or LIMIT) BUY, product MIS (intraday).
2. **The protective stop, right after the entry fills**: an SL-M SELL for
   the filled quantity at the plan's stop (Kite `order_type` "SL-M" with
   `trigger_price`; Angel One `variety` "STOPLOSS", `ordertype`
   "STOPLOSS_MARKET" with `triggerprice` and `price` "0", the SmartAPI
   convention for market types).
3. **The resting target, right after the stop is placed**: a LIMIT SELL for
   the filled quantity at the plan's target (`ticket.py target --index N`).
   A wick that touches the target fills it, even between two checks.

The broker does NOT link the stop and the target. **When one fills, cancel the
other at once**: `ticket.py fill ... --target-hit` (or `--stop-hit`) records
the fill and prints the cancel call for the leg that is still open. If that
cancel fails because the leg already executed, both legs filled and the user
is short: `ticket.py cancelled --symbol S --broker-id ID --failed executed`,
check `get_positions`, and offer to buy the negative quantity back at once
(a market BUY, MIS) for the user's yes.

**`watched`: the old way.** Only the stop rests; trader-alerts watches the
target and the user flattens when it rings. A wick between two checks is
missed.

The user's yes for the bracket ticket covers its stop, its target and the
cancel of the other leg; Claude's Allow once card still asks for each call.
Until the stop is placed (resting and watched), the position has no stop at
the exchange: place it at once. If the stop call fails, say so loudly and
offer to retry or flatten. The target never goes in before the stop.

## Paper mode (the default)

A paper fill is simulated at `--filled-price`, else the limit price or the
plan's entry, else a price in `trader/prices.json` under 15 minutes old. A
market flatten has no price of its own, so pass `--filled-price` (the live
price from trader-alerts) or keep prices.json fresh.

Live trading needs `"paper": false`, literally. A missing, empty or null
`paper` means paper. A live entry also needs an exchange stop on the order
(a bracket): an order with only a plan stop is refused ("no exchange stop:
use a bracket order").

Neither Zerodha nor Angel One has a paper account. With `"paper": true`
Claude calls **no broker tool at all**. The ticket header says
`PAPER (SIMULATED: no order goes to Zerodha)`. After the user's yes:
`ticket.py result --index N --status paper` records a simulated fill at the
limit price or the plan's entry (or `--filled-price P`, for example the live
price from trader-alerts) and a simulated stop. It is logged with
`"paper": true`, and trader-risk counts it like a real trade, so the daily
limits are practised too.

## Auto-paper (delegated paper approvals, for the autopilot)

When the user will be away and wants the pack to trade **paper only** on its
own, they can delegate the yes for paper orders in `trader/config.json`:

```json
{"paper": true, "autopilot": {"auto_paper": true}}
```

Then `ticket.py approve --index N --fingerprint <first 8 hex of show --json's
"fingerprint"> --auto-paper` records the approval without a human reply
(reply "auto-paper (delegated by the user in config; no human yes)",
`"auto": true`), and `ticket.py result --index N --status paper
--filled-price P` records the simulated fill as usual. `log` marks these
rows AUTO-PAPER. Every guard is separate and literal:

- `"paper"` must not be false. **A live config makes --auto-paper impossible**,
  even with `auto_paper` true. Live orders always need the user's own yes.
- `autopilot.auto_paper` must be the JSON value `true` (not "true", not 1).
- Every other approve gate still holds: a PASS no more than 5 minutes old,
  the fingerprint of the ticket, the clock for entries, a canonical order.
- Flatten tickets (exits) can be auto-approved the same way; exits have no
  time-of-day gate.

Never set `auto_paper` for the user: only they turn it on, in their own config.

## Setup A: Zerodha (official kite-mcp-server, self-hosted)

Zerodha's own server is github.com/zerodha/kite-mcp-server (MIT, Go). The
hosted endpoint `https://mcp.kite.trade/mcp` is fine for quotes, holdings
and positions, but it leaves out the order tools (`place_order`,
`modify_order`, `cancel_order`), so placing orders needs the self-hosted
server with the user's own Kite Connect app.

1. The user creates a Kite Connect app at developers.kite.trade (a paid
   developer subscription) and notes the API key and secret. Redirect URL
   for the local server: `http://localhost:8080/callback`.
2. The user installs Go 1.21+ and builds the server:
   `git clone https://github.com/zerodha/kite-mcp-server && cd kite-mcp-server && go build -o kite-mcp-server`.
3. **Claude Desktop**: Settings > Developer > Edit Config opens
   `claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/`,
   Windows `%APPDATA%\Claude\`). The user adds the `kite` block from
   `claude_desktop_config.example.json` here and types their own key and
   secret into the file, with the full path to the binary and `APP_MODE` stdio.
   **Claude Code**: either copy the `kite` block of `mcp.example.json` into
   the project's `.mcp.json` (it reads `KITE_API_KEY` / `KITE_API_SECRET`
   from the user's shell environment), or run in their own terminal:
   `claude mcp add kite --env APP_MODE=stdio --env KITE_API_KEY=... --env KITE_API_SECRET=... -- /full/path/to/kite-mcp-server`.
4. **Keys never go in the chat.** Never ask for them, never read the config
   file back into the conversation. If the user pastes a key or secret
   anyway, tell them to delete that message and regenerate the secret in the
   Kite Connect dashboard.
5. **Log in every morning.** Kite access tokens expire daily (around 6 AM
   IST). Before the session, call the `login` tool and have the user finish
   the Zerodha login in their browser, never in the chat.
6. Check it: `get_profile` and `get_margins` (read only).

Zerodha squares off open MIS equity positions automatically from about
3:20 PM IST (and charges for it); the pack's `flat_by` 15:10 is before that.

## Setup B: Angel One (community angel-one-mcp server)

Angel One does not publish an MCP server. This pack uses the community
server `angel-one-mcp` on npm (github.com/ameernoufil/angel-one-mcp, MIT).
**It is not made or supported by Angel One.** The user decides whether to
trust it with their SmartAPI credentials; reading its source first is wise.

1. The user creates a SmartAPI app at smartapi.angelbroking.com (API key),
   and enables TOTP on their Angel One account (the base32 TOTP secret).
2. Node.js 18+ (for `npx`).
3. Claude Desktop: add the `angel-one` block from
   `claude_desktop_config.example.json`. Claude Code: the `angel-one` block of
   `mcp.example.json` in `.mcp.json`, with the variables in the shell environment.
4. **Set the server's own limits to match trader-risk**:
   `HARD_MAX_ORDER_VALUE` = the user's `max_order_inr` (80000 by default) and
   `HARD_MAX_ORDER_QTY` to a quantity they would never exceed. Hard limits
   can only be changed by editing the config and restarting.
   **Never pass `force: true`**: it bypasses the soft limits.
5. **Security warning.** `ANGEL_PASSWORD` (the MPIN) plus `ANGEL_TOTP_SECRET`
   is a full login to the Angel One account: anyone with both can log in and
   trade as the user. Keep them only in the local config file (or
   the shell environment), never in the chat, a repo, a screenshot or a
   synced folder. If either leaks, change the MPIN and reset TOTP at once.
6. Check it: `get_profile` and `get_funds` (read only).

Angel One symbols: `place_order` wants `tradingsymbol` "RELIANCE-EQ" and the
`symboltoken` from `search_scrip` (`{"exchange": "NSE", "searchscrip":
"RELIANCE"}`). Use the token only from the row whose tradingsymbol is
**exactly** `RELIANCE-EQ`; no exact match, stop and tell the user. Angel One
squares off MIS (INTRADAY) equity from about 3:15 PM IST.

## Tool permissions (both brokers)

In Claude Desktop: Customize > Connectors (or Settings > Connectors), open the
server, and per tool:

- `place_order`, `modify_order`, `cancel_order`, GTT and `convert_position`
  tools: **Needs approval**. Never Always allow.
- read-only tools (`get_profile`, `get_margins` / `get_funds`, `get_positions`,
  `get_orders` / `get_order_book`, `get_ltp`, `search_instruments` /
  `search_scrip`): Always allow is fine.

In Claude Code keep the order tools on ask in `.claude/settings.json`, for example
`"permissions": {"ask": ["mcp__kite__place_order", "mcp__kite__cancel_order",
"mcp__angel-one__place_order", "mcp__angel-one__cancel_order"]}`, and never
put an order tool in `allow`.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY, BLOCKED or stale ("PASS is stale") goes back to `/trader-risk`. Tickets
   that are not READY print no broker calls at all.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all".
3. Run `ticket.py approve --index N --fingerprint <the 8 hex on the ticket you showed>
   --reply "<the user's reply, word for word>"`. The fingerprint proves the order did
   not change between the ticket and the yes; a mismatch means show it again.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order. A yes counts only as "yes" or "y", optionally followed by nothing but
   this order's symbol, its side, words like place, it, order, please, go,
   ahead, confirm, do, now, shares (and the marks , . ! ; @ -), and numbers: a
   bare number must be the qty or the limit price; a loss figure counts only as
   "max loss N" or "₹N". "Sure", "haan", "yes to all", "yes, abort", "yes after
   lunch", "yes sell it" on a buy, the stop or target as a bare number, or any
   other stock or number is not a yes. Ask again. Never turn a reply into a yes
   yourself. approve re-checks the 5-minute PASS and the clock.
   **A PASS and a yes each count for 5 minutes.** After that, re-run
   `/trader-risk`, show the ticket again if the max loss changed, and ask
   again. A yes older than the PASS it relies on does not count. A declined,
   denied or failed order re-opens only with a new PASS and a new yes.
4. Paper: call no broker tool; `ticket.py result --index N --status paper`. Done.
5. Live: only after "Recorded your yes", make exactly the calls the ticket
   printed (Angel One: `search_scrip` first, then `place_order` with the
   exact-match token). Claude's card appears: Allow once or Deny.
6. Record it: `ticket.py result --index N --status placed --broker-id <order id>
   --filled-qty Q --filled-price P` (from `get_orders` / `get_order_book`), or
   `--status denied`, or `--status failed --note "..."`. A failed order is not
   retried without a new check and a new yes. The 5-minute limit gates MAKING
   the call, never RECORDING what happened: a late `result` is always accepted
   for today's yes. A yes with no result that is too old to act on shows
   "UNRESOLVED": no calls, never READY again, until you check `get_orders`
   (Kite tag `dts<fingerprint>`, or the Angel One order book) and record
   `result --status placed ...`, or `--status denied` if it was never sent.
7. At once: `ticket.py stop --index N` prints the SL-M stop call for the
   filled quantity. Make it, then `ticket.py stop --index N --status placed
   --broker-id <id>` (or `--status failed`, which prints a warning: retry or flatten).
   Only one stop per position: if one is already open, `stop` refuses and prints
   the `modify_order` call to change its quantity instead (Kite: variety,
   order_id, order_type SL-M, quantity, trigger_price; Angel One: all its
   modify_order fields with price "0").
8. Resting mode only: right after the stop, `ticket.py target --index N` prints
   the LIMIT target call. Make it, then `ticket.py target --index N --status
   placed --broker-id <id>`. It refuses before the stop is recorded, and never
   places a second target. Robo mode has no step 7 or 8.
9. If `target_order` changes after a yes, the ticket is no longer the one the user approved: `result`,
   `stop` and `target` answer that no yes (or no placed entry) is recorded for it. Put the config back
   to what it was when the user said yes, record what happened, and only then change it.
10. If the config changes after a yes (paper, broker), `result`, `stop` and `target` refuse:
   "config changed since your yes: re-check and re-approve".

## Steps: while it is open, and out by flat_by

- trader-alerts watches the stop, the target and the flat-by time
  (`alerts.py add --from-plan`). The SL-M stop at the exchange does the stopping.
- When the stop or a resting target fills (trader-alerts rings; `get_orders`
  shows it COMPLETE), record it: `ticket.py fill --symbol RELIANCE --side sell
  --qty 20 --price 1214.40 --stop-hit` (or `--target-hit`). In resting mode it
  prints the cancel for the other leg: make that call at once, then
  `ticket.py cancelled --symbol RELIANCE --broker-id ID`. A Robo bracket
  cancels its own other leg; confirm in `get_orders`.
- At the target, or by `flat_by` (15:10 IST), anything still open gets closed:
  1. Ask the broker what is held: `get_positions` (read only). If the stop
     already filled, there is nothing to sell.
  2. `ticket.py flatten --symbol RELIANCE --qty 20` writes the flatten
     ticket: cancel EVERY recorded open stop, resting target and Robo bracket
     for the symbol, one call each (Kite `cancel_order` {variety "regular",
     order_id}; Angel One `cancel_order` {variety "STOPLOSS" for a stop,
     "NORMAL" for a target, "ROBO" for a Robo, orderid}), then a MARKET SELL in
     the position's own product (MIS unless the fill recorded otherwise). After
     cancelling a Robo, call `get_positions` again: Angel may already have
     closed the position; sell only what is still held.
     **If any cancel fails because the stop already executed, do NOT send the
     sell; check get_positions.** If a cancel fails because the stop was already
     cancelled, it is gone: the sell can go.
     Record each cancel as it happens: `ticket.py result --index N --cancelled ID`,
     or `--cancel-failed ID --reason executed|already-cancelled`. A flatten built
     after a partial cancel only cancels the stops still open.
     If the cancels went through but the sell is then denied or failed, the
     ticket prints **NO STOP AT THE EXCHANGE** with the SL-M call to put the
     stop back (the held qty, the cancelled stop's trigger). Re-flatten at once,
     or place that stop and record it: `ticket.py result --index N --restop-id ID`.
     In paper mode the stop is put back as a simulated one.
  3. `/trader-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one ticket, the calls in
     order (cancel first), then `result`.

## Example

```
ORDER 1 of 1, LIVE ACCOUNT, REAL MONEY, Zerodha (Kite MCP)
  BUY 20 RELIANCE on NSE, market, intraday (MIS), good for today
  stop-loss 1215.60 (SL-M, placed after the entry fills)
  target 1240.40 (a resting LIMIT order, placed after the stop; when one fills, cancel the other)
  max loss about ₹248 if the stop fills at 1215.60 (it can fill worse in a fast market)
  fingerprint: 1a2b3c4d
  READY: trader-risk PASS, waiting for your yes
  Step 1, after your yes, Claude calls the Zerodha (Kite MCP) tools:
    1. place_order with exactly:
       {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
        "transaction_type": "BUY", "quantity": 20, "product": "MIS",
        "order_type": "MARKET", "validity": "DAY", "tag": "dts1a2b3c4d"}
  Step 2, only after the entry fills: the protective stop for the filled qty
    1. place_order with exactly:
       {... "transaction_type": "SELL", "order_type": "SL-M", "trigger_price": 1215.60 ...}
  Step 3, only after the stop is placed: the resting target for the filled qty
    1. place_order with exactly:
       {... "transaction_type": "SELL", "order_type": "LIMIT", "price": 1240.40 ...}
```

The `why` and the plan stay in the file; they are never sent.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never calls a broker tool in paper mode.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys, secrets, MPINs or TOTP secrets.
- Never switches `"paper"` to false. The user does that themselves.
- Never leaves a resting target and a resting stop unwatched: when one fills, the other is cancelled at once.
- Never places a target before the stop, or a second stop or target for the same position.
- Never passes `force: true` to angel-one-mcp, and never uses F&O, options or BSE.
- Never promises a fill price: a market order and an SL-M stop fill at whatever the market gives.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order in the last 5 minutes, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
  Never set an order tool to "Always allow".
- No F&O, margin beyond the config, short selling or delivery (CNC) unless the config allows it and the
  user confirms.
- Broker keys live in the local MCP config or environment variables, never in the chat.
