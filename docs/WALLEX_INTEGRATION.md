# Wallex integration review

Source of truth: developers.wallex.ir pages fetched 2026-10-22 (spot-intro, spot-types, spot-create-order, spot-cancel-order, spot-single-order-detail, spot-active-orders, spot-order-list, spot-active-markets, basic-services-candles/depth/markets/account-fee, trade-credit-*). REST was chosen over the official MCP endpoint: MCP would put an LLM next to order tools, and its semantics are undocumented; REST is deterministic and fully documented.

**`api.wallex.ir` could not be reached from the development host (TLS handshake stalls; likely geo/network restriction). Nothing below has been exercised against the real API.** Run `python -m wtrader probe` from a host that can reach Wallex.

| Endpoint | Code | Doc-verified | Tested | Open questions (probe/organizer) |
|---|---|---|---|---|
| `GET /v1/markets` | `WallexClient.markets` | Path seen in SDKs + gisoo; precision decimals per python-wallex | mocked | Confirm `stepSize/tickSize` are decimal counts, `minNotional` unit |
| `GET /hector/web/v1/markets` | `spot_flags` | yes (`is_spot`, `is_tmn_based`) | not used for gating yet | Use to drop non-spot markets |
| `GET /v1/udf/history` | `candles` | yes (resolutions 1,15,60,240,…; arrays s/t/o/h/l/c/v) | mocked | `t` = bar open? (assumed, off-grid bars dropped); gisoo observed >1h returns 1h — 240 must be verified, else resample from 60 |
| `GET /v1/depth` | `book` | yes | mocked | Depth level count; docs warn about rate limits → poll only symbols with positions/orders |
| `GET /v1/account/balances` | `balances` | indexed example | mocked | Whether `value` includes `locked` (assumed yes, per python-wallex). For credit accounts, `/v1/account/balances-detail` exists with explicit `available/freeze` — switch if `balances` is wrong |
| `GET /v1/account/fee` | `taker_fees` | yes (`takerFeeRate`) | not tested | Unit (fraction vs percent) is **unverified**; code treats values > 0.05 as percent. Fee currency per side unverified |
| `POST /v1/account/orders` | `submit` | yes: LIMIT/MARKET/STOP_LIMIT/STOP_MARKET; `client_id` A–Z,0–9,_ and must be unique; 201 on success; 20 req/10 s | mocked | Exact rejection body for duplicate client_id; behavior under credit sub-account |
| `GET /v1/account/orders/{client_id}` | `lookup` | yes | mocked | Not-found semantics right after POST (eventual consistency) |
| `DELETE /v1/account/orders/{client_id}` | `cancel` | yes | mocked | Response when already FINISHED |
| `GET /v1/account/openOrders` | `open_orders` | yes (paged) | mocked | Result container shape (`orders` vs keyed lists) — both parsed |

## Authentication and the competition account

- Header `x-api-key`. Trade-credit accounts are fully separate (own wallet, own API key, own history) and **also require header `sub-account-client-id`** (spot-intro). The adapter sends both; live mode refuses to start without `WALLEX_SUB_ACCOUNT_CLIENT_ID` — remove that gate only if the organizer confirms the competition account is a main account.
- Trade-credit packages carry a **daily credit fee** and a **liquidation threshold** (trade-credit-types). If the 5M TMN credit is such a package, those costs must enter the P&L and the drawdown limits must stay well clear of the liquidation level. Confirm with organizers.
- Keys are read only from the environment; `Secrets.__repr__` never prints them; the probe report redacts nothing secret because it never contains headers.

## Orders

- Entries and exits are bounded **marketable LIMIT** orders (ask×(1+25 bps) / bid×(1−30…250 bps)). MARKET orders were avoided: docs say they auto-cancel past a 2% move or thin books, and their quantity semantics are unverified.
- `client_id` = `W` + 24 hex chars of SHA-256(economic key) — deterministic per intent, so restarts cannot mint a second id, and Wallex's uniqueness rule rejects a duplicate POST.
- Ambiguous POST (timeout, 5xx, unparseable 2xx, mismatched id) → `UNKNOWN`, never retried; reconciled via `lookup`; not found after 120 s → `QUARANTINED` + alert + entries halted.
- Native `STOP_LIMIT/STOP_MARKET` exist but are **not used**: they lock inventory, interact with monitored exits (cancel/fill races) and are untested. Protective exits are monitored every 5 s; if the process is down, positions are unprotected. Mitigation: docker `restart: unless-stopped`, alerts, small position sizes. Adding a far-away native stop as a crash backstop is listed as remaining work.
- Precision: quantity floored to step, prices floored/ceiled to tick using Decimal increment arithmetic; min quantity/notional checked after rounding.
