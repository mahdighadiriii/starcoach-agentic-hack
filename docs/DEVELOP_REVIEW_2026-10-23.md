# Develop comparison before implementation — 2026-10-23

Feature working tree: `feat/wallex-agentic-trader`, HEAD `ac3edeb`. Develop reference: `origin/develop` `0ad75f3`, detached worktree `../develop`. Initial SSH fetch failed (public-key authentication denied). The final noninteractive HTTPS remote check independently confirms GitHub develop is exactly `0ad75f3d685b6b89183aa7e1bdbfdc0273b0a1e8`, matching the inspected worktree; remote freshness is verified. The histories have no merge base. Main checkout has never left the feature branch.

Protection: complete archive including ignored `.env`, data, logs, evidence and Git metadata; SHA-256 and source inventory in `snapshot.json`/`before_hashes.json`; three consistent SQLite backups. All nonruntime hashes still match after branch inspection. Both existing engines and dashboards left running.

| Area | Develop implementation | Current feature implementation | Decision |
|---|---|---|---|
| Architecture | `app/main.py`: FastAPI with LangGraph `app/agent.py`, global mutable journal/risk, synchronous cycle in worker | `wtrader/`: explicit engine/venue/executor/store/risk boundaries, single SQLite writer, isolated background readers | Keep feature modular monolith. No FastAPI/LangGraph transplant |
| Account | `app/wallex.py`: main balance free = value−locked; key shared with all requests | Private/public header separation, explicit sub-account header, balances-detail, fees, credit reads | Keep feature; improve scope labeling: absent header does not independently identify main wallet ownership |
| Credit | `app/markets.py`: 345 disabled markets attributed to Telegram, 2026-10-21; `RUNBOOK.md` calls key a team credit key | No deny list; explicit live allowlist and credit/wallet preflight | Preserve list as conservative deny-only reference with provenance, never infer all others tradable. Does not prove package or isolation |
| Risk | `app/risk.py`: float, 500k/order, 25% whole-account equity, 5% start-to-current loss, confidence gate | Decimal, 1M allocation, 200k/order, 2 positions, 30k daily loss, reserved/unknown funds, breakers and kill switch | Keep feature limits and recheck deny list at risk and execution boundary |
| Orders | No persisted client ID, wrong cancel URL/query, no reconciliation; `/api/run` may overlap autoloop | Persisted idempotent intent, cumulative fills, unknown quarantine, ownership guard | Do not transfer order code or manual execution endpoints |
| End window | `app/agent.py:flatten` cancels every order and sells every account asset | Bot-owned positions only; persist/reconcile exits | Do not transfer account-wide flatten; reuse boundary-test scenarios only |
| Data | Whole market table repeatedly fetched, 15s timeout, incomplete candles accepted, no WebSocket | Closed/gap-checked candles, buffered books, reconnecting WebSocket, bounded reads | Keep feature. No timing/network settings transferred |
| Strategies | SMA12/26 + RSI fallback; unconstrained LLM proposes sizes and symbols | Fixed SCALP/DONCHIAN candidates; bounded selector; historical cost/replay tooling | No fallback/parameter transfer: no evidence of edge |
| Persistence | JSONL append/reload; restores first equity but not halted state; no simulated positions | WAL SQLite ledger, checkpoints, run manifests, restart recovery | Keep SQLite. Never delete historical journal/evidence per old runbook |
| Config | `.env` via pydantic, DRY_RUN/WINDOW_END/MAX_ORDER_QUOTE, BoxAPI key | YAML + secrets, TRADING_MODE/LIVE_ENABLED, explicit live hold, Anthropic/baseline | Keep feature config; do not import larger limits or live setup instructions |
| Tests | `test_risk.py` (7), `test_flatten.py` (5): basic caps, disabled market, window, journal, rounding | 178 baseline tests; sandbox run 177 pass / 1 socket permission failure (rerun with socket permission needed) | Adapt disabled-market and window boundary scenarios; no wholesale incompatible tests |
| Dashboard | Manual run/autoloop/flatten controls; approved labeled executed even failed | Read-only evidence views, engage-only kill switch, separate experimental/Paper/real balances | Keep feature; add maker/taker, account scope and unknown credit/wallet visibility |

Confirmed additional defects: optional package 401 can label an otherwise valid key globally rejected; without sub-account ID the probe skips credit discovery and dashboard labels scope as main account; only taker rates exposed; optional discovery errors must not erase balances or imply zero credit. Sub-account list parsing/pagination currently can silently hide accounts or malformed responses.

