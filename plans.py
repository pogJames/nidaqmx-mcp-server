"""Test plans: schema, validation, and the authoring tools.

A plan is a declarative document — not a command stream — so it can be checked before
it touches hardware, cost-estimated, read back in plain language, re-run against a new
unit, and diffed. `install(mcp, ...)` is called once from the server with the hardware
callables injected, which keeps this module free of any nidaqmx import: everything here
is pure logic over dicts plus two read-only capability queries.

The limit evaluator and `$axis` resolution live here rather than in the executor,
because validation and execution have to agree on them exactly.
"""
import inspect
from collections.abc import Mapping
from typing import Any, Callable, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

import plan_store

# A grid this large is a mistake, not an intention — 100k points at 100 ms each is
# nearly three hours.
POINT_CAP = 100_000

# Percentage of zero is undefined, so within_pct against a reference that reaches zero
# is rejected at validation rather than silently passing everything.
ZERO_FLOOR = 1e-12


# ---------- schema ----------

class Axis(BaseModel):
    """One swept parameter. Either `from`/`to`/`step` for a regular sweep, or an
    explicit `values` list for log spacing or arbitrary setpoints. Steps reference it
    as `"$name"` anywhere in their args."""
    name: str
    unit: str = ""
    from_: float | None = Field(default=None, alias="from")
    to: float | None = None
    step: float | None = None
    values: list[float] | None = None

    model_config = {"populate_by_name": True, "extra": "forbid"}

    def points(self) -> list[float]:
        """Integer step counts, so 0.1-volt steps don't drift into 0.30000000000000004."""
        if self.values is not None:
            return list(self.values)
        if self.from_ is None or self.to is None or not self.step:
            raise ValueError(
                f"axis {self.name!r} needs either from/to/step or values")
        n = round((self.to - self.from_) / self.step) + 1
        return [self.from_ + i * self.step for i in range(n)]

    def span(self) -> tuple[float, float] | None:
        """None when the axis is malformed. Validation reports every problem it finds,
        so a bad axis must not stop the checks that come after it."""
        try:
            pts = self.points()
        except ValueError:
            return None
        return (min(pts), max(pts)) if pts else None


class Loop(BaseModel):
    """No axes runs the steps once (a functional test); one axis is a sweep; two or
    more is a shmoo over their cartesian product."""
    axes: list[Axis] = []
    settle_ms: int = 0

    # Unknown keys are an error, not a silent no-op: a plan author guessing at a
    # field name must be told it is wrong rather than having it quietly ignored.
    model_config = {"extra": "forbid"}


LimitOp = Literal["lt", "lte", "gt", "gte", "between", "within_pct", "within_abs"]


class Limit(BaseModel):
    """A check against one number in a step's response.

    `stat` names a statistic (mean, rms, peak, min, max, std) when the response has a
    per-channel stats block, or a top-level key like `frequency_hz` or `gain` when it
    doesn't. `channel` picks which channel's stats to read; it can be omitted when the
    step measured exactly one.

    `of` makes the check relative to an axis value, e.g. `"$vsupply"`."""
    stat: str
    channel: str | None = None
    op: LimitOp
    value: float | None = None
    min: float | None = None
    max: float | None = None
    of: str | None = None

    # Unknown keys are an error, not a silent no-op: a plan author guessing at a
    # field name must be told it is wrong rather than having it quietly ignored.
    model_config = {"extra": "forbid"}


class Step(BaseModel):
    """One action. `limits` turns it into a test; without them it's just a stimulus."""
    id: str
    action: str
    args: dict[str, Any] = {}
    limits: list[Limit] = []

    # Unknown keys are an error, not a silent no-op: a plan author guessing at a
    # field name must be told it is wrong rather than having it quietly ignored.
    model_config = {"extra": "forbid"}


class Plan(BaseModel):
    plan_id: str
    title: str = ""
    description: str = ""
    defaults: dict[str, Any] = {}
    loop: Loop = Loop()
    setup: list[Step] = []
    steps: list[Step]
    cleanup: list[Step] = []
    on_step_fail: Literal["continue", "abort_run"] = "continue"
    created_at: str | None = None
    updated_at: str | None = None

    # Unknown keys are an error, not a silent no-op: a plan author guessing at a
    # field name must be told it is wrong rather than having it quietly ignored.
    model_config = {"extra": "forbid"}


