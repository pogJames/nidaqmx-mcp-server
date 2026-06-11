import atexit
import time
import uuid
from typing import Any

import grpc
import nidaqmx
import numpy as np
from nidaqmx.constants import (
    READ_ALL_AVAILABLE,
    AcquisitionType,
    CJCSource,
    CountDirection,
    CurrentShuntResistorLocation,
    Edge,
    ExcitationSource,
    FrequencyUnits,
    Level,
    LineGrouping,
    LoggingMode,
    LoggingOperation,
    PowerIdleOutputBehavior,
    RegenerationMode,
    ResistanceConfiguration,
    RTDType,
    Sense,
    StrainGageBridgeType,
    TaskMode,
    TemperatureUnits,
    TerminalConfiguration,
    ThermocoupleType,
    TimeUnits,
)
from nidaqmx.grpc_session_options import GrpcSessionOptions
from nidaqmx.types import CtrFreq
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("NI-DAQmx-Full")
channel = grpc.insecure_channel("localhost:31763")


def grpc_opts(name: str):
    return GrpcSessionOptions(grpc_channel=channel, session_name=name)


# ---------- Enum maps ----------

TERM = {
    "diff": TerminalConfiguration.DIFF,
    "rse": TerminalConfiguration.RSE,
    "nrse": TerminalConfiguration.NRSE,
}
ACQ = {
    "continuous": AcquisitionType.CONTINUOUS,
    "finite": AcquisitionType.FINITE,
}
EDGE = {"rising": Edge.RISING, "falling": Edge.FALLING}
COUNT_DIR = {"up": CountDirection.COUNT_UP, "down": CountDirection.COUNT_DOWN}
TC = {
    "J": ThermocoupleType.J, "K": ThermocoupleType.K, "T": ThermocoupleType.T,
    "E": ThermocoupleType.E, "N": ThermocoupleType.N, "B": ThermocoupleType.B,
    "R": ThermocoupleType.R, "S": ThermocoupleType.S,
}
CJC = {
    "built_in": CJCSource.BUILT_IN,
    "constant": CJCSource.CONSTANT_USER_VALUE,
    "channel": CJCSource.SCANNABLE_CHANNEL,
}
RTD = {
    "Pt3750": RTDType.PT_3750, "Pt3851": RTDType.PT_3851,
    "Pt3911": RTDType.PT_3911, "Pt3916": RTDType.PT_3916,
    "Pt3920": RTDType.PT_3920, "Pt3928": RTDType.PT_3928,
}
WIRING = {
    "2-wire": ResistanceConfiguration.TWO_WIRE,
    "3-wire": ResistanceConfiguration.THREE_WIRE,
    "4-wire": ResistanceConfiguration.FOUR_WIRE,
}
BRIDGE = {
    "full": StrainGageBridgeType.FULL_BRIDGE_I,
    "full-2": StrainGageBridgeType.FULL_BRIDGE_II,
    "full-3": StrainGageBridgeType.FULL_BRIDGE_III,
    "half": StrainGageBridgeType.HALF_BRIDGE_I,
    "half-2": StrainGageBridgeType.HALF_BRIDGE_II,
    "quarter": StrainGageBridgeType.QUARTER_BRIDGE_I,
    "quarter-2": StrainGageBridgeType.QUARTER_BRIDGE_II,
}
SHUNT = {
    "internal": CurrentShuntResistorLocation.INTERNAL,
    "external": CurrentShuntResistorLocation.EXTERNAL,
}
IDLE = {"low": Level.LOW, "high": Level.HIGH}
SENSE_MAP = {"local": Sense.LOCAL, "remote": Sense.REMOTE}
PWR_IDLE = {
    "output_disabled": PowerIdleOutputBehavior.OUTPUT_DISABLED,
    "maintain_existing_value": PowerIdleOutputBehavior.MAINTAIN_EXISTING_VALUE,
}
LOG_MODE = {
    "off": LoggingMode.OFF,
    "log": LoggingMode.LOG,
    "log_and_read": LoggingMode.LOG_AND_READ,
}
LOG_OP = {
    "create_or_replace": LoggingOperation.CREATE_OR_REPLACE,
    "create": LoggingOperation.CREATE,
    "open": LoggingOperation.OPEN,
    "open_or_create": LoggingOperation.OPEN_OR_CREATE,
}
GROUPING = {
    "per_line": LineGrouping.CHAN_PER_LINE,
    "all_lines": LineGrouping.CHAN_FOR_ALL_LINES,
}


# ---------- Task registry ----------

_TASKS: dict[str, dict[str, Any]] = {}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _register(task_id: str, task, kind: str, channels: str) -> None:
    _TASKS[task_id] = {"task": task, "type": kind, "channels": channels}


def _get(task_id: str):
    entry = _TASKS.get(task_id)
    if entry is None:
        raise ValueError(f"unknown task_id: {task_id}")
    return entry["task"]


def _drop(task_id: str) -> None:
    entry = _TASKS.pop(task_id, None)
    if entry:
        try:
            entry["task"].close()
        except Exception:
            pass


def _shutdown() -> None:
    for tid in list(_TASKS):
        _drop(tid)


atexit.register(_shutdown)


def _apply_trigger(task, start_trigger_src, edge, retriggerable):
    if start_trigger_src:
        task.triggers.start_trigger.cfg_dig_edge_start_trig(
            start_trigger_src, trigger_edge=EDGE.get(edge, Edge.RISING),
        )
        if retriggerable:
            task.triggers.start_trigger.retriggerable = True