Read-only account evidence before edits: 5M TMN free / 0 locked on both balance endpoints; no open/history orders; all eight configured markets maker .0025/taker .0030; one active nonsuspended `prop` account. Active package and overview both 401 with and without its returned header. No configured sub-account ID. This does not establish hackathon funding provenance, loan amount, wallet link state, or exchange isolation of the authorized 1M. Do not auto-select a discovered scope or write identifiers to config/dashboard.

Planned small changes: port deny-only market reference; safe account overview with independent endpoint outcomes, redacted scope/credit status and full fee rates; bounded sub-account pagination/validation; preserve successful balances when optional reads fail; endpoint-specific access health; use same overview for probe and asynchronous display; dashboard unknown/isolation labels; tests for those defects and preserved risk/end-window behavior. Strict YAML, isolated profile, live hold, signal rules and risk limits unchanged.

Potential merge conflicts: app removal vs wtrader replacement; completely different README, Docker, requirements and test_risk; existing dirty/untracked wtrader changes. Selective edits only, staged in a temporary copy and verified against original hashes before installation.

## Implemented and verified

Transferred develop's 345-symbol exclusion set to `wtrader/markets.py`, retaining its exact values and explicit provenance. It is enforced at deterministic risk, direct executor BUY guard, and preflight. None of the eight evaluation markets is excluded. The list is an unverified conservative restriction, never a positive permission list. The exact end-window boundary scenario is adapted from develop tests; feature ownership/recovery tests remain.

Added `wtrader/account.py` to share sanitized read-only account snapshots between the CLI probe and the existing asynchronous account worker. It exposes both fee rates, selected/default scope, visible account status, separate optional credit outcomes, unknown wallet/isolation state and original timestamps after balance failures. It never selects an account or changes exchange state. Added aggregate credit reads, bounded account-list pagination and strict finite balance/fee validation. Preflight now rejects missing market fees, unknown account activation and absent/unrecognized wallet state.

Changed files in this review: `.env.example` (comments only), `README.md`, `docs/WALLEX_INTEGRATION.md`, this report, `wtrader/account.py` (new), `wtrader/markets.py` (new), `wtrader/wallex.py`, `wtrader/engine.py`, `wtrader/cli.py`, `wtrader/risk.py`, `wtrader/preflight.py`, `wtrader/dashboard.html`, `tests/test_account_review.py` (new). Existing strategy/risk YAML and `.env` contents remain unchanged. No new dependency, strategy condition or trading authorization was introduced.

### Testing

- Feature baseline: 177 passed, one dashboard socket bind denied by the sandbox; this was an environment restriction.
- Complete reviewed feature suite with socket permission: **206 passed**, including 28 new regression cases. Temporary test databases only. `pip check` passes.
- Develop: **12 passed**, 16 upstream deprecation warnings, with its own pinned dependencies in a temporary Python 3.13 environment. Python 3.14 could not build its pinned pydantic-core; the failure log is preserved. Develop was never launched against the exchange. Passing its small tests does not validate its order code.
- Real staged probe: 187 TMN spot markets; 50×50 BTC depth; closed 1m/hourly bars (latest minute publication lag was observed); BTC TMN cross-rate ratio 1.00013; two-sided WebSocket; balance, maker/taker fee, open-order and history GETs pass. An initial staged fee-parser failure revealed extra response metadata and is preserved alongside the successful corrected probe.
- Isolated real-data Shadow exercise: separate temporary SQLite/state, eight-market WebSocket, real account reads, zero tick/API failures, 1M allocation available, 5M paper funding, real trading false. No trades forced. BTC RANGE blocked the market filter; ADA/TRX latest signal bars were stale. Browser JSON/DOM/screenshot verifies simulated/experimental labels, 5M real balance, actual .250%/.300% fees, unknown credit/isolation and disabled real submission. Restart/recovery exercise preserves the same ledger.

### Authenticated findings and limits

`WALLEX_API_KEY` is present in local `.env` (mode 0600); no key or account identity is printed, stored in the report, or sent to a nonofficial origin. `WALLEX_SUB_ACCOUNT_CLIENT_ID` is absent. Main balance and detailed balance both report 5M available / zero locked. The latter succeeds with and without the returned account header. The amount equality does not prove wallet identity or isolation. All eight markets report .0025 maker / .0030 taker coefficients. Order history first page is empty; open orders are empty. No order POST/DELETE is used.

One active nonsuspended `prop` account is discoverable. `/v1/prop/user/packages/overview` and `/v1/prop/user/packages/active` return **401 with and without its header**. Therefore credit principal, active package, daily financing costs, liquidation thresholds, wallet linkage, key/account ownership and hackathon origin of funds cannot be independently established. This is partial endpoint access, not a failed balance authentication. No transfer, wallet connection or package activation was attempted.