# ---------- $axis resolution ----------

def resolve_args(args: dict, env: dict) -> dict:
    """Replace every `"$name"` with the current value of that axis. Applied recursively
    so a channel list or nested config can carry references too."""
    return {k: _resolve(v, env) for k, v in args.items()}


def _resolve(value: Any, env: dict) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if name not in env:
            raise KeyError(f"unknown axis reference: {value}")
        return env[name]
    if isinstance(value, dict):
        return {k: _resolve(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, env) for v in value]
    return value


def _refs(value: Any) -> list[str]:
    """Every `$name` appearing anywhere in a value."""
    if isinstance(value, str) and value.startswith("$"):
        return [value[1:]]
    if isinstance(value, dict):
        return [r for v in value.values() for r in _refs(v)]
    if isinstance(value, list):
        return [r for v in value for r in _refs(v)]
    return []


# ---------- limit evaluation ----------

def measured_value(response: dict, limit: Limit) -> float:
    """Pull the number a limit checks out of a tool response.

    Prefers the per-channel stats block; falls back to a top-level key for the tools
    that return a bare number (measure_frequency, count_edges)."""
    stats = response.get("stats")
    if isinstance(stats, dict) and stats:
        if limit.channel is not None:
            if limit.channel not in stats:
                raise KeyError(f"channel {limit.channel!r} not in response stats "
                               f"({', '.join(stats)})")
            return float(stats[limit.channel][limit.stat])
        if len(stats) == 1:
            return float(next(iter(stats.values()))[limit.stat])
        raise KeyError(f"step measured {len(stats)} channels; limit on {limit.stat!r} "
                       f"must name one of {', '.join(stats)}")
    if limit.stat in response:
        return float(response[limit.stat])
    raise KeyError(f"response has no {limit.stat!r}")


def evaluate_limit(limit: Limit, measured: float,
                   reference: float | None = None) -> dict:
    """Judge one limit. Returns the full comparison, not just a verdict, so a failing
    point shows how far off it was without re-deriving anything."""
    out: dict[str, Any] = {"stat": limit.stat, "channel": limit.channel,
                           "op": limit.op, "measured": measured}
    if limit.op in ("lt", "lte", "gt", "gte"):
        bound = float(limit.value)  # type: ignore[arg-type]
        ok = {"lt": measured < bound, "lte": measured <= bound,
              "gt": measured > bound, "gte": measured >= bound}[limit.op]
        out["limit"] = bound
        out["margin"] = measured - bound
    elif limit.op == "between":
        lo, hi = float(limit.min), float(limit.max)  # type: ignore[arg-type]
        ok = lo <= measured <= hi
        out["limit"] = {"min": lo, "max": hi}
        out["margin"] = min(measured - lo, hi - measured)
    else:
        ref = float(reference)  # type: ignore[arg-type]
        tol = float(limit.value)  # type: ignore[arg-type]
        allowed = abs(ref) * tol / 100.0 if limit.op == "within_pct" else tol
        deviation = abs(measured - ref)
        ok = deviation <= allowed
        out.update({"reference": ref, "limit": tol, "allowed": allowed,
                    "deviation": deviation,
                    "margin": allowed - deviation})
    out["result"] = "PASS" if ok else "FAIL"
    return out


# ---------- action metadata ----------

# Which args name physical channels, and on which subsystem. Only ai/ao channel names
# can be checked: get_device_info reports digital as a line count and counters as
# measurement types, so those rely on the capability check instead.
_CHANNEL_ARGS: dict[str, tuple[tuple[str, str], ...]] = {
    "measure_voltage":     (("channels", "ai"),),
    "measure_current":     (("channels", "ai"),),
    "measure_temperature": (("channels", "ai"),),
    "set_voltage":         (("channel", "ao"),),
    "set_waveform":        (("channel", "ao"),),
}

def _accepted_args(actions, action: str) -> set[str] | None:
    """Parameter names an action accepts, or None when that cannot be known.

    A step arg the tool does not take is a run wasted at the point it is reached — and
    validation is the only place to catch it, because the tool itself only complains
    once it is called. Returns None when `actions` is a bare name set (no callable to
    inspect) or the action takes **kwargs."""
    if not isinstance(actions, Mapping):
        return None
    fn = actions.get(action)
    if not callable(fn):
        return None
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return None
    return set(params)


