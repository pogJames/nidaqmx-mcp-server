from pathlib import Path
from typing import Any

import numpy as np
from nptdms import TdmsFile
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("TDMS-Viewer")


def _open(path: str) -> TdmsFile:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"TDMS file not found: {p}")
    return TdmsFile.read(str(p))


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="replace")
    if hasattr(v, "isoformat"):
        return v.isoformat()
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


def _resolve_channel(tdms: TdmsFile, selector: str):
    """Selector is 'group/channel'. For single-group files, bare 'channel' also works."""
    if "/" in selector:
        group_name, chan_name = selector.split("/", 1)
        return tdms[group_name][chan_name]
    groups = tdms.groups()
    if len(groups) != 1:
        raise ValueError(
            f"selector '{selector}' is ambiguous; file has {len(groups)} groups. "
            "Use 'group/channel'."
        )
    return groups[0][selector]


def _expand_channels(tdms: TdmsFile, channels) -> list[str]:
    if channels is None:
        return [f"{g.name}/{c.name}" for g in tdms.groups() for c in g.channels()]
    if isinstance(channels, str):
        return [channels]
    return list(channels)


# ---------- Discovery & inspection ----------

@mcp.tool()
def list_tdms_files(directory: str = ".", recursive: bool = False) -> list[dict]:
    """List TDMS files in a directory with size and mtime."""
    d = Path(directory).expanduser()
    if not d.is_dir():
        raise ValueError(f"not a directory: {d}")
    pattern = "**/*.tdms" if recursive else "*.tdms"
    out = []
    for p in sorted(d.glob(pattern)):
        st = p.stat()
        out.append({
            "path": str(p),
            "size_bytes": st.st_size,
            "mtime": st.st_mtime,
        })
    return out


@mcp.tool()
def inspect_tdms(path: str) -> dict:
    """Return file structure: file properties, groups, and each channel's metadata."""
    tdms = _open(path)
    groups = []
    for g in tdms.groups():
        chans = []
        for c in g.channels():
            chans.append({
                "name": c.name,
                "num_samples": len(c),
                "dtype": str(c.dtype),
                "properties": {str(k): _jsonable(v) for k, v in c.properties.items()},
            })
        groups.append({
            "name": g.name,
            "properties": {str(k): _jsonable(v) for k, v in g.properties.items()},
            "channels": chans,
        })
    return {
        "path": str(Path(path).expanduser()),
        "properties": {str(k): _jsonable(v) for k, v in tdms.properties.items()},
        "groups": groups,
    }


@mcp.tool()
def read_tdms_channel(
    path: str,
    channel: str,
    start: int = 0,
    end: int | None = None,
    max_samples: int = 10000,
) -> dict:
    """Read raw samples from a TDMS channel. `channel` is 'group/channel'."""
    tdms = _open(path)
    chan = _resolve_channel(tdms, channel)
    total = len(chan)
    end = total if end is None else min(end, total)
    if end - start > max_samples:
        raise ValueError(
            f"range {end - start} exceeds max_samples={max_samples}; "
            "narrow the range or raise max_samples"
        )
    data = chan[start:end]
    return {
        "channel": f"{chan.group_name}/{chan.name}",
        "total_samples": total,
        "start": start,
        "end": end,
        "data": [float(v) for v in data],
    }


@mcp.tool()
def tdms_stats(
    path: str,
    channel: str,
    start: int = 0,
    end: int | None = None,
) -> dict:
    """Compute n/min/max/mean/std for a TDMS channel slice."""
    tdms = _open(path)
    chan = _resolve_channel(tdms, channel)
    data = chan[start:end] if end is not None else chan[start:]
    arr = np.asarray(data, dtype=float)
    if arr.size == 0:
        return {"channel": f"{chan.group_name}/{chan.name}", "n": 0}
    return {
        "channel": f"{chan.group_name}/{chan.name}",
        "n": int(arr.size),
        "min": float(arr.min()),
        "max": float(arr.max()),
        "mean": float(arr.mean()),
        "std": float(arr.std()),
    }


@mcp.tool()
def plot_tdms(
    path: str,
    channels: list[str] | str | None = None,
    start: int = 0,
    end: int | None = None,
    max_points: int = 1000,
    use_time_axis: bool = True,
    max_channels: int = 8,
) -> dict:
    """Get chart-ready data for one or more TDMS channels so the host can render a graph.

    This tool does NOT render an image itself. It returns pre-decimated x/y arrays which
    you should pass to a rendering tool like `visualize:show_widget` (Chart.js) to actually
    display the graph inline. This is the correct tool to use whenever the user asks to
    "plot" or "graph" a TDMS file.

    `channels` is a list of 'group/channel' selectors, a single string, or None (all).
    `max_points` caps each series via stride decimation to keep responses small.
    `use_time_axis` derives x from each channel's wf_increment when present, else sample index.

    Returns: {"xlabel": str, "series": [{"name", "units", "x": [...], "y": [...]}, ...]}
    """
    tdms = _open(path)
    selectors = _expand_channels(tdms, channels)
    if not selectors:
        raise ValueError("no channels found")
    if len(selectors) > max_channels:
        raise ValueError(
            f"{len(selectors)} channels selected; exceeds max_channels={max_channels}"
        )

    series = []
    xlabel = "sample index"
    for sel in selectors:
        chan = _resolve_channel(tdms, sel)
        total = len(chan)
        e = total if end is None else min(end, total)
        raw = np.asarray(chan[start:e], dtype=float)
        if raw.size > max_points > 0:
            stride = raw.size // max_points + 1
            y = raw[::stride]
        else:
            stride = 1
            y = raw
        dt = float(chan.properties.get("wf_increment", 0.0)) if use_time_axis else 0.0
        if dt > 0:
            x = ((np.arange(y.size) * stride + start) * dt).tolist()
            xlabel = "time (s)"
        else:
            x = (np.arange(y.size) * stride + start).tolist()
        units = (
            chan.properties.get("unit_string")
            or chan.properties.get("NI_UnitDescription")
            or ""
        )
        series.append({
            "name": f"{chan.group_name}/{chan.name}",
            "units": str(units) if units else "",
            "x": x,
            "y": [float(v) for v in y],
        })
    return {"xlabel": xlabel, "series": series}


if __name__ == "__main__":
    mcp.run(transport="stdio")