The current VPN route is utun4; TCP succeeds but verified TLS times out. en0 currently has 172.20.10.7 and completes verified TLS 1.3 and HTTP. Interface bindings must still be checked after network changes.

Risk limits remain 5M configured reference, 1M authorized allocation, 200k/order, two concurrent positions and 30k/day plus existing drawdown/kill/idempotency/reconciliation. Actual exchange locked balance is distinct from paper-order reservations and persisted bot committed capital. No exchange-level 1M isolation has been demonstrated. Fee denomination on actual fills, partial-fill/cancel/timeout semantics and protective live execution remain untested by design. Shadow retains .0035/side conservative fees; display of .0030 taker does not silently reprice experiments.

The prior quantitative audit remains applicable: all corrected proxy test results are negative; 14-day actual Wallex data is insufficient to validate a positive edge. This review changes no signal or sizing parameters and makes no profitability claim.

### Readiness

| Area | Verdict |
|---|---|
| Public REST/WebSocket | Verified on current en0 path; ongoing data gaps/staleness still handled explicitly |
| Authenticated read-only | Balances, fees, orders and account list verified; credit endpoints denied |
| Shadow evaluation | Operational with simulated ledger, independent worker reads, tested recovery and visible unknowns |
| Strategy | Experimental; no credible validated positive edge |
| Live | Disabled and not ready; account scope/isolation, package costs, execution semantics and outage protection remain blockers |

No commit, push, blind merge, destructive checkout or database/evidence deletion occurred. The primary checkout stayed on `feat/wallex-agentic-trader`; develop is a separate detached worktree. Remote develop freshness was verified through HTTPS after the SSH fetch failed; the remote and inspected commit match.

Runtime details and final preservation/test evidence are recorded under `evidence/develop_review_20261023/`. The full secret-containing backup remains outside the repository in `/private/tmp/wallex-develop-review-20261023/complete-working-tree.tar.gz`, inside a mode-0700 directory with a mode-0600 archive.

## Final installed runtime and blockers

- Branch `feat/wallex-agentic-trader`, HEAD `ac3edeb`, original dirty/untracked work retained plus four new review files. No commit or push. All original files still exist; no unexpected nonruntime changes. `.env` and all three YAML configuration hashes match the original snapshot. Original database row counts are preserved or increased. Review delta: `evidence/develop_review_20261023/review-changes.patch`; final inventory and preservation check are beside it.
- Evaluation engine **97850**, dashboard **97851**, [dashboard](http://127.0.0.1:8791/), `config/shadow_eval.yaml`, `state/evaluation/shadow.sqlite`. Graceful reload from 87249/84959 was necessary to load account reads and the new HTML; its ledger and old logs/manifests were retained. At reload the database had no orders or positions. The current source manifest matches installed code.
- Strict engine **68923** and dashboard **69323** on [port 8787](http://127.0.0.1:8787/) remain untouched and run their earlier loaded code. The working-tree improvements do not update those running Python objects. The current reviewed implementation was validated in the isolated evaluation engine.
- Twelve samples over approximately one minute: real trading false, paper venue, healthy WebSocket, zero tick/API failures; protection cycle maximum **3.139 ms**. This is a short observation, not an outage guarantee. Deterministic stall tests separately verify that candle/account/credit waits cannot block protection.
- Current evaluation equity/available allocation **1,000,000 TMN**, committed/reserved **0**, no orders or positions. Actual reported exchange balance **5,000,000 free / 0 locked**, displayed separately. Fees remain .0035/side in simulation; .0025 maker/.0030 taker are the independently read exchange rates. No completed trades means win rate/profit factor are unavailable.
- Latest decision: HOLD / NO_SETUP; BTC is RANGE and the BTC filter blocks entries, including TRX despite its local TREND_UP. No signal threshold was relaxed. Earlier stale ADA/TRX bars were explicitly reported.
- Installed read-only preflight: **25 checks**, expected FAIL on missing explicit sub-account ID and empty confirmed market allowlist. WARN on current SOL/XRP book conditions and allocation isolation. No arming or live command was run. Credit package/overview remain 401 in the independent scope probes.

Remaining external blockers: establish the API key's account/competition ownership and explicit scope; obtain package permissions or organizer evidence for funding, financing cost, liquidation and wallet linkage; confirm account market permissions; verify any exchange-level allocation isolation. Live additionally requires independently verified order/fill/fee/cancel/idempotency behavior and protection during outages. Native stops are unused and fresh prices/exchange reachability cannot be guaranteed during an outage. No credible positive strategy edge is demonstrated.