def _apply_logging(
    task,
    tdms_path: str | None,
    log_operation: str = "create_or_replace",
    group_name: str | None = None,
    mode: LoggingMode = LoggingMode.LOG_AND_READ,
) -> None:
    if not tdms_path:
        return
    kwargs: dict[str, Any] = {
        "operation": LOG_OP.get(log_operation, LoggingOperation.CREATE_OR_REPLACE),
    }
    if group_name:
        kwargs["group_name"] = group_name
    task.in_stream.configure_logging(tdms_path, mode, **kwargs)




# ---------- System / device ----------

@mcp.tool()
def list_devices() -> list[str]:
    """List available DAQmx devices visible to the gRPC server."""
    system = nidaqmx.system.System(grpc_options=grpc_opts("mcp_system"))
    return [d.name for d in system.devices]


@mcp.tool()
def get_driver_version() -> dict:
    """Return the installed NI-DAQmx driver version."""
    system = nidaqmx.system.System(grpc_options=grpc_opts("mcp_system"))
    v = system.driver_version
    return {"major": v.major_version, "minor": v.minor_version, "update": v.update_version}


@mcp.tool()
def get_device_info(device: str = "Dev1") -> dict:
    """Get product type, serial number, and channel lists for a device."""
    system = nidaqmx.system.System(grpc_options=grpc_opts("mcp_system"))
    d = system.devices[device]
    return {
        "name": d.name,
        "product_type": d.product_type,
        "product_category": str(d.product_category),
        "serial_number": d.serial_num,
        "ai_channels": [c.name for c in d.ai_physical_chans],
        "ao_channels": [c.name for c in d.ao_physical_chans],
        "ai_voltage_rngs": list(d.ai_voltage_rngs) if d.ai_voltage_rngs else [],
        "ao_voltage_rngs": list(d.ao_voltage_rngs) if d.ao_voltage_rngs else [],
        "di_lines": [c.name for c in d.di_lines],
        "do_lines": [c.name for c in d.do_lines],
        "ci_channels": [c.name for c in d.ci_physical_chans],
        "co_channels": [c.name for c in d.co_physical_chans],
    }


@mcp.tool()
def reset_device(device: str = "Dev1") -> str:
    """Reset a device to default state (clears tasks and routes)."""
    system = nidaqmx.system.System(grpc_options=grpc_opts("mcp_system"))
    system.devices[device].reset_device()
    return f"Reset {device}"


@mcp.tool()
def self_test_device(device: str = "Dev1") -> str:
    """Run the device's built-in self-test."""
    system = nidaqmx.system.System(grpc_options=grpc_opts("mcp_system"))
    try:
        system.devices[device].self_test_device()
        return f"{device}: self-test passed"
    except nidaqmx.DaqError as e:
        return f"{device}: self-test failed: {e}"


@mcp.tool()
def get_channel_property(task_id: str, channel: str, name: str) -> Any:
    """Read a channel attribute (e.g. 'ai_max', 'ai_min', 'ao_max', 'do_num_lines') from a task channel."""
    task = _get(task_id)
    coll = None
    for c in (task.ai_channels, task.ao_channels, task.di_channels, task.do_channels,
              task.ci_channels, task.co_channels):
        try:
            coll = c[channel]
            break
        except Exception:
            continue
    if coll is None:
        raise ValueError(f"channel {channel} not found on task {task_id}")
    return getattr(coll, name)


@mcp.tool()
def set_channel_property(task_id: str, channel: str, name: str, value: Any) -> str:
    """Write a channel attribute on a task channel (e.g. 'ai_max'=5.0)."""
    task = _get(task_id)
    for c in (task.ai_channels, task.ao_channels, task.di_channels, task.do_channels,
              task.ci_channels, task.co_channels):
        try:
            obj = c[channel]
        except Exception:
            continue
        setattr(obj, name, value)
        return f"{channel}.{name} = {value}"
    raise ValueError(f"channel {channel} not found on task {task_id}")


# ---------- Analog input: one-shot ----------

@mcp.tool()
def read_voltage(
    channels: str = "Dev1/ai0",
    num_samples: int = 10,
    sample_rate: float = 1000.0,
    terminal_config: str = "diff",
    min_val: float = -10.0,
    max_val: float = 10.0,
    log_to: str | None = None,
    log_operation: str = "create_or_replace",
    group_name: str | None = None,
) -> list:
    """Finite AI voltage read. `channels` may be a range (e.g. 'Dev1/ai0:3') for multi-channel.

    Pass `log_to=<path.tdms>` to also stream samples to a TDMS file (LOG_AND_READ).
    If log_to is not set, data is only returned in-memory and cannot be accessed 
    by dashboard tools like load_tdms or overlay. Always set log_to when the intent 
    is to log or visualize data.
    """
    tid = _new_id("ai_v")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.ai_channels.add_ai_voltage_chan(
            channels,
            terminal_config=TERM.get(terminal_config, TerminalConfiguration.DIFF),
            min_val=min_val, max_val=max_val,
        )
        task.timing.cfg_samp_clk_timing(
            sample_rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=num_samples,
        )
        _apply_logging(task, log_to, log_operation, group_name)
        return task.read(number_of_samples_per_channel=num_samples)


