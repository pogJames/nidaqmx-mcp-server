import time
import grpc
import nidaqmx
import numpy as np
from nidaqmx.constants import (
    AcquisitionType,
    CJCSource,
    CountDirection,
    CurrentShuntResistorLocation,
    Edge,
    ExcitationSource,
    FrequencyUnits,
    Level,
    LineGrouping,
    ResistanceConfiguration,
    RTDType,
    StrainGageBridgeType,
    TemperatureUnits,
    TerminalConfiguration,
    ThermocoupleType,
    TimeUnits,
)
from nidaqmx.grpc_session_options import GrpcSessionOptions
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("NI-DAQmx")
channel = grpc.insecure_channel("localhost:31763")


def grpc_opts(name: str):
    return GrpcSessionOptions(grpc_channel=channel, session_name=name)


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


# ---------- System / device ----------

@mcp.tool()
def list_devices() -> list[str]:
    """List available DAQmx devices visible to the gRPC server."""
    system = nidaqmx.system.System(grpc_options=grpc_opts("mcp_system"))
    return [d.name for d in system.devices]


@mcp.tool()
def get_device_info(device: str = "Dev1") -> dict:
    """Get product type, serial number, and channel lists for a device."""
    system = nidaqmx.system.System(grpc_options=grpc_opts("mcp_system"))
    d = system.devices[device]
    return {
        "name": d.name,
        "product_type": d.product_type,
        "serial_number": d.serial_num,
        "ai_channels": [c.name for c in d.ai_physical_chans],
        "ao_channels": [c.name for c in d.ao_physical_chans],
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


# ---------- Analog input ----------

@mcp.tool()
def read_voltage(
    physical_channel: str = "Dev1/ai0",
    num_samples: int = 10,
    terminal_config: str = "diff",
    sample_rate: int = 1000,
    acquisition_type: str = "continuous",
    min_val: float = -10.0,
    max_val: float = 10.0,
) -> list[float]:
    """Read analog voltage from a single AI channel."""
    with nidaqmx.Task(new_task_name="mcp_read_v", grpc_options=grpc_opts("mcp_read_v")) as task:
        task.ai_channels.add_ai_voltage_chan(
            physical_channel,
            terminal_config=TERM.get(terminal_config, TerminalConfiguration.DIFF),
            min_val=min_val, max_val=max_val,
        )
        task.timing.cfg_samp_clk_timing(
            rate=sample_rate,
            sample_mode=ACQ.get(acquisition_type.lower(), AcquisitionType.CONTINUOUS),
            samps_per_chan=num_samples,
        )
        return task.read(number_of_samples_per_channel=num_samples)


@mcp.tool()
def read_current(
    physical_channel: str = "Dev1/ai0",
    num_samples: int = 10,
    sample_rate: int = 1000,
    min_val: float = -0.02,
    max_val: float = 0.02,
    shunt_resistor_loc: str = "internal",
    ext_shunt_resistor_val: float = 249.0,
) -> list[float]:
    """Read analog current (amps) from a single AI channel."""
    with nidaqmx.Task(new_task_name="mcp_read_i", grpc_options=grpc_opts("mcp_read_i")) as task:
        task.ai_channels.add_ai_current_chan(
            physical_channel,
            min_val=min_val, max_val=max_val,
            shunt_resistor_loc=SHUNT.get(shunt_resistor_loc, CurrentShuntResistorLocation.INTERNAL),
            ext_shunt_resistor_val=ext_shunt_resistor_val,
        )
        task.timing.cfg_samp_clk_timing(
            rate=sample_rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=num_samples,
        )
        return task.read(number_of_samples_per_channel=num_samples)


@mcp.tool()
def read_thermocouple(
    physical_channel: str = "Dev1/ai0",
    tc_type: str = "K",
    cjc_source: str = "built_in",
    cjc_value: float = 25.0,
    num_samples: int = 1,
    min_val: float = 0.0,
    max_val: float = 100.0,
) -> list[float]:
    """Read temperature (deg C) via thermocouple on a single AI channel."""
    with nidaqmx.Task(new_task_name="mcp_tc", grpc_options=grpc_opts("mcp_tc")) as task:
        task.ai_channels.add_ai_thrmcpl_chan(
            physical_channel,
            min_val=min_val, max_val=max_val,
            units=TemperatureUnits.DEG_C,
            thermocouple_type=TC.get(tc_type.upper(), ThermocoupleType.K),
            cjc_source=CJC.get(cjc_source, CJCSource.BUILT_IN),
            cjc_val=cjc_value,
        )
        result = task.read(number_of_samples_per_channel=num_samples)
        return result if isinstance(result, list) else [result]


@mcp.tool()
def read_rtd(
    physical_channel: str = "Dev1/ai0",
    rtd_type: str = "Pt3851",
    r0: float = 100.0,
    wiring: str = "4-wire",
    current_excit: float = 0.0025,
    num_samples: int = 1,
    min_val: float = 0.0,
    max_val: float = 100.0,
) -> list[float]:
    """Read temperature (deg C) via RTD on a single AI channel."""
    with nidaqmx.Task(new_task_name="mcp_rtd", grpc_options=grpc_opts("mcp_rtd")) as task:
        task.ai_channels.add_ai_rtd_chan(
            physical_channel,
            min_val=min_val, max_val=max_val,
            units=TemperatureUnits.DEG_C,
            rtd_type=RTD.get(rtd_type, RTDType.PT_3851),
            resistance_config=WIRING.get(wiring, ResistanceConfiguration.FOUR_WIRE),
            current_excit_source=ExcitationSource.EXTERNAL,
            current_excit_val=current_excit,
            r_0=r0,
        )
        result = task.read(number_of_samples_per_channel=num_samples)
        return result if isinstance(result, list) else [result]


@mcp.tool()
def read_strain(
    physical_channel: str = "Dev1/ai0",
    bridge_config: str = "full",
    gage_factor: float = 2.0,
    initial_bridge_voltage: float = 0.0,
    nominal_gage_resistance: float = 350.0,
    voltage_excit: float = 2.5,
    num_samples: int = 1,
    min_val: float = -0.001,
    max_val: float = 0.001,
) -> list[float]:
    """Read strain (strain units) from a bridge-based AI channel."""
    with nidaqmx.Task(new_task_name="mcp_strain", grpc_options=grpc_opts("mcp_strain")) as task:
        task.ai_channels.add_ai_strain_gage_chan(
            physical_channel,
            min_val=min_val, max_val=max_val,
            strain_config=BRIDGE.get(bridge_config, StrainGageBridgeType.FULL_BRIDGE_I),
            voltage_excit_source=ExcitationSource.INTERNAL,
            voltage_excit_val=voltage_excit,
            gage_factor=gage_factor,
            initial_bridge_voltage=initial_bridge_voltage,
            nominal_gage_resistance=nominal_gage_resistance,
        )
        result = task.read(number_of_samples_per_channel=num_samples)
        return result if isinstance(result, list) else [result]


# ---------- Analog output ----------

@mcp.tool()
def write_voltage(physical_channel: str = "Dev1/ao0", voltage: float = 0.0) -> str:
    """Write a single DC voltage to an AO channel."""
    with nidaqmx.Task(new_task_name="mcp_write_v", grpc_options=grpc_opts("mcp_write_v")) as task:
        task.ao_channels.add_ao_voltage_chan(physical_channel)
        task.write(voltage)
    return f"Wrote {voltage}V to {physical_channel}"


@mcp.tool()
def write_waveform(
    physical_channel: str = "Dev1/ao0",
    waveform_type: str = "sine",
    frequency: float = 10.0,
    amplitude: float = 2.0,
    offset: float = 0.0,
    sample_rate: int = 1000,
    duration: float = 5.0,
    custom_samples: list[float] | None = None,
) -> str:
    """Output a sine/square/triangle/ramp waveform (or a custom sample array) on an AO channel for a duration."""
    if waveform_type == "custom":
        if not custom_samples:
            return "custom waveform requires custom_samples"
        wf = np.asarray(custom_samples, dtype=float)
    else:
        n = max(2, int(sample_rate / frequency)) if frequency > 0 else int(sample_rate)
        t = np.linspace(0.0, 1.0, n, endpoint=False)
        if waveform_type == "sine":
            wf = amplitude * np.sin(2 * np.pi * t) + offset
        elif waveform_type == "square":
            wf = amplitude * np.sign(np.sin(2 * np.pi * t)) + offset
        elif waveform_type == "triangle":
            wf = amplitude * (2.0 * np.abs(2.0 * (t - np.floor(t + 0.5))) - 1.0) + offset
        elif waveform_type == "ramp":
            wf = amplitude * (2.0 * t - 1.0) + offset
        else:
            return f"unknown waveform_type: {waveform_type}"

    with nidaqmx.Task(new_task_name="mcp_wf", grpc_options=grpc_opts("mcp_wf")) as task:
        task.ao_channels.add_ao_voltage_chan(physical_channel)
        task.timing.cfg_samp_clk_timing(
            rate=sample_rate, sample_mode=AcquisitionType.CONTINUOUS, samps_per_chan=len(wf),
        )
        task.write(wf)
        task.start()
        time.sleep(duration)
        task.stop()
    return f"{waveform_type} waveform on {physical_channel} for {duration}s"


# ---------- Digital I/O ----------

@mcp.tool()
def read_digital(lines: str = "Dev1/port0/line0") -> list[bool]:
    """Read one or more DI lines (e.g. 'Dev1/port0/line0' or 'Dev1/port0/line0:7')."""
    with nidaqmx.Task(new_task_name="mcp_di", grpc_options=grpc_opts("mcp_di")) as task:
        task.di_channels.add_di_chan(lines, line_grouping=LineGrouping.CHAN_PER_LINE)
        result = task.read()
        return result if isinstance(result, list) else [result]


@mcp.tool()
def write_digital(
    lines: str = "Dev1/port0/line0",
    values: list[bool] | bool = False,
) -> str:
    """Drive one or more DO lines. Pass a bool for a single line or a list of bools for multiple."""
    with nidaqmx.Task(new_task_name="mcp_do", grpc_options=grpc_opts("mcp_do")) as task:
        task.do_channels.add_do_chan(lines, line_grouping=LineGrouping.CHAN_PER_LINE)
        task.write(values)
    return f"Wrote {values} to {lines}"


# ---------- Counter ----------

@mcp.tool()
def count_edges(
    counter: str = "Dev1/ctr0",
    edge: str = "rising",
    duration: float = 1.0,
) -> int:
    """Count rising or falling edges on a CI channel over a time window."""
    with nidaqmx.Task(new_task_name="mcp_ce", grpc_options=grpc_opts("mcp_ce")) as task:
        task.ci_channels.add_ci_count_edges_chan(
            counter,
            edge=EDGE.get(edge, Edge.RISING),
            initial_count=0,
            count_direction=CountDirection.COUNT_UP,
        )
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
    with nidaqmx.Task(new_task_name="mcp_freq", grpc_options=grpc_opts("mcp_freq")) as task:
        task.ci_channels.add_ci_freq_chan(
            counter,
            min_val=min_freq, max_val=max_freq,
            units=FrequencyUnits.HZ,
            edge=Edge.RISING,
        )
        result = task.read(number_of_samples_per_channel=num_samples)
        return result if isinstance(result, list) else [result]


@mcp.tool()
def measure_pulse_width(
    counter: str = "Dev1/ctr0",
    starting_edge: str = "rising",
    min_val: float = 0.000001,
    max_val: float = 1.0,
    num_samples: int = 10,
) -> list[float]:
    """Measure pulse width (seconds) on a CI channel."""
    with nidaqmx.Task(new_task_name="mcp_pw", grpc_options=grpc_opts("mcp_pw")) as task:
        task.ci_channels.add_ci_pulse_width_chan(
            counter,
            min_val=min_val, max_val=max_val,
            units=TimeUnits.SECONDS,
            starting_edge=EDGE.get(starting_edge, Edge.RISING),
        )
        result = task.read(number_of_samples_per_channel=num_samples)
        return result if isinstance(result, list) else [result]


@mcp.tool()
def generate_pulse_train(
    counter: str = "Dev1/ctr0",
    frequency: float = 1000.0,
    duty_cycle: float = 0.5,
    duration: float = 5.0,
) -> str:
    """Generate a finite pulse train (Hz / duty cycle) on a CO channel for a duration."""
    n_pulses = max(1, int(frequency * duration))
    with nidaqmx.Task(new_task_name="mcp_co", grpc_options=grpc_opts("mcp_co")) as task:
        task.co_channels.add_co_pulse_chan_freq(
            counter,
            units=FrequencyUnits.HZ,
            idle_state=Level.LOW,
            freq=frequency,
            duty_cycle=duty_cycle,
        )
        task.timing.cfg_implicit_timing(
            sample_mode=AcquisitionType.FINITE, samps_per_chan=n_pulses,
        )
        task.start()
        task.wait_until_done(timeout=duration + 5.0)
        task.stop()
    return f"Pulse train {frequency}Hz @ {duty_cycle * 100:.0f}% on {counter} for {duration}s"


if __name__ == "__main__":
    mcp.run(transport="stdio")
