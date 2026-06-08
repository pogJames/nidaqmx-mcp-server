import grpc
import nidaqmx
from nidaqmx.constants import TerminalConfiguration, AcquisitionType
from nidaqmx.grpc_session_options import GrpcSessionOptions
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("NI-DAQmx")
channel = grpc.insecure_channel("localhost:31763")

def grpc_opts(name: str):
    return GrpcSessionOptions(grpc_channel=channel, session_name=name)

@mcp.tool()
def read_voltage(
    physical_channel: str = "Dev1/ai0", 
    num_samples: int = 10, 
    terminal_config: str = "diff",
    sample_rate: int = 100,
    acquisition_type: str = "continuous"
) -> list[float]:
    """Read analog voltage"""
    term_map = {
        "diff": TerminalConfiguration.DIFF,
        "rse": TerminalConfiguration.RSE,
        "nrse": TerminalConfiguration.NRSE,
    }
    
    acq_map = {
        "continuous": AcquisitionType.CONTINUOUS,
        "finite": AcquisitionType.FINITE,
    }
    
    with nidaqmx.Task(new_task_name="mcp_read", grpc_options=grpc_opts("mcp_read")) as task:
        task.ai_channels.add_ai_voltage_chan(
            physical_channel,
            terminal_config=term_map.get(terminal_config, TerminalConfiguration.DIFF),
        )
        task.timing.cfg_samp_clk_timing(
            rate=sample_rate,
            sample_mode=acq_map.get(acquisition_type.lower(), AcquisitionType.CONTINUOUS),
            samps_per_chan=num_samples
        )
        return task.read(number_of_samples_per_channel=num_samples)

@mcp.tool()
def write_voltage(physical_channel: str = "Dev1/ao0", voltage: float = 0.0) -> str:
    """Write analog voltage"""
    with nidaqmx.Task(new_task_name="mcp_write", grpc_options=grpc_opts("mcp_write")) as task:
        task.ao_channels.add_ao_voltage_chan(physical_channel)
        task.write(voltage)
    return f"Wrote {voltage}V to {physical_channel}"

@mcp.tool()
def write_sine(
    physical_channel: str = "Dev1/ao0",
    frequency: float = 10.0,
    amplitude: float = 2.0,
    sample_rate: int = 1000,
    duration: float = 5.0,
) -> str:
    """Output a sine wave on an analog output channel for a given duration."""
    import numpy as np
    import time

    samples = int(sample_rate)
    t = np.linspace(0, samples / sample_rate, samples, endpoint=False)
    waveform = amplitude * np.sin(2 * np.pi * frequency * t)

    opts = grpc_opts("mcp_sine")
    with nidaqmx.Task(new_task_name="mcp_sine", grpc_options=opts) as task:
        task.ao_channels.add_ao_voltage_chan(physical_channel)
        task.timing.cfg_samp_clk_timing(
            rate=sample_rate,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=samples,
        )
        task.write(waveform)
        task.start()
        time.sleep(duration)
        task.stop()

    return f"Sine {frequency}Hz, {amplitude}V on {physical_channel} for {duration}s"

@mcp.tool()
def list_devices() -> list[str]:
    """List available devices"""
    system = nidaqmx.system.System(grpc_options=grpc_opts("mcp_system"))
    return [d.name for d in system.devices]

if __name__ == "__main__":
    mcp.run(transport="stdio")