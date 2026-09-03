"""Plan execution: a worker thread, a SQLite queue, and the run tools.

submit_run enqueues and returns immediately; a single worker thread claims runs one at
a time and drives the hardware by calling the atomic tool functions in-process. The
blocking nidaqmx calls therefore run off the asyncio loop, so the SSE dashboard and MCP
stay responsive while a sweep is executing.

The queue lives in the database rather than in memory, so moving the worker into its own
process later needs no change to any tool signature.

Everything about *what* a plan means — axis points, `$ref` resolution, limit evaluation —
comes from plans.py. Validation and execution have to agree exactly, so neither gets its
own copy.
"""
import inspect
import itertools
import json
import threading
import time
from typing import Callable, Literal

from mcp.server.fastmcp import FastMCP

import plan_store
import plans
import run_store

# Measurements are asked for their raw samples so a failing point can keep the evidence.
# The array is discarded when the point passes: re-measuring after a failure would
# capture a passing trace of a failure, and keeping every trace would bloat the record.
_KEEPS_RAW = frozenset({"measure_voltage", "measure_current", "measure_temperature"})

# How long a paused run sleeps between checks of its control column.
_PAUSE_POLL_S = 0.5

# How often an idle worker re-checks the queue, as a backstop to the wake event.
_IDLE_POLL_S = 5.0

# Setup and cleanup are steps, but not points. They are stored against these
# sentinel indices so one table holds every step, and split back out on read.
SETUP_INDEX = -1
CLEANUP_INDEX = -2


