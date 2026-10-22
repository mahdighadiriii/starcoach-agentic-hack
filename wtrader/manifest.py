"""Run manifest: ties every decision and order to the exact code, config, prompt and model."""
from __future__ import annotations

import hashlib
import platform
import subprocess
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from .ai_agent import prompt_hash
from .config import Settings
from .strategy import strategy_version
from .llm import effective_provider

ROOT = Path(__file__).resolve().parent.parent


def _git(*args) -> str | None:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return None


def source_digest() -> str:
    """Hash of all tracked runtime sources, independent of git."""
    h = hashlib.sha256()
    for p in sorted(list((ROOT / "wtrader").glob("*.py")) + list((ROOT / "prompts").glob("*.md")) + list((ROOT / "wtrader").glob("*.html"))):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def build_manifest(s: Settings, data_source: str, venue: str) -> dict:
    status = _git("status", "--porcelain", "--untracked-files=normal")
    pkgs = {}
    for name in ("openai", "anthropic", "httpx", "PyYAML", "websocket-client"):
        try:
            pkgs[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pkgs[name] = None
    return {
        "deployed_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(status) if status is not None else None,
        "source_sha256": source_digest(),
        "config_path": s.config_path,
        "config_hash": s.config_hash(),
        "strategy_version": strategy_version(s.strategy.enabled_families, s.market.signal_seconds,
                                             s.market.context_seconds),
        "strategy_families": list(s.strategy.enabled_families),
        "prompt_version": s.ai.prompt_version,
        "prompt_sha256_16": prompt_hash(s.ai.prompt_version),
        "model": s.ai.model if s.ai.mode == "ai" else None,
        "ai_mode": s.ai.mode,
        'llm_provider': effective_provider(s.ai),
        'llm_base_url': s.ai.base_url if effective_provider(s.ai) == 'boxapi' else None,
        "mode": s.mode, "data_source": data_source, "venue": venue,
        "python": platform.python_version(), "packages": pkgs,
    }