# Actions that act on the server rather than on a device. Requiring `dev` on these
# would force a plan to pass an argument the tool does not accept — which is exactly
# what broke cleanup on the first real run.
_NO_DEVICE = frozenset({"stop"})

# Which subsystem a step's `rate` argument drives, so it can be checked against that
# module's sampling limits rather than assumed to be achievable.
_RATE_SUBSYSTEM = {
    "measure_voltage": "ai", "measure_current": "ai", "measure_temperature": "ai",
    "set_waveform": "ao",
}

# Actions that drive an output, and so need settle time before the next measurement.
_DRIVES_OUTPUT = frozenset({"set_voltage", "set_digital", "set_waveform", "pulse"})

# Args carrying a voltage that must sit inside the module's AO range.
_OUTPUT_VOLTS: dict[str, tuple[str, ...]] = {
    "set_voltage": ("volts",),
    "set_waveform": ("amplitude", "offset"),
}


def _step_seconds(action: str, args: dict) -> float:
    """Rough per-step wall time. Measurements are dominated by samples/rate; anything
    else is a driver round-trip, call it 20 ms."""
    if action in ("measure_voltage", "measure_current", "measure_temperature"):
        rate = _num(args.get("rate"), 1000.0)
        return _num(args.get("samples"), 3000.0) / rate if rate else 0.02
    if action == "count_edges":
        return _num(args.get("duration_s"), 1.0)
    if action == "pulse":
        freq = _num(args.get("frequency"), 1000.0)
        return _num(args.get("count"), 1.0) / freq if freq else 0.02
    return 0.02


def _num(value: Any, default: float) -> float:
    """Axis references and other non-numbers fall back to the default for estimation."""
    return float(value) if isinstance(value, (int, float)) else default


# ---------- hardware view ----------

class _Hardware:
    """Read-only capability lookups, cached per validation so a 12-step plan doesn't
    re-query the same module a dozen times.

    Every call is guarded, but a failed lookup has two very different causes and they
    must not be conflated. No rig attached means the plan is being written at a desk, so
    the hardware checks downgrade to a warning. A rig that answers and simply has no
    such device is a mistake in the plan, and is an error — otherwise a plan naming a
    device that was unplugged validates clean and every one of its steps dies at run
    time."""

    def __init__(self, device_info: Callable[[str], dict],
                 support: Callable[[str, list[str]], dict],
                 devices: Callable[[], dict] | None = None):
        self._device_info, self._support, self._devices = device_info, support, devices
        self._info: dict[str, dict | None] = {}
        self._present: list[str] | None = None
        self._system_up: bool | None = None
        self.offline: str | None = None
        self.missing: dict[str, str] = {}

    def _system_answers(self) -> bool:
        """Whether the system responds at all. Asked once and cached — this single call
        is what separates 'no such device' from 'no rig'."""
        if self._system_up is None:
            if self._devices is None:
                self._system_up, self._present = False, []
            else:
                try:
                    self._present = list(self._devices()["devices"])
                    self._system_up = True
                except Exception:                        # noqa: BLE001
                    self._present, self._system_up = [], False
        return bool(self._system_up)

    def info(self, device: str) -> dict | None:
        if device not in self._info:
            try:
                self._info[device] = self._device_info(device)
            except Exception as exc:                     # noqa: BLE001 - reported, not raised
                self._info[device] = None
                # Only a device the system positively does not list is "missing". A
                # lookup that fails for a device that *is* listed is some other
                # trouble — transient, or a permission — and stays a warning rather
                # than accusing the plan of naming something that doesn't exist.
                if self._system_answers() and device not in (self._present or []):
                    self.missing[device] = ", ".join(self._present or [])
                else:
                    self.offline = self.offline or f"{type(exc).__name__}: {exc}"
        return self._info[device]

    def absent(self, device: str) -> str | None:
        """A message when the system is up and has no such device, else None."""
        self.info(device)
        if device not in self.missing:
            return None
        present = self.missing[device]
        return (f"no device named {device!r} on this system"
                + (f" (has {present})" if present else ""))

    def supports(self, device: str, action: str) -> tuple[bool, str]:
        """(ok, reason) — `reason` is a complete phrase, because a chassis and a
        missing capability need to be explained differently."""
        info = self.info(device)
        if info is None:
            return True, ""
        if info.get("chassis"):
            modules = ", ".join(m["device"] for m in info.get("modules") or [])
            return False, (
                f"{device} is a chassis and carries no channels; target a module "
                f"({modules})" if modules else
                f"{device} is a chassis but enumerates no modules, so its capabilities "
                f"cannot be read — try restart_device({device!r})")
        try:
            result = self._support(device, [action])["tools"][action]
        except Exception:                                # noqa: BLE001
            return True, ""
        if result["supported"]:
            return True, ""
        return False, (f"{device} cannot run {action} — "
                       f"missing {'; '.join(result['missing'])}")

    def channels(self, device: str, subsystem: str) -> list[str] | None:
        info = self.info(device)
        if info is None or info.get("chassis"):
            return None
        return (info.get(subsystem) or {}).get("channels")

    def ao_range(self, device: str) -> tuple[float, float] | None:
        info = self.info(device)
        if info is None:
            return None
        rngs = (info.get("ao") or {}).get("voltage_rngs")
        return (min(rngs), max(rngs)) if rngs else None

    def rate_limits(self, device: str, subsystem: str,
                    channel_count: int) -> tuple[float | None, float | None]:
        """The sampling rates this module will accept. Scanning several AI channels
        costs rate on a multiplexed module, so the multi-channel ceiling applies as
        soon as a step asks for more than one."""
        info = self.info(device)
        if info is None:
            return None, None
        caps = info.get(subsystem) or {}
        ceiling = caps.get("max_rate")
        if subsystem == "ai":
            ceiling = (caps.get("max_multi_chan_rate") if channel_count > 1
                       else None) or caps.get("max_single_chan_rate") or ceiling
        return caps.get("min_rate"), ceiling


