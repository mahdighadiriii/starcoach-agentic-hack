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
    market_filter_symbol: str | None = "BTCTMN"
    edge_artifact: str | None = "reports/edge_artifact.json"
    edge_gate: str = "advisory"   # strict: families without positive held-out edge cannot trade


@dataclass
class RiskCfg:
    risk_per_trade_fraction: str = "0.005"
    max_symbol_notional_fraction: str = "0.30"
    max_total_exposure_fraction: str = "0.60"
    max_aggregate_stop_risk_fraction: str = "0.012"
    max_positions: int = 2
    daily_loss_fraction: str = "0.02"
    max_drawdown_fraction: str = "0.04"
    min_order_tmn: str = "100000"
    cooldown_bars_after_stop: int = 4


@dataclass
class ExecutionCfg:
    entry_cancel_after_seconds: int = 20
    exit_poll_seconds: float = 5
    exit_limit_slippage_bps: str = "30"      # marketable-limit band for exits
    liquidation_slippage_steps_bps: list[str] = field(default_factory=lambda: ["30", "60", "120", "250"])
    request_timeout_seconds: float = 6
    read_attempts: int = 3
    unknown_reconcile_seconds: int = 120     # after this, UNKNOWN -> QUARANTINED
    taker_fee_fraction: str = "0.0035"       # conservative default; replaced by /v1/account/fee when available
    fee_currency_policy: str = "buy_base_sell_quote"


@dataclass
class AiCfg:
    mode: str = "ai"                  # ai | baseline
    model: str = "claude-opus-5-5"
    effort: str = "low"
    prompt_version: str = "selector_v3"
    deadline_seconds: float = 25
    max_calls_per_day: int = 160
    outage_action: str = "HOLD"       # HOLD (block entries) | BASELINE (fall back to deterministic choice)
    feedback_trades: int = 10


@dataclass
class Settings:
    mode: str = "shadow"              # backtest | shadow | live
    run_id: str | None = None
    db_path: str = "state/trader.sqlite"
    evidence_dir: str = "evidence"
    status_path: str = "state/status.json"
    monitor_port: int = 8787
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
    if os.environ.get("LLM_MODEL_ID"):
        s.ai.model = os.environ["LLM_MODEL_ID"]
    if os.environ.get("AI_MODE"):
        s.ai.mode = os.environ["AI_MODE"]
    allow = os.environ.get("WALLEX_ALLOWED_MARKETS")
    if allow:
        s.market.account_allowlist = [x.strip().upper() for x in allow.split(",") if x.strip()]
    for env, attr in (("TRADE_START", "trade_start"), ("TRADE_END", "trade_end")):
        if os.environ.get(env):
            setattr(s.competition, attr, os.environ[env])
    return s


@dataclass(frozen=True)
class Secrets:
    wallex_api_key: str | None
    wallex_sub_account_id: str | None
    wallex_base_url: str
    alert_webhook: str | None

    @staticmethod
    def from_env() -> "Secrets":
        return Secrets(
            wallex_api_key=os.environ.get("WALLEX_API_KEY") or None,
            wallex_sub_account_id=os.environ.get("WALLEX_SUB_ACCOUNT_CLIENT_ID") or None,
            wallex_base_url=os.environ.get("WALLEX_API_BASE_URL") or "https://api.wallex.ir",
            alert_webhook=os.environ.get("ALERT_WEBHOOK_URL") or None,
        )

    def __repr__(self) -> str:  # never print secrets
        return f"Secrets(key={'set' if self.wallex_api_key else 'unset'}, sub_account={'set' if self.wallex_sub_account_id else 'unset'})"