@mcp.tool()
def read_voltage_waveform(
    channels: str = "Dev1/ai0",
    num_samples: int = 50,
    sample_rate: float = 1000.0,
    log_to: str | None = None,
    log_operation: str = "create_or_replace",
    group_name: str | None = None,
) -> dict:
    """Finite AI voltage read returned as a waveform with timing metadata.

    Pass `log_to=<path.tdms>` to also stream samples to a TDMS file (LOG_AND_READ).
    If log_to is not set, data is only returned in-memory and cannot be accessed 
    by dashboard tools like load_tdms or overlay. Always set log_to when the intent 
    is to log or visualize data.
    """
    tid = _new_id("ai_wfm")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.ai_channels.add_ai_voltage_chan(channels)
        task.timing.cfg_samp_clk_timing(
            sample_rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=num_samples,
        )
        _apply_logging(task, log_to, log_operation, group_name)
        wf = task.read_waveform()
        return {
            "data": list(wf.scaled_data),
            "channel_name": wf.channel_name,
            "units": str(wf.units),
            "t0": str(wf.timing.start_time),
            "dt": wf.timing.sample_interval.total_seconds(),
        }


@mcp.tool()
def read_voltage_triggered(
    channels: str = "Dev1/ai0",
    num_samples: int = 1000,
    sample_rate: float = 1000.0,
    start_trigger_src: str | None = "/Dev1/PFI0",
    reference_trigger_src: str | None = None,
    pretrigger_samples: int = 0,
    edge: str = "rising",
    min_val: float = -10.0,
    max_val: float = 10.0,
) -> list:
    """Finite AI voltage read gated by a digital start or reference trigger."""
    tid = _new_id("ai_trig")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.ai_channels.add_ai_voltage_chan(channels, min_val=min_val, max_val=max_val)
        task.timing.cfg_samp_clk_timing(
            sample_rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=num_samples,
        )
        if reference_trigger_src:
            task.triggers.reference_trigger.cfg_dig_edge_ref_trig(
                reference_trigger_src,
                pretrigger_samples=max(2, pretrigger_samples),
                trigger_edge=EDGE.get(edge, Edge.RISING),
            )
        if start_trigger_src and not reference_trigger_src:
            task.triggers.start_trigger.cfg_dig_edge_start_trig(
                start_trigger_src, trigger_edge=EDGE.get(edge, Edge.RISING),
            )
        return task.read(number_of_samples_per_channel=num_samples)


@mcp.tool()
def read_current(
    channels: str = "Dev1/ai0",
    num_samples: int = 10,
    sample_rate: float = 1000.0,
    min_val: float = -0.02,
    max_val: float = 0.02,
    shunt_resistor_loc: str = "internal",
    ext_shunt_resistor_val: float = 249.0,
    log_to: str | None = None,
    log_operation: str = "create_or_replace",
    group_name: str | None = None,
) -> list:
    """Finite AI current read (amps).

    Pass `log_to=<path.tdms>` to also stream samples to a TDMS file (LOG_AND_READ).
    If log_to is not set, data is only returned in-memory and cannot be accessed 
    by dashboard tools like load_tdms or overlay. Always set log_to when the intent 
    is to log or visualize data.
    """
    tid = _new_id("ai_i")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.ai_channels.add_ai_current_chan(
            channels, min_val=min_val, max_val=max_val,
            shunt_resistor_loc=SHUNT.get(shunt_resistor_loc, CurrentShuntResistorLocation.INTERNAL),
            ext_shunt_resistor_val=ext_shunt_resistor_val,
        )
        task.timing.cfg_samp_clk_timing(
            sample_rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=num_samples,
        )
        _apply_logging(task, log_to, log_operation, group_name)
        return task.read(number_of_samples_per_channel=num_samples)


@mcp.tool()
def read_thermocouple(
    channels: str = "Dev1/ai0",
    tc_type: str = "K",
    cjc_source: str = "built_in",
    cjc_value: float = 25.0,
    num_samples: int = 1,
    min_val: float = 0.0,
    max_val: float = 100.0,
    log_to: str | None = None,
    log_operation: str = "create_or_replace",
    group_name: str | None = None,
) -> list:
    """Single-point or finite thermocouple temperature read (deg C).

    Pass `log_to=<path.tdms>` to also stream samples to a TDMS file (LOG_AND_READ).
    If log_to is not set, data is only returned in-memory and cannot be accessed 
    by dashboard tools like load_tdms or overlay. Always set log_to when the intent 
    is to log or visualize data.
    """
    tid = _new_id("ai_tc")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.ai_channels.add_ai_thrmcpl_chan(
            channels, min_val=min_val, max_val=max_val,
            units=TemperatureUnits.DEG_C,
            thermocouple_type=TC.get(tc_type.upper(), ThermocoupleType.K),
            cjc_source=CJC.get(cjc_source, CJCSource.BUILT_IN),
            cjc_val=cjc_value,
        )
        _apply_logging(task, log_to, log_operation, group_name)
        result = task.read(number_of_samples_per_channel=num_samples)
        return result if isinstance(result, list) else [result]


