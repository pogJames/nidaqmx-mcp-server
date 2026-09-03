import asyncio
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, TypeVar

import grpc
import nidaqmx
import numpy as np
from mcp.server.fastmcp import FastMCP
from nidaqmx.grpc_session_options import GrpcSessionOptions, SessionInitializationBehavior
from nidaqmx.constants import (
    AccelUnits,
    AcquisitionType,
    ADCTimingMode,
    CJCSource,
    Coupling,
    CountDirection,
    CurrentShuntResistorLocation,
    CurrentUnits,
    Edge,
    ExcitationSource,
    LineGrouping,
    OverwriteMode,
    ReadRelativeTo,
    RegenerationMode,
    ResistanceConfiguration,
    RTDType,
    TemperatureUnits,
    TerminalConfiguration,
    ThermocoupleType,
)
from pydantic import BaseModel
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse

import config
import plans
import reports
import runner
import systemlink
import vib_report
import vibration
from systemlink import upload_samples

mcp = FastMCP("NI-DAQmx", host="0.0.0.0", port=80)
channel = grpc.insecure_channel(config.GRPC_TARGET)

DASHBOARD_URL = config.DASHBOARD_URL

_INDEX_HTML = ((Path(__file__).parent / "index.html").read_text(encoding="utf-8")
               .replace("/*{{THEME}}*/", reports._CSS))


# SHARED UTILITIES ================================================================

def _grpc(name: str) -> GrpcSessionOptions:
    return GrpcSessionOptions(grpc_channel=channel, session_name=name)

def _clear_stale_task(name: str):
    opts = GrpcSessionOptions(grpc_channel=channel, session_name=name,
        initialization_behavior=SessionInitializationBehavior.ATTACH_TO_SERVER_SESSION)
    try:
        nidaqmx.Task(new_task_name=name, grpc_options=opts).close()
    except nidaqmx.errors.Error:
        pass

def _new_task(name: str):
    """Create a task under its own gRPC session (the session name must match the task
    name). A failure usually means a previous process died still holding that session,
    so clear it and retry once — costs nothing on the happy path.

    Every hardware tool builds its task here, so this is the one place the run lease has
    to be checked: introspection never reaches it and stays available during a run."""
    runner.LEASE.check()
    try:
        return nidaqmx.Task(new_task_name=name, grpc_options=_grpc(name))
    except nidaqmx.errors.Error:
        _clear_stale_task(name)
        return nidaqmx.Task(new_task_name=name, grpc_options=_grpc(name))

@contextmanager
def _task(name: str):
    """A finite task: created, used, always closed. Leaving no hardware state behind
    is what makes the tools built on this safe to use as plan steps."""
    task = _new_task(name)
    try:
        yield task
    finally:
        task.close()

def _phys(dev: str, channel: str) -> str:
    return f"{dev}/{channel}"

def _system():
    return nidaqmx.system.System(grpc_options=_grpc("mcp_system"))

def _stats(samples) -> dict:
    a = np.asarray(samples, dtype=float).ravel()
    mn, mx = float(a.min()), float(a.max())
    return {
        "n": int(a.size),
        "mean": float(a.mean()),
        "min": mn, "max": mx,
        "std": float(a.std()),
        "rms": float(np.sqrt(np.mean(a ** 2))),
        "peak": max(abs(mn), abs(mx)),
    }

_M = TypeVar("_M", bound=BaseModel)


def _cfg(model: type[_M], value: Any) -> _M:
    """Accept a config block as either the model or a plain dict, defaulting when it is
    omitted. MCP coerces arguments against the schema on its way in, but the plan
    runner calls these tools directly with args straight out of plan JSON, so the same
    coercion has to happen here."""
    return model() if value is None else model.model_validate(value)


def _rows(data, channels: list[str]) -> list:
    """nidaqmx returns a flat list for one channel and a list-of-lists for several."""
    return [data] if len(channels) == 1 else list(data)

def _per_channel(data, channels: list[str]) -> dict:
    """Stats keyed by channel name — always a mapping, even for a single channel, so
    nothing downstream has to branch on channel count to read a result."""
    return {ch: _stats(row) for ch, row in zip(channels, _rows(data, channels))}