class _Lease:
    """Exclusive hardware access for the duration of a run.

    A run holds gRPC sessions under the same names the atomic tools use, so an
    interactive call mid-sweep would not merely perturb the measurement — it would
    collide on the session. The worker thread is exempt by identity; everything else is
    refused while a run is executing. Introspection never goes through here, so Claude
    can still watch a run it cannot disturb.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.run_id: str | None = None
        self.thread: threading.Thread | None = None

    def acquire(self, run_id: str) -> None:
        with self._lock:
            self.run_id, self.thread = run_id, threading.current_thread()

    def release(self) -> None:
        with self._lock:
            self.run_id, self.thread = None, None

    def check(self) -> None:
        """Raise unless the caller is allowed to touch hardware right now."""
        with self._lock:
            run_id, owner = self.run_id, self.thread
        if run_id is None or threading.current_thread() is owner:
            return
        raise RuntimeError(
            f"run {run_id} is executing and holds the hardware. Wait for it to finish, "
            f"or stop it with control_run({run_id!r}, 'abort'). Reading device info, "
            f"capabilities, status, plans and runs is still allowed.")


LEASE = _Lease()


# ---------- overrides ----------

def apply_overrides(plan: dict, overrides: dict | None) -> dict:
    """Adjust axis extents and settle time without touching the stored plan.

    Deliberately narrow: only `settle_ms` and per-axis `from`/`to`/`step`/`values`.
    Anything else would make the run record no longer describe the plan it names."""
    if not overrides:
        return plan
    allowed_axis = {"from", "to", "step", "values"}
    unknown = set(overrides) - {"settle_ms", "axes"}
    if unknown:
        raise ValueError(f"overrides may only set settle_ms and axes, got {sorted(unknown)}")

    out = json.loads(json.dumps(plan))
    loop = out.setdefault("loop", {})
    if "settle_ms" in overrides:
        loop["settle_ms"] = overrides["settle_ms"]

    by_name = {a.get("name"): a for a in loop.get("axes", [])}
    for name, changes in (overrides.get("axes") or {}).items():
        if name not in by_name:
            raise ValueError(f"plan has no axis {name!r} (has {', '.join(by_name)})")
        bad = set(changes) - allowed_axis
        if bad:
            raise ValueError(f"axis override may only set {sorted(allowed_axis)}, "
                             f"got {sorted(bad)}")
        if "values" in changes:
            for key in ("from", "to", "step"):
                by_name[name].pop(key, None)
        by_name[name].update(changes)
    return out


# ---------- execution ----------

def _points(plan: plans.Plan) -> list[dict]:
    """Every axis combination, as {axis_name: value}. The first axis is the outer loop,
    so listing the slowest-settling axis first minimises how often it moves."""
    if not plan.loop.axes:
        return [{}]
    names = [a.name for a in plan.loop.axes]
    return [dict(zip(names, combo))
            for combo in itertools.product(*(a.points() for a in plan.loop.axes))]


def _strip_raw(response: dict, keep: bool) -> dict:
    """Raw arrays are evidence for a failure, noise everywhere else."""
    if keep or not isinstance(response, dict) or "raw" not in response:
        return response
    return {k: v for k, v in response.items() if k != "raw"}


def _artifact(response: dict) -> str | None:
    link = response.get("systemlink") if isinstance(response, dict) else None
    return link.get("file_id") if isinstance(link, dict) else None


def _merge_defaults(action: Callable, defaults: dict, step_args: dict) -> dict:
    """Plan defaults under the step's own args, minus any default the action cannot
    take.

    `defaults` is a convenience for the device every step shares, so it must not force
    an argument onto a step that has no use for it — `stop()` takes no `dev`, and being
    handed one is what made cleanup fail. Step args are never filtered: those were
    written deliberately, so a wrong one should surface rather than vanish."""
    params = inspect.signature(action).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return {**defaults, **step_args}
    return {**{k: v for k, v in defaults.items() if k in params}, **step_args}


def _run_step(run_id: str, plan: plans.Plan, step: plans.Step, axis_env: dict,
              point_index: int, actions: dict[str, Callable]) -> str:
    """Execute one step and persist its outcome. Returns PASS, FAIL or ERROR."""
    args = plans.resolve_args(
        _merge_defaults(actions[step.action], plan.defaults, step.args), axis_env)
    if step.action in _KEEPS_RAW:
        args.setdefault("return_raw", True)

    response: dict = {}
    error: str | None = None
    limit_results: list[dict] = []
    try:
        response = actions[step.action](**args)
        for limit in step.limits:
            reference = axis_env.get((limit.of or "")[1:]) if limit.of else None
            measured = plans.measured_value(response, limit)
            limit_results.append(plans.evaluate_limit(limit, measured, reference))
        result = "FAIL" if any(r["result"] == "FAIL" for r in limit_results) else "PASS"
    except Exception as exc:                              # noqa: BLE001
        result, error = "ERROR", f"{type(exc).__name__}: {exc}"
        run_store.add_event(run_id, "error", f"{step.id}: {error}", point_index)

    run_store.add_point({
        "run_id": run_id, "point_index": point_index,
        "axis_values": json.dumps(axis_env), "step_id": step.id,
        "action": step.action,
        "response": json.dumps(_strip_raw(response, result != "PASS"), default=str),
        "limit_results": json.dumps(limit_results) if limit_results else None,
        "result": result, "error": error, "artifact": _artifact(response),
        "timestamp": run_store.now(),
    })

    if result == "PASS" and step.action in plans._DRIVES_OUTPUT and plan.loop.settle_ms:
        time.sleep(plan.loop.settle_ms / 1000.0)
    return result


def _run_section(run_id: str, plan: plans.Plan, steps: list[plans.Step], axis_env: dict,
                 point_index: int, actions: dict[str, Callable]) -> list[str]:
    return [_run_step(run_id, plan, s, axis_env, point_index, actions) for s in steps]


def _await_control(run_id: str) -> str | None:
    """Block while paused; return the control word once it is no longer 'pause'."""
    control = run_store.get_control(run_id)
    if control != "pause":
        return control
    run_store.add_event(run_id, "info", "paused")
    while control == "pause":
        time.sleep(_PAUSE_POLL_S)
        control = run_store.get_control(run_id)
    run_store.update(run_id, status="running")
    run_store.add_event(run_id, "info", f"resumed ({control or 'running'})")
    return control


def execute(run: dict, actions: dict[str, Callable], teardown: Callable) -> None:
    """Drive one run to completion. Never raises: every outcome ends up in the record,
    because a worker that dies takes the queue with it."""
    run_id = run["run_id"]
    plan = plans.Plan.model_validate(json.loads(run["plan_json"]))
    points = _points(plan)
    outcomes: list[str] = []
    aborted = False
    failure: str | None = None

    LEASE.acquire(run_id)
    try:
        run_store.update(run_id, point_count=len(points))
        run_store.add_event(run_id, "info",
                            f"started: {len(points)} point(s), {len(plan.steps)} step(s)")
        outcomes += _run_section(run_id, plan, plan.setup, {}, SETUP_INDEX,
                                 actions)

        for index, axis_env in enumerate(points):
            results = _run_section(run_id, plan, plan.steps, axis_env, index, actions)
            outcomes += results
            run_store.update(run_id, points_done=index + 1)

            if plan.on_step_fail == "abort_run" and any(r != "PASS" for r in results):
                aborted = True
                run_store.add_event(run_id, "warn",
                                    f"aborting after point {index}: on_step_fail=abort_run",
                                    index)
                break
            if _await_control(run_id) == "abort":
                aborted = True
                run_store.add_event(run_id, "warn", f"aborted by request at point {index}",
                                    index)
                break
    except Exception as exc:                              # noqa: BLE001
        failure = f"{type(exc).__name__}: {exc}"
        run_store.add_event(run_id, "error", f"run failed: {failure}")
    finally:
        # Cleanup always runs, and a cleanup failure never masks the original error.
        try:
            outcomes += _run_section(run_id, plan, plan.cleanup, {},
                                     CLEANUP_INDEX, actions)
        except Exception as exc:                          # noqa: BLE001
            run_store.add_event(run_id, "error", f"cleanup failed: {exc}")
        try:
            teardown()
        except Exception as exc:                          # noqa: BLE001
            run_store.add_event(run_id, "error", f"teardown failed: {exc}")
        LEASE.release()

    clean = all(r == "PASS" for r in outcomes) and not aborted and failure is None
    status = ("failed" if failure else "aborted" if aborted
              else "completed")
    run_store.update(run_id, status=status, verdict="PASS" if clean else "FAIL",
                     control=None, ended_at=run_store.now(), error=failure)
    run_store.add_event(run_id, "info",
                        f"finished: {status}, verdict {'PASS' if clean else 'FAIL'}")


# ---------- worker ----------

_WAKE = threading.Event()


def _worker(actions: dict[str, Callable], teardown: Callable) -> None:
    while True:
        _WAKE.wait(timeout=_IDLE_POLL_S)
        _WAKE.clear()
        while (run := run_store.claim_next()) is not None:
            execute(run, actions, teardown)


# ---------- record assembly ----------

Include = Literal["status", "points", "log", "all"]


def _progress(run: dict) -> dict:
    total = run.get("point_count") or 0
    done = run.get("points_done") or 0
    return {"points_done": done, "point_count": total,
            "percent": round(100.0 * done / total, 1) if total else None}


def _step_record(row: dict, include_raw: bool) -> dict:
    response = json.loads(row["response"] or "{}")
    return {
        "step_id": row["step_id"], "action": row["action"], "result": row["result"],
        "stats": response.get("stats"),
        "limits": json.loads(row["limit_results"]) if row["limit_results"] else None,
        "error": row["error"], "artifact": row["artifact"],
        **({"response": response} if include_raw else {}),
    }


def _grouped_points(run_id: str, include_raw: bool) -> dict:
    """Step rows regrouped into the points a reader thinks in.

    Setup and cleanup are stored against sentinel indices because they are steps like
    any other, but they are not points — reporting them as such would make a 3-point
    sweep look like it ran five. They come back under their own keys."""
    sections: dict[int, list[dict]] = {SETUP_INDEX: [], CLEANUP_INDEX: []}
    grouped: dict[int, dict] = {}
    for row in run_store.get_points(run_id):
        index = row["point_index"]
        if index in sections:
            sections[index].append(_step_record(row, include_raw))
            continue
        point = grouped.setdefault(index, {
            "point_index": index,
            "axis_values": json.loads(row["axis_values"] or "{}"),
            "steps": [],
        })
        point["steps"].append(_step_record(row, include_raw))
    return {
        "setup": sections[SETUP_INDEX],
        "points": [grouped[k] for k in sorted(grouped)],
        "cleanup": sections[CLEANUP_INDEX],
    }


def build_record(run_id: str, include: Include = "status") -> dict:
    run = run_store.get(run_id)
    if run is None:
        raise ValueError(f"unknown run_id: {run_id}")

    record = {
        "run_id": run_id, "plan_id": run["plan_id"],
        "status": run["status"], "verdict": run["verdict"],
        "progress": _progress(run), "summary": run_store.counts(run_id),
        "created_at": run["created_at"], "started_at": run["started_at"],
        "ended_at": run["ended_at"], "error": run["error"],
        "overrides": json.loads(run["overrides"]) if run["overrides"] else None,
    }
    if include in ("points", "all"):
        record.update(_grouped_points(run_id, include == "all"))
    if include in ("log", "all"):
        record["log"] = run_store.get_events(run_id)
    if include == "status":
        # The one thing worth surfacing without asking for the full log.
        failures = [e for e in run_store.get_events(run_id) if e["level"] == "error"]
        record["last_error"] = failures[-1]["message"] if failures else None
    return record


# ---------- MCP surface ----------

def install(mcp: FastMCP, *, actions: dict[str, Callable], teardown: Callable,
            validate: Callable) -> None:
    """Register the run tools and start the worker.

    `actions` maps a plan action name to the atomic tool that performs it, `teardown`
    releases all held hardware, and `validate` re-checks a plan against the hardware
    before it is queued."""
    run_store.init_db()
    for run_id in run_store.reconcile():
        run_store.add_event(run_id, "error", "marked interrupted at server startup")

    threading.Thread(target=_worker, args=(actions, teardown),
                     name="plan-runner", daemon=True).start()

    @mcp.tool()
    def submit_run(plan_id: str, overrides: dict | None = None) -> dict:
        """Queue a stored plan for execution and return immediately.

        Returns a run_id; the run proceeds in the background, so the conversation can
        end without stopping it. Poll it with get_run, and stop it with control_run.

        The plan is re-validated against live hardware first, so a rig that changed
        since the plan was written is caught before anything is driven. `overrides`
        may adjust only settle_ms and axis extents — use it to trial a short version,
        e.g. {"axes": {"vsupply": {"step": 1.0}}}. The stored plan is unchanged and the
        run records what was actually executed.

        While a run is executing it holds the hardware exclusively: the measurement and
        stimulus tools refuse until it finishes."""
        plan = plan_store.load(plan_id)
        if plan is None:
            raise ValueError(f"unknown plan_id: {plan_id}")

        effective = apply_overrides(plan, overrides)
        report = validate(effective)
        if not report["valid"]:
            raise ValueError("plan no longer validates against this hardware: "
                             + "; ".join(report["errors"]))

        run_id = run_store.new_run_id()
        run_store.create({
            "run_id": run_id, "plan_id": plan_id,
            "plan_json": json.dumps(effective),
            "overrides": json.dumps(overrides) if overrides else None,
            "status": "queued",
            "point_count": report["point_count"], "created_at": run_store.now(),
        })
        _WAKE.set()
        return {"run_id": run_id, "status": "queued",
                "point_count": report["point_count"],
                "estimated_duration_s": report["estimated_duration_s"],
                "summary": report["summary"]}

    @mcp.tool()
    def get_run(run_id: str, include: Include = "status") -> dict:
        """Fetch a run record.

        "status" is the cheap poll — verdict, progress and the last error, with no
        per-point data; use it while a run is in flight. "points" adds every point's
        stats and limit results. "log" adds the event trail, which is where to look
        when explaining a failure. "all" adds both plus raw responses, and is large."""
        return build_record(run_id, include)

    @mcp.tool()
    def list_runs(plan_id: str | None = None, status: str | None = None,
                  limit: int = 20) -> dict:
        """Recent runs, newest first, optionally filtered by plan or status."""
        return {"runs": run_store.list_runs(plan_id=plan_id, status=status, limit=limit)}

    @mcp.tool()
    def control_run(run_id: str, action: Literal["pause", "resume", "abort"]) -> dict:
        """Pause, resume or abort a run. The worker checks between points, so a request
        takes effect at the next point boundary rather than mid-measurement — a step is
        never cut in half. An aborted run still runs its cleanup and releases the
        hardware, and is recorded as FAIL and incomplete."""
        run = run_store.get(run_id)
        if run is None:
            raise ValueError(f"unknown run_id: {run_id}")
        if run["status"] not in ("queued", "running", "paused"):
            raise ValueError(f"run {run_id} is {run['status']} and cannot be controlled")

        run_store.set_control(run_id, None if action == "resume" else action)
        if action == "pause":
            run_store.update(run_id, status="paused")
        run_store.add_event(run_id, "info", f"{action} requested")
        return {"run_id": run_id, "action": action, "status": run_store.get(run_id)["status"]}
