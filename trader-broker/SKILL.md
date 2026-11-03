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

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the broker's order tools are set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 20 --filled-price 1228.10
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status paper          # paper mode
python3 "${CLAUDE_SKILL_DIR}/ticket.py" stop --index 1                           # step 2: the stop call
python3 "${CLAUDE_SKILL_DIR}/ticket.py" stop --index 1 --status placed --broker-id ID
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --qty 20 --price 1214.40 --stop-hit
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

Neither Kite nor Angel One offers a bracket order for NSE equity intraday any
more, so the pack builds its own, in two steps:

1. **The entry**: a MARKET (or LIMIT) BUY, product MIS (intraday).
2. **The protective stop, right after the entry fills**: an SL-M SELL for
   the filled quantity at the plan's stop (Kite `order_type` "SL-M" with
   `trigger_price`; Angel One `variety` "STOPLOSS", `ordertype`
   "STOPLOSS_MARKET" with `triggerprice`). The user's yes for the bracket
   ticket covers this stop; Claude's Allow once card still asks for it.

**The target is never a resting order.** A resting target sell next to a
resting stop sell could both fill and leave the user short. trader-alerts
watches the target; when it rings, flatten (which cancels the stop first).

Until step 2 is placed, the position has no stop at the exchange: place it
at once. If the stop call fails, say so loudly and offer to retry or flatten.

## Paper mode (the default)

Neither Zerodha nor Angel One has a paper account. With `"paper": true`
Claude calls **no broker tool at all**. The ticket header says
`PAPER (SIMULATED: no order goes to Zerodha)`. After the user's yes:
`ticket.py result --index N --status paper` records a simulated fill at the
limit price or the plan's entry (or `--filled-price P`, for example the live
price from trader-alerts) and a simulated stop. It is logged with
`"paper": true`, and trader-risk counts it like a real trade, so the daily
limits are practised too.

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
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "haan", "yes to all", or a yes that names a
   different stock or amount). Ask again. Never turn a reply into a yes yourself.
4. Paper: call no broker tool; `ticket.py result --index N --status paper`. Done.
5. Live: only after "Recorded your yes", make exactly the calls the ticket
   printed (Angel One: `search_scrip` first, then `place_order` with the
   exact-match token). Claude's card appears: Allow once or Deny.
6. Record it: `ticket.py result --index N --status placed --broker-id <order id>
   --filled-qty Q --filled-price P` (from `get_orders` / `get_order_book`), or
   `--status denied`, or `--status failed --note "..."`. A failed order is not
   retried without a new check and a new yes.
7. At once: `ticket.py stop --index N` prints the SL-M stop call for the
   filled quantity. Make it, then `ticket.py stop --index N --status placed
   --broker-id <id>` (or `--status failed`, which prints a warning: retry or flatten).

## Steps: while it is open, and out by flat_by

- trader-alerts watches the stop, the target and the flat-by time
  (`alerts.py add --from-plan`). The SL-M stop at the exchange does the stopping.
- When the stop fills, record it: `ticket.py fill --symbol RELIANCE --side
  sell --qty 20 --price 1214.40 --stop-hit`.
- At the target, or by `flat_by` (15:10 IST), anything still open gets closed:
  1. Ask the broker what is held: `get_positions` (read only). If the stop
     already filled, there is nothing to sell.
  2. `ticket.py flatten --symbol RELIANCE --qty 20` writes the flatten
     ticket: cancel the recorded open stop (Kite `cancel_order` {variety
     "regular", order_id}; Angel One `cancel_order` {variety "STOPLOSS",
     orderid}), then a MARKET SELL MIS for those shares.
  3. `/trader-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one ticket, the calls in
     order (cancel first), then `result`.

## Example

```
ORDER 1 of 1, LIVE ACCOUNT, REAL MONEY, Zerodha (Kite MCP)
  BUY 20 RELIANCE on NSE, market, intraday (MIS), good for today
  stop-loss 1215.60 (SL-M, placed after the entry fills)
  target 1240.40 (watched by trader-alerts, not a resting order)
  max loss about ₹248 if the stop fills at 1215.60 (it can fill worse in a fast market)
  READY: trader-risk PASS, waiting for your yes
  Step 1, after your yes, Claude calls the Zerodha (Kite MCP) tools:
    1. place_order with exactly:
       {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
        "transaction_type": "BUY", "quantity": 20, "product": "MIS",
        "order_type": "MARKET", "validity": "DAY", "tag": "dts1a2b3c4d"}
  Step 2, only after the entry fills: the protective stop for the filled qty
    1. place_order with exactly:
       {... "transaction_type": "SELL", "order_type": "SL-M", "trigger_price": 1215.60 ...}
```

The `why` and the plan stay in the file; they are never sent.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never calls a broker tool in paper mode.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys, secrets, MPINs or TOTP secrets.
- Never switches `"paper"` to false. The user does that themselves.
- Never leaves a resting target and a resting stop at the same time.
- Never passes `force: true` to angel-one-mcp, and never uses F&O, options or BSE.
- Never promises a fill price: a market order and an SL-M stop fill at whatever the market gives.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval".
  Never set an order tool to "Always allow".
- No F&O, margin beyond the config, short selling or delivery (CNC) unless the config allows it and the
  user confirms.
- Broker keys live in the local MCP config or environment variables, never in the chat.
