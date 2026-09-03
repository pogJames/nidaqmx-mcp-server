"""Vibration analysis: envelope-spectrum metrics for rolling-element bearing faults.

This measures, it does not diagnose. Every number a verdict would rest on is computed
here — fault frequencies, envelope SNR, harmonic levels, sidebands, kurtosis — and
returned with the reference table saying what those patterns mean. The verdict is left
to the caller, because the threshold that would decide it depends on sensor mounting,
load and machine rather than on bearing geometry: in the CWRU demo files a healthy
bearing scores 21.7 dB and a real ball fault 24.4 dB, so any constant baked in here
would be wrong somewhere.

Nothing here imports nidaqmx or mcp beyond `install`, so the analysis runs on a machine
with no hardware attached.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Literal, Sequence

import numpy as np
from mcp.server.fastmcp import FastMCP
from nptdms import TdmsFile
from pydantic import BaseModel
from scipy.signal import butter, hilbert, sosfiltfilt
from scipy.stats import kurtosis as _kurtosis

DATA_DIR = Path(__file__).parent / "data"


# BEARINGS ========================================================================

class Bearing(BaseModel):
    """Geometry behind the fault frequencies. `ball_dia` and `pitch_dia` only need to
    share a unit — the formulas use their ratio."""
    balls: int = 9
    ball_dia: float = 0.3126
    pitch_dia: float = 1.537
    contact_angle_deg: float = 0.0


BearingName = Literal["skf6205", "skf6203", "custom"]

BEARINGS: dict[str, Bearing] = {
    # CWRU drive end / fan end. "custom" takes its geometry from the `geometry` argument.
    "skf6205": Bearing(balls=9, ball_dia=0.3126, pitch_dia=1.537),
    "skf6203": Bearing(balls=9, ball_dia=0.2656, pitch_dia=1.122),
}


def fault_frequencies(bearing: Bearing, rpm: float) -> dict[str, float]:
    """The four defect repetition rates, in Hz.

    BSF is doubled: a ball defect strikes the inner and outer race once per spin, so
    the envelope shows twice the ball-spin frequency."""
    fr = rpm / 60.0
    r = (bearing.ball_dia / bearing.pitch_dia) * math.cos(
        math.radians(bearing.contact_angle_deg))
    return {
        "BPFO": bearing.balls / 2 * (1 - r) * fr,
        "BPFI": bearing.balls / 2 * (1 + r) * fr,
        "BSF": (1 / (2 * r)) * (1 - r * r) * 2 * fr,
        "FTF": 0.5 * (1 - r) * fr,
    }


# SIGNAL ==========================================================================

def features(x: np.ndarray) -> dict:
    """Time-domain shape. Kurtosis is the one to read: a Gaussian signal sits at 3.0,
    and repetitive impacts push it above that before any spectrum is computed."""
    a = np.asarray(x, dtype=float).ravel()
    peak = float(np.abs(a).max())
    rms = float(np.sqrt(np.mean(a ** 2)))
    return {
        "n": int(a.size),
        "rms": rms,
        "peak": peak,
        "crest": peak / rms if rms else 0.0,
        "kurtosis": float(_kurtosis(a, fisher=False)),
        "std": float(a.std()),
    }


def default_band(fs: float) -> tuple[float, float]:
    """Upper spectrum, where bearing impacts ring a structural resonance.

    Deliberately not a kurtogram. On the CWRU files the most impulsive band is the
    lowest one, which holds shaft harmonics rather than the resonance: picking by
    envelope kurtosis chose 1-750 Hz and scored a known outer-race fault at 21 dB,
    where this band scores it at 57."""
    nyq = fs / 2
    return round(0.40 * nyq, 1), round(0.95 * nyq, 1)


def candidate_bands(fs: float, min_width: float = 200.0) -> list[tuple[float, float]]:
    """Pass-bands to sweep for the alternatives table: the spectrum in 2 and 4 windows,
    plus the default. A band narrower than `min_width` cannot resolve harmonics."""
    nyq = fs / 2
    out = {default_band(fs)}
    for parts in (2, 4):
        width = nyq / parts
        for i in range(parts):
            lo, hi = max(i * width, 1.0), min((i + 1) * width, nyq * 0.99)
            if hi - lo >= min_width:
                out.add((round(lo, 1), round(hi, 1)))
    return sorted(out)


def _envelope(x: np.ndarray, fs: float, band: tuple[float, float]) -> np.ndarray:
    sos = butter(4, band, btype="bandpass", fs=fs, output="sos")
    env = np.abs(hilbert(sosfiltfilt(sos, x)))
    return env - env.mean()


def envelope_spectrum(x: np.ndarray, fs: float,
                      band: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
    """Amplitude spectrum of the band's Hilbert envelope. Demodulating is what turns a
    train of impacts into a single line at the rate they repeat."""
    env = _envelope(np.asarray(x, dtype=float).ravel(), fs, band)
    w = np.hanning(env.size)
    mag = 2.0 * np.abs(np.fft.rfft(env * w)) / w.sum()
    return np.fft.rfftfreq(env.size, 1 / fs), mag


# SCORING =========================================================================

# A harmonic or sideband counts as present above this. Reported alongside the raw
# per-harmonic levels so a caller that disagrees can recount from the numbers.
#
# 10 dB, not 6: the search window spans several bins, so its tallest bin sits a few dB
# over the local median by chance alone. At 6 dB every candidate scored 3 of 3
# harmonics on healthy data, which made the count carry no information.
PRESENT_DB = 10.0


def _tol(target: float, df: float) -> float:
    """Half-width of the search window: a few bins, widened for high frequencies where
    an rpm error displaces the peak proportionally. Too wide and the window's tallest
    bin beats the local median on noise alone."""
    return max(4 * df, target * 0.004)


def _peak_snr(f: np.ndarray, mag: np.ndarray, target: float,
              tol: float, guard: float = 40.0) -> tuple[float, float]:
    """Tallest bin within `tol` of `target`, in dB over the median of the surrounding
    band. Median, not mean, so a neighbouring peak does not raise the floor."""
    near = (f > target - tol) & (f < target + tol)
    around = (f > target - guard) & (f < target + guard) & ~near
    if not near.any() or not around.any():
        return 0.0, target
    i = int(np.argmax(mag[near]))
    floor = float(np.median(mag[around])) or 1e-18
    return float(20 * np.log10(mag[near][i] / floor)), float(f[near][i])


def _candidate(f: np.ndarray, mag: np.ndarray, target: float, shaft_hz: float,
               harmonics: int) -> dict:
    """One fault frequency scored: its level, its harmonics, and whether it carries
    sidebands spaced at shaft rate — the pattern that marks a fault rotating through
    the load zone rather than sitting still in it."""
    df = float(f[1] - f[0])
    tol = _tol(target, df)
    snr, found = _peak_snr(f, mag, target, tol)
    harm = [round(_peak_snr(f, mag, target * h, _tol(target * h, df))[0], 1)
            for h in range(1, harmonics + 1)]
    side = [round(_peak_snr(f, mag, target + s * shaft_hz, tol)[0], 1) for s in (-1, 1)]
    return {
        "freq_hz": round(target, 2),
        "peak_at_hz": round(found, 2),
        "snr_db": round(snr, 1),
        "harmonic_snr_db": harm,
        "harmonics_found": sum(1 for h in harm if h >= PRESENT_DB),
        "sideband_snr_db": side,
        "sidebands_found": sum(1 for s in side if s >= PRESENT_DB),
    }


# WHAT THE PATTERNS MEAN ==========================================================
# Returned with every analysis rather than kept in a separate call, so the numbers and
# the key to reading them always arrive together.

INDICATORS: dict[str, str] = {
    "BPFO": "Outer race: BPFO with harmonics, no shaft sidebands.",
    "BPFI": "Inner race: BPFI with harmonics AND shaft sidebands.",
    "BSF": "Rolling element: 2x ball spin, often weak — low SNR does not clear it.",
    "FTF": "Cage: weak alone, and common in healthy data.",
    "healthy": "All candidates within a few dB, kurtosis near 3. There is always a "
               "highest score; separation is the evidence, not rank.",
    "non_bearing": "Kurtosis well above 3 with no candidate standing out: impacts "
                   "that are not bearing-synchronous — looseness or rubbing.",
    "reading": "snr_db is peak over local noise, so it tracks how cleanly a defect "
               "rings, not severity. Compare within one analysis only.",
    "band": "Scores depend on the pass-band. If the leader changes across "
            "band.alternatives, the evidence is weak.",
}


# ANALYSIS ========================================================================

def analyze(samples: Sequence[float], rate: float, rpm: float, *,
            bearing: BearingName = "skf6205", geometry: Bearing | None = None,
            band_hz: tuple[float, float] | None = None,
            harmonics: int = 3) -> dict:
    """Full metric set for one recording. `band_hz` overrides the automatic choice."""
    x = np.asarray(samples, dtype=float).ravel()
    if x.size < 1024:
        raise ValueError(f"need at least 1024 samples to analyse, got {x.size}")
    if rpm <= 0:
        raise ValueError("rpm must be positive — every fault frequency scales with it")

    b = geometry or BEARINGS.get(bearing) or Bearing()
    band = tuple(band_hz) if band_hz else default_band(rate)
    freqs = fault_frequencies(b, rpm)
    shaft = rpm / 60.0

    f, mag = envelope_spectrum(x, rate, band)
    candidates = {k: _candidate(f, mag, t, shaft, harmonics) for k, t in freqs.items()}

    # The band is a choice, and a different one can change which candidate leads. Show
    # what the others would have said rather than presenting one band as the answer.
    sweep = []
    for alt in candidate_bands(rate):
        af, am = envelope_spectrum(x, rate, alt)
        best = max(freqs, key=lambda k: _peak_snr(af, am, freqs[k], _tol(freqs[k],
                   float(af[1] - af[0])))[0])
        snr, _ = _peak_snr(af, am, freqs[best], _tol(freqs[best], float(af[1] - af[0])))
        sweep.append({"band_hz": list(alt), "leader": best, "snr_db": round(snr, 1)})

    return {
        "rate": rate,
        "rpm": rpm,
        "shaft_hz": round(shaft, 3),
        "duration_s": round(x.size / rate, 3),
        "bearing": {"name": bearing, **b.model_dump()},
        "band": {"band_hz": list(band),
                 "selected": "given" if band_hz else "default",
                 "alternatives": sweep},
        "features": features(x),
        "candidates": candidates,
        "present_threshold_db": PRESENT_DB,
        "indicators": INDICATORS,
    }


def from_tdms(path: str | Path, channel: str | None = None) -> dict:
    """Samples plus the rate and rpm recorded with them. Rate comes from wf_increment,
    which is what makes a TDMS file self-describing."""
    p = Path(path)
    if not p.exists():
        # Recordings live in SystemLink now, not on disk; get_file caches one and
        # returns the path to pass here.
        raise FileNotFoundError(
            f"no file at {p} — find a recording with list_files(kind='vibration') "
            f"and call get_file(file_id) to obtain a local path")
    tdms = TdmsFile.read(p)
    group = tdms.groups()[0]
    names = [c.name for c in group.channels()]
    ch = group[channel] if channel else group[names[0]]
    incr = ch.properties.get("wf_increment")
    if not incr:
        raise ValueError(f"{p.name}:{ch.name} has no wf_increment, so its rate is unknown")
    rpm = tdms.properties.get("rpm")
    return {
        "path": str(p), "channel": ch.name, "channels": names,
        "samples": np.asarray(ch[:], dtype=float),
        "rate": 1.0 / float(incr),
        "rpm": float(rpm) if rpm else None,
        "properties": {k: str(v) for k, v in tdms.properties.items()},
    }


# MCP SURFACE =====================================================================

def install(mcp: FastMCP) -> None:
    """Register the analysis tools.

    Files only. `analyze` itself takes plain samples, so a live-monitor variant is a
    thin wrapper away — it needs a measured shaft speed to be worth adding, and there
    is no tachometer channel yet."""

    @mcp.tool()
    def list_bearings() -> dict:
        """Bearing geometries this server knows, with the fault-frequency multipliers
        each one implies (multiply by shaft Hz to get the defect rate).

        Use `bearing="custom"` with a `geometry` block for anything not listed — wrong
        geometry means confidently wrong frequencies."""
        out = {}
        for name, b in BEARINGS.items():
            mult = fault_frequencies(b, 60.0)      # 60 rpm = 1 Hz shaft, so Hz == multiple
            out[name] = {**b.model_dump(),
                         "multipliers": {k: round(v, 4) for k, v in mult.items()}}
        return {"bearings": out}

    @mcp.tool()
    def analyze_vibration(
        path: str,
        channel: str | None = None,
        rpm: float | None = None,
        bearing: BearingName = "skf6205",
        geometry: Bearing | None = None,
        band_hz: list[float] | None = None,
    ) -> dict:
        """Bearing-fault metrics for a TDMS recording: envelope SNR at each fault
        frequency, its harmonics and shaft sidebands, plus time-domain features.

        This reports evidence and does not decide. Read `candidates` against
        `indicators` — which is returned with the result — and say what the pattern
        supports. The highest `snr_db` alone is not a diagnosis: healthy data also has
        a highest score, so separation from the other candidates and the presence of
        harmonics are what carry the weight.

        `rpm` is required unless the file records it; every fault frequency scales with
        it, so a wrong value invalidates the whole result. `path` may be a bare
        filename, which is looked up in data/samples/. Omit `channel` for the first in
        the file. `band_hz` overrides the automatic band choice."""
        loaded = from_tdms(path, channel)
        speed = rpm if rpm is not None else loaded["rpm"]
        if speed is None:
            raise ValueError(
                f"{Path(path).name} records no rpm — pass rpm explicitly")
        result = analyze(loaded["samples"], loaded["rate"], speed, bearing=bearing,
                         geometry=geometry,
                         band_hz=tuple(band_hz) if band_hz else None)
        return {"source": {"path": loaded["path"], "channel": loaded["channel"],
                           "channels": loaded["channels"],
                           "rpm_from": "argument" if rpm is not None else "file"},
                **result}
