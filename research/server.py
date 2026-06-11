import json
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from app import analysis
from app.state import state
from app.web import app as web_app
from shared.paths import DATA_DIR, resolve as _resolve_data_path

HOST = "127.0.0.1"
PORT = 7878
URL = f"http://{HOST}:{PORT}/"

mcp = FastMCP("tdms-research")


def _fig_to_dict(fig) -> dict:
    return json.loads(fig.to_json())


@mcp.tool()
def open_view() -> str:
    """Open the dashboard in the default browser."""
    webbrowser.open(URL)
    return URL


@mcp.tool()
def load_tdms(path: str) -> dict:
    """Load a TDMS file. `path` may be a bare filename (resolved under shared/data/)
    or an absolute path. Returns the file_id plus the group→channel map."""
    resolved = str(_resolve_data_path(path))
    file_id = state.add_file(resolved)
    f = state.get_file(file_id)
    groups = {g.name: [c.name for c in g.channels()] for g in f.tdms.groups()}
    state.notify("refresh_files")
    return {"file_id": file_id, "path": resolved, "groups": groups}


@mcp.tool()
def list_recordings() -> list[dict]:
    """List TDMS files in the shared data folder with size and mtime."""
    out = []
    for p in sorted(DATA_DIR.glob("*.tdms")):
        st = p.stat()
        out.append({"name": p.name, "size_bytes": st.st_size, "mtime": st.st_mtime})
    return out


@mcp.tool()
def unload(file_id: str) -> str:
    """Drop a loaded file from memory."""
    state.drop_file(file_id)
    state.notify("refresh_files")
    return f"unloaded {file_id}"


@mcp.tool()
def list_files() -> list[dict]:
    """List currently loaded files."""
    return [{"file_id": fid, "path": f.path} for fid, f in state.files.items()]


@mcp.tool()
def list_groups(file_id: str) -> list[str]:
    """List groups in a loaded file."""
    f = state.get_file(file_id)
    return [g.name for g in f.tdms.groups()]


@mcp.tool()
def list_channels(file_id: str, group: str) -> list[str]:
    """List channels in a group."""
    f = state.get_file(file_id)
    return [c.name for c in f.tdms[group].channels()]


class Channel(BaseModel):
    file_id: str
    group: str
    channel: str


class Trace(Channel):
    t_offset: float | None = Field(
        default=None,
        description="Shift this trace's time axis by this many seconds.",
    )
    t_start: float | None = None
    t_end: float | None = None


def _channel_xy(c: Channel) -> tuple:
    """Load (x, y, dt) for a channel."""
    f = state.get_file(c.file_id)
    x, y = analysis.channel_data(f, c.group, c.channel)
    dt = float(x[1] - x[0]) if len(x) > 1 else 1.0
    return x, y, dt


@mcp.tool()
def get_channel_info(channel: Channel) -> dict:
    """Return length and TDMS properties for a channel."""
    f = state.get_file(channel.file_id)
    ch = f.tdms[channel.group][channel.channel]
    return {
        "length": int(len(ch)),
        "properties": {k: str(v) for k, v in ch.properties.items()},
    }


@mcp.tool()
def get_stats(channel: Channel) -> dict:
    """Return min/max/mean/std/rms/peak for a channel."""
    _, y, _ = _channel_xy(channel)
    return analysis.stats(y)


def _build_trace(t: Trace, decimate: int = 1) -> dict:
    x, y, _ = _channel_xy(t)
    if t.t_offset:
        x = x + t.t_offset
    if t.t_start is not None or t.t_end is not None:
        lo = t.t_start if t.t_start is not None else x[0]
        hi = t.t_end if t.t_end is not None else x[-1]
        mask = (x >= lo) & (x <= hi)
        x, y = x[mask], y[mask]
    if decimate > 1:
        x, y = x[::decimate], y[::decimate]
    return {
        "x": x.tolist(), "y": y.tolist(),
        "name": f"{t.file_id} - {t.group}/{t.channel}",
    }


@mcp.tool()
def plot_channel(trace: Trace, decimate: int = 1) -> str:
    """Plot a channel on the dashboard. Optional decimation and time-window slicing."""
    plot_trace = _build_trace(trace, decimate=decimate)
    title = plot_trace["name"]
    fig = analysis.make_line_fig([plot_trace], title=title)
    state.push_figure(_fig_to_dict(fig))
    return f"plotted {len(plot_trace['y'])} samples for {title}"


def _align_offsets(traces: list[Trace]) -> list[float]:
    """Per-trace `t_offset` values that align each trace's first peak to the first trace."""
    peak_times = []
    for t in traces:
        x, y, _ = _channel_xy(t)
        peak_times.append(analysis.find_first_peak(x, y)["time"])
    anchor = peak_times[0]
    return [anchor - pt for pt in peak_times]


