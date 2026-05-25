# NI-DAQmx MCP Server

A small [MCP](https://modelcontextprotocol.io) server that lets Claude Code (or any MCP client) drive NI-DAQmx hardware — read analog voltages, write analog voltages, and list devices — through the **NI gRPC Device Server**.

## How it fits together

```
┌──────────────┐  stdio   ┌──────────────┐  Python   ┌────────────────────────┐  gRPC    ┌──────────────┐
│ Claude Code  │ ───────► │  server.py   │ ────────► │ nidaqmx (grpc_session) │ ───────► │ ni_grpc_     │ ──► NI-DAQmx ──► HW
│ (MCP client) │ ◄─────── │  (FastMCP)   │ ◄──────── │                        │ ◄─────── │ device_server│
└──────────────┘          └──────────────┘           └────────────────────────┘          └──────────────┘
                                                                                          localhost:31763
```

- **`server.py`** — FastMCP server exposing 3 tools (`read_voltage`, `write_voltage`, `list_devices`). Communicates with Claude Code over stdio.
- **`build/RelWithDebInfo/ni_grpc_device_server.exe`** — the NI gRPC Device Server binary (from [ni/grpc-device](https://github.com/ni/grpc-device)). Bridges gRPC calls to the local NI-DAQmx driver.
- **`server_config.json`** — binds the gRPC server to `[::1]:31763` (loopback only).
- **`nidaqmx_pb2*.py` / `nidevice_pb2*.py` / `session_pb2*.py`** — generated proto stubs (not directly imported by `server.py`; included as a reference / for future expansion).

## Prerequisites

1. **NI-DAQmx Driver** installed on this machine — https://www.ni.com/en-us/support/downloads/drivers/download.ni-daqmx.html
2. **Python 3.10+**
3. **A DAQ device** in NI MAX — real hardware or a simulated device (NI MAX → right-click *Devices and Interfaces* → *Create New* → *Simulated NI-DAQmx Device*).
4. **Claude Code** — https://claude.com/claude-code

## Install Python dependencies

```powershell
pip install "mcp[cli]" nidaqmx grpcio
```

## Step 1 — Start the NI gRPC Device Server

Open a terminal and run the bundled binary:

```powershell
.\build\RelWithDebInfo\ni_grpc_device_server.exe
```

It reads `server_config.json` and listens on `localhost:31763`. Leave this terminal open. You should see a line like `Server listening on [::1]:31763`.

> The MCP server (`server.py`) does **not** start the gRPC device server for you — they are independent processes. If the gRPC server is not running, every MCP tool call will fail with `failed to connect to localhost:31763`.

## Step 2 — Register as a Claude Code custom connector

You can register the MCP server either from the CLI or by editing the MCP config file directly.

### Option A — CLI (recommended)

From this folder:

```powershell
claude mcp add ni-daqmx -- python "C:\path\to\server\server.py"
```

This adds it under **user** scope (available across all your Claude Code projects). Use `--scope project` to scope it to the current repo instead, which writes to `.mcp.json` in the project root.

Verify:

```powershell
claude mcp list
```

You should see `ni-daqmx` listed.

### Option B — Edit `~/.claude.json` (or `.mcp.json`) by hand

Add this under `mcpServers`:

```json
{
  "mcpServers": {
    "ni-daqmx": {
      "command": "python",
      "args": ["C:\\path\\to\\server\\server.py"],
      "env": {}
    }
  }
}
```

Restart Claude Code so it picks up the change.

## Step 3 — Use it from Claude Code

Inside Claude Code, run `/mcp` — `ni-daqmx` should appear with the tools `read_voltage`, `write_voltage`, `list_devices`. Then just ask:

- *"List the DAQ devices."* → calls `list_devices`
- *"Read 100 samples from Dev1/ai0 using RSE."* → calls `read_voltage(physical_channel="Dev1/ai0", num_samples=100, terminal_config="rse")`
- *"Set Dev1/ao0 to 2.5 volts."* → calls `write_voltage(physical_channel="Dev1/ao0", voltage=2.5)`

## Tool reference

| Tool | Signature | Notes |
|------|-----------|-------|
| `read_voltage` | `(physical_channel="Dev1/ai0", num_samples=10, terminal_config="diff") -> list[float]` | `terminal_config` is one of `diff`, `rse`, `nrse`. Blocking finite read. |
| `write_voltage` | `(physical_channel="Dev1/ao0", voltage=0.0) -> str` | Single-sample write. Returns a confirmation string. |
| `list_devices`  | `() -> list[str]` | Returns device names as seen in NI MAX. |

All three open a fresh `nidaqmx.Task` against the gRPC channel `localhost:31763` and close it on return — there is no persistent session, so calls are independent.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `failed to connect to all addresses` / `UNAVAILABLE` | `ni_grpc_device_server.exe` not running | Start it (Step 1). |
| `DaqError: -200220 Device identifier is invalid` | The device name passed in doesn't exist | `list_devices` first, or check NI MAX. |
| `ModuleNotFoundError: No module named 'mcp'` | Wrong Python env | Install deps in the same interpreter Claude Code launches (`python -c "import sys; print(sys.executable)"`). |
| Tools don't appear in `/mcp` | Server failed to start | Run `python server.py` manually; fix the traceback, then restart Claude Code. |
| Reads return zeros on a real device | Wiring / terminal config mismatch | Try `terminal_config="rse"` for single-ended signals; verify ground reference. |