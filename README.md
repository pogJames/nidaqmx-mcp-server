# new_server

MCP server for an NI cDAQ rig: measurement tools, declarative test plans, a live
dashboard, bearing-fault analysis, and HTML reports. Files and reports live in
SystemLink; only plans, the run database, and a re-fetchable cache stay local.

37 tools over `streamable-http`.

## Setup

```bash
pip install -r ../requirements.txt
cp .env.example .env      # then fill in the credentials
python server.py
```

`.env` (untracked) — everything not derivable:

```
DAQ_HOST=192.168.1.129        # gRPC device server + SystemLink, same box
GRPC_PORT=31763
SYSTEMLINK_USER=<login>       # a SystemLink account; basic auth, no API key needed
SYSTEMLINK_PASSWORD=<password>
SYSTEMLINK_VERIFY_TLS=false   # self-signed cert
```

`HOST` is detected from the interface that routes toward `DAQ_HOST`. Set it in `.env`
only where that can't be right — container, NAT, reverse proxy.

Binds **port 80**, so the dashboard is a bare `http://<host>`. Privileged on Linux:
run as root, or change the port in `server.py:47` and `config.DASHBOARD_URL` together.

## Claude Desktop

```json
{ "mcpServers": { "nidaqmx": {
    "command": "npx",
    "args": ["-y", "mcp-remote", "http://192.168.1.72/mcp", "--allow-http"] } } }
```

## Modules

| | |
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
plan_template → validate_plan → create_plan → submit_run → get_run → generate_report
start_monitor → start_recording → stop_recording            → vib_*.tdms
list_files(kind=…) → get_file → analyze_vibration → vibration_report
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

## This rig

| device | module | subsystems |
|---|---|---|
| DAQ1Mod1, DAQ1Mod2 | NI 9234 | `ai` only, ±5 V, IEPE/TEDS |
| DAQ1Mod3 | NI 9219 | `ai` only, max 100 S/s |
| DAQ1Mod4 | NI 9263 | `ao` only |

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
