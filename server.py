import grpc
import nidaqmx
from nidaqmx.constants import TerminalConfiguration
from nidaqmx.grpc_session_options import GrpcSessionOptions
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("NI-DAQmx")
channel = grpc.insecure_channel("localhost:31763")

def grpc_opts(name: str):
    return GrpcSessionOptions(grpc_channel=channel, session_name=name)

@mcp.tool()
def read_voltage(physical_channel: str = "Dev1/ai0", num_samples: int = 10, terminal_config: str = "diff",) -> list[float]:
    """Read analog voltage"""
    term_map = {
        "diff": TerminalConfiguration.DIFF,
        "rse": TerminalConfiguration.RSE,
        "nrse": TerminalConfiguration.NRSE,
    }
    with nidaqmx.Task(new_task_name="mcp_read", grpc_options=grpc_opts("mcp_read")) as task:
        task.ai_channels.add_ai_voltage_chan(
            physical_channel,
            terminal_config=term_map.get(terminal_config, TerminalConfiguration.DIFF),
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
def list_devices() -> list[str]:
    """List available devices"""
    system = nidaqmx.system.System(grpc_options=grpc_opts("mcp_system"))
    return [d.name for d in system.devices]

mcp.run(transport="stdio")