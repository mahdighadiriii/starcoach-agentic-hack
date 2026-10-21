"""LangGraph trading agent.

observe -> analyze (LLM) -> risk_gate (code) -> execute -> journal
"""
import json
import time
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from app.config import Settings, get_settings
from app.indicators import summarize
from app.markets import DISABLED_MARKETS
from app.risk import Decision, RiskState, check, in_flatten_window
from app.wallex import WallexClient

SYSTEM_PROMPT = """You are a cautious crypto trading analyst for a hackathon agent on the Wallex spot exchange.
All markets are quoted in Iranian toman (TMN); profit only counts once it is back in TMN.
Given market data and indicators for one symbol, decide BUY, SELL or HOLD.
Prefer HOLD when signals conflict. Never chase pumps. Explain briefly.
size_quote is the order value in TMN, between min_notional and max_order_quote.
Reply with ONLY JSON:
{"action": "BUY|SELL|HOLD", "confidence": 0.0-1.0, "size_quote": <number>, "reasoning": "<1-3 sentences>"}"""


class AgentState(TypedDict, total=False):
    symbol: str
    market: dict[str, Any]
    balances: dict[str, float]
    equity: float
    decision: Decision
    risk_notes: list[str]
    approved: bool
    execution: dict[str, Any]
    error: str


JOURNAL: list[dict] = []
RISK_STATE = RiskState()


def record(entry: dict, s: Settings) -> None:
    """Append a decision to the in-memory journal and to the JSONL file on disk."""
    JOURNAL.append(entry)
    path = Path(s.journal_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def load_journal(s: Settings) -> int:
    """Reload past decisions after a restart, and keep the kill switch's starting equity."""
    path = Path(s.journal_path)
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]
    JOURNAL[:] = entries
    first = next((e["equity"] for e in entries if e.get("equity")), None)
    if first and RISK_STATE.start_equity is None:
        RISK_STATE.start_equity = first
    return len(entries)


def _llm(s: Settings):
    if not s.llm_api_key:
        return None
    from langchain_openai import ChatOpenAI
    # gpt-5 family on BoxAPI rejects any temperature other than the default (1)
    return ChatOpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key, model=s.llm_model, temperature=1)


def _rule_based(symbol: str, ind: dict, s: Settings) -> Decision:
    """Fallback when no LLM key is set, so the pipeline still runs end-to-end."""
    fast, slow, r = ind.get("sma_12"), ind.get("sma_26"), ind.get("rsi_14")
    if fast and slow and r is not None:
        if fast > slow and r < 65:
            return Decision(symbol, "BUY", 0.65, s.max_order_quote, f"SMA12>SMA26 uptrend, RSI {r:.0f} not overbought")
        if fast < slow and r > 35:
            return Decision(symbol, "SELL", 0.65, s.max_order_quote, f"SMA12<SMA26 downtrend, RSI {r:.0f} not oversold")
    return Decision(symbol, "HOLD", 0.5, 0, "no clear signal")


def build_graph(s: Settings | None = None, client: WallexClient | None = None):
    s = s or get_settings()
    wx = client or WallexClient(s)
    llm = _llm(s)

    def observe(state: AgentState) -> AgentState:
        sym = state["symbol"]
        ticker = wx.ticker(sym)
        ind = summarize(wx.candles(sym))
        balances = wx.balances()
        price = ticker["last"] or ind.get("last_close", 0)
        prices = wx.prices(s.quote_asset) if balances else {}
        # whole portfolio valued in TMN, so the kill switch and position limits see everything
        equity = sum(qty * (1.0 if asset == s.quote_asset else prices.get(asset, 0.0))
                     for asset, qty in balances.items())
        market = {**ticker, "last": price, "indicators": ind,
                  "min_notional": float(wx.market_info(sym).get("minNotional") or 0)}
        return {"market": market, "balances": balances, "equity": equity}

    def analyze(state: AgentState) -> AgentState:
        sym, market = state["symbol"], state["market"]
        if llm is None:
            return {"decision": _rule_based(sym, market["indicators"], s)}
        user = json.dumps({"symbol": sym, "market": market, "max_order_quote": s.max_order_quote,
                           "quote_asset": s.quote_asset})
        try:
            raw = llm.invoke([("system", SYSTEM_PROMPT), ("user", user)]).content
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            d = Decision(sym, str(data["action"]).upper(), float(data["confidence"]),
                         float(data.get("size_quote", 0)), str(data.get("reasoning", "")))
        except Exception as e:  # bad JSON / API error -> safe default
            d = Decision(sym, "HOLD", 0.0, 0, f"LLM error, holding: {e}")
        return {"decision": d}

    def risk_gate(state: AgentState) -> AgentState:
        res = check(state["decision"], price=state["market"]["last"], balances=state["balances"],
                    equity=state["equity"], state=RISK_STATE, s=s,
                    min_notional=state["market"]["min_notional"])
        return {"approved": res.approved, "decision": res.decision, "risk_notes": res.notes}

    def execute(state: AgentState) -> AgentState:
        d, price = state["decision"], state["market"]["last"]
        try:
            out = wx.place_order(d.symbol, d.action, d.size_quote / price, price)
        except Exception as e:
            out = {"error": str(e)}
        return {"execution": out}

    def journal(state: AgentState) -> AgentState:
        d = state["decision"]
        record({
            "ts": int(time.time()),
            "symbol": d.symbol,
            "price": state["market"]["last"],
            "equity": state["equity"],
            "indicators": state["market"]["indicators"],
            "action": d.action,
            "confidence": d.confidence,
            "size_quote": d.size_quote,
            "reasoning": d.reasoning,
            "approved": state.get("approved", False),
            "risk_notes": state.get("risk_notes", []),
            "execution": state.get("execution"),
            "dry_run": s.dry_run,
        }, s)
        return {}

    g = StateGraph(AgentState)
    for name, fn in [("observe", observe), ("analyze", analyze), ("risk_gate", risk_gate),
                     ("execute", execute), ("journal", journal)]:
        g.add_node(name, fn)
    g.set_entry_point("observe")
    g.add_edge("observe", "analyze")
    g.add_edge("analyze", "risk_gate")
    g.add_conditional_edges("risk_gate", lambda st: "execute" if st["approved"] else "journal")
    g.add_edge("execute", "journal")
    g.add_edge("journal", END)
    return g.compile()


