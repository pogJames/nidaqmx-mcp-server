import json
import threading
import webbrowser

import uvicorn
from mcp.server.fastmcp import FastMCP

from app import analysis
from app.state import state
from app.web import app as web_app

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
    """Load a TDMS file. Returns its file_id plus the group→channel map."""
    file_id = state.add_file(path)
    f = state.get_file(file_id)
    groups = {g.name: [c.name for c in g.channels()] for g in f.tdms.groups()}
    state.notify("refresh_files")
    return {"file_id": file_id, "path": path, "groups": groups}


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


@mcp.tool()
def get_channel_info(file_id: str, group: str, channel: str) -> dict:
    """Return length and TDMS properties for a channel."""
    f = state.get_file(file_id)
    ch = f.tdms[group][channel]
    return {
        "length": int(len(ch)),
        "properties": {k: str(v) for k, v in ch.properties.items()},
    }


@mcp.tool()
def get_stats(file_id: str, group: str, channel: str) -> dict:
    """Return min/max/mean/std/rms/peak for a channel."""
    f = state.get_file(file_id)
    _, y = analysis.channel_data(f, group, channel)
    return analysis.stats(y)


@mcp.tool()
def plot_channel(
    file_id: str,
    group: str,
    channel: str,
    decimate: int = 1,
    t_start: float | None = None,
    t_end: float | None = None,
) -> str:
    """Plot a channel on the dashboard. Optional decimation and time-window slicing."""
    f = state.get_file(file_id)
    x, y = analysis.channel_data(f, group, channel)
    if t_start is not None or t_end is not None:
        lo = t_start if t_start is not None else x[0]
        hi = t_end if t_end is not None else x[-1]
        mask = (x >= lo) & (x <= hi)
        x, y = x[mask], y[mask]
    if decimate > 1:
        x, y = x[::decimate], y[::decimate]
    fig = analysis.make_line_fig(
        [{"x": x.tolist(), "y": y.tolist(), "name": channel}],
        title=channel, ylabel=channel,
    )
    state.push_figure(_fig_to_dict(fig))
    return f"plotted {len(y)} samples"


@mcp.tool()
def overlay(traces: list[dict], decimate: int = 1, align: bool = False) -> str:
    """Overlay multiple channels on the same axes.

    Each trace: {file_id, group, channel, t_offset?}.
    `t_offset` (seconds) shifts that trace's time axis — e.g. `t_offset=-0.15`
    plots the trace 0.15 s earlier so it lines up with another trace.
    """
    if align:
        peaks = first_peak(traces)
        anchor = peaks[0]["time"]
        traces[0]["t_offset"] = 0.0
        for t, p in zip(traces[1:], peaks[1:]):
            t["t_offset"] = anchor - p["time"]

    plot_traces = []
    total_samples = 0
    for t in traces:
        f = state.get_file(t["file_id"])
        x, y = analysis.channel_data(f, t["group"], t["channel"])
        t_offset = t.get("t_offset")
        if t_offset:
            x = x + float(t_offset)
        if decimate > 1:
            x, y = x[::decimate], y[::decimate]
        plot_traces.append({
            "x": x.tolist(), "y": y.tolist(),
            "name": f"{t['group']}/{t['channel']}",
        })
        total_samples += len(y)
    title = " + ".join(t["file_id"] for t in traces)
    fig = analysis.make_line_fig(plot_traces, title=title)
    state.push_figure(_fig_to_dict(fig))
    return f"overlaid {len(plot_traces)} traces ({total_samples} samples total)"


def first_peak(traces: list[dict], threshold_frac: float = 0.999) -> list[dict]:
    """Find peak times for aligning waves — feed into `overlay`'s `t_offset`.

    Traces: [{file_id, group, channel}, ...]. Returns one result per trace
    with `time` (peak timestamp), `value`, `index`, `peak_value`,
    `threshold`, and `error?` on failure.
    """
    results = []
    for t in traces:
        result = {
            "file_id": t.get("file_id"),
            "group": t.get("group"),
            "channel": t.get("channel"),
        }
        try:
            f = state.get_file(t["file_id"])
            x, y = analysis.channel_data(f, t["group"], t["channel"])
            result.update(analysis.find_first_peak(x, y, threshold_frac))
        except Exception as e:
            result["error"] = str(e)
        results.append(result)
    return results


@mcp.tool()
def plot_fft(file_id: str, group: str, channel: str) -> dict:
    """FFT magnitude (Hann window) of a channel. Returns peak frequency."""
    f = state.get_file(file_id)
    x, y = analysis.channel_data(f, group, channel)
    dt = float(x[1] - x[0]) if len(x) > 1 else 1.0
    freqs, mag = analysis.fft_data(y, dt)
    fig = analysis.make_line_fig(
        [{"x": freqs.tolist(), "y": mag.tolist(), "name": channel}],
        title=f"FFT — {channel}", xlabel="frequency (Hz)", ylabel="magnitude",
    )
    fig.update_xaxes(type="log")
    state.push_figure(_fig_to_dict(fig))
    i = int(mag.argmax())
    return {"peak_freq_hz": float(freqs[i]), "peak_mag": float(mag[i])}


@mcp.tool()
def plot_psd(file_id: str, group: str, channel: str, nperseg: int | None = None) -> str:
    """Welch power spectral density of a channel."""
    f = state.get_file(file_id)
    x, y = analysis.channel_data(f, group, channel)
    dt = float(x[1] - x[0]) if len(x) > 1 else 1.0
    freqs, pxx = analysis.welch_psd(y, dt, nperseg=nperseg)
    fig = analysis.make_line_fig(
        [{"x": freqs.tolist(), "y": pxx.tolist(), "name": channel}],
        title=f"PSD — {channel}", xlabel="frequency (Hz)", ylabel="power/Hz",
    )
    fig.update_xaxes(type="log")
    fig.update_yaxes(type="log")
    state.push_figure(_fig_to_dict(fig))
    return "ok"


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