def _fft(samples, rate: float, bins: int) -> dict | None:
    """Single-sided amplitude spectrum. Hann-windowed with a coherent-gain correction,
    so a 1 g tone reads 1 g rather than an arbitrary bin height. `mags[i]` is the bin at
    `i * df` Hz; bin 0 is zeroed because DC is the channel's offset, not vibration, and
    would otherwise swamp the plot's autoscale and win `peak`.

    Thinned to at most `bins` for transport by taking the tallest bin of each group —
    a stride would silently delete every peak that landed on the wrong index. `peak` is
    measured before thinning, so it keeps full resolution however coarse the curve is."""
    a = np.asarray(samples, dtype=float).ravel()
    if a.size < 16:
        return None
    a = a - a.mean()
    w = np.hanning(a.size)
    mag = 2.0 * np.abs(np.fft.rfft(a * w)) / w.sum()
    df = rate / a.size
    i = int(np.argmax(mag))
    peak = {"freq": i * df, "mag": float(mag[i])}
    k = max(mag.size // bins, 1)
    if k > 1:
        mag = mag[:mag.size // k * k].reshape(-1, k).max(1)
    return {
        "df": df * k,
        "mags": np.round(mag, 6).tolist(),
        "peak": peak,
    }


# HELD-STATE REGISTRY =============================================================
# Anything that stays open after a tool returns lives here, keyed by role. The role
# doubles as the gRPC session name, so a session orphaned by a dead process is always
# clearable by the same key. One registry means get_status and stop stay honest as
# new held-state tools are added.

_HELD: dict[str, dict] = {}
_HELD_LOCK = threading.Lock()


def _public(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k != "task"}


def _peek(role: str) -> dict | None:
    with _HELD_LOCK:
        return _HELD.get(role)


def _hold(role: str, task, **info) -> dict:
    """Register a task that outlives the call, replacing any previous holder."""
    _release(role)
    with _HELD_LOCK:
        _HELD[role] = {"task": task, **info}
        return _public(_HELD[role])


def _release(role: str) -> dict | None:
    with _HELD_LOCK:
        entry = _HELD.pop(role, None)
    if entry is None:
        return None
    entry["task"].close()
    return _public(entry)


def _claim(role: str) -> None:
    """Free a role before building a new task for it.

    Closes an in-process holder, or clears a session orphaned by a previous process.
    That second case matters: creating a task whose session already exists *attaches*
    to it rather than failing, so a task left running by a dead process is inherited
    and only reveals itself later when configuring it raises -200479. Clearing first
    is the difference between a clean restart and a server that has to be restarted."""
    if _release(role) is None:
        _clear_stale_task(role)


def _release_prefix(prefix: str) -> list[str]:
    with _HELD_LOCK:
        roles = [r for r in _HELD if r.startswith(prefix)]
    return [r for r in roles if _release(r) is not None]


# SYSTEM TOOLS ====================================================================

@mcp.tool()
def list_devices() -> dict:
    """List NI-DAQmx devices visible to the gRPC server."""
    return {"devices": [d.name for d in _system().devices]}


def _cap(fn, default=None):
    """Read one capability attribute. A device that lacks the property raises rather
    than returning empty, and "this module has no AO" is an answer, not a failure —
    so an unsupported attribute reads as `default` and is dropped by `_drop_empty`."""
    try:
        return fn()
    except nidaqmx.errors.DaqError:
        return default


def _names(vals) -> list[str]:
    return [v.name for v in vals or []]


def _drop_empty(d: dict) -> dict:
    """Omit attributes the device doesn't support, so the reported keys are exactly
    the constraints that apply to it."""
    return {k: v for k, v in d.items() if v not in (None, [], {})}


def _teds(pc) -> dict | None:
    """TEDS identity of the sensor wired to a channel, or None when there isn't one
    (DAQmx raises rather than reporting an empty sensor)."""
    if _cap(lambda: pc.teds_mfg_id) is None:
        return None
    return {"mfg_id": pc.teds_mfg_id, "model": pc.teds_model_num,
            "serial": pc.teds_serial_num}


def _ai_caps(d) -> dict:
    """AI constraints: the same set NI MAX greys its dialogs from. Terminal configs
    are read off the first physical channel — they're a property of the channel, not
    the device, but no module in this family mixes them across its own channels."""
    chans = list(d.ai_physical_chans)
    if not chans:
        return {}
    teds = {c.name: t for c in chans if (t := _teds(c))}
    return _drop_empty({
        "channels": [c.name for c in chans],
        "meas_types": _names(_cap(lambda: d.ai_meas_types)),
        "terminal_configs": _names(_cap(lambda: chans[0].ai_term_cfgs)),
        "couplings": _names(_cap(lambda: d.ai_couplings)),
        "voltage_rngs": _cap(lambda: list(d.ai_voltage_rngs)),
        "current_rngs": _cap(lambda: list(d.ai_current_rngs)),
        "bridge_rngs": _cap(lambda: list(d.ai_bridge_rngs)),
        "resistance_rngs": _cap(lambda: list(d.ai_resistance_rngs)),
        "min_rate": _cap(lambda: d.ai_min_rate),
        "max_single_chan_rate": _cap(lambda: d.ai_max_single_chan_rate),
        "max_multi_chan_rate": _cap(lambda: d.ai_max_multi_chan_rate),
        "simultaneous": _cap(lambda: d.ai_simultaneous_sampling_supported),
        "samp_modes": _names(_cap(lambda: d.ai_samp_modes)),
        "trig_usage": _names(_cap(lambda: d.ai_trig_usage)),
        "teds": teds,
    })


def _ao_caps(d) -> dict:
    chans = list(d.ao_physical_chans)
    if not chans:
        return {}
    return _drop_empty({
        "channels": [c.name for c in chans],
        "output_types": _names(_cap(lambda: d.ao_output_types)),
        "voltage_rngs": _cap(lambda: list(d.ao_voltage_rngs)),
        "current_rngs": _cap(lambda: list(d.ao_current_rngs)),
        "min_rate": _cap(lambda: d.ao_min_rate),
        "max_rate": _cap(lambda: d.ao_max_rate),
        "trig_usage": _names(_cap(lambda: d.ao_trig_usage)),
    })


@mcp.tool()
def get_device_info(dev: str) -> dict:
    """Full capability report for one device — the constraints NI MAX greys its
    dialogs from, so a test plan can be checked before it touches hardware:
    supported measurement types, terminal configurations, couplings, input/output
    ranges, rate limits, trigger and sampling modes, and any TEDS sensor found.
    Only the keys the device actually supports are present.

    If `dev` is a cDAQ chassis, this returns just its module list (name, product
    type, slot) — the modules carry the channels, so call this again with a module
    name to get that module's constraints. A chassis that enumerates no modules says
    so and names the fix, rather than looking like a device with no capabilities."""
    d = _system().devices[dev]
    category = _cap(lambda: d.product_category.name)
    info = {
        "device": d.name,
        "product_type": d.product_type,
        "product_category": category,
        "serial_number": d.serial_num,
    }

    # Branch on what the device is, not on how many modules it happened to report. A
    # chassis that has lost its enumeration returns an empty list, and falling through
    # to the leaf branch would describe it as a device with no AI, AO, or counters —
    # which reads to a plan validator as a real capability gap rather than a chassis
    # that needs re-reserving.
    modules = list(d.chassis_module_devices)
    if category == "COMPACT_DAQ_CHASSIS" or modules:
        report = {**info, "chassis": True, "modules": [
            _drop_empty({"device": m.name, "product_type": m.product_type,
                         "slot": _cap(lambda: m.compact_daq_slot_num)})
            for m in modules]}
        if not modules:
            report["warning"] = (
                f"no modules enumerated — the chassis is reachable but its slots are "
                f"not readable. Try restart_device({dev!r}); until then any plan "
                f"targeting its modules will look unsupported.")
        return report

    return _drop_empty({
        **info,
        "slot": _cap(lambda: d.compact_daq_slot_num),
        "ai": _ai_caps(d),
        "ao": _ao_caps(d),
        "ci_meas_types": _names(_cap(lambda: d.ci_meas_types)),
        "co_output_types": _names(_cap(lambda: d.co_output_types)),
        "di_lines": _cap(lambda: len(d.di_lines)),
        "do_lines": _cap(lambda: len(d.do_lines)),
        "dig_trig_supported": _cap(lambda: d.dig_trig_supported),
        "self_cal_supported": _cap(lambda: d.self_cal_supported),
    })


@mcp.tool()
def restart_device(dev: str) -> dict:
    """Reset a network device's reservation (e.g. an unresponsive cDAQ chassis) by
    unreserving then force-reserving it. Use when a device stops responding without
    a full driver/task reset."""
    d = _system().devices[dev]
    d.unreserve_network_device()
    d.reserve_network_device(override_reservation=True)
    return {"device": dev, "restarted": True}


@mcp.tool()
def get_status() -> dict:
    """Everything the server is currently holding open: monitors, held DC levels,
    digital lines, continuous generation. `idle` means the hardware is clear — call
    this to confirm a clean teardown after a test."""
    with _HELD_LOCK:
        held = {role: _public(entry) for role, entry in _HELD.items()}
    with _REC_LOCK:
        recording = _rec_status(_REC)
    return {"held": held, "count": len(held),
            "idle": not held and recording is None,
            "recording": recording, "dashboard": DASHBOARD_URL}


# CAPABILITY CHECKING =============================================================
# What each hardware tool needs from the device it targets, expressed in the same
# DAQmx capability names get_device_info reports. This is the table plan validation
# reads: every step names a tool and a device, so a plan can be checked against real
# hardware without driving a single output.

HardwareTool = Literal[
    "measure_voltage", "measure_current", "measure_temperature", "read_digital",
    "count_edges", "measure_frequency", "set_voltage", "set_digital",
    "set_waveform", "pulse", "start_monitor",
]

# tool -> subsystem -> any one of these capabilities satisfies it
_REQUIRES: dict[str, dict[str, tuple[str, ...]]] = {
    "measure_voltage":     {"ai": ("VOLTAGE",)},
    "measure_current":     {"ai": ("CURRENT",)},
    "measure_temperature": {"ai": ("TEMPERATURE_THERMOCOUPLE", "TEMPERATURE_RTD")},
    "read_digital":        {"di": ("LINES",)},
    "count_edges":         {"ci": ("COUNT_EDGES",)},
    "measure_frequency":   {"ci": ("FREQUENCY",)},
    "set_voltage":         {"ao": ("VOLTAGE",)},
    "set_digital":         {"do": ("LINES",)},
    "set_waveform":        {"ao": ("VOLTAGE",)},
    "pulse":               {"co": ("PULSE_FREQUENCY",)},
    "start_monitor":       {"ai": ("ACCELERATION_ACCELEROMETER_CURRENT_INPUT",
                                   "VOLTAGE")},
}


def _capabilities(d) -> dict[str, set[str]]:
    """What each subsystem of a device can do, as DAQmx capability names. Digital has
    no measurement types, so the presence of lines stands in as "LINES"."""
    return {
        "ai": set(_names(_cap(lambda: d.ai_meas_types))),
        "ao": set(_names(_cap(lambda: d.ao_output_types))),
        "ci": set(_names(_cap(lambda: d.ci_meas_types))),
        "co": set(_names(_cap(lambda: d.co_output_types))),
        "di": {"LINES"} if _cap(lambda: len(d.di_lines)) else set(),
        "do": {"LINES"} if _cap(lambda: len(d.do_lines)) else set(),
    }


@mcp.tool()
def check_support(dev: str, tools: list[HardwareTool] | None = None) -> dict:
    """Which hardware tools this device can actually run, and why not for the rest.

    Cross-references each tool's required DAQmx capability against what the device
    reports, so a test plan can be checked before it touches hardware. Omit `tools` to
    report on all of them.

    A cDAQ chassis carries no channels of its own, so it reports itself as a chassis
    and lists its modules to target instead of reporting every tool unsupported."""
    d = _system().devices[dev]
    if _cap(lambda: d.product_category.name) == "COMPACT_DAQ_CHASSIS":
        modules = [m.name for m in d.chassis_module_devices]
        return {
            "device": dev, "product_type": d.product_type, "chassis": True,
            "capabilities": {}, "tools": {}, "supported": [], "unsupported": [],
            "modules": modules,
            "note": (f"{dev} is a chassis and carries no channels; check its "
                     f"modules instead ({', '.join(modules)})") if modules else
                    (f"{dev} is a chassis but enumerates no modules — try "
                     f"restart_device({dev!r})"),
        }
    have = _capabilities(d)
    checked = {}
    for name in tools or list(_REQUIRES):
        missing = [f"{sub}:{' or '.join(opts)}"
                   for sub, opts in _REQUIRES[name].items()
                   if not have.get(sub, set()).intersection(opts)]
        checked[name] = {"supported": not missing, "missing": missing}
    return {
        "device": dev,
        "product_type": d.product_type,
        "capabilities": {k: sorted(v) for k, v in have.items() if v},
        "tools": checked,
        "supported": sorted(n for n, r in checked.items() if r["supported"]),
        "unsupported": sorted(n for n, r in checked.items() if not r["supported"]),
    }


# MEASUREMENT TOOLS ===============================================================

TermConfig = Literal["diff", "rse", "nrse", "pseudodiff"]

_TERM = {
    "diff": TerminalConfiguration.DIFF,
    "rse": TerminalConfiguration.RSE,
    "nrse": TerminalConfiguration.NRSE,
    "pseudodiff": TerminalConfiguration.PSEUDO_DIFF,
}

EdgeSel = Literal["rising", "falling"]

_EDGE = {"rising": Edge.RISING, "falling": Edge.FALLING}


class VRange(BaseModel):
    """Expected signal range in volts. Narrow it to gain ADC resolution — measuring a
    100 mV signal on the default +/-10 V range throws away about 7 bits. Must sit
    inside the module's `ai.voltage_rngs` from get_device_info."""
    min_v: float = -10.0
    max_v: float = 10.0


@mcp.tool()
def measure_voltage(
    dev: str,
    channels: list[str],
    rate: float = 1000,
    samples: int = 3000,
    terminal_config: TermConfig = "diff",
    v_range: VRange | None = None,
    return_raw: bool = False,
    upload: bool = False,
) -> dict:
    """Finite AI voltage measurement across one or more channels. Creates, reads, and
    clears the task, so it leaves no hardware state behind — this is the deterministic
    measurement to use as a plan step.

    Stats are keyed by channel name. Raw samples only when return_raw=true. Set
    upload=true to write the waveform to TDMS and upload it to SystemLink; the
    result's `systemlink` carries the file_id.

    `terminal_config` must be one the module reports in `ai.terminal_configs`."""
    r = _cfg(VRange, v_range)
    with _task("ai_v") as task:
        for ch in channels:
            task.ai_channels.add_ai_voltage_chan(
                _phys(dev, ch), terminal_config=_TERM[terminal_config],
                min_val=r.min_v, max_val=r.max_v)
        task.timing.cfg_samp_clk_timing(
            rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=samples)
        data = task.read(number_of_samples_per_channel=samples,
                         timeout=samples / rate + 10.0)
    rows = _rows(data, channels)
    return {
        "device": dev, "channels": channels, "rate": rate,
        "terminal_config": terminal_config, "v_range": r.model_dump(),
        "stats": _per_channel(data, channels),
        "raw": {ch: list(row) for ch, row in zip(channels, rows)} if return_raw else None,
        "systemlink": upload_samples(
            rows if len(channels) > 1 else rows[0],
            device=dev, channel=channels[0], names=channels, units="Volts",
            sample_rate=rate) if upload else None,
    }


ShuntLoc = Literal["default", "internal", "external"]

_SHUNT = {
    "default": CurrentShuntResistorLocation.LET_DRIVER_CHOOSE,
    "internal": CurrentShuntResistorLocation.INTERNAL,
    "external": CurrentShuntResistorLocation.EXTERNAL,
}


@mcp.tool()
def measure_current(
    dev: str,
    channels: list[str],
    rate: float = 1000,
    samples: int = 3000,
    min_a: float = -0.02,
    max_a: float = 0.02,
    terminal_config: TermConfig = "diff",
    shunt: ShuntLoc = "default",
    ext_shunt_ohms: float = 249.0,
    return_raw: bool = False,
    upload: bool = False,
) -> dict:
    """Finite AI current measurement, in amps. Creates, reads, and clears the task.

    Defaults span a 4-20 mA loop. `shunt="default"` lets the driver use the module's
    built-in shunt; pass "external" with `ext_shunt_ohms` when the sense resistor is
    wired outside. Requires a module reporting `ai: CURRENT` — check_support says
    whether yours does.

    upload=true writes the waveform to TDMS and uploads it to SystemLink."""
    with _task("ai_i") as task:
        for ch in channels:
            task.ai_channels.add_ai_current_chan(
                _phys(dev, ch), terminal_config=_TERM[terminal_config],
                min_val=min_a, max_val=max_a, units=CurrentUnits.AMPS,
                shunt_resistor_loc=_SHUNT[shunt], ext_shunt_resistor_val=ext_shunt_ohms)
        task.timing.cfg_samp_clk_timing(
            rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=samples)
        data = task.read(number_of_samples_per_channel=samples,
                         timeout=samples / rate + 10.0)
    rows = _rows(data, channels)
    return {
        "device": dev, "channels": channels, "rate": rate, "units": "A",
        "range_a": {"min": min_a, "max": max_a}, "shunt": shunt,
        "stats": _per_channel(data, channels),
        "raw": {ch: list(row) for ch, row in zip(channels, rows)} if return_raw else None,
        "systemlink": upload_samples(
            rows if len(channels) > 1 else rows[0],
            device=dev, channel=channels[0], names=channels, units="Amps",
            sample_rate=rate) if upload else None,
    }


TempSensor = Literal["thermocouple", "rtd"]

TcType = Literal["J", "K", "N", "R", "S", "T", "B", "E"]

_TC = {t: getattr(ThermocoupleType, t) for t in ("J", "K", "N", "R", "S", "T", "B", "E")}

RtdType = Literal["pt3750", "pt3851", "pt3911", "pt3916", "pt3920", "pt3928"]

_RTD = {
    "pt3750": RTDType.PT_3750, "pt3851": RTDType.PT_3851, "pt3911": RTDType.PT_3911,
    "pt3916": RTDType.PT_3916, "pt3920": RTDType.PT_3920, "pt3928": RTDType.PT_3928,
}

_WIRES = {
    2: ResistanceConfiguration.TWO_WIRE,
    3: ResistanceConfiguration.THREE_WIRE,
    4: ResistanceConfiguration.FOUR_WIRE,
}


class Thermocouple(BaseModel):
    """Thermocouple wiring. `cjc_c` is the cold-junction reference temperature, used
    only when the module has no built-in CJC sensor — most cDAQ temperature modules
    do, so leave `cjc_builtin` true unless yours doesn't report one."""
    tc_type: TcType = "K"
    cjc_builtin: bool = True
    cjc_c: float = 25.0


class Rtd(BaseModel):
    """RTD wiring. `r_0` is the nominal resistance at 0 C (100 ohm for a Pt100).
    3-wire is the usual compromise; 4-wire cancels lead resistance entirely."""
    rtd_type: RtdType = "pt3851"
    r_0: float = 100.0
    wires: Literal[2, 3, 4] = 3
    excit_a: float = 0.0025


@mcp.tool()
def measure_temperature(
    dev: str,
    channels: list[str],
    sensor: TempSensor = "thermocouple",
    thermocouple: Thermocouple | None = None,
    rtd: Rtd | None = None,
    min_c: float = 0.0,
    max_c: float = 100.0,
    rate: float = 10,
    samples: int = 10,
    return_raw: bool = False,
    upload: bool = False,
) -> dict:
    """Finite AI temperature measurement, in degrees Celsius. Creates, reads, and
    clears the task.

    `sensor` picks which wiring block applies — `thermocouple` or `rtd`; the other is
    ignored. Both default to the common case (type-K with built-in CJC, or a 3-wire
    Pt100 at 3851). Temperature channels are slow by nature, so the default rate is
    10 S/s rather than 1 kS/s.

    Requires a module reporting `ai: TEMPERATURE_THERMOCOUPLE` or `TEMPERATURE_RTD`.

    upload=true writes the waveform to TDMS and uploads it to SystemLink."""
    tc, rt = _cfg(Thermocouple, thermocouple), _cfg(Rtd, rtd)
    with _task("ai_temp") as task:
        for ch in channels:
            if sensor == "thermocouple":
                task.ai_channels.add_ai_thrmcpl_chan(
                    _phys(dev, ch), min_val=min_c, max_val=max_c,
                    units=TemperatureUnits.DEG_C, thermocouple_type=_TC[tc.tc_type],
                    cjc_source=CJCSource.BUILT_IN if tc.cjc_builtin
                    else CJCSource.CONSTANT_USER_VALUE, cjc_val=tc.cjc_c)
            else:
                task.ai_channels.add_ai_rtd_chan(
                    _phys(dev, ch), min_val=min_c, max_val=max_c,
                    units=TemperatureUnits.DEG_C, rtd_type=_RTD[rt.rtd_type],
                    resistance_config=_WIRES[rt.wires],
                    current_excit_source=ExcitationSource.INTERNAL,
                    current_excit_val=rt.excit_a, r_0=rt.r_0)
        task.timing.cfg_samp_clk_timing(
            rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=samples)
        data = task.read(number_of_samples_per_channel=samples,
                         timeout=samples / rate + 10.0)
    rows = _rows(data, channels)
    return {
        "device": dev, "channels": channels, "rate": rate, "units": "degC",
        "sensor": sensor,
        "wiring": (tc if sensor == "thermocouple" else rt).model_dump(),
        "stats": _per_channel(data, channels),
        "raw": {ch: list(row) for ch, row in zip(channels, rows)} if return_raw else None,
        "systemlink": upload_samples(
            rows if len(channels) > 1 else rows[0],
            device=dev, channel=channels[0], names=channels, units="degC",
            sample_rate=rate) if upload else None,
    }


@mcp.tool()
def read_digital(dev: str, lines: str) -> dict:
    """Read digital input. `lines` is either a single line ("port0/line0", returns a
    bool) or a whole port ("port0", returns an integer bitmask). Finite: opens the
    task, reads, closes."""
    per_line = "line" in lines
    with _task("di_read") as task:
        task.di_channels.add_di_chan(
            _phys(dev, lines),
            line_grouping=LineGrouping.CHAN_PER_LINE if per_line
            else LineGrouping.CHAN_FOR_ALL_LINES)
        value = task.read()
    return {"device": dev, "lines": lines, "as_port": not per_line,
            "value": bool(value) if per_line else int(value)}


@mcp.tool()
def count_edges(
    dev: str,
    counter: str = "ctr0",
    duration_s: float = 1.0,
    edge: EdgeSel = "rising",
) -> dict:
    """Count edges on a counter input for `duration_s`, then clear the task. Returns
    the raw count and the implied average rate. Blocks for the duration."""
    with _task("ci_count") as task:
        task.ci_channels.add_ci_count_edges_chan(
            _phys(dev, counter), edge=_EDGE[edge],
            initial_count=0, count_direction=CountDirection.COUNT_UP)
        task.start()
        time.sleep(duration_s)
        count = int(task.read())
    return {"device": dev, "counter": counter, "edge": edge,
            "duration_s": duration_s, "count": count,
            "rate_hz": count / duration_s if duration_s else 0.0}


@mcp.tool()
def measure_frequency(
    dev: str,
    counter: str = "ctr0",
    min_hz: float = 2.0,
    max_hz: float = 1_000_000.0,
    edge: EdgeSel = "rising",
) -> dict:
    """Measure the frequency of a signal on a counter input. `min_hz`/`max_hz` bound
    the expected range — the driver picks its measurement method from them, so a tight
    range measures faster and more precisely than a wide one."""
    with _task("ci_freq") as task:
        task.ci_channels.add_ci_freq_chan(
            _phys(dev, counter), min_val=min_hz, max_val=max_hz, edge=_EDGE[edge])
        task.start()
        hz = float(task.read(timeout=10.0))
    return {"device": dev, "counter": counter, "edge": edge,
            "frequency_hz": hz, "period_s": 1.0 / hz if hz else None}


# STIMULUS TOOLS ==================================================================

WaveKind = Literal["sine", "square", "triangle", "sawtooth"]

_WAVE = {
    "sine": lambda p: np.sin(p),
    "square": lambda p: np.sign(np.sin(p)),
    "triangle": lambda p: 2 / np.pi * np.arcsin(np.sin(p)),
    "sawtooth": lambda p: 2 * ((p / (2 * np.pi)) % 1.0) - 1,
}


def _wave(kind: WaveKind, frequency: float, amplitude: float, offset: float,
          rate: float, n: int) -> np.ndarray:
    phase = 2 * np.pi * frequency * (np.arange(n) / rate)
    return offset + amplitude * _WAVE[kind](phase)


@mcp.tool()
def set_voltage(dev: str, channel: str, volts: float = 0.0) -> dict:
    """Drive a DC voltage and hold it. The task stays open, so the output stays at
    `volts` until changed or released with stop("outputs").

    Repeated calls on the same channel write through the existing task instead of
    rebuilding it, so a sweep can step its setpoint without glitching the output."""
    phys = _phys(dev, channel)
    with _HELD_LOCK:
        current = _HELD.get("ao_dc")
        if current is not None and current["phys"] == phys:
            current["task"].write(volts)
            current["volts"] = volts
            return _public(current)

    _claim("ao_dc")
    task = _new_task("ao_dc")
    try:
        task.ao_channels.add_ao_voltage_chan(phys)
        task.write(volts)
    except Exception:
        task.close()
        raise
    return _hold("ao_dc", task, device=dev, channel=channel, phys=phys, volts=volts)


@mcp.tool()
def set_digital(dev: str, lines: str, value: int) -> dict:
    """Drive a digital output and hold it. `lines` is either a single line
    ("port0/line0", value 0 or 1) or a whole port ("port0", value as an integer
    bitmask).

    Each line spec is held under its own role, so an enable set by an earlier step
    stays set while a later step drives a different line. stop("outputs") releases
    all of them."""
    per_line = "line" in lines
    role = f"do_{dev}_{lines}".replace("/", "_")
    level = bool(value) if per_line else int(value)
    with _HELD_LOCK:
        current = _HELD.get(role)
        if current is not None:
            current["task"].write(level)
            current["value"] = value
            return _public(current)

    _claim(role)
    task = _new_task(role)
    try:
        task.do_channels.add_do_chan(
            _phys(dev, lines),
            line_grouping=LineGrouping.CHAN_PER_LINE if per_line
            else LineGrouping.CHAN_FOR_ALL_LINES)
        task.write(level)
    except Exception:
        task.close()
        raise
    return _hold(role, task, device=dev, lines=lines, value=value,
                 as_port=not per_line)


@mcp.tool()
def set_waveform(
    dev: str,
    channel: str,
    kind: WaveKind = "sine",
    amplitude: float = 1.0,
    offset: float = 0.0,
    frequency: float = 1.0,
    rate: float = 1000.0,
) -> dict:
    """Drive an AC signal and hold it. One cycle is buffered and regenerated by the
    driver for a glitch-free continuous loop. Replaces any existing generation;
    release it with stop("outputs").

    Held like set_voltage and set_digital, and deterministic in the same way — the
    output is whatever was commanded — so this is the stimulus for a plan whose axis
    is frequency or amplitude, held steady while each point is measured."""
    n = max(int(rate / frequency), 2)
    _claim("ao_gen")
    task = _new_task("ao_gen")
    try:
        task.ao_channels.add_ao_voltage_chan(_phys(dev, channel))
        task.timing.cfg_samp_clk_timing(
            rate, sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=n)
        task.out_stream.regen_mode = RegenerationMode.ALLOW_REGENERATION
        task.write(_wave(kind, frequency, amplitude, offset, rate, n), auto_start=False)
        task.start()
    except Exception:
        task.close()
        raise
    return _hold("ao_gen", task, generating=True, device=dev, channel=channel,
                 kind=kind, amplitude=amplitude, offset=offset,
                 frequency=frequency, rate=rate)


@mcp.tool()
def pulse(
    dev: str,
    counter: str = "ctr0",
    frequency: float = 1000.0,
    duty_cycle: float = 0.5,
    count: int = 1,
) -> dict:
    """Emit a finite pulse train on a counter output, then clear the task. Blocks
    until the pulses are done. Use for triggers and single-shot stimulus."""
    with _task("co_pulse") as task:
        task.co_channels.add_co_pulse_chan_freq(
            _phys(dev, counter), freq=frequency, duty_cycle=duty_cycle)
        task.timing.cfg_implicit_timing(
            sample_mode=AcquisitionType.FINITE, samps_per_chan=count)
        task.start()
        task.wait_until_done(timeout=count / frequency + 10.0)
    return {"device": dev, "counter": counter, "frequency": frequency,
            "duty_cycle": duty_cycle, "count": count,
            "duration_s": count / frequency}


# LIFECYCLE =======================================================================

StopTarget = Literal["monitor", "outputs", "all"]

# Role prefixes each target releases. Outputs covers held DC, continuous generation,
# and every digital line, so a plan's cleanup step is one call.
_STOP_PREFIX: dict[str, tuple[str, ...]] = {
    "monitor": ("ai_monitor",),
    "outputs": ("ao_dc", "ao_gen", "do_"),
    "all": ("",),
}


@mcp.tool()
def stop(target: StopTarget = "all") -> dict:
    """Release held hardware. "monitor" stops continuous acquisition; "outputs" drops
    held DC levels, digital lines, and continuous generation; "all" clears everything.

    Safe to call when nothing is held — it reports only what it actually released."""
    # A recording reads the monitor's task; closing it out from under an in-flight read
    # would fault. Halt the recorder first, and say so rather than dropping its samples
    # silently — they are the one thing here that cannot be re-acquired.
    recorded = None
    if target in ("monitor", "all"):
        rec = _stop_recording()
        if rec is not None:
            recorded = {**_rec_status(rec), "discarded": True,
                        "note": "stopped by stop(); samples were not uploaded"}

    released = [role for p in _STOP_PREFIX[target] for role in _release_prefix(p)]
    return {"target": target, "released": released, "count": len(released),
            "recording": recorded}


# MONITORING / STREAMING ==========================================================

# Accelerometer sample rates the module supports; voltage mode is fixed slow.
AccelRate = Literal[3200, 6400, 12800, 25600, 51200]


def _add_ai_chan(task, dev: str, channel: str, accel: bool):
    """Add one AI channel in one of two modes:
    accel=True  → IEPE accelerometer via TEDS: units in g, sensitivity read from the
                  sensor's chip, IEPE excitation on, AC-coupled (DC raises -201172).
    accel=False → plain voltage: raw volts, no excitation."""
    phys = _phys(dev, channel)
    if accel:
        chan = task.ai_channels.add_teds_ai_accel_chan(phys, units=AccelUnits.G,
            current_excit_source=ExcitationSource.INTERNAL, current_excit_val=0.004)
        chan.ai_coupling = Coupling.AC
    else:
        chan = task.ai_channels.add_ai_voltage_chan(
            phys, terminal_config=TerminalConfiguration.DIFF)
        chan.ai_adc_timing_mode = ADCTimingMode.HIGH_SPEED
    return chan


def _read_latest(seconds: float):
    with _HELD_LOCK:
        mon = _HELD.get("ai_monitor")
        if mon is None:
            return None
        task = mon["task"]
        acquired = task.in_stream.total_samp_per_chan_acquired
        n = min(int(seconds * mon["rate"]), acquired)
        if n <= 0:
            return None
        task.in_stream.relative_to = ReadRelativeTo.MOST_RECENT_SAMPLE
        task.in_stream.offset = -n
        return task.read(number_of_samples_per_channel=n, timeout=0)


@mcp.tool()
def start_monitor(
    dev: str = "DAQ1Mod2",
    channel: str = "ai0",
    accel: bool = True,
    rate: AccelRate = 12800,
) -> dict:
    """Start continuous AI monitoring on a channel. The driver keeps a rolling
    buffer (overwrite mode) so it never overflows. Replaces any existing monitor.

    Returns `dashboard`, the URL of the live chart. Give that link to the user so they
    can watch the signal — there is nothing to see until a monitor is running, and the
    page is the point of starting one. `stream` is the raw SSE feed behind it.

    Exploratory, not a plan step: it has no natural end, and what it returns depends
    on when you ask. Use measure_voltage for a deterministic reading, or read_latest
    for a snapshot of this monitor.

    accel=True (default) reads an IEPE/TEDS accelerometer in g at `rate` Hz;
    accel=False reads raw voltage, always at 100 Hz (`rate` is ignored)."""
    _claim("ai_monitor")
    task = _new_task("ai_monitor")
    rate = float(rate) if accel else 100.0
    try:
        _add_ai_chan(task, dev, channel, accel)
        task.timing.cfg_samp_clk_timing(rate, sample_mode=AcquisitionType.CONTINUOUS)
        task.in_stream.overwrite = OverwriteMode.OVERWRITE_UNREAD_SAMPLES
        task.start()
    except Exception:
        task.close()
        raise
    return _hold("ai_monitor", task, monitoring=True, device=dev, channel=channel,
                 rate=rate, units="g" if accel else "V",
                 dashboard=DASHBOARD_URL, stream=f"{DASHBOARD_URL}/stream")


@mcp.tool()
def read_latest(seconds: float = 1.0) -> dict:
    """Snapshot the most recent `seconds` of data from the active monitor. The sample
    count is derived from the monitor's current rate. Stats + raw, non-blocking."""
    data = _read_latest(seconds)
    if data is None:
        return {"monitoring": False}
    return {"monitoring": True, "seconds": seconds, "stats": _stats(data), "raw": list(data)}


# RECORDING =======================================================================
# A recording consumes the monitor's own buffer rather than starting a second task.
# That is not just to dodge a reservation conflict — a separate task would capture a
# different window, at its own rate, in its own units, which is not "record what I am
# looking at". Reading sequentially from the live task captures exactly the samples
# the dashboard is drawing.
#
# The state deliberately does not live in _HELD: _release() closes an entry's task,
# and the recorder borrows the monitor's rather than owning one.

_REC: dict | None = None
_REC_LOCK = threading.Lock()
_REC_POLL_S = 0.05


def _read_since(task, next_index: int, buf: int):
    """Samples acquired since `next_index`, in order. Returns (chunk, index, lost).

    Takes _HELD_LOCK because it moves the same task's read pointer that _read_latest
    does; the two interleaving unlocked would corrupt each other's reads.

    The monitor runs OVERWRITE_UNREAD_SAMPLES, so falling more than a buffer behind
    means those samples are gone. That is counted, not silently skipped: a recording
    with an unmarked hole is worse than one that admits the hole."""
    with _HELD_LOCK:
        mon = _HELD.get("ai_monitor")
        if mon is None or mon["task"] is not task:
            return None, next_index, 0
        acquired = task.in_stream.total_samp_per_chan_acquired
        n = acquired - next_index
        if n <= 0:
            return None, next_index, 0
        lost = 0
        if n > buf:
            lost, next_index, n = n - buf, acquired - buf, buf
        task.in_stream.relative_to = ReadRelativeTo.FIRST_SAMPLE
        task.in_stream.offset = next_index
        chunk = task.read(number_of_samples_per_channel=n, timeout=1.0)
    return np.asarray(chunk, dtype=float), next_index + n, lost


def _record_loop(rec: dict) -> None:
    """Drain the monitor into `rec` until stopped, the cap is reached, or the monitor
    goes away. Runs on its own thread so a recording outlives the browser tab."""
    task, buf = rec["task"], rec["buf"]
    while not rec["stop"].is_set():
        chunk, rec["next_index"], lost = _read_since(task, rec["next_index"], buf)
        if chunk is None and _peek("ai_monitor") is None:
            rec["ended"] = "monitor stopped"
            break
        if chunk is not None:
            rec["chunks"].append(chunk)
            rec["samples"] += int(chunk.size)
            rec["lost"] += lost
        if rec["samples"] >= rec["max_samples"]:
            rec["ended"] = "reached max_seconds"
            break
        time.sleep(_REC_POLL_S)
    else:
        rec["ended"] = "stopped"
    rec["done"].set()


def _rec_status(rec: dict | None) -> dict | None:
    if rec is None:
        return None
    return {"device": rec["device"], "channel": rec["channel"], "rate": rec["rate"],
            "units": rec["units"], "samples": rec["samples"],
            "seconds": round(rec["samples"] / rec["rate"], 3),
            "lost_samples": rec["lost"], "max_seconds": rec["max_seconds"],
            "active": not rec["done"].is_set()}


def _stop_recording() -> dict | None:
    """Halt the thread and hand back the state. Called by stop_recording and by stop(),
    which must not close the monitor's task while a read is in flight."""
    global _REC
    with _REC_LOCK:
        rec, _REC = _REC, None
    if rec is None:
        return None
    rec["stop"].set()
    rec["thread"].join(timeout=5.0)
    return rec


@mcp.tool()
def start_recording(max_seconds: float = 300.0) -> dict:
    """Record the running monitor to memory, for upload when stopped.

    Captures the monitor's own samples — same channel, same rate, same units as the
    live chart — so the recording is what you were watching. Requires start_monitor
    first, and replaces any recording already running.

    Stops itself at `max_seconds` so a forgotten recording cannot grow without bound:
    at 12.8 kS/s that default is about 24 MB. Call stop_recording to end it early and
    upload."""
    mon = _peek("ai_monitor")
    if mon is None:
        raise ValueError("no monitor running — call start_monitor first")
    _stop_recording()

    task = mon["task"]
    rec = {
        "task": task, "buf": int(task.in_stream.input_buf_size),
        "next_index": int(task.in_stream.total_samp_per_chan_acquired),
        "chunks": [], "samples": 0, "lost": 0,
        "device": mon["device"], "channel": mon["channel"],
        "rate": mon["rate"], "units": mon["units"],
        "max_seconds": max_seconds,
        "max_samples": int(max_seconds * mon["rate"]),
        "started": time.time(), "ended": None,
        "stop": threading.Event(), "done": threading.Event(),
    }
    rec["thread"] = threading.Thread(target=_record_loop, args=(rec,), daemon=True)
    with _REC_LOCK:
        globals()["_REC"] = rec
    rec["thread"].start()
    return {"recording": True, **_rec_status(rec)}


@mcp.tool()
def stop_recording(upload: bool = True) -> dict:
    """End the recording and upload it to SystemLink as a TDMS file.

    Uploaded as kind=vibration with the monitor's device, channel, rate and units, so
    it lists beside the reference recordings and can be fed straight to
    analyze_vibration. `upload=false` discards the samples and reports what was
    captured — useful to abandon a run without leaving a file behind."""
    rec = _stop_recording()
    if rec is None:
        return {"recording": False, "note": "nothing was recording"}

    status = _rec_status(rec)
    if not rec["chunks"]:
        return {"recording": False, "uploaded": None,
                "note": "no samples captured", **status}
    data = np.concatenate(rec["chunks"])
    if not upload:
        return {"recording": False, "uploaded": None, **status}

    result = upload_samples(
        data, kind="vibration", device=rec["device"], channel=rec["channel"],
        names=[rec["channel"]], units=rec["units"], sample_rate=rec["rate"],
        tags={"created_by": "stop_recording", "lost_samples": rec["lost"],
              "ended": rec["ended"] or "stopped"})
    return {"recording": False, **status,
            "file_id": result["file_id"], "filename": result["filename"]}


@mcp.custom_route("/record/start", methods=["POST"])
async def record_start(request):
    """Start recording, for the dashboard's button."""
    try:
        return JSONResponse(start_recording())
    except Exception as exc:                      # the page shows the reason inline
        return JSONResponse({"error": str(exc)}, status_code=400)


@mcp.custom_route("/record/stop", methods=["POST"])
async def record_stop(request):
    """Stop recording and upload, for the dashboard's button."""
    try:
        return JSONResponse(stop_recording())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@mcp.custom_route("/stream", methods=["GET"])
async def stream(request):
    """Server-Sent Events feed of the live monitor. Emits the latest ~1 s window each
    second when a monitor is active, or an idle heartbeat otherwise (so the transport
    is testable with no acquisition running). get_status reports the dashboard address;
    the feed is that address plus /stream."""
    async def gen():
        tick = 0
        while True:
            if await request.is_disconnected():
                break
            mon = _peek("ai_monitor")
            if mon is not None:
                data = _read_latest(1.0)
                if data is not None:
                    pts = list(data)
                    with _REC_LOCK:
                        rec = _rec_status(_REC)
                    payload = {"type": "samples", "device": mon["device"],
                               "channel": mon["channel"], "rate": mon["rate"],
                               "units": mon["units"], "data": pts[::10],
                               "recording": rec,
                               "fft": _fft(data, mon["rate"], bins=1280)}
                    yield f"data: {json.dumps(payload)}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'idle', 'tick': tick})}\n\n"
            tick += 1
            await asyncio.sleep(0.25)
    return StreamingResponse(gen(), media_type="text/event-stream")


@mcp.custom_route("/", methods=["GET"])
async def index(request):
    """Minimal live-chart dashboard that renders the /stream SSE feed on a canvas."""
    return HTMLResponse(_INDEX_HTML)


# PLAN TOOLS ======================================================================

# A plan step may name any tool with a declared capability requirement — that table is
# what lets validation check a step against real hardware — except start_monitor,
# whose result depends on when you call it rather than on its arguments. stop is added
# for cleanup sections.
PLAN_ACTIONS = (frozenset(_REQUIRES) | {"stop"}) - {"start_monitor"}

# The executor dispatches a plan step by name, and validation inspects the same
# callables to check a step's arguments. One table for both means a step can only reach
# a tool the allowlist vetted, and cannot pass it an argument it does not accept.
_ACTION_TOOLS = {name: globals()[name] for name in PLAN_ACTIONS}

plans.install(
    mcp,
    actions=_ACTION_TOOLS,
    device_info=get_device_info,
    support=lambda dev, tools: check_support(dev, tools),
    devices=list_devices,
)


# RUN TOOLS =======================================================================

runner.install(
    mcp,
    actions=_ACTION_TOOLS,
    teardown=lambda: stop("all"),
    validate=lambda plan: plans.validate(
        plan, _ACTION_TOOLS,
        plans._Hardware(get_device_info, check_support, list_devices)),
)


# REPORT TOOLS ====================================================================

reports.install(mcp, build_record=runner.build_record)


# VIBRATION ANALYSIS ==============================================================

# Files only for now: analysing the live monitor needs a measured shaft speed, and
# there is no tachometer channel.
vibration.install(mcp)
vib_report.install(mcp)
systemlink.install(mcp)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