# ---------- validation ----------

def validate(plan_dict: dict, actions: frozenset[str], hw: _Hardware) -> dict:
    """Check a plan without running it. Returns valid/errors/warnings plus point count,
    duration estimate, and a plain-language summary to read back to the engineer."""
    try:
        plan = Plan.model_validate(plan_dict)
    except Exception as exc:                             # noqa: BLE001
        return {"valid": False, "errors": [str(exc)], "warnings": [],
                "point_count": 0, "summary": ""}

    errors: list[str] = []
    warnings: list[str] = []

    if not plan_store.is_slug(plan.plan_id):
        errors.append(f"plan_id {plan.plan_id!r} must be a lowercase slug "
                      "(letters, digits, - and _)")

    axis_names = [a.name for a in plan.loop.axes]
    _check_axes(plan.loop.axes, errors)
    _check_step_ids(plan, errors)

    point_count = 1
    for axis in plan.loop.axes:
        try:
            point_count *= max(len(axis.points()), 1)
        except ValueError:
            pass                                          # already reported by _check_axes
    if point_count > POINT_CAP:
        errors.append(f"{point_count} points exceeds the cap of {POINT_CAP}; "
                      "widen the step or shorten the range")

    sections = [("setup", plan.setup, False), ("steps", plan.steps, True),
                ("cleanup", plan.cleanup, False)]
    for name, steps, allow_axis in sections:
        for step in steps:
            _check_step(step, name, allow_axis, axis_names, plan, actions, hw,
                        errors, warnings)

    if not plan.steps:
        errors.append("steps must not be empty")

    if hw.offline:
        warnings.append(f"could not reach hardware to verify channels and "
                        f"capabilities ({hw.offline}); those checks were skipped")

    seconds = _estimate(plan, point_count)
    if plan.loop.settle_ms == 0 and any(
            s.action in _DRIVES_OUTPUT for s in plan.steps):
        warnings.append("settle_ms is 0; a measurement may run before the output has "
                        "settled")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "shape": _shape(plan),
        "point_count": point_count,
        "step_count": len(plan.steps),
        "measurements_total": point_count * sum(1 for s in plan.steps if s.limits),
        "estimated_duration_s": round(seconds, 1),
        "summary": _summarize(plan, point_count, seconds),
    }