@mcp.tool()
def read_rtd(
    channels: str = "Dev1/ai0",
    rtd_type: str = "Pt3851",
    r0: float = 100.0,
    wiring: str = "4-wire",
    current_excit: float = 0.0025,
    num_samples: int = 1,
    min_val: float = 0.0,
    max_val: float = 100.0,
    log_to: str | None = None,
    log_operation: str = "create_or_replace",
    group_name: str | None = None,
) -> list:
    """Single-point or finite RTD temperature read (deg C).

    Pass `log_to=<path.tdms>` to also stream samples to a TDMS file (LOG_AND_READ).
    If log_to is not set, data is only returned in-memory and cannot be accessed 
    by dashboard tools like load_tdms or overlay. Always set log_to when the intent 
    is to log or visualize data.
    """
    tid = _new_id("ai_rtd")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.ai_channels.add_ai_rtd_chan(
            channels, min_val=min_val, max_val=max_val,
            units=TemperatureUnits.DEG_C,
            rtd_type=RTD.get(rtd_type, RTDType.PT_3851),
            resistance_config=WIRING.get(wiring, ResistanceConfiguration.FOUR_WIRE),
            current_excit_source=ExcitationSource.EXTERNAL,
            current_excit_val=current_excit,
            r_0=r0,
        )
        _apply_logging(task, log_to, log_operation, group_name)
        result = task.read(number_of_samples_per_channel=num_samples)
        return result if isinstance(result, list) else [result]


@mcp.tool()
def read_strain(
    channels: str = "Dev1/ai0",
    bridge_config: str = "full",
    gage_factor: float = 2.0,
    initial_bridge_voltage: float = 0.0,
    nominal_gage_resistance: float = 350.0,
    voltage_excit: float = 2.5,
    num_samples: int = 1,
    min_val: float = -0.001,
    max_val: float = 0.001,
    log_to: str | None = None,
    log_operation: str = "create_or_replace",
    group_name: str | None = None,
) -> list:
    """Single-point or finite strain-gage read (strain units).

    Pass `log_to=<path.tdms>` to also stream samples to a TDMS file (LOG_AND_READ).
    If log_to is not set, data is only returned in-memory and cannot be accessed 
    by dashboard tools like load_tdms or overlay. Always set log_to when the intent 
    is to log or visualize data.
    """
    tid = _new_id("ai_strain")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.ai_channels.add_ai_strain_gage_chan(
            channels, min_val=min_val, max_val=max_val,
            strain_config=BRIDGE.get(bridge_config, StrainGageBridgeType.FULL_BRIDGE_I),
            voltage_excit_source=ExcitationSource.INTERNAL,
            voltage_excit_val=voltage_excit,
            gage_factor=gage_factor,
            initial_bridge_voltage=initial_bridge_voltage,
            nominal_gage_resistance=nominal_gage_resistance,
        )
        _apply_logging(task, log_to, log_operation, group_name)
        result = task.read(number_of_samples_per_channel=num_samples)
        return result if isinstance(result, list) else [result]


@mcp.tool()
def read_power(
    channel: str = "TS1Mod1/power",
    voltage_setpoint: float = 0.0,
    current_setpoint: float = 0.03,
    output_enable: bool = True,
    num_samples: int = 1,
    sample_rate: float = 10.0,
    remote_sense: str = "local",
    idle_output_behavior: str = "output_disabled",
) -> list:
    """Finite (or single-point) power-channel read; returns list of (voltage, current) tuples."""
    tid = _new_id("ai_pwr")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        chan = task.ai_channels.add_ai_power_chan(
            channel, voltage_setpoint, current_setpoint, output_enable,
        )
        chan.pwr_idle_output_behavior = PWR_IDLE.get(
            idle_output_behavior, PowerIdleOutputBehavior.OUTPUT_DISABLED,
        )
        chan.pwr_remote_sense = SENSE_MAP.get(remote_sense, Sense.LOCAL)
        if num_samples > 1:
            task.timing.cfg_samp_clk_timing(
                sample_rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=num_samples,
            )
        data = task.read(number_of_samples_per_channel=num_samples)
        return [(d.voltage, d.current) for d in (data if isinstance(data, list) else [data])]


# ---------- Analog input: streamed ----------

@mcp.tool()
def start_continuous_voltage(
    channels: str = "Dev1/ai0",
    sample_rate: float = 1000.0,
    terminal_config: str = "diff",
    min_val: float = -10.0,
    max_val: float = 10.0,
    start_trigger_src: str | None = None,
    trigger_edge: str = "rising",
    retriggerable: bool = False,
    finite_samps_per_trig: int | None = None,
) -> str:
    """Start a continuous (or per-trigger finite) AI voltage acquisition. Returns task_id; poll with read_buffered. Use start_logging to record to a TDMS file."""
    task_id = _new_id("ai_cont")
    task = nidaqmx.Task(new_task_name=task_id, grpc_options=grpc_opts(task_id))
    try:
        task.ai_channels.add_ai_voltage_chan(
            channels,
            terminal_config=TERM.get(terminal_config, TerminalConfiguration.DIFF),
            min_val=min_val, max_val=max_val,
        )
        if retriggerable and finite_samps_per_trig:
            task.timing.cfg_samp_clk_timing(
                sample_rate, sample_mode=AcquisitionType.FINITE,
                samps_per_chan=finite_samps_per_trig,
            )
        else:
            task.timing.cfg_samp_clk_timing(sample_rate, sample_mode=AcquisitionType.CONTINUOUS)
        _apply_trigger(task, start_trigger_src, trigger_edge, retriggerable)
        task.start()
    except Exception:
        task.close()
        raise
    _register(task_id, task, "ai_voltage_continuous", channels)
    return task_id