def flatten(wx: WallexClient, s: Settings) -> list[dict]:
    """End of window: cancel open orders and sell every non-TMN balance, so all profit is realized in TMN."""
    start = len(JOURNAL)
    for o in wx.open_orders():
        cid = o.get("clientOrderId") or o.get("client_id")
        try:
            out = wx.cancel_order(cid)
        except Exception as e:
            out = {"error": str(e)}
        record({"ts": int(time.time()), "kind": "flatten", "symbol": o.get("symbol"), "action": "CANCEL",
                "reasoning": "end of trading window: free locked funds before selling to TMN",
                "approved": True, "risk_notes": [], "execution": out, "dry_run": s.dry_run}, s)

    markets = wx.markets()
    for asset, qty in wx.balances().items():
        if asset == s.quote_asset or qty <= 0:
            continue
        sym = f"{asset}{s.quote_asset}"
        m = markets.get(sym)
        entry = {"ts": int(time.time()), "kind": "flatten", "symbol": sym, "action": "SELL", "confidence": 1.0,
                 "reasoning": "end of trading window: convert to TMN so profit is realized",
                 "approved": False, "risk_notes": [], "execution": None, "dry_run": s.dry_run}
        try:
            bid = float((m or {}).get("stats", {}).get("bidPrice") or 0)
        except ValueError:
            bid = 0.0
        value = qty * bid
        entry.update(price=bid, size_quote=value)
        if sym in DISABLED_MARKETS:
            entry["risk_notes"] = [f"{sym} is disabled in the credit account, cannot convert {qty} {asset}"]
        elif not m or bid <= 0:
            entry["risk_notes"] = [f"no live {sym} market, cannot convert {qty} {asset}"]
        elif value < float(m.get("minNotional") or 0):
            entry["risk_notes"] = [f"dust: {value:,.0f} TMN below min notional, left as is"]
        else:
            price = bid * (1 - s.flatten_slippage_pct)
            try:
                entry["execution"] = wx.place_order(sym, "SELL", qty, price)
                entry["approved"] = True
                entry["risk_notes"] = [f"limit sell at bid -{s.flatten_slippage_pct:.1%} to fill immediately"]
            except Exception as e:
                entry["execution"] = {"error": str(e)}
        record(entry, s)
    return JOURNAL[start:]


def run_cycle(graph=None, client: WallexClient | None = None) -> list[dict]:
    s = get_settings()
    if in_flatten_window(s):
        return flatten(client or WallexClient(s), s)
    graph = graph or build_graph(s, client)
    start = len(JOURNAL)
    for sym in s.symbol_list:
        if sym in DISABLED_MARKETS:
            record({"ts": int(time.time()), "symbol": sym, "action": "SKIP", "approved": False,
                    "reasoning": "market is disabled in the hackathon credit account", "risk_notes": [],
                    "dry_run": s.dry_run}, s)
            continue
        try:
            graph.invoke({"symbol": sym})
        except Exception as e:
            record({"ts": int(time.time()), "symbol": sym, "action": "ERROR", "reasoning": str(e),
                    "approved": False, "risk_notes": [], "dry_run": s.dry_run}, s)
    return JOURNAL[start:]