def _check_axes(axes: list[Axis], errors: list[str]) -> None:
    seen = set()
    for axis in axes:
        if not axis.name.isidentifier():
            errors.append(f"axis name {axis.name!r} must be a valid identifier")
        if axis.name in seen:
            errors.append(f"duplicate axis name {axis.name!r}")
        seen.add(axis.name)

        if axis.values is not None:
            if not axis.values:
                errors.append(f"axis {axis.name!r}: values must not be empty")
            continue
        if axis.from_ is None or axis.to is None or axis.step is None:
            errors.append(f"axis {axis.name!r}: needs either from/to/step or values")
        elif axis.step <= 0:
            errors.append(f"axis {axis.name!r}: step must be > 0")
        elif (axis.to - axis.from_) * axis.step < 0:
            errors.append(f"axis {axis.name!r}: step sign does not run from "
                          f"{axis.from_} toward {axis.to}")


def _check_step_ids(plan: Plan, errors: list[str]) -> None:
    seen = set()
    for step in [*plan.setup, *plan.steps, *plan.cleanup]:
        if step.id in seen:
            errors.append(f"duplicate step id {step.id!r}")
        seen.add(step.id)


def _check_step(step: Step, section: str, allow_axis: bool, axis_names: list[str],
                plan: Plan, actions: frozenset[str], hw: _Hardware,
                errors: list[str], warnings: list[str]) -> None:
    where = f"{section}[{step.id}]"

    if step.action not in actions:
        errors.append(f"{where}: {step.action!r} is not usable as a plan step "
                      f"(available: {', '.join(sorted(actions))})")
        return

    accepted = _accepted_args(actions, step.action)
    if accepted is not None:
        unexpected = sorted(set(step.args) - accepted)
        if unexpected:
            errors.append(f"{where}: {step.action} does not take {unexpected} "
                          f"(accepts {sorted(accepted)})")

    args = {**plan.defaults, **step.args}

    for ref in _refs(args):
        if ref not in axis_names:
            errors.append(f"{where}: ${ref} does not name an axis "
                          f"({', '.join(axis_names) or 'none declared'})")
        elif not allow_axis:
            errors.append(f"{where}: {section} steps run once, outside the loop, "
                          f"so they cannot reference ${ref}")

    if step.limits and section != "steps":
        errors.append(f"{where}: only steps may carry limits")

    if step.action in _NO_DEVICE:
        for limit in step.limits:
            _check_limit(limit, where, plan, axis_names, errors, warnings)
        return

    device = args.get("dev")
    if not isinstance(device, str) or not device:
        errors.append(f"{where}: args.dev is required "
                      "(set it once in plan.defaults if every step shares it)")
        return

    gone = hw.absent(device)
    if gone:
        errors.append(f"{where}: {gone}")
        return

    supported, reason = hw.supports(device, step.action)
    if not supported:
        errors.append(f"{where}: {reason}")

    _check_channels(step, where, args, device, hw, errors)
    _check_output_range(step, where, args, device, plan, hw, errors)
    _check_rate(step, where, args, device, hw, errors)
    for limit in step.limits:
        _check_limit(limit, where, plan, axis_names, errors, warnings)


def _check_channels(step: Step, where: str, args: dict, device: str,
                    hw: _Hardware, errors: list[str]) -> None:
    for arg, subsystem in _CHANNEL_ARGS.get(step.action, ()):
        available = hw.channels(device, subsystem)
        if available is None:
            continue
        requested = args.get(arg)
        names = requested if isinstance(requested, list) else [requested]
        for name in names:
            if not isinstance(name, str):
                continue
            if f"{device}/{name}" not in available:
                errors.append(
                    f"{where}: {device} has no {subsystem} channel {name!r} "
                    f"(has {', '.join(c.split('/')[-1] for c in available)})")


def _check_rate(step: Step, where: str, args: dict, device: str,
                hw: _Hardware, errors: list[str]) -> None:
    """A sample rate the module cannot deliver is a whole run wasted — DAQmx only
    complains once the task is configured, which is after the first output has already
    been driven. Slow modules are where this bites: a 9219 tops out at 100 S/s while
    1 kS/s is the natural thing to write."""
    subsystem = _RATE_SUBSYSTEM.get(step.action)
    rate = args.get("rate")
    if subsystem is None or not isinstance(rate, (int, float)):
        return
    channels = args.get("channels")
    low, high = hw.rate_limits(device, subsystem,
                               len(channels) if isinstance(channels, list) else 1)
    if high is not None and rate > high:
        errors.append(f"{where}: rate {rate:g} S/s is above {device}'s {subsystem} "
                      f"maximum of {high:g} S/s")
    if low is not None and rate < low:
        errors.append(f"{where}: rate {rate:g} S/s is below {device}'s {subsystem} "
                      f"minimum of {low:g} S/s")