@mcp.tool()
def start_continuous_thermocouple(
    channels: str = "Dev1/ai0",
    sample_rate: float = 10.0,
    tc_type: str = "K",
    cjc_source: str = "constant",
    cjc_value: float = 25.0,
) -> str:
    """Start continuous thermocouple acquisition. Returns task_id."""
    task_id = _new_id("ai_tc_cont")
    task = nidaqmx.Task(new_task_name=task_id, grpc_options=grpc_opts(task_id))
    try:
        task.ai_channels.add_ai_thrmcpl_chan(
            channels,
            units=TemperatureUnits.DEG_C,
            thermocouple_type=TC.get(tc_type.upper(), ThermocoupleType.K),
            cjc_source=CJC.get(cjc_source, CJCSource.CONSTANT_USER_VALUE),
            cjc_val=cjc_value,
        )
        task.timing.cfg_samp_clk_timing(sample_rate, sample_mode=AcquisitionType.CONTINUOUS)
        task.start()
    except Exception:
        task.close()
        raise
    _register(task_id, task, "ai_thrmcpl_continuous", channels)
    return task_id


@mcp.tool()
def start_continuous_power(
    channel: str = "TS1Mod1/power",
    voltage_setpoint: float = 0.0,
    current_setpoint: float = 0.03,
    output_enable: bool = True,
    sample_rate: float = 10.0,
    remote_sense: str = "local",
    idle_output_behavior: str = "output_disabled",
) -> str:
    """Start hardware-timed continuous power acquisition. Returns task_id."""
    task_id = _new_id("ai_pwr_cont")
    task = nidaqmx.Task(new_task_name=task_id, grpc_options=grpc_opts(task_id))
    try:
        chan = task.ai_channels.add_ai_power_chan(
            channel, voltage_setpoint, current_setpoint, output_enable,
        )
        chan.pwr_idle_output_behavior = PWR_IDLE.get(
            idle_output_behavior, PowerIdleOutputBehavior.OUTPUT_DISABLED,
        )
        chan.pwr_remote_sense = SENSE_MAP.get(remote_sense, Sense.LOCAL)
        task.timing.cfg_samp_clk_timing(sample_rate, sample_mode=AcquisitionType.CONTINUOUS)
        task.start()
    except Exception:
        task.close()
        raise
    _register(task_id, task, "ai_power_continuous", channel)
    return task_id


@mcp.tool()
def read_buffered(task_id: str, n: int = -1, timeout: float = 10.0) -> list:
    """Read up to n samples from a running task's buffer (-1 = all available). Call repeatedly."""
    task = _get(task_id)
    count = READ_ALL_AVAILABLE if n < 0 else n
    return task.read(number_of_samples_per_channel=count, timeout=timeout)


@mcp.tool()
def read_buffered_waveform(task_id: str, n: int = 100) -> dict:
    """Read n samples from a running task as a waveform with timing metadata."""
    task = _get(task_id)
    wf = task.read_waveform(number_of_samples_per_channel=n)
    return {
        "data": list(wf.scaled_data),
        "channel_name": wf.channel_name,
        "units": str(wf.units),
        "t0": str(wf.timing.start_time),
        "dt": wf.timing.sample_interval.total_seconds(),
    }


# ---------- Analog output ----------

@mcp.tool()
def write_voltage(channel: str = "Dev1/ao0", voltage: float = 0.0) -> str:
    """Write a single DC voltage to an AO channel."""
    tid = _new_id("ao_v")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.ao_channels.add_ao_voltage_chan(channel)
        task.write(voltage)
    return f"Wrote {voltage}V to {channel}"


def _build_waveform(waveform_type, frequency, amplitude, offset, sample_rate, custom_samples):
    if waveform_type == "custom":
        if not custom_samples:
            raise ValueError("custom waveform requires custom_samples")
        return np.asarray(custom_samples, dtype=float)
    n = max(2, int(sample_rate / frequency)) if frequency > 0 else int(sample_rate)
    t = np.linspace(0.0, 1.0, n, endpoint=False)
    if waveform_type == "sine":
        return amplitude * np.sin(2 * np.pi * t) + offset
    if waveform_type == "square":
        return amplitude * np.sign(np.sin(2 * np.pi * t)) + offset
    if waveform_type == "triangle":
        return amplitude * (2.0 * np.abs(2.0 * (t - np.floor(t + 0.5))) - 1.0) + offset
    if waveform_type == "ramp":
        return amplitude * (2.0 * t - 1.0) + offset
    raise ValueError(f"unknown waveform_type: {waveform_type}")


@mcp.tool()
def write_waveform(
    channel: str = "Dev1/ao0",
    waveform_type: str = "sine",
    frequency: float = 1.0,
    amplitude: float = 1.0,
    offset: float = 0.0,
    sample_rate: float = 1000.0,
    duration: float = 5.0,
    custom_samples: list[float] | None = None,
) -> str:
    """Output a finite waveform (sine/square/triangle/ramp/custom) on an AO channel for `duration` seconds."""
    wf = _build_waveform(waveform_type, frequency, amplitude, offset, sample_rate, custom_samples)
    tid = _new_id("ao_wfm")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.ao_channels.add_ao_voltage_chan(channel)
        task.timing.cfg_samp_clk_timing(
            sample_rate, sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=len(wf),
        )
        task.write(wf)
        task.start()
        time.sleep(duration)
        task.stop()
    return f"{waveform_type} on {channel} for {duration}s"


