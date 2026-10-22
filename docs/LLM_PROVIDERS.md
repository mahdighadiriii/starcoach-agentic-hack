# LLM providers and BoxAPI migration — 2026-10-23

BoxAPI is supported through the official OpenAI Python SDK, with optional native Anthropic compatibility and an explicit deterministic baseline. The existing `Decision` contract remains HOLD or one frozen candidate at multiplier 0.5/1.0. Models have no exchange clients, database connections, tools or order access. Risk limits and all strategy YAML files are unchanged. Live remains disabled.

## Configuration

Edit the existing `.env` privately; preserve existing exchange credentials:

```dotenv
LLM_PROVIDER=boxapi
BOXAPI_API_KEY=
BOXAPI_BASE_URL=https://ai.boxapi.ir/ai/v1
LLM_MODEL_ID=gpt-5.4-mini
AI_MODE=ai
```

The API key stays in environment variables, outside dataclasses, public settings, hashes, manifests and cache identities. `OPENAI_API_KEY` is not used. BoxAPI URLs must use the official HTTPS origin/path, without credentials, query strings or redirects. TLS validation stays enabled and ambient HTTP proxy settings are not inherited.

`AI_MODE=baseline` or `LLM_PROVIDER=baseline` selects the baseline without importing or calling an LLM SDK. A baseline SELECT is an explicit deterministic-mode decision. An LLM failure always HOLDs, even if an older YAML file says `outage_action: BASELINE`; failures cannot trigger baseline trading.

For backwards compatibility, configurations without `LLM_PROVIDER` retain the Anthropic default and their existing model. Explicitly selecting a different provider supplies that provider's default model; `LLM_MODEL_ID` overrides it. BoxAPI's default is `gpt-5.4-mini`. For native Anthropic, set `LLM_PROVIDER=anthropic`, an explicit supported `LLM_MODEL_ID`, and `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`.

```sh
.venv/bin/pip install -r requirements.txt
# Optional native Anthropic provider:
.venv/bin/pip install -r requirements-anthropic.txt
# Optional Anthropic container dependency:
docker build --build-arg WITH_ANTHROPIC=true -t wallex-trader .
```

Core dependencies use tested `openai==1.109.1` and `httpx==0.28.1`. Optional `anthropic==2.1.0` uses its own `httpx2` transport; it is not supplied a Wallex/OpenAI httpx client. Native SDK authentication is delegated to that SDK. Anthropic was verified with a mocked SDK transport, without real credentials.

## Provider boundary and safety

`wtrader/llm.py` contains a small provider protocol, `BoxAPIProvider`, `AnthropicProvider`, normalized replies and sanitized error categories. `wtrader/ai_agent.py` retains the selector, snapshot grounding, validation, cache, retry budget and usage accounting. `make_selector` picks the explicit baseline or LLM selector. The application does not expose model-generated order parameters.

BoxAPI sends `chat.completions.create` with system/user messages, a strict named JSON schema, the exact configured model and `max_completion_tokens`. It omits temperature, tools, Anthropic beta flags, server-side fallback and Anthropic `output_config`. Reasoning effort is sent only when the catalog advertises support. The authenticated model catalog is checked once per selector process on its worker; the documented short `gpt-5.4-mini` alias is matched to `openai/gpt-5.4-mini` without changing the request name. Unsupported models or unexpected response model substitutions fail closed.

Both providers return to the same local validator. It rejects extra/missing fields, duplicate JSON keys, stale/mismatched snapshot IDs, unknown candidates/evidence, invalid multipliers/confidence, tool calls, refusals, truncation, malformed JSON and invalid response envelopes. No heuristic JSON extraction or unconstrained fallback request is used.

All provider calls, catalog reads and retry delays run on the existing selector worker. The protection thread consumes completed futures and remains the sole SQLite/order writer. Late results remain EXPIRED and cannot trade; late usage may be attached to the expired record. The engine never waits on a running LLM future. Tests simulate a blocked BoxAPI SDK request while position protection completes a stop exit.

SDK retries are disabled. Application retries default to two attempts, configurable up to three, sharing the existing deadline and daily generation-attempt budget. HTTP 429 observes a bounded Retry-After cooldown; 5xx/connection failures may retry within budget. Authentication and request/model errors do not retry. Timed-out generation is not immediately duplicated because it may already have been processed and billed. Provider error text, bodies, URLs and headers are never copied to exceptions or evidence. Keys are scrubbed from returned text and SDK credential echoes.

Cache keys include provider, API endpoint, model, schema, prompt hash, generation parameters and the full context. Existing cache files are preserved; incompatible old keys are not reused. Replay hits are locally revalidated, make no API request and add zero incremental estimated cost. Corrupt or unwritable caches HOLD safely.

## Usage and cost

Decisions retain normalized input/output, cached-input and reasoning-token counts where reported. Reasoning output is already part of completion output and is not added twice. Refused, invalid and late responses retain available usage. Unknown usage or prices remain unknown, not zero. Dashboard metrics describe the current selector process; per-decision usage is persisted by the engine.

Optional environment variables accept USD per million tokens:

```dotenv
LLM_INPUT_USD_PER_MILLION=
LLM_OUTPUT_USD_PER_MILLION=
LLM_CACHED_INPUT_USD_PER_MILLION=
```

