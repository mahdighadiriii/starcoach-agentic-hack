# Wallex integration review

> Current verification (2026-10-23): [develop review and account evidence](DEVELOP_REVIEW_2026-10-23.md). A key is now configured. Both balance endpoints report 5,000,000 TMN available / zero locked; fees are 0.25% maker and 0.30% taker on all eight configured markets; open orders and history are empty. One active nonsuspended `prop` account is listed. Credit overview and active-package GETs return 401, including with the listed account header. No configured sub-account ID was inferred or written. Funding source, main-wallet linkage, and exchange isolation of 1M remain **unverified**. Network REST/WS checks pass on en0 with certificate validation. No exchange orders were submitted, modified or canceled.

`WallexClient.fee_rates()` exposes both coefficients and ignores the real endpoint's `default`/`metaData` tier entries. `read_account_snapshot` keeps real balances, fees and credit checks separate from paper balances; optional endpoint denial is `auth: partial`, not proof the balance key is invalid. Metadata refresh is bounded and cached for five minutes on the account worker. Balance failures retain the previous timestamp and display an error. Missing/malformed balances, missing live fee coverage and unrecognized wallet state fail closed. Absence of a sub-account header is labeled `key_default_unverified`.

Official references: [account fees](https://developers.wallex.ir/docs/basic-services-account-fee), [sub-account list](https://developers.wallex.ir/docs/generated/active-sub-accounts-list), [balance details](https://developers.wallex.ir/docs/generated/sub-account-balances-details), [credit overview](https://developers.wallex.ir/docs/generated/active-user-packages-overview), [active package](https://developers.wallex.ir/docs/generated/active-user-package-details). These contracts describe credit wallets; they do not establish this key's hackathon funding provenance or isolate the bot's allocation.

The dated sections below retain earlier verification history and are superseded by the current account evidence above.

> Audit update (2026-10-22): [independent verification and limitations](AUDIT_2026-10-22.md). Fee coefficients with ambiguous units are now refused rather than guessed. Public REST/WebSocket checks passed with TLS verification and en0 binding; private endpoints remain unverified because no API key was configured. Probe evidence is timestamped and failures return a nonzero exit code.

Source of truth: developers.wallex.ir pages fetched 2026-10-22 (spot-intro, spot-types, spot-create-order, spot-cancel-order, spot-single-order-detail, spot-active-orders, spot-order-list, spot-active-markets, basic-services-candles/depth/markets/account-fee, trade-credit-*). REST was chosen over the official MCP endpoint: MCP would put an LLM next to order tools, and its semantics are undocumented; REST is deterministic and fully documented.

## Verification status — 2026-10-22 (third session, REAL API, read-only)

Supersedes the "not reachable" notes further down for every item marked **real** here.

**Network root cause.** This host reaches `api.wallex.ir` (31.7.66.220) through en0, while the VPN route stalls during TLS. This demonstrates a route-dependent failure here; it does not establish a universal geographic access policy. The development Mac runs a
full-tunnel VPN (JumpJump, `utun4`, US egress via OVH): the local tunnel accepts TCP itself, then TLS never
completes (`ConnectTimeout: handshake timed out`). The same Mac's physical link (`en0`, Iranian mobile egress)
reaches the API in ~0.3 s (TLS 1.3, certificate verified). curl and Python behave identically; no proxy env vars or
macOS proxy settings are involved. Fixes, in order of preference: exclude `api.wallex.ir` / `wallex.ir` from the VPN
(split tunnel), or set `WALLEX_BIND_INTERFACE=en0` (REST binds the source address, WebSocket uses `IP_BOUND_IF`;
the address is re-resolved after transport failures), or `WALLEX_HTTP_PROXY` with an Iranian egress.
`python -m wtrader probe` now prints the route interface and names the VPN when it is the cause.

Swagger files read: `spot-b9d06896….json`, `basic-services-89626420….json`, `trade-credit-ff5c60b5….yaml`.

| Item | Contract (docs) | Real API (public, read-only) |
|---|---|---|
| Auth header | `x-api-key` (securitySchemes); trade-credit adds `sub-account-client-id` | no-key request → 401 `{"code":1201,"message":"authorization header is missing"}`. **Authenticated calls NOT verified: no `WALLEX_API_KEY` configured** |
| `GET /hector/web/v1/markets` | `symbol, base_asset, quote_asset, is_spot, amount_precision, price_precision` | **real**: 187 TMN spot markets; BTCTMN tick 1, step 1e-8 |
| `GET /v1/markets` (undocumented) | — | answers; minNotional 50,000 TMN on sampled markets (< min_order 100k < max_order 200k) |
| `GET /v1/udf/history` | `symbol, resolution, from, to`; arrays `s,t,o,h,l,c,v` | **real**: resolution `1` and `60` ok; **`5` returns `s=error`** (5m is resampled from 1m, as implemented). 1m series are gap-filled by Wallex (1150/1150 bars, all 9 markets); 230 x 5m in one request works. The last 1h bar is the forming hour (dropped by `closed()`) |
| `GET /v1/depth?symbol=` | `result.ask/bid[].price,quantity,sum` | **real**: 50x50 levels; BTCTMN ≈ BTCUSDT×USDTTMN (ratio 1.00005) |
| WebSocket `orderbook:{SYM}@buyDepth/sellDepth` | Centrifugo | **real**: server 6.9.1, `ping 25`. Push payload is `pub.data = {"message": [levels]}` — **the previous parser expected a list and dropped every update (fixed, regression test with the captured frame)** |
| `GET /v1/account/balances`, `/v1/account/fee`, orders, openOrders | field names match Swagger (`value/locked`, `takerFeeRate`, `clientOrderId/executedQty/executedSum/status/fills`, `result.*[]`) | not callable without a key |
| Trade credit: `GET /sub-accounts`, `/v1/account/balances-detail`, `/v1/prop/user/packages/active`, `connect-to-wallet` | paths and `sub-account-client-id` header match the YAML | not callable without a key |
| Rate limits | not stated in the Swagger files | client throttles 18 orders / 30 reads per 10 s |

No order was placed, canceled or modified; the probe client asserts `trading_enabled is False`.

### Older notes (sessions 1–2, before the network path was found)

| Endpoint | Code | Doc-verified | Tested | Open questions (probe/organizer) |
|---|---|---|---|---|
| `GET /v1/markets` | `WallexClient.markets` | Path seen in SDKs + gisoo; precision decimals per python-wallex | mocked | Confirm `stepSize/tickSize` are decimal counts, `minNotional` unit |
| `GET /hector/web/v1/markets` | `spot_flags` | yes (`is_spot`, `is_tmn_based`) | not used for gating yet | Use to drop non-spot markets |
| `GET /v1/udf/history` | `candles` | yes (resolutions 1,15,60,240,…; arrays s/t/o/h/l/c/v) | mocked (incl. 5m resampled from 1m) | `t` = bar open? (assumed, off-grid bars dropped). Only 1/15/60 are requested; 5m and 4h are resampled from complete finer bars, so the 240 ambiguity no longer matters. Max bars per request unverified (5m decisions need ~1150 one-minute bars) |
| `GET /v1/depth` | `book` | yes | mocked | Depth level count; docs warn about rate limits → poll only symbols with positions/orders |
| `GET /v1/account/balances` | `balances` | indexed example | mocked | Whether `value` includes `locked` (assumed yes, per python-wallex). For credit accounts, `/v1/account/balances-detail` exists with explicit `available/freeze` — switch if `balances` is wrong |
| `GET /v1/account/fee` | `taker_fees` | yes (`takerFeeRate`) | not tested | Unit (fraction vs percent) is **unverified**; ambiguous coefficients are refused, with no percent-conversion guess. Fee currency per side unverified |
| `POST /v1/account/orders` | `submit` | yes: LIMIT/MARKET/STOP_LIMIT/STOP_MARKET; `client_id` A–Z,0–9,_ and must be unique; 201 on success; 20 req/10 s | mocked | Exact rejection body for duplicate client_id; behavior under credit sub-account |
| `GET /v1/account/orders/{client_id}` | `lookup` | yes | mocked | Not-found semantics right after POST (eventual consistency) |
| `DELETE /v1/account/orders/{client_id}` | `cancel` | yes | mocked | Response when already FINISHED |
| `GET /v1/account/openOrders` | `open_orders` | yes (paged) | mocked | Result container shape (`orders` vs keyed lists) — both parsed. Used every 6 ticks for foreign/unknown-order reconciliation (raw `side`, `price`, `origQty` needed) |
| `GET /v1/account/trades` | not implemented | unverified | not tested | Not used by probe/preflight. Current read-only verification uses order history and per-order fills. |

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

## Verification status — 2026-10-22 (second session)

`api.wallex.ir` was again unreachable from the development host (TCP timeouts; `wallex.ir` web loads, and
the public fee page renders client-side from the same API). `developers.wallex.ir` served a broken
Docusaurus shell, so fee levels could not be re-read either. **No Wallex operation has been exercised
against the real API.** Everything in the table is contract-tested with mocked transports only.

| Item | Status |
|---|---|
| Authentication (`x-api-key`, `sub-account-client-id`) | headers mocked-tested; real auth UNVERIFIED |
| Supported markets / TMN conventions (`IRT`→`TMN`, precision as decimal counts) | parser mocked; real list UNVERIFIED |
| Balances (`value` includes `locked`?) | UNVERIFIED — affects free-TMN and the allocation check; preflight prints raw balances for review |
| Exchange filters / minimums | parsed; `preflight` fails if a market minimum exceeds `max_order_tmn` |
| Actual trading fees | UNVERIFIED. Third-party pages disagree (0.1% vs 0.35% for TMN markets). Config keeps 0.35%/side; live replaces it with `/v1/account/fee` at startup and `preflight` warns if the account fee is higher than assumed |
| Order placement / status / partial fills / cancel | mocked + paper venue; never sent to Wallex |
| Open-order reconciliation, trade history | implemented; real shapes UNVERIFIED |
| Rate limits | client throttles 18 orders/10 s and 30 reads/10 s; `preflight` records any rate-limit headers |
| Retry / idempotency | deterministic `client_id`; ambiguous POSTs never retried; tested with injected timeouts |

Run `python -m wtrader probe` and `python -m wtrader preflight` from a host that reaches Wallex, and
commit the outputs as fixtures before arming live.