@mcp.tool()
def start_continuous_waveform(
    channel: str = "Dev1/ao0",
    waveform_type: str = "sine",
    frequency: float = 1.0,
    amplitude: float = 1.0,
    offset: float = 0.0,
    sample_rate: float = 1000.0,
    custom_samples: list[float] | None = None,
    regen: bool = True,
) -> str:
    """Start continuous AO waveform generation. Returns task_id. If regen=False, feed new data via write_buffer."""
    task_id = _new_id("ao_cont")
    task = nidaqmx.Task(new_task_name=task_id, grpc_options=grpc_opts(task_id))
    try:
        task.ao_channels.add_ao_voltage_chan(channel)
        if not regen:
            task.out_stream.regen_mode = RegenerationMode.DONT_ALLOW_REGENERATION
        task.timing.cfg_samp_clk_timing(sample_rate, sample_mode=AcquisitionType.CONTINUOUS)
        if regen:
            wf = _build_waveform(waveform_type, frequency, amplitude, offset, sample_rate, custom_samples)
            task.write(wf)
            task.start()
        # If non-regen, caller must write_buffer first; we defer start to the first write_buffer call.
    except Exception:
        task.close()
        raise
    _register(task_id, task, "ao_continuous", channel)
    return task_id


@mcp.tool()
def write_buffer(task_id: str, samples: list[float], auto_start: bool = True) -> str:
    """Append samples to a running (or pending) AO task buffer. Used for non-regen continuous output."""
    task = _get(task_id)
    written = task.write(np.asarray(samples, dtype=float), auto_start=auto_start)
    return f"wrote {written} samples to {task_id}"


# ---------- Digital input ----------

@mcp.tool()
def read_digital_lines(lines: str = "Dev1/port0/line0:7") -> list[bool]:
    """Read one or more DI lines as booleans (one channel per line)."""
    tid = _new_id("di_l")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.di_channels.add_di_chan(lines, line_grouping=LineGrouping.CHAN_PER_LINE)
        result = task.read()
        return result if isinstance(result, list) else [result]


@mcp.tool()
def read_digital_port(port: str = "Dev1/port0") -> int:
    """Read a DI port as a single integer (all lines combined)."""
    tid = _new_id("di_p")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.di_channels.add_di_chan(port, line_grouping=LineGrouping.CHAN_FOR_ALL_LINES)
        return int(task.read())


@mcp.tool()
def read_digital_port_clocked(
    port: str = "Dev1/port0",
    num_samples: int = 50,
    sample_rate: float = 1000.0,
) -> list[int]:
    """Finite hardware-timed DI port acquisition; returns list of port values."""
    tid = _new_id("di_pc")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.di_channels.add_di_chan(port, line_grouping=LineGrouping.CHAN_FOR_ALL_LINES)
        task.timing.cfg_samp_clk_timing(
            sample_rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=num_samples,
        )
        return [int(v) for v in task.read(READ_ALL_AVAILABLE)]


@mcp.tool()
def start_continuous_digital_lines(
    lines: str = "Dev1/port0/line0:7",
    sample_rate: float = 1000.0,
) -> str:
    """Start continuous hardware-timed DI acquisition on lines. Returns task_id."""
    task_id = _new_id("di_cont")
    task = nidaqmx.Task(new_task_name=task_id, grpc_options=grpc_opts(task_id))
    try:
        task.di_channels.add_di_chan(lines, line_grouping=LineGrouping.CHAN_PER_LINE)
        task.timing.cfg_samp_clk_timing(sample_rate, sample_mode=AcquisitionType.CONTINUOUS)
        task.start()
    except Exception:
        task.close()
        raise
    _register(task_id, task, "di_continuous", lines)
    return task_id


# ---------- Digital output ----------

@mcp.tool()
def write_digital_lines(
    lines: str = "Dev1/port0/line0",
    values: list[bool] | bool = False,
) -> str:
    """Drive one or more DO lines. Pass a bool or list of bools."""
    tid = _new_id("do_l")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.do_channels.add_do_chan(lines, line_grouping=LineGrouping.CHAN_PER_LINE)
        task.write(values)
    return f"Wrote {values} to {lines}"


@mcp.tool()
def write_digital_port(port: str = "Dev1/port0", value: int = 0) -> str:
    """Write a single integer value to a DO port (all lines)."""
    tid = _new_id("do_p")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.do_channels.add_do_chan(port, line_grouping=LineGrouping.CHAN_FOR_ALL_LINES)
        task.write(int(value))
    return f"Wrote 0x{value:x} to {port}"


@mcp.tool()
def write_digital_clocked(
    target: str = "Dev1/port0/line0",
    samples: list[int] | list[bool] | None = None,
    sample_rate: float = 1000.0,
    grouping: str = "per_line",
) -> str:
    """Finite hardware-timed digital generation on lines or a port."""
    if not samples:
        raise ValueError("samples is required")
    tid = _new_id("do_c")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.do_channels.add_do_chan(target, line_grouping=GROUPING.get(grouping, LineGrouping.CHAN_PER_LINE))
        task.timing.cfg_samp_clk_timing(
            sample_rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=len(samples),
        )
        task.write(samples, auto_start=False)
        task.start()
        task.wait_until_done(timeout=max(10.0, len(samples) / sample_rate + 5.0))
        task.stop()
    return f"wrote {len(samples)} samples to {target}"


@mcp.tool()
def start_continuous_digital_port(
    port: str = "Dev1/port0",
    samples: list[int] | None = None,
    sample_rate: float = 10.0,
) -> str:
    """Start continuous hardware-timed DO port generation (samples are re-generated by hardware). Returns task_id."""
    if not samples:
        raise ValueError("samples is required")
    task_id = _new_id("do_cont")
    task = nidaqmx.Task(new_task_name=task_id, grpc_options=grpc_opts(task_id))
    try:
        task.do_channels.add_do_chan(port, line_grouping=LineGrouping.CHAN_FOR_ALL_LINES)
        task.timing.cfg_samp_clk_timing(sample_rate, sample_mode=AcquisitionType.CONTINUOUS)
        task.write([int(s) for s in samples])
        task.start()
    except Exception:
        task.close()
        raise
    _register(task_id, task, "do_continuous", port)
    return task_id