Populate these after verifying the provider's current rates. Estimates are not invoice amounts. Without a cache-read rate, cached input uses the full configured input price conservatively. Anthropic cache-creation costs require separate pricing and remain unknown. A request failure without usage has unknown cost. Aggregate cost remains unknown when any attempted generation is unpriced. Baseline/cache replay makes no incremental generation charge.

The rolling daily call budget and metric counters are process-local and reset on restart. Persisted decision usage remains in SQLite; provider billing records are authoritative for actual charges. A worker abandoned on shutdown may incur an unobserved charge. Network deadlines cannot guarantee cancellation of server-side work or DNS resolution; expired output is discarded regardless.

## Authenticated verification

Official references: [BoxAPI API documentation](https://boxapi.ir/docs/ai/), [model catalog and prices](https://boxapi.ir/docs/ai/models/), [OpenAI-compatible structured-output request](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

On 2026-10-23 local time, the key configured in `.env` successfully read 468 model records. The catalog listed `openai/gpt-5.4-mini`; BoxAPI's documentation explicitly uses `gpt-5.4-mini`. One generation request with that exact configured alias, strict decision schema, low reasoning effort and a 512-token cap returned a validated HOLD for an empty synthetic snapshot. No account balances, real candidate data or credentials were included in the prompt. No Wallex client was constructed.

| Result | Evidence |
|---|---|
| Model | requested and returned `gpt-5.4-mini`; catalog `openai/gpt-5.4-mini` |
| Structured response | valid HOLD, matching snapshot, null candidate, zero multiplier |
| Latency | 6,868 ms including catalog verification |
| Input/output | 967 / 182 tokens; output includes 13 reasoning tokens; zero cached input |
| Generation attempts | one; no automatic retry or second paid test |
| Estimated generation cost | $0.0017758875 using published $0.8625 input / $5.1750 output per million tokens |
| Actual charge | not established; runtime prices remain unconfigured rather than silently assumed |
| Exchange operations | zero Wallex requests; live trading disabled |

The connectivity result does not validate strategy selection quality, profitability, sustained provider availability, actual invoice charges or native Anthropic authentication. No AI strategy backtest or running engine was switched to the model as part of this verification.

To repeat a paid connectivity check deliberately:

```sh
.venv/bin/python -m wtrader --config config/shadow_eval.yaml llm-probe
```

It uses a synthetic empty context, one generation attempt and a bounded token cap, writes timestamped sanitized evidence under `evidence/evaluation/llm_probe/`, and exits nonzero on failure. Baseline mode skips the request. The dashboard command never loads API keys.

## Migration inventory and runtime preservation

Anthropic-specific dependencies originally appeared in `ai_agent.py` (SDK client, beta Messages format, fallback fields and token response), `config.py` (model defaults/redaction), `cli.py` (credential checks), `engine.py`/`dashboard.html` (credential labels), `manifest.py` (package metadata), core requirements and operational documentation. SDK-specific code is now isolated in the optional provider adapter. Existing strategy configuration values and historical audit evidence are preserved.

Implementation changes: `wtrader/llm.py` (new), `ai_agent.py`, `config.py`, `cli.py`, `engine.py`, `manifest.py`, `evaluate.py`, `dashboard.html`; `requirements.txt`, `requirements-anthropic.txt` (new), `Dockerfile`, `.env.example`; `tests/test_llm_providers.py` (new), `tests/test_ai.py`; `README.md`, `docs/ARCHITECTURE.md`, `docs/OPERATIONS.md`, `docs/STRATEGY.md`, `docs/COMPETITION_READINESS.md`, `docs/IMPLEMENTATION_STATUS.md`, and this document.

The working tree, ignored configuration/evidence/data and consistent SQLite backups were preserved in `/private/tmp/wallex-boxapi-20261023/` before implementation (reproducible virtual-environment binaries excluded from the archive). Changes were staged and tested in an independent copy. Existing Shadow engines 68923/97850 and dashboards 69323/97851 are not restarted: they retain their previously loaded code and baseline selection. Installing source changes does not activate BoxAPI inside those running processes. The new integration is available on the next deliberately started process using the configured environment. Real Wallex execution remains held disabled, and strict/advisory configuration boundaries and risk caps are unchanged.

Final tests, review patch, preservation checks and sanitized connectivity evidence are recorded in `evidence/boxapi_migration_20261023/`. No commit or push is performed.

## Test results

The complete installed suite passes **254 tests** (48 additional provider/security/scheduling cases compared with the 206-test baseline), with no failures or skips. Coverage includes provider/environment selection, both native SDK request contracts, actual BoxAPI catalog/alias formatting, JSON/grounding rejection, authentication, missing credentials/SDKs, timeouts, rate-limit retry/cooldown, attempt budgets, secret scrubbing, usage/cost/cache accounting, late-result rejection and independent stop protection. Provider SDK requests in automated tests are mocked; databases are temporary. `pip check` passes. The single real BoxAPI request above is separate from these tests. Final installed-suite and browser results are retained with the migration evidence.

Installed verification finished with **254 passed in 7.01s**, zero failures or skips, and `pip check` reporting no broken requirements. Browser rendering and dashboard API checks passed against the new source with existing runtime data through a temporary loopback server. The original four Shadow processes were preserved. Only the previously absent `openai`, `distro` and `tqdm` packages were added to the project virtual environment; no installed package was upgraded or removed. The Docker image was not built.
