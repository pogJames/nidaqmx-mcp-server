# Agentic Testing and Monitoring System

Test benches have capable hardware and mature software. But humans still need to:

- look up specs
- configure channels
- plan tests
- code the tests
- monitor tests
- run the analysis
- write the report
- etc etc etc

So...

***an MCP server for NI DAQ rigs → Do ALL the above in Claude or ChatGPT or other LLMs.***
  
> ***Describe what you want in plain English and let the AI run it, end to end!***


  What keeps that trustworthy is ***what it won't do***. Plans are checked against the rig's
  real capabilities before anything is driven. Every run is recorded and every report is
  self-contained. Analysis returns evidence, not verdicts — the interpretation is signed
  by whoever wrote it. 
  
Integrated with the NI ecosystem: [NI SystemLink](https://www.ni.com/en/shop/electronic-test-instrumentation/application-software-for-electronic-test-and-instrumentation-category/systemlink.html) for file management, and [NI-DAQmx](https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html) + [NI gRPC Device Server](https://github.com/ni/grpc-device/releases) for hardware control.

## Setup

### Hardware requirements

- Your laptop → actual user or client
- Matrix-800 → agentic helper in the form of an MCP server
- x86 PC → simulating existing NI environment
- NI CompactDAQ with its modules

### Prerequisites

- [Claude Desktop](https://claude.com/download) installed on your laptop
- [NI Systemlink](https://www.ni.com/en/shop/electronic-test-instrumentation/application-software-for-electronic-test-and-instrumentation-category/systemlink.html?srsltid=AfmBOopJHEtmiJhni50_LrCdFA-rTdUQIQ9wwcHDU51wg6AtWrGNe8Nb) installed and activated on x86 PC
- [NI DAQmx](https://www.ni.com/en/support/downloads/drivers/download.ni-daq-mx.html?srsltid=AfmBOoqT2gVISixMBwv0jWhaQPJnV1vh9WPWSOt7L1ZGmBzlNjsH6CzT#607420) installed on x86 PC
- [NI gRPC Device Server](https://github.com/ni/grpc-device/releases) running on x86 PC

### MCP server on Matrix-800 Quick Start

```bash
pip install -r ../requirements.txt
cp .env.example .env      # then fill in the credentials
python server.py
```

`.env` (untracked) — everything not derivable:

```
DAQ_HOST=192.168.1.129        # x86 PC address
GRPC_PORT=31763               # default gRPC address
SYSTEMLINK_USER=<login>       # Systemlink account
SYSTEMLINK_PASSWORD=<password>
SYSTEMLINK_VERIFY_TLS=false   # self-signed cert
```

`HOST` is detected from the interface that routes toward `DAQ_HOST`. Set it in `.env`
only where that can't be right (container, NAT, reverse proxy). Codebase binds **port 80**, so the dashboard is a bare `http://<host>`.

### Claude Desktop config

```json
{ "mcpServers": { 
    "nidaqmx": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://<host>/mcp", "--allow-http"] } } }
```

## Modules

| File| Use |
|---|---|
| `config.py` | every address and credential; loads `.env` |
| `server.py` | hardware tools, monitor, recorder, dashboard routes |
| `plans.py` / `plan_store.py` | plan schema, validation, JSON files under `data/plans/` |
| `runner.py` / `run_store.py` | execution, run records in `data/runs.db` |
| `reports.py` | run reports → HTML → SystemLink |
| `vibration.py` | envelope-spectrum bearing metrics (no verdict) |
| `vib_report.py` | vibration reports → HTML → SystemLink |
| `systemlink.py` | File service: upload, query, cached download |

## Tools

37 tools over `streamable-http`:

**System** `list_devices` `get_device_info` `check_support` `restart_device` `get_status`

**Measure** `measure_voltage` `measure_current` `measure_temperature` `read_digital`
`count_edges` `measure_frequency`

**Stimulus** `set_voltage` `set_digital` `set_waveform` `pulse`

**Monitor** `start_monitor` `read_latest` `start_recording` `stop_recording` `stop`

**Plans** `plan_template` `validate_plan` `create_plan` `update_plan` `get_plan`
`list_plans` `delete_plan`

**Runs** `submit_run` `get_run` `list_runs` `control_run` `generate_report`

**Vibration** `list_bearings` `analyze_vibration` `vibration_report`

**Files** `list_files` `get_file`

## Workflows

```
1. plan_template → validate_plan → create_plan → submit_run → get_run → generate_report
2. start_monitor → start_recording → stop_recording → vib_*.tdms
3. list_files(kind=vibration) → get_file → analyze_vibration → vibration_report
```

`analyze_vibration` reports evidence — SNR per fault frequency, harmonics, sidebands,
kurtosis — plus an `indicators` table saying what the patterns mean. It never returns a
verdict: the threshold that would decide one depends on mounting and load, not bearing
geometry. Pass your reading to `vibration_report`, which attributes it as yours.

## SystemLink conventions

Every uploaded file carries a `kind`, and the filename prefix follows it:

| kind | prefix | what |
|---|---|---|
| `measurement` | `meas_` | finite AI/AO capture |
| `vibration` | `vib_` | accelerometer recording |
| `test-report` | `runrep_` | plan run report |
| `vibration-report` | `vibrep_` | bearing analysis report |

Recordings also carry `device`, `channels`, `units`, `sample_rate_hz`, `duration_s`,
`samples`, and `rpm` — enough to choose a file from `list_files` without downloading it.

## Codebase developed using these hardware

| device | module | subsystems |
|---|---|---|
| DAQ1 | [NI cDAQ-9183](https://www.ni.com/en/shop/hardware/compactdaq-chassis/model-cdaq-9183?srsltid=AfmBOoqeBu97vUzquiVHt7cEzGD1LD7TPEENw8b2U5Nwt2DpKUGiJsv3) | chassis with 4 module slots
| DAQ1Mod1 | [NI-9234](https://www.ni.com/en/shop/hardware/sound-and-vibration/model-ni-9234?srsltid=AfmBOor8DX9HIoS2oYq32d7fqGyWQiY9IeOf2Yp7RwkvlKnXS2Jsjqif) | `ai` only, ±5 V, IEPE/TEDS |
| DAQ1Mod2 | [NI-9234](https://www.ni.com/en/shop/hardware/sound-and-vibration/model-ni-9234?srsltid=AfmBOor8DX9HIoS2oYq32d7fqGyWQiY9IeOf2Yp7RwkvlKnXS2Jsjqif) | `ai` only, ±5 V, IEPE/TEDS |
| DAQ1Mod3 | [NI-9219](https://www.ni.com/en/shop/hardware/strain--pressure--and-force/model-ni-9219?srsltid=AfmBOor8fyOaZk8F7peMm7eqTBDr95DviDRROVCGE0X6Fifv_XI-Vfzb) | `ai` only, max 100 S/s |
| DAQ1Mod4 | [NI-9263](https://www.ni.com/docs/en-US/bundle/ni-compactrio/page/ni-9263.html?srsltid=AfmBOookar-oHUL-DfKTa4xUd6V8XOJV-BPVR4u4-k64dlPJ2vAEw7Dp) | `ao` only |

No counters and no digital lines, so `count_edges`, `measure_frequency`, `pulse`,
`read_digital` and `set_digital` have no hardware here. `check_support` says so per
device, and `validate_plan` rejects a plan step before it touches anything.

## Gotchas

- **9234 needs `pseudodiff`**, not `diff`, and only accepts ±5 V. The default ±10 V
  `v_range` fails with `-200077`; `start_monitor(accel=False)` fails the same way.
- **Address SystemLink by IP.** `desktop-adqmgfd` is a NetBIOS name only Windows hosts
  on the LAN resolve, and this server runs on Linux.
- **Port 31763 must be open** on the DAQ box, and any firewall rule scoped to the
  subnet stops matching if the subnet changes.
- **One task per 9234.** A monitor reserves the whole module, so a second read on any
  of its channels fails `-50103`. Different modules run concurrently.
- **`rpm` must be right.** A 2% error moves a fault frequency outside the search
  window and a 57 dB defect reads as 3 dB, silently.
- **`index.html` is read once at import.** Restart to pick up dashboard changes.