# ---------- Counter input ----------

@mcp.tool()
def count_edges(
    counter: str = "Dev1/ctr0",
    edge: str = "rising",
    duration: float = 1.0,
    term: str | None = None,
    direction: str = "up",
    initial_count: int = 0,
) -> int:
    """Count rising/falling edges on a CI channel over a time window. `term` routes the input signal."""
    tid = _new_id("ci_ce")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        chan = task.ci_channels.add_ci_count_edges_chan(
            counter,
            edge=EDGE.get(edge, Edge.RISING),
            initial_count=initial_count,
            count_direction=COUNT_DIR.get(direction, CountDirection.COUNT_UP),
        )
        if term:
            chan.ci_count_edges_term = term
        task.start()
        time.sleep(duration)
        count = task.read()
        task.stop()
    return int(count)


@mcp.tool()
def measure_frequency(
    counter: str = "Dev1/ctr0",
    min_freq: float = 1.0,
    max_freq: float = 1000.0,
    num_samples: int = 10,
) -> list[float]:
    """Measure frequency (Hz) on a CI channel."""
    tid = _new_id("ci_f")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.ci_channels.add_ci_freq_chan(
            counter, min_val=min_freq, max_val=max_freq,
            units=FrequencyUnits.HZ, edge=Edge.RISING,
        )
        result = task.read(number_of_samples_per_channel=num_samples)
        return result if isinstance(result, list) else [result]


@mcp.tool()
def measure_pulse_frequency(
    counter: str = "Dev1/ctr0",
    min_freq: float = 2.0,
    max_freq: float = 100000.0,
    term: str | None = None,
) -> dict:
    """Measure a single pulse's frequency and duty cycle on a CI channel."""
    with mcp_task(_new_id("ci_pf")) as task:
        chan = task.ci_channels.add_ci_pulse_chan_freq(
            counter, "", min_val=min_freq, max_val=max_freq, units=FrequencyUnits.HZ,
        )
        if term:
            chan.ci_pulse_freq_term = term
        task.start()
        data = task.read()
        task.stop()
    return {"frequency_hz": data.freq, "duty_cycle": data.duty_cycle}


@mcp.tool()
def measure_pulse_width(
    counter: str = "Dev1/ctr0",
    starting_edge: str = "rising",
    min_val: float = 0.000001,
    max_val: float = 1.0,
    num_samples: int = 10,
) -> list[float]:
    """Measure pulse width (seconds) on a CI channel."""
    tid = _new_id("ci_pw")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.ci_channels.add_ci_pulse_width_chan(
            counter, min_val=min_val, max_val=max_val,
            units=TimeUnits.SECONDS,
            starting_edge=EDGE.get(starting_edge, Edge.RISING),
        )
        result = task.read(number_of_samples_per_channel=num_samples)
        return result if isinstance(result, list) else [result]


@mcp.tool()
def start_continuous_count_edges(
    counter: str = "Dev1/ctr0",
    sample_clock_src: str = "/Dev1/PFI9",
    sample_rate: float = 1000.0,
    term: str | None = "/Dev1/PFI8",
    edge: str = "rising",
) -> str:
    """Start a buffered edge-count task clocked by an external signal. Returns task_id."""
    task_id = _new_id("ci_ce_cont")
    task = nidaqmx.Task(new_task_name=task_id, grpc_options=grpc_opts(task_id))
    try:
        chan = task.ci_channels.add_ci_count_edges_chan(
            counter, edge=EDGE.get(edge, Edge.RISING),
            initial_count=0, count_direction=CountDirection.COUNT_UP,
        )
        task.timing.cfg_samp_clk_timing(
            sample_rate, source=sample_clock_src, sample_mode=AcquisitionType.CONTINUOUS,
        )
        if term:
            chan.ci_count_edges_term = term
        task.start()
    except Exception:
        task.close()
        raise
    _register(task_id, task, "ci_count_continuous", counter)
    return task_id


@mcp.tool()
def start_continuous_frequency(
    counter: str = "Dev1/ctr0",
    min_freq: float = 1.0,
    max_freq: float = 1000.0,
) -> str:
    """Start continuous frequency measurement on a CI channel. Returns task_id."""
    task_id = _new_id("ci_f_cont")
    task = nidaqmx.Task(new_task_name=task_id, grpc_options=grpc_opts(task_id))
    try:
        task.ci_channels.add_ci_freq_chan(
            counter, min_val=min_freq, max_val=max_freq,
            units=FrequencyUnits.HZ, edge=Edge.RISING,
        )
        task.timing.cfg_implicit_timing(sample_mode=AcquisitionType.CONTINUOUS)
        task.start()
    except Exception:
        task.close()
        raise
    _register(task_id, task, "ci_freq_continuous", counter)
    return task_id


# ---------- Counter output ----------

@mcp.tool()
def generate_pulse_train(
    counter: str = "Dev1/ctr0",
    frequency: float = 1000.0,
    duty_cycle: float = 0.5,
    duration: float = 5.0,
    idle_state: str = "low",
) -> str:
    """Generate a finite pulse train (Hz / duty cycle) on a CO channel for a duration."""
    n_pulses = max(1, int(frequency * duration))
    tid = _new_id("co_pt")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        task.co_channels.add_co_pulse_chan_freq(
            counter, units=FrequencyUnits.HZ,
            idle_state=IDLE.get(idle_state, Level.LOW),
            freq=frequency, duty_cycle=duty_cycle,
        )
        task.timing.cfg_implicit_timing(
            sample_mode=AcquisitionType.FINITE, samps_per_chan=n_pulses,
        )
        task.start()
        task.wait_until_done(timeout=duration + 5.0)
        task.stop()
    return f"Pulse train {frequency}Hz @ {duty_cycle * 100:.0f}% on {counter} for {duration}s"