def _check_output_range(step: Step, where: str, args: dict, device: str,
                        plan: Plan, hw: _Hardware, errors: list[str]) -> None:
    """Every voltage the step could reach must sit inside the module's AO range —
    including both ends of an axis it sweeps. This is the check that stops a plan
    from asking for 50 V before anything is driven."""
    span = hw.ao_range(device)
    if span is None:
        return
    low, high = span
    for arg in _OUTPUT_VOLTS.get(step.action, ()):
        for volts in _reachable(args.get(arg), plan):
            if not low <= volts <= high:
                errors.append(f"{where}: {arg}={volts:g} V is outside {device}'s "
                              f"output range [{low:g}, {high:g}] V")


def _reachable(value: Any, plan: Plan) -> list[float]:
    """Literal values pass through; an axis reference contributes both endpoints. A
    malformed axis yields nothing — _check_axes has already reported it."""
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, str) and value.startswith("$"):
        for axis in plan.loop.axes:
            if axis.name == value[1:]:
                return list(axis.span() or ())
    return []


def _check_limit(limit: Limit, where: str, plan: Plan, axis_names: list[str],
                 errors: list[str], warnings: list[str]) -> None:
    if limit.op == "between":
        if limit.min is None or limit.max is None:
            errors.append(f"{where}: 'between' needs both min and max")
        elif limit.min > limit.max:
            errors.append(f"{where}: min {limit.min} is above max {limit.max}")
    elif limit.value is None:
        errors.append(f"{where}: {limit.op!r} needs a value")

    if limit.op in ("within_pct", "within_abs"):
        if limit.of is None:
            errors.append(f"{where}: {limit.op!r} needs 'of' naming the reference, "
                          "e.g. \"$vsupply\"")
            return
        if not limit.of.startswith("$") or limit.of[1:] not in axis_names:
            errors.append(f"{where}: 'of' must reference a declared axis, got "
                          f"{limit.of!r}")
            return
        if limit.op == "within_pct":
            axis = next(a for a in plan.loop.axes if a.name == limit.of[1:])
            span = axis.span()
            if span is None:
                return                                    # malformed axis already reported
            low, high = span
            if low <= ZERO_FLOOR <= high or low <= -ZERO_FLOOR <= high:
                errors.append(
                    f"{where}: within_pct against ${axis.name} is undefined because "
                    f"the axis reaches zero ({low:g}..{high:g}); use within_abs")
    elif limit.of is not None:
        warnings.append(f"{where}: 'of' is ignored by {limit.op!r}")


# ---------- estimate & summary ----------

def _estimate(plan: Plan, point_count: int) -> float:
    settle = plan.loop.settle_ms / 1000.0
    once = sum(_step_seconds(s.action, {**plan.defaults, **s.args})
               for s in [*plan.setup, *plan.cleanup])
    per_point = sum(
        _step_seconds(s.action, {**plan.defaults, **s.args})
        + (settle if s.action in _DRIVES_OUTPUT else 0.0)
        for s in plan.steps)
    return once + point_count * per_point


def _shape(plan: Plan) -> str:
    return {0: "functional", 1: "sweep"}.get(len(plan.loop.axes), "shmoo")


def _summarize(plan: Plan, point_count: int, seconds: float) -> str:
    """A plain-language restatement — this is what Claude reads back before the
    engineer confirms."""
    axes = plan.loop.axes
    if not axes:
        head = f"Run {len(plan.steps)} step(s) once"
    else:
        head = " x ".join(_axis_phrase(a) for a in axes)
        head = f"{'Sweep' if len(axes) == 1 else 'Shmoo'} {head} ({point_count} points)"

    body = "; then ".join(_step_phrase(s, plan) for s in plan.steps)
    tail = f" Estimated {_duration(seconds)}."
    settle = (f" Settling {plan.loop.settle_ms} ms after each output change."
              if plan.loop.settle_ms else "")
    return f"{head}. At each point: {body}.{settle}{tail}"


def _axis_phrase(axis: Axis) -> str:
    unit = f" {axis.unit}" if axis.unit else ""
    if axis.values is not None:
        return f"{axis.name} over {len(axis.values)} values{unit}"
    return (f"{axis.name} {axis.from_:g}->{axis.to:g}{unit} "
            f"in {axis.step:g}{unit} steps")


