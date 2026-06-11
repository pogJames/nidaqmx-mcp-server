# tdms-research

MCP server for browsing, analyzing, and reporting on TDMS files via a local
web UI. Claude drives the tools; the dashboard (`http://127.0.0.1:7878/`)
renders interactive Plotly charts and a live pass/fail panel.

## Install

```powershell
cd research
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

## Run standalone (for development)

```powershell
python server.py
```

This starts the MCP server on stdio and the dashboard on
`http://127.0.0.1:7878/`. Use `open_view` from Claude to launch the browser,
or visit the URL directly.

## Register with Claude Desktop

Edit `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tdms-research": {
      "command": "C:/Users/james/Desktop/Coding/NI-examples/research/.venv/Scripts/python.exe",
      "args": ["C:/Users/james/Desktop/Coding/NI-examples/research/server.py"]
    }
  }
}
```

Restart Claude Desktop.

## Tool surface (v1)

**Files**
- `load_tdms(path)` — returns `file_id` and group → channel map.
- `unload(file_id)`, `list_files`, `list_groups`, `list_channels`, `get_channel_info`.

**Stats and plots**
- `get_stats(file_id, group, channel)` — min/max/mean/std/rms/peak.
- `plot_channel(file_id, group, channel, decimate=, t_start=, t_end=)`.
- `overlay([{file_id, group, channel}, …], decimate=)`.
- `plot_fft(file_id, group, channel)` — returns peak frequency.
- `plot_psd(file_id, group, channel, nperseg=)`.

**Limits / pass-fail**
- `add_limit(file_id, group, channel, kind, op, value)` — kinds: `min, max, mean, std, rms, peak`; ops: `<, <=, >, >=`.
- `evaluate_limits()` — updates statuses, returns the list.
- `clear_limits()`, `clear_view()`.

**View**
- `open_view()` — launches the browser tab.

## Architecture

```
server.py            # MCP entry point — starts uvicorn in a background thread
app/state.py         # in-memory session (files, limits, SSE subscribers)
app/analysis.py      # nptdms reads + numpy/scipy DSP + plotly figure builders
app/web.py           # FastAPI routes + SSE
app/static/          # index.html, app.js, style.css (Plotly via CDN)
```

The MCP thread mutates `state` and broadcasts via `state.push_figure` /
`state.notify`; the FastAPI thread relays those events over Server-Sent
Events to all connected browser tabs.

## Notes & limitations (v1)

- `TdmsFile.read()` loads the whole file into memory. Fine for sub-GB
  captures; for larger files swap to `TdmsFile.open()` + streamed reads.
- Plotly is loaded from `cdn.plot.ly`. For offline use, vendor
  `plotly-2.35.2.min.js` into `app/static/` and update `index.html`.
- No persistence yet — files, limits, and annotations are lost on restart.
  SQLite is the planned backing store (see `discussion of v2 features`).