@mcp.tool()
def generate_single_pulse(
    counter: str = "Dev1/ctr0",
    high_time: float = 1.0,
    low_time: float = 0.5,
    initial_delay: float = 0.0,
    idle_state: str = "low",
    term: str | None = None,
    timeout: float = 10.0,
) -> str:
    """Generate a single digital pulse with explicit high/low times on a CO channel."""
    tid = _new_id("co_sp")
    with nidaqmx.Task(new_task_name=tid, grpc_options=grpc_opts(tid)) as task:
        chan = task.co_channels.add_co_pulse_chan_time(
            counter, idle_state=IDLE.get(idle_state, Level.LOW),
            initial_delay=initial_delay, low_time=low_time, high_time=high_time,
        )
        if term:
            chan.co_pulse_term = term
        task.start()
        task.wait_until_done(timeout=timeout)
        task.stop()
    return f"single pulse high={high_time}s low={low_time}s on {counter}"


@mcp.tool()
def start_continuous_pulse_train(
    counter: str = "Dev1/ctr0",
    frequency: float = 1000.0,
    duty_cycle: float = 0.5,
    idle_state: str = "low",
    term: str | None = None,
) -> str:
    """Start a continuous pulse train. Returns task_id."""
    task_id = _new_id("co_pt_cont")
    task = nidaqmx.Task(new_task_name=task_id, grpc_options=grpc_opts(task_id))
    try:
        chan = task.co_channels.add_co_pulse_chan_freq(
            counter, units=FrequencyUnits.HZ,
            idle_state=IDLE.get(idle_state, Level.LOW),
            freq=frequency, duty_cycle=duty_cycle,
        )
        if term:
            chan.co_pulse_term = term
        task.timing.cfg_implicit_timing(sample_mode=AcquisitionType.CONTINUOUS)
        task.start()
    except Exception:
        task.close()
        raise
    _register(task_id, task, "co_pulse_continuous", counter)
    return task_id


@mcp.tool()
def start_buffered_pulse_train(
    counter: str = "Dev1/ctr0",
    freqs: list[float] | None = None,
    duty_cycles: list[float] | None = None,
    sample_clock_src: str | None = None,
    sample_rate: float = 1000.0,
    idle_state: str = "low",
    term: str | None = None,
) -> str:
    """Start a continuous buffered pulse train where each sample varies freq and/or duty cycle.

    Implicit timing if sample_clock_src is None; otherwise hardware-timed off the given clock.
    """
    if not freqs or not duty_cycles or len(freqs) != len(duty_cycles):
        raise ValueError("freqs and duty_cycles must be provided with equal length")
    task_id = _new_id("co_pt_buf")
    task = nidaqmx.Task(new_task_name=task_id, grpc_options=grpc_opts(task_id))
    try:
        chan = task.co_channels.add_co_pulse_chan_freq(
            counter, units=FrequencyUnits.HZ,
            idle_state=IDLE.get(idle_state, Level.LOW),
            freq=freqs[0], duty_cycle=duty_cycles[0],
        )
        if term:
            chan.co_pulse_term = term
        if sample_clock_src:
            task.timing.cfg_samp_clk_timing(
                sample_rate, source=sample_clock_src, sample_mode=AcquisitionType.CONTINUOUS,
            )
        else:
            task.timing.cfg_implicit_timing(sample_mode=AcquisitionType.CONTINUOUS)
        data = [CtrFreq(f, d) for f, d in zip(freqs, duty_cycles)]
        task.write(data)
        task.start()
    except Exception:
        task.close()
        raise
    _register(task_id, task, "co_pulse_buffered", counter)
    return task_id


# ---------- Task management ----------

@mcp.tool()
def list_tasks() -> list[dict]:
    """List all open server-side tasks."""
    return [
        {"task_id": tid, "type": meta["type"], "channels": meta["channels"]}
        for tid, meta in _TASKS.items()
    ]


@mcp.tool()
def stop_task(task_id: str) -> str:
    """Stop a running task without clearing it (it can be restarted)."""
    task = _get(task_id)
    task.stop()
    return f"stopped {task_id}"


@mcp.tool()
def clear_task(task_id: str) -> str:
    """Stop and close a task, removing it from the registry."""
    _drop(task_id)
    return f"cleared {task_id}"


@mcp.tool()
def wait_until_done(task_id: str, timeout: float = 10.0) -> str:
    """Block until the task finishes or the timeout expires."""
    _get(task_id).wait_until_done(timeout=timeout)
    return f"{task_id} done"


# Alias used by measure_pulse_frequency; bound to a context-manager wrapper for one-shot tasks.
class mcp_task:
    def __init__(self, tid: str):
        self.tid = tid
        self.task: nidaqmx.Task | None = None

    def __enter__(self):
        self.task = nidaqmx.Task(new_task_name=self.tid, grpc_options=grpc_opts(self.tid))
        return self.task

    def __exit__(self, *exc):
        if self.task is not None:
            self.task.close()


if __name__ == "__main__":
    mcp.run(transport="stdio")
