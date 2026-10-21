from datetime import datetime
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    llm_base_url: str = "https://ai.boxapi.ir/ai/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-5-mini"

    wallex_base_url: str = "https://api.wallex.ir"
    wallex_api_key: str = ""

    dry_run: bool = True
    symbols: str = "BTCTMN,ETHTMN,USDTTMN"
    # Profit only counts when realized in toman, so everything is quoted in TMN
    quote_asset: str = "TMN"
    max_order_quote: float = 500_000.0
    max_position_pct: float = 0.25
    max_daily_loss_pct: float = 0.05
    min_confidence: float = 0.6
    loop_interval_sec: int = 300
    # Every decision is appended here as one JSON line (the decision log deliverable)
    journal_path: str = "data/journal.jsonl"

    # End of the trading window, with timezone, e.g. 2026-10-26T23:59:00+03:30.
    # From flatten_minutes_before until then the bot stops trading and sells everything to TMN.
    window_end: datetime | None = None
    flatten_minutes_before: int = 60
    # Flatten sells are limit orders this far below the best bid, so they fill right away
    flatten_slippage_pct: float = 0.005

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
