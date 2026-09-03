"""SQLite persistence for run records.

Plans are documents and live as JSON files; runs are the opposite shape. They are
appended to point-by-point while a reader polls progress, they must survive a crash
mid-run with completed points intact, they grow without bound, and reports query across
them. That is what SQLite is for, and the `runs` table doubles as the executor's queue.

Owns the schema and all SQL. Nothing here knows about hardware, MCP, or plan semantics.
"""
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DATA_DIR = Path(os.environ.get("NI_DATA_DIR") or Path(__file__).parent / "data")
DB_PATH = DATA_DIR / "runs.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  plan_id     TEXT NOT NULL,
  plan_json   TEXT NOT NULL,
  overrides   TEXT,
  status      TEXT NOT NULL,
  verdict     TEXT,
  control     TEXT,
  point_count INTEGER,
  points_done INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  started_at  TEXT,
  ended_at    TEXT,
  error       TEXT
);

CREATE TABLE IF NOT EXISTS run_points (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  point_index   INTEGER NOT NULL,
  axis_values   TEXT,
  step_id       TEXT NOT NULL,
  action        TEXT NOT NULL,
  response      TEXT,
  limit_results TEXT,
  result        TEXT NOT NULL,
  error         TEXT,
  artifact      TEXT,
  timestamp     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  level       TEXT NOT NULL,
  message     TEXT NOT NULL,
  point_index INTEGER,
  timestamp   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_points_run ON run_points(run_id, point_index);
CREATE INDEX IF NOT EXISTS idx_events_run ON run_events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_queue ON runs(status, created_at);
"""

# runs.status   queued | running | paused | completed | failed | aborted | interrupted
# runs.control  NULL | pause | abort  -- read by the worker between points
# runs.verdict  PASS | FAIL
# run_points.result  PASS | FAIL | ERROR


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """A connection per operation. The worker writes points while MCP tools read
    progress, so WAL is what keeps a status poll from blocking behind an insert."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"run_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def reconcile() -> list[str]:
    """Any run still active at startup belongs to a process that is gone. Mark them
    interrupted so nothing waits forever on a worker that died."""
    active = ("running", "queued", "paused")
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT run_id FROM runs WHERE status IN {active}").fetchall()
        if rows:
            conn.execute(
                f"UPDATE runs SET status = 'interrupted', verdict = 'FAIL', "
                f"ended_at = ?, error = 'server restarted while this run was active' "
                f"WHERE status IN {active}", (now(),))
    return [r["run_id"] for r in rows]


# ---------- runs ----------

def create(run: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, plan_id, plan_json, overrides, "
            "status, point_count, created_at) VALUES (:run_id, :plan_id, :plan_json, "
            ":overrides, :status, :point_count, :created_at)", run)


def claim_next() -> dict | None:
    """Atomically take the oldest queued run. BEGIN IMMEDIATE takes the write lock up
    front, so the claim and the status change cannot interleave with another worker."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM runs WHERE status = 'queued' "
            "ORDER BY created_at, rowid LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE runs SET status = 'running', started_at = ? "
                     "WHERE run_id = ?", (now(), row["run_id"]))
    return dict(row)


def update(run_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    with _connect() as conn:
        conn.execute(f"UPDATE runs SET {cols} WHERE run_id = :run_id",
                     {**fields, "run_id": run_id})


def get(run_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(plan_id: str | None = None, status: str | None = None,
              limit: int = 20) -> list[dict]:
    where, params = [], []
    if plan_id:
        where.append("plan_id = ?")
        params.append(plan_id)
    if status:
        where.append("status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, plan_id, status, verdict, point_count, "
            f"points_done, created_at, ended_at FROM runs {clause} "
            "ORDER BY created_at DESC LIMIT ?", (*params, limit)).fetchall()
    return [dict(r) for r in rows]


def set_control(run_id: str, action: str | None) -> None:
    with _connect() as conn:
        conn.execute("UPDATE runs SET control = ? WHERE run_id = ?", (action, run_id))


def get_control(run_id: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT control FROM runs WHERE run_id = ?",
                           (run_id,)).fetchone()
    return row["control"] if row else None


# ---------- points & events ----------

def add_point(point: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO run_points (run_id, point_index, axis_values, step_id, "
            "action, response, limit_results, result, error, artifact, timestamp) "
            "VALUES (:run_id, :point_index, :axis_values, :step_id, :action, "
            ":response, :limit_results, :result, :error, :artifact, :timestamp)", point)


def get_points(run_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM run_points WHERE run_id = ? ORDER BY point_index, id",
            (run_id,)).fetchall()
    return [dict(r) for r in rows]


def add_event(run_id: str, level: str, message: str,
              point_index: int | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO run_events (run_id, level, message, point_index, timestamp) "
            "VALUES (?, ?, ?, ?, ?)", (run_id, level, message, point_index, now()))


def get_events(run_id: str, limit: int = 100) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT level, message, point_index, timestamp FROM run_events "
            "WHERE run_id = ? ORDER BY id LIMIT ?", (run_id, limit)).fetchall()
    return [dict(r) for r in rows]


def counts(run_id: str) -> dict:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT result, COUNT(*) n FROM run_points WHERE run_id = ? "
            "GROUP BY result", (run_id,)).fetchall()
    tally = {r["result"]: r["n"] for r in rows}
    return {"steps_passed": tally.get("PASS", 0),
            "steps_failed": tally.get("FAIL", 0),
            "steps_errored": tally.get("ERROR", 0)}
