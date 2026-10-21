import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app import agent
from app.config import get_settings
from app.risk import in_flatten_window
from app.wallex import WallexClient

STATIC = Path(__file__).parent / "static"
_loop: dict = {"task": None}


async def _autoloop():
    s = get_settings()
    while True:
        await asyncio.to_thread(agent.run_cycle)
        await asyncio.sleep(s.loop_interval_sec)


@asynccontextmanager
async def lifespan(_: FastAPI):
    agent.load_journal(get_settings())
    yield
    if _loop["task"]:
        _loop["task"].cancel()


app = FastAPI(title="StarCoach Agentic Trader", lifespan=lifespan)


@app.get("/")
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    s = get_settings()
    return {
        "ok": True,
        "dry_run": s.dry_run,
        "symbols": s.symbol_list,
        "llm": bool(s.llm_api_key),
        "wallex_key": bool(s.wallex_api_key),
        "halted": agent.RISK_STATE.halted,
        "window_end": s.window_end,
        "flattening": in_flatten_window(s),
        "autoloop": bool(_loop["task"] and not _loop["task"].done()),
    }


@app.post("/api/run")
async def run_once():
    return await asyncio.to_thread(agent.run_cycle)


@app.get("/api/journal")
def journal(limit: int = 100):
    return agent.JOURNAL[-limit:][::-1]


@app.post("/api/autoloop/{state}")
async def autoloop(state: str):
    task = _loop["task"]
    if state == "on" and not (task and not task.done()):
        _loop["task"] = asyncio.create_task(_autoloop())
    elif state == "off" and task:
        task.cancel()
        _loop["task"] = None
    return health()


@app.post("/api/flatten")
async def flatten_now():
    """Run the end-of-window conversion to TMN immediately (the same routine the loop runs automatically)."""
    s = get_settings()
    return await asyncio.to_thread(agent.flatten, WallexClient(s), s)


@app.post("/api/kill")
def kill():
    agent.RISK_STATE.halted = True
    if _loop["task"]:
        _loop["task"].cancel()
        _loop["task"] = None
    return health()
