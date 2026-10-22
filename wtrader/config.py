"""Configuration: one YAML file (versioned, hashed) plus secrets from the environment.

Secrets never live in YAML and never enter the config hash or logs.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import yaml

D = Decimal


@dataclass
class CompetitionCfg:
    trade_start: str | None = None          # ISO-8601 with offset, organizer-confirmed
    trade_end: str | None = None
    initial_capital_tmn: str = "5000000"
    halt_entries_before_end_minutes: int = 180
    begin_liquidation_before_end_minutes: int = 60

    def start_dt(self) -> datetime | None:
        return datetime.fromisoformat(self.trade_start) if self.trade_start else None

    def end_dt(self) -> datetime | None:
        return datetime.fromisoformat(self.trade_end) if self.trade_end else None


@dataclass
class MarketCfg:
    candidate_symbols: list[str] = field(default_factory=lambda: ["BTCTMN", "ETHTMN", "SOLTMN", "XRPTMN", "DOGETMN"])
    account_allowlist: list[str] = field(default_factory=list)   # organizer-confirmed; empty blocks live
    max_spread_bps: str = "40"
    max_book_age_seconds: float = 8
    max_slippage_bps: str = "25"          # depth-walk VWAP beyond best price at proposed size
    max_depth_fraction: str = "0.25"      # order <= this fraction of visible depth within the slippage band
    max_market_data_age_seconds: float = 120   # no successful market-data read for this long -> entries halted
    max_account_data_age_seconds: float = 60   # no successful balance read for this long -> entries halted
    depth_source: str = "websocket"       # websocket (Centrifugo orderbook channels, REST fallback) | rest
    signal_seconds: int = 900
    context_seconds: int = 3600
    warmup_bars: int = 120


@dataclass
class StrategyCfg:
    enabled_families: list[str] = field(default_factory=lambda: ["TREND_PULLBACK", "CONFIRMED_BREAKOUT"])
    atr_period: int = 14
    atr_stop_multiplier: str = "1.5"
    target_r_multiple: str = "2.0"
    stop_distance_min_fraction: str = "0.004"
    stop_distance_max_fraction: str = "0.03"
    max_holding_bars: int = 16
    candidate_ttl_seconds: int = 90
    max_candidates: int = 3
    pullback_rsi_min: str = "45"
    pullback_rsi_max: str = "68"
    breakout_volume_ratio: str = "1.5"
    min_volume_ratio: str = "0.8"
    trend_target_r: str = "8"
    trend_max_holding_bars: int = 72
    trail_atr_multiplier: str = "3.0"
    # short-duration families (SCALP_MOMENTUM, SCALP_REVERSION): fixed-fraction stop/target, minutes-long holds
    scalp_stop_fraction: str = "0.007"
    scalp_target_fraction: str = "0.012"
    scalp_max_holding_bars: int = 6           # x signal_seconds (6 x 5m = 30 min)
    scalp_breakout_lookback: int = 12
    scalp_volume_ratio: str = "1.5"
    scalp_rsi_max: str = "78"
    scalp_reversion_rsi: str = "30"
    scalp_min_atr_fraction: str = "0.0025"    # signal-bar ATR / price: can price plausibly reach the target in time?
    market_filter_symbol: str | None = "BTCTMN"
    edge_artifact: str | None = "reports/edge_artifact.json"
    edge_gate: str = "strict"   # strict: families without positive held-out edge cannot trade


@dataclass
class RiskCfg:
    risk_per_trade_fraction: str = "0.005"
    max_symbol_notional_fraction: str = "0.30"
    max_total_exposure_fraction: str = "0.60"
    max_aggregate_stop_risk_fraction: str = "0.012"
    max_positions: int = 2
    daily_loss_fraction: str = "0.03"
    max_drawdown_fraction: str = "0.04"
    min_order_tmn: str = "100000"
    cooldown_bars_after_stop: int = 4
    # Strict capital allocation, enforced in the risk gate AND again in the executor before every BUY.
    # Equity, sizing and breakers are computed on the bot's own allocation, never on the whole account.
    total_credit_tmn: str = "5000000"         # informational: what the account holds; never traded beyond the allocation
    authorized_capital_tmn: str = "1000000"   # realized losses shrink it; profits (realized or not) never grow it
    max_order_tmn: str = "200000"             # per BUY order, fee included
    daily_loss_limit_tmn: str = "30000"       # bot P&L (realized + unrealized) vs Tehran day start
    min_net_target_bps: str = "30"            # skip entries whose target, after fees/spread/slippage, nets less than this
    expected_exit_slippage_bps: str = "5"
    halt_on_foreign_orders: bool = True       # exchange open orders the bot did not create halt entries (never canceled)


@dataclass
class ExecutionCfg:
    entry_cancel_after_seconds: int = 20
    exit_poll_seconds: float = 5
    exit_limit_slippage_bps: str = "30"      # marketable-limit band for exits
    liquidation_slippage_steps_bps: list[str] = field(default_factory=lambda: ["30", "60", "120", "250"])
    live_rollout_approved: bool = False  # audit hold: real execution is not validated
    request_timeout_seconds: float = 6
    connect_timeout_seconds: float = 4       # TCP+TLS; a stalled handshake fails fast instead of eating the read budget
    read_attempts: int = 3
    circuit_failures: int = 3                # consecutive transport failures that open the Wallex circuit
    circuit_cooldown_seconds: float = 30     # while open, reads fail immediately (no 3x retry per call)
    unknown_reconcile_seconds: int = 120     # after this, UNKNOWN -> QUARANTINED
    taker_fee_fraction: str = "0.0035"       # conservative default; replaced by /v1/account/fee when available
    fee_currency_policy: str = "buy_base_sell_quote"
    exchange_reconcile_every_ticks: int = 6  # GET openOrders cadence (foreign/unknown order detection)
    kill_switch_path: str = "state/KILL"
    live_arm_path: str = "state/live_arm.json"
    preflight_path: str = "state/live_preflight.json"


@dataclass
class AiCfg:
    mode: str = "ai"                  # ai | baseline
    provider: str = "anthropic"       # legacy default; .env.example selects boxapi
    model: str = "claude-opus-5-5"
    base_url: str = "https://ai.boxapi.ir/ai/v1"
    effort: str = "low"
    prompt_version: str = "selector_v3"
    deadline_seconds: float = 25
    max_calls_per_day: int = 160
    outage_action: str = "HOLD"       # legacy BASELINE is accepted but failures always HOLD
    max_attempts: int = 2             # SDK retries disabled; bounded application retry budget
    max_output_tokens: int = 1200
    input_usd_per_million: str | None = None
    output_usd_per_million: str | None = None
    cached_input_usd_per_million: str | None = None
    feedback_trades: int = 10


@dataclass
class Settings:
    mode: str = "shadow"              # backtest | shadow | live
    run_id: str | None = None
    db_path: str = "state/trader.sqlite"
    live_db_path: str = "state/live.sqlite"   # live never shares records with shadow/paper runs
    evidence_dir: str = "evidence"
    status_path: str = "state/status.json"
    monitor_port: int = 8787
    experimental_shadow: bool = False
    competition: CompetitionCfg = field(default_factory=CompetitionCfg)
    market: MarketCfg = field(default_factory=MarketCfg)
    strategy: StrategyCfg = field(default_factory=StrategyCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    execution: ExecutionCfg = field(default_factory=ExecutionCfg)
    ai: AiCfg = field(default_factory=AiCfg)
    config_path: str | None = None

    def to_public_dict(self) -> dict:
        d = asdict(self)
        d.pop("config_path", None)
        return d

    def config_hash(self) -> str:
        blob = json.dumps(self.to_public_dict(), sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()


def _merge(dc, values: dict):
    for k, v in (values or {}).items():
        if not hasattr(dc, k):
            raise ValueError(f"unknown config key {type(dc).__name__}.{k}")
        cur = getattr(dc, k)
        if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
            _merge(cur, v)
        else:
            setattr(dc, k, str(v) if isinstance(cur, str) and isinstance(v, (int, float)) else v)
    return dc


def load_settings(path: str | None = None) -> Settings:
    s = Settings()
    if path:
        _merge(s, yaml.safe_load(Path(path).read_text()) or {})
        s.config_path = path
    if os.environ.get("TRADING_MODE"):
        s.mode = os.environ["TRADING_MODE"]
    provider = os.environ.get('LLM_PROVIDER')
    if provider:
        provider = provider.strip().lower()
        if provider != s.ai.provider:
            s.ai.model = 'gpt-5.4-mini' if provider == 'boxapi' else 'claude-opus-5-5'
        s.ai.provider = provider
    if os.environ.get('BOXAPI_BASE_URL'):
        s.ai.base_url = os.environ['BOXAPI_BASE_URL'].rstrip('/')
    for env, attr in (('LLM_INPUT_USD_PER_MILLION', 'input_usd_per_million'),
                      ('LLM_OUTPUT_USD_PER_MILLION', 'output_usd_per_million'),
                      ('LLM_CACHED_INPUT_USD_PER_MILLION', 'cached_input_usd_per_million')):
        if os.environ.get(env):
            setattr(s.ai, attr, os.environ[env])
    if os.environ.get("LLM_MODEL_ID"):
        s.ai.model = os.environ["LLM_MODEL_ID"]
    if os.environ.get("AI_MODE"):
        s.ai.mode = os.environ["AI_MODE"]
    if s.ai.provider == 'baseline':
        s.ai.mode = 'baseline'
    allow = os.environ.get("WALLEX_ALLOWED_MARKETS")
    if allow:
        s.market.account_allowlist = [x.strip().upper() for x in allow.split(",") if x.strip()]
    for env, attr in (("TRADE_START", "trade_start"), ("TRADE_END", "trade_end")):
        if os.environ.get(env):
            setattr(s.competition, attr, os.environ[env])
    validate_settings(s)
    return s


def validate_settings(s: Settings):
    from urllib.parse import urlsplit
    ai = s.ai
    if ai.mode not in ('ai', 'baseline') or ai.provider not in ('boxapi', 'anthropic', 'baseline'):
        raise ValueError('invalid AI_MODE or LLM_PROVIDER')
    if (not 0 < ai.deadline_seconds <= 120 or type(ai.max_attempts) is not int or not 1 <= ai.max_attempts <= 3
            or type(ai.max_calls_per_day) is not int or ai.max_calls_per_day < 0
            or type(ai.max_output_tokens) is not int or not 128 <= ai.max_output_tokens <= 4000):
        raise ValueError('LLM timeout, attempts, token cap and call budget must be bounded')
    if not isinstance(ai.model, str) or not ai.model.strip() or len(ai.model) > 200:
        raise ValueError('invalid LLM model')
    if ai.provider == 'boxapi':
        u = urlsplit(ai.base_url)
        if (u.scheme != 'https' or u.hostname != 'ai.boxapi.ir' or u.port not in (None, 443)
                or u.path.rstrip('/') != '/ai/v1' or u.username or u.password or u.query or u.fragment):
            raise ValueError('BOXAPI_BASE_URL must use the official HTTPS BoxAPI origin and /ai/v1 path')
    for value in (ai.input_usd_per_million, ai.output_usd_per_million, ai.cached_input_usd_per_million):
        if value is not None and (not D(value).is_finite() or D(value) < 0):
            raise ValueError('LLM token prices must be finite nonnegative USD rates')
    if s.strategy.edge_gate not in ("strict", "advisory"):
        raise ValueError("edge_gate must be strict or advisory")
    if s.mode == "live" and (s.experimental_shadow or s.strategy.edge_gate != "strict"):
        raise ValueError("experimental/advisory profiles are restricted to Shadow evaluation")
    if type(s.execution.live_rollout_approved) is not bool or type(s.experimental_shadow) is not bool:
        raise ValueError("live_rollout_approved and experimental_shadow must be booleans")
    r = s.risk
    if not (0 < D(r.authorized_capital_tmn) <= D(r.total_credit_tmn) <= D("5000000")
            and 0 < D(r.authorized_capital_tmn) <= D("1000000")
            and 0 < D(r.max_order_tmn) <= D("200000")
            and 0 < r.max_positions <= 2
            and 0 < D(r.daily_loss_limit_tmn) <= D("30000")):
        raise ValueError("configuration exceeds authorized capital/risk limits")
    if not (0 < s.execution.read_attempts <= 3 and 0 < s.execution.request_timeout_seconds <= 10
            and 0 < s.execution.connect_timeout_seconds <= 10 and s.execution.exit_poll_seconds > 0):
        raise ValueError("network retries/timeouts and protection interval must be bounded")
    if not 0 <= D(s.execution.taker_fee_fraction) <= D(".01"):
        raise ValueError("invalid fee coefficient")
    if s.market.signal_seconds <= 0 or s.market.context_seconds < s.market.signal_seconds:
        raise ValueError("invalid candle timeframes")


def load_dotenv(path: str | Path = ".env") -> list[str]:
    """Load KEY=VALUE lines from a local .env file. Variables already in the environment win.

    Returns the names loaded (never the values). Supports comments, `export KEY=...` and quoted values.
    """
    p = Path(path)
    if not p.is_file():
        return []
    loaded = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.removeprefix("export ").partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = val
            loaded.append(key)
    return loaded


SECRET_ENV = ("WALLEX_API_KEY", "WALLEX_SUB_ACCOUNT_CLIENT_ID", "BOXAPI_API_KEY", "LLM_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
              "ALERT_WEBHOOK_URL", "WALLEX_HTTP_PROXY")


def secret_values() -> list[str]:
    return [v for k in SECRET_ENV if len(v := os.environ.get(k) or "") >= 6]


def redact(text) -> str:
    """Replace any configured secret value in `text` (defense in depth for errors, logs and status files)."""
    out = str(text)
    for v in secret_values():
        out = out.replace(v, "***")
    return out


class RedactingFilter:
    """logging filter: secrets never reach a log line, even if an exception message echoes one."""

    def filter(self, record) -> bool:
        msg = record.getMessage()
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        return True


@dataclass(frozen=True)
class Secrets:
    wallex_api_key: str | None
    wallex_sub_account_id: str | None
    wallex_base_url: str
    alert_webhook: str | None
    wallex_proxy: str | None = None          # optional egress proxy for Wallex only (e.g. an Iranian-IP host)
    wallex_bind_interface: str | None = None # optional: send Wallex traffic out of this interface (e.g. en0), not the VPN

    @staticmethod
    def from_env() -> "Secrets":
        return Secrets(
            wallex_api_key=os.environ.get("WALLEX_API_KEY") or None,
            wallex_sub_account_id=os.environ.get("WALLEX_SUB_ACCOUNT_CLIENT_ID") or None,
            wallex_base_url=os.environ.get("WALLEX_API_BASE_URL") or "https://api.wallex.ir",
            alert_webhook=os.environ.get("ALERT_WEBHOOK_URL") or None,
            wallex_proxy=os.environ.get("WALLEX_HTTP_PROXY") or None,
            wallex_bind_interface=os.environ.get("WALLEX_BIND_INTERFACE") or None,
        )

    def __repr__(self) -> str:  # never print secrets
        return (f"Secrets(key={'set' if self.wallex_api_key else 'unset'}, "
                f"sub_account={'set' if self.wallex_sub_account_id else 'unset'}, "
                f"proxy={'set' if self.wallex_proxy else 'unset'}, "
                f"bind_interface={self.wallex_bind_interface or 'unset'})")

    __str__ = __repr__
