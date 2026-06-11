import numpy as np
import plotly.graph_objects as go
from scipy import signal

from .state import LoadedFile


def channel_data(loaded: LoadedFile, group: str, channel: str) -> tuple[np.ndarray, np.ndarray]:
    ch = loaded.tdms[group][channel]
    y = np.asarray(ch[:], dtype=float)
    try:
        x = np.asarray(ch.time_track(), dtype=float)
    except (KeyError, ValueError):
        x = np.arange(len(y), dtype=float)
    return x, y


def stats(y: np.ndarray) -> dict:
    if len(y) == 0:
        return {"n": 0}
    return {
        "n": int(len(y)),
        "min": float(np.min(y)),
        "max": float(np.max(y)),
        "mean": float(np.mean(y)),
        "std": float(np.std(y)),
        "rms": float(np.sqrt(np.mean(y * y))),
        "peak": float(np.max(np.abs(y))),
    }


def fft_data(y: np.ndarray, dt: float) -> tuple[np.ndarray, np.ndarray]:
    n = len(y)
    if n < 2 or dt <= 0:
        return np.array([0.0]), np.array([0.0])
    win = signal.windows.hann(n)
    yw = (y - np.mean(y)) * win
    Y = np.fft.rfft(yw)
    freqs = np.fft.rfftfreq(n, d=dt)
    mag = (2.0 / np.sum(win)) * np.abs(Y)
    return freqs, mag


def find_first_peak(x: np.ndarray, y: np.ndarray, threshold_frac: float = 0.99) -> dict:
    if len(y) == 0:
        raise ValueError("empty channel")
    peak_val = float(np.max(y))
    threshold = threshold_frac * peak_val
    above = y >= threshold
    if not above.any():
        raise ValueError("no samples above threshold")
    transitions = np.diff(above.astype(np.int8))
    starts = np.where(transitions == 1)[0] + 1
    ends = np.where(transitions == -1)[0] + 1
    if above[0]:
        starts = np.concatenate(([0], starts))
    if above[-1]:
        ends = np.concatenate((ends, [len(above)]))
    first_start = int(starts[0])
    first_end = int(ends[0]) - 1
    mid = (first_start + first_end) // 2
    return {
        "time": float(x[mid]),
        "value": float(y[mid]),
        "index": int(mid),
        "cluster_start_index": first_start,
        "cluster_end_index": first_end,
        "peak_value": peak_val,
        "threshold": float(threshold),
    }


def welch_psd(y: np.ndarray, dt: float, nperseg: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    if dt <= 0 or len(y) < 8:
        return np.array([0.0]), np.array([0.0])
    fs = 1.0 / dt
    nperseg = nperseg or min(len(y), 4096)
    f, pxx = signal.welch(y, fs=fs, nperseg=nperseg)
    return f, pxx


def make_line_fig(traces: list[dict], title: str = "", xlabel: str = "time (s)", ylabel: str = "") -> go.Figure:
    fig = go.Figure()
    for t in traces:
        fig.add_trace(go.Scattergl(x=t["x"], y=t["y"], name=t["name"], mode="lines"))
    fig.update_layout(
        title=title,
        xaxis_title=xlabel,
        yaxis_title=ylabel,
        template="plotly_white",
        margin=dict(l=60, r=20, t=50, b=50),
        legend=dict(orientation="h", y=-0.2),
    )
    return fig
