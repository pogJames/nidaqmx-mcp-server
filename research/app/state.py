import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from nptdms import TdmsFile


@dataclass
class LoadedFile:
    file_id: str
    path: str
    tdms: TdmsFile


@dataclass
class State:
    files: dict[str, LoadedFile] = field(default_factory=dict)
    limits: list[dict] = field(default_factory=list)
    current_figure: dict | None = None
    subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_file(self, path: str) -> str:
        tdms = TdmsFile.read(path)
        fid = f"f_{uuid.uuid4().hex[:8]}"
        with self._lock:
            self.files[fid] = LoadedFile(fid, path, tdms)
        return fid

    def get_file(self, file_id: str) -> LoadedFile:
        with self._lock:
            f = self.files.get(file_id)
        if f is None:
            raise ValueError(f"unknown file_id: {file_id}")
        return f

    def drop_file(self, file_id: str) -> None:
        with self._lock:
            self.files.pop(file_id, None)

    def subscribe(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        with self._lock:
            self.subscribers.append((loop, queue))

    def unsubscribe(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        with self._lock:
            try:
                self.subscribers.remove((loop, queue))
            except ValueError:
                pass

    def _broadcast(self, msg: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self.subscribers)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, msg)
            except Exception:
                pass

    def push_figure(self, fig_dict: dict) -> None:
        self.current_figure = fig_dict
        self._broadcast({"type": "figure", "data": fig_dict})

    def notify(self, kind: str) -> None:
        self._broadcast({"type": kind})


state = State()