def _step_phrase(step: Step, plan: Plan) -> str:
    args = {**plan.defaults, **step.args}
    target = args.get("channels") or args.get("channel") or args.get("lines") \
        or args.get("counter") or ""
    if isinstance(target, list):
        target = ", ".join(str(t) for t in target)
    verb = step.action.replace("_", " ")
    phrase = f"{verb} {target}".strip()
    if step.limits:
        phrase += f" (checking {', '.join(_limit_phrase(l) for l in step.limits)})"
    return phrase


def _limit_phrase(limit: Limit) -> str:
    where = f"{limit.channel} " if limit.channel else ""
    if limit.op == "between":
        return f"{where}{limit.stat} between {limit.min:g} and {limit.max:g}"
    if limit.op == "within_pct":
        return f"{where}{limit.stat} within {limit.value:g}% of {limit.of}"
    if limit.op == "within_abs":
        return f"{where}{limit.stat} within {limit.value:g} of {limit.of}"
    word = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">="}[limit.op]
    return f"{where}{limit.stat} {word} {limit.value:g}"


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


# ---------- templates ----------

PlanShape = Literal["functional", "sweep", "shmoo"]

# A worked example of each shape. These exist so a plan can be authored from a real
# starting point instead of reverse-engineered by trial and error: `plan` is a free-form
# object at the MCP boundary, so nothing else advertises what belongs inside it.
_TEMPLATES: dict[str, dict] = {
    # No axes: the steps run once, in order.
    "functional": {
        "plan_id": "example_functional",
        "title": "Loopback check",
        "defaults": {"dev": "Dev1"},
        "steps": [
            {"id": "drive", "action": "set_voltage",
             "args": {"channel": "ao0", "volts": 2.5}},
            {"id": "read", "action": "measure_voltage",
             "args": {"channels": ["ai0"], "rate": 1000, "samples": 100},
             "limits": [{"stat": "mean", "op": "between", "min": 2.45, "max": 2.55}]},
        ],
        "cleanup": [{"id": "off", "action": "stop", "args": {"target": "outputs"}}],
    },
    # One axis: the steps repeat at every setpoint, with "$name" carrying its value.
    "sweep": {
        "plan_id": "example_sweep",
        "title": "AI0 tracks AO0",
        "defaults": {"dev": "Dev1"},
        "loop": {
            "axes": [{"name": "vsupply", "from": 0.5, "to": 5.0, "step": 0.25,
                      "unit": "V"}],
            "settle_ms": 50,
        },
        "steps": [
            {"id": "drive", "action": "set_voltage",
             "args": {"channel": "ao0", "volts": "$vsupply"}},
            {"id": "read", "action": "measure_voltage",
             "args": {"channels": ["ai0"], "rate": 1000, "samples": 100},
             "limits": [{"stat": "mean", "op": "within_pct", "of": "$vsupply",
                         "value": 2.0}]},
        ],
        "cleanup": [{"id": "off", "action": "stop", "args": {"target": "outputs"}}],
        "on_step_fail": "continue",
    },
    # Two axes: their cartesian product. The first axis is the outer loop, so list the
    # slowest-settling one first. An axis may use "values" instead of from/to/step.
    "shmoo": {
        "plan_id": "example_shmoo",
        "title": "Voltage x frequency shmoo",
        "defaults": {"dev": "Dev1"},
        "loop": {
            "axes": [
                {"name": "vsupply", "from": 3.0, "to": 5.0, "step": 0.5, "unit": "V"},
                {"name": "fclk", "values": [1000, 10000, 100000], "unit": "Hz"},
            ],
            "settle_ms": 20,
        },
        "steps": [
            {"id": "supply", "action": "set_voltage",
             "args": {"channel": "ao0", "volts": "$vsupply"}},
            {"id": "clock", "action": "set_waveform",
             "args": {"channel": "ao1", "frequency": "$fclk", "amplitude": 1.0}},
            {"id": "read", "action": "measure_voltage",
             "args": {"channels": ["ai0", "ai1"]},
             "limits": [{"channel": "ai0", "stat": "rms", "op": "gt", "value": 0.1}]},
        ],
    },
}


# ---------- MCP surface ----------