@mcp.tool()
def overlay_channels(traces: list[Trace], decimate: int = 1, align: bool = False) -> str:
    """Overlay multiple channels on the same axes.

    If `align=True`, each trace is time-shifted so its first peak lines up with
    the first trace's first peak.
    """
    if align:
        offsets = _align_offsets(traces)
        traces = [t.model_copy(update={"t_offset": o}) for t, o in zip(traces, offsets)]
    plot_traces = [_build_trace(t, decimate=decimate) for t in traces]
    title = " + ".join(t["name"] for t in plot_traces)
    fig = analysis.make_line_fig(plot_traces, title=title)
    state.push_figure(_fig_to_dict(fig))
    return f"overlaid {len(plot_traces)} traces for {title}"


def _plot_spectrum(
    channel: Channel, freqs, values,
    *, kind: str, ylabel: str, peak_key: str, log_y: bool = False,
) -> dict:
    """Plot a frequency-domain spectrum and return the peak."""
    fig = analysis.make_line_fig(
        [{"x": freqs.tolist(), "y": values.tolist(), "name": channel.channel}],
        title=f"{kind} — {channel.channel}", xlabel="frequency (Hz)", ylabel=ylabel,
    )
    fig.update_xaxes(type="log")
    if log_y:
        fig.update_yaxes(type="log")
    state.push_figure(_fig_to_dict(fig))
    i = int(values.argmax())
    return {"peak_freq_hz": float(freqs[i]), peak_key: float(values[i])}


@mcp.tool()
def plot_fft(channel: Channel) -> dict:
    """FFT magnitude (Hann window) of a channel. Returns peak frequency."""
    _, y, dt = _channel_xy(channel)
    freqs, mag = analysis.fft_data(y, dt)
    return _plot_spectrum(channel, freqs, mag, kind="FFT", ylabel="magnitude", peak_key="peak_mag")


@mcp.tool()
def plot_psd(channel: Channel, nperseg: int | None = None) -> dict:
    """Welch power spectral density of a channel. Returns peak frequency."""
    _, y, dt = _channel_xy(channel)
    freqs, pxx = analysis.welch_psd(y, dt, nperseg=nperseg)
    return _plot_spectrum(channel, freqs, pxx, kind="PSD", ylabel="power/Hz", peak_key="peak_power", log_y=True)


@mcp.tool()
def add_limit(
    file_id: str,
    group: str,
    channel: str,
    kind: str,
    op: str,
    value: float,
) -> dict:
    """Add a pass/fail limit.

    kind: min|max|mean|std|rms|peak.
    op:   <, <=, >, >= (or word forms: lt, lte, gt, gte). HTML-escaped
          forms like &lt; are also accepted and normalized.
    """
    canonical = _normalize_op(op)
    if canonical is None:
        raise ValueError(f"unknown op: {op!r}. Use one of <, <=, >, >= (or lt, lte, gt, gte).")
    record = {
        "file_id": file_id, "group": group, "channel": channel,
        "kind": kind, "op": canonical, "value": value,
        "status": None, "actual": None,
    }
    state.limits.append(record)
    state.notify("refresh_limits")
    return record


@mcp.tool()
def clear_limits() -> str:
    """Remove all pass/fail limits."""
    state.limits.clear()
    state.notify("refresh_limits")
    return "cleared"


_OPS = {
    "<":  lambda a, b: a <  b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a >  b,
    ">=": lambda a, b: a >= b,
}

_OP_ALIASES = {
    "<": "<", "lt": "<", "&lt;": "<", "less": "<", "lessthan": "<",
    "<=": "<=", "lte": "<=", "le": "<=", "&lt;=": "<=", "&le;": "<=",
    "=<": "<=", "atmost": "<=",
    ">": ">", "gt": ">", "&gt;": ">", "greater": ">", "greaterthan": ">",
    ">=": ">=", "gte": ">=", "ge": ">=", "&gt;=": ">=", "&ge;": ">=",
    "=>": ">=", "atleast": ">=",
}


def _normalize_op(op: str) -> str | None:
    if not isinstance(op, str):
        return None
    return _OP_ALIASES.get(op.strip().lower())


@mcp.tool()
def evaluate_limits() -> list[dict]:
    """Evaluate all configured limits and update their pass/fail status."""
    results = []
    for limit in state.limits:
        f = state.get_file(limit["file_id"])
        _, y = analysis.channel_data(f, limit["group"], limit["channel"])
        s = analysis.stats(y)
        actual = s.get(limit["kind"])
        canonical = _normalize_op(limit["op"]) or limit["op"]
        limit["op"] = canonical
        cmp = _OPS.get(canonical)
        if actual is None or cmp is None:
            limit["status"] = "error"
            limit["actual"] = actual
        else:
            limit["actual"] = float(actual)
            limit["status"] = "pass" if cmp(actual, limit["value"]) else "fail"
        results.append(dict(limit))
    state.notify("refresh_limits")
    return results


@mcp.tool()
def clear_view() -> str:
    """Clear the active chart."""
    state.push_figure({"data": [], "layout": {"title": ""}})
    return "cleared"


def _run_web() -> None:
    config = uvicorn.Config(web_app, host=HOST, port=PORT, log_level="warning")
    uvicorn.Server(config).run()


if __name__ == "__main__":
    threading.Thread(target=_run_web, daemon=True).start()
    mcp.run(transport="stdio")
