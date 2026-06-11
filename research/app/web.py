import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .state import state

app = FastAPI(title="tdms-research")
_static = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(_static / "index.html"))


@app.get("/api/files")
def list_files() -> list[dict]:
    return [{"file_id": fid, "path": f.path} for fid, f in state.files.items()]


@app.get("/api/limits")
def list_limits() -> list[dict]:
    return state.limits


@app.get("/api/figure")
def current_figure() -> JSONResponse:
    return JSONResponse(state.current_figure or {})


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    state.subscribe(loop, queue)

    async def stream():
        try:
            if state.current_figure:
                yield f"data: {json.dumps({'type': 'figure', 'data': state.current_figure})}\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            state.unsubscribe(loop, queue)

    return StreamingResponse(stream(), media_type="text/event-stream")