def install(mcp: FastMCP, *, actions,
            device_info: Callable[[str], dict],
            support: Callable[[str, list[str]], dict],
            devices: Callable[[], dict] | None = None) -> None:
    """Register the plan tools. `actions` is the set of hardware tools a step may name;
    `device_info` and `support` are the read-only capability queries validation uses."""

    def _hw() -> _Hardware:
        return _Hardware(device_info, support, devices)

    @mcp.tool()
    def plan_template(shape: PlanShape = "sweep") -> dict:
        """A complete, working example plan to copy and edit. Call this FIRST when
        writing a plan — the `plan` argument elsewhere is a free-form object, so this
        is what tells you the format.

        "functional" runs its steps once. "sweep" repeats them across one axis.
        "shmoo" repeats them across the cartesian product of two or more axes.

        Also returns `actions` (what a step may call) and `limit_ops` (how a limit may
        compare). Unknown fields are rejected, so edit the example rather than
        inventing keys."""
        return {
            "shape": shape,
            "plan": _TEMPLATES[shape],
            "actions": sorted(actions),
            "limit_ops": list(LimitOp.__args__),
            "notes": [
                "loop.axes: omit for a one-shot test, one axis to sweep, two+ to shmoo.",
                "An axis is either from/to/step or an explicit values list.",
                "\"$name\" in any step arg resolves to that axis's current value.",
                "within_pct/within_abs need `of` naming an axis, e.g. \"$vsupply\".",
                "defaults are merged under every step's args; a step may override them.",
                "setup and cleanup run outside the loop, so they cannot use $refs.",
                "Call validate_plan before create_plan to see point count and duration.",
            ],
        }

    @mcp.tool()
    def validate_plan(plan: dict) -> dict:
        """Check a plan without running it or storing it. Start from plan_template if
        you have not written one before — guessing at field names wastes calls, and
        unknown keys are rejected rather than ignored.

        Returns valid/errors/warnings, the point count, a duration estimate, and a
        plain-language summary to read back to the engineer before they commit. Every
        step is checked against the real device: that the action exists, that the
        module can actually perform it, that the channels are present, and that no
        voltage it could reach — including both ends of a swept axis — falls outside
        the module's output range.

        Only read-only queries touch the hardware. With no rig reachable the plan is
        still checked structurally and the hardware checks are downgraded to a
        warning, so plans can be authored away from the bench."""
        return validate(plan, actions, _hw())

    @mcp.tool()
    def create_plan(plan: dict) -> dict:
        """Validate a plan and store it as data/plans/<plan_id>.json. See
        plan_template for the format. Refuses an
        invalid plan, and refuses to overwrite an existing plan_id — use update_plan
        for that. Returns the validation report alongside the path."""
        report = validate(plan, actions, _hw())
        if not report["valid"]:
            raise ValueError("plan is invalid: " + "; ".join(report["errors"]))
        if plan_store.exists(plan["plan_id"]):
            raise ValueError(f"plan_id {plan['plan_id']!r} already exists; "
                             "use update_plan to change it")
        path = plan_store.save(plan)
        return {"plan_id": plan["plan_id"], "path": str(path), "report": report}

    @mcp.tool()
    def update_plan(plan: dict) -> dict:
        """Replace an existing plan with a new revision. Validates first, so a plan on
        disk is always one that passed. The plan_id must already exist."""
        plan_id = plan.get("plan_id")
        if not plan_id or not plan_store.exists(plan_id):
            raise ValueError(f"unknown plan_id: {plan_id!r}")
        report = validate(plan, actions, _hw())
        if not report["valid"]:
            raise ValueError("plan is invalid: " + "; ".join(report["errors"]))
        path = plan_store.save(plan)
        return {"plan_id": plan_id, "path": str(path), "report": report}

    @mcp.tool()
    def get_plan(plan_id: str) -> dict:
        """Return one stored plan in full."""
        plan = plan_store.load(plan_id)
        if plan is None:
            raise ValueError(f"unknown plan_id: {plan_id}")
        return {"plan": plan}

    @mcp.tool()
    def list_plans() -> dict:
        """Summaries of every stored plan, most recently edited first — id, title,
        axes, and step count, but not the full document."""
        return {"plans": plan_store.list_all()}

    @mcp.tool()
    def delete_plan(plan_id: str) -> dict:
        """Delete a stored plan. Run records keep their own copy of the plan they
        executed, so past results stay readable."""
        return {"plan_id": plan_id, "deleted": plan_store.delete(plan_id)}
