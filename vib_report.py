"""HTML report for one vibration analysis.

Shares the look and the chart conventions of reports.py — self-contained page, inline
SVG, no CDN — but describes a different thing: a run report answers "did it pass", this
answers "what does the spectrum say".

The reading at the top is written by the caller, not derived here. vibration.py
deliberately reports evidence without a verdict, and rendering is not the place to
smuggle one back in; what this page does is put the evidence somewhere a person can
check the reading against.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
from mcp.server.fastmcp import FastMCP

import reports
import systemlink
import vibration
from reports import _e, _fmt

# Layout of every chart on the page.
W, PAD_L, PAD_R, PAD_T = 940, 66, 16, 18

_EXTRA_CSS = """
.reading { background: var(--card); border: 1px solid var(--rule); border-left: 3px
  solid var(--accent); border-radius: 8px; padding: 14px 16px; margin: 16px 0 4px;
  font-size: 15px; line-height: 1.55; }
.reading .who { display: block; font-size: 11.5px; color: var(--muted);
  margin-top: 8px; }
.lead { font-weight: 600; }
.bar { display: inline-block; height: 9px; border-radius: 2px;
  background: var(--accent); vertical-align: middle; }
.agree { color: var(--ink-2); font-size: 12.5px; margin-top: 10px; }

/* The shared rule is `svg { display:block; width:100% }`, which is right for a chart
   and would stretch a legend sample across the whole card. */
.legend svg { display: inline-block; width: 26px; height: 10px; flex: none; }
.legend span { gap: 7px; }

/* The shared table rule is nowrap, which is right for numbers and wrong for the
   indicator sentences — they overshoot the card. This table holds prose. */
table.prose td { white-space: normal; text-align: left; line-height: 1.5;
  color: var(--ink-2); }
table.prose td:first-child { white-space: nowrap; vertical-align: top;
  width: 1%; color: var(--ink); }
"""


def source_name(path: str | Path) -> str:
    """The recording's own name, not the cache's.

    get_file stores downloads as `<file_id>.tdms`, so a path straight out of the cache
    carries an id where a reader expects a name. Look the id back up rather than
    letting `6a97c71b…tdms` reach the report title."""
    p = Path(path)
    if p.parent == systemlink.CACHE_DIR:
        try:
            props = systemlink.file_info(p.stem)["properties"]
            return props.get("Name") or props.get("original_name") or p.name
        except Exception:
            return p.name          # offline or deleted upstream: the id is all we have
    return p.name


def _subject(name: str) -> str:
    """Filename stem for the report, without repeating the source's kind prefix —
    `vib_cwru_97_normal.tdms` names a report about `cwru_97_normal`."""
    stem = Path(name).stem
    for prefix in systemlink.PREFIX.values():
        if stem.startswith(prefix + "_"):
            return stem[len(prefix) + 1:]
    return stem


def _thin(values: np.ndarray, target: int) -> tuple[np.ndarray, int]:
    """Keep the tallest sample of each group. A stride would drop whichever narrow peak
    happened to land between two kept indices, and narrow peaks are the whole subject."""
    k = max(values.size // target, 1)
    if k == 1:
        return values, 1
    trimmed = values[:values.size // k * k]
    return trimmed.reshape(-1, k).max(axis=1), k


# CHARTS ==========================================================================

def _spectrum_svg(result: dict, freqs: np.ndarray, mags: np.ndarray) -> str:
    """Envelope spectrum with every predicted frequency marked.

    The y-axis is dB over the spectrum's own noise floor, so bar heights read on the
    same scale as the SNR column. It is a global floor where the table uses a local
    one, so a tall peak reads a little higher here than its tabulated dB."""
    cands = result["candidates"]
    leader = max(cands, key=lambda k: cands[k]["snr_db"])
    shaft = result["shaft_hz"]

    # Envelope energy lives at the bottom of the range; plotting to Nyquist would
    # squeeze every marker into the left few percent.
    x_max = min(float(freqs[-1]), max(c["freq_hz"] for c in cands.values()) * 4.2)
    keep = freqs <= x_max
    m = mags[keep]

    floor = float(np.median(m)) or 1e-18
    db = 20 * np.log10(np.maximum(m, 1e-18) / floor)
    db, _ = _thin(np.maximum(db, 0.0), 900)
    step = x_max / max(db.size - 1, 1)

    top = max(float(db.max()) * 1.12, 10.0)
    h = 360
    plot_h = h - PAD_T - 88          # room for two label rows plus the Hz axis
    plot_w = W - PAD_L - PAD_R

    def X(hz: float) -> float:
        return PAD_L + plot_w * min(hz / x_max, 1.0)

    def Y(value: float) -> float:
        return PAD_T + plot_h * (1 - min(value / top, 1.0))

    p = [f'<svg viewBox="0 0 {W} {h}" role="img" aria-label="Envelope spectrum, '
         f'{leader} leading at {cands[leader]["snr_db"]} dB">']

    for frac in (0, .25, .5, .75, 1):
        value = top * frac
        y = Y(value)
        p.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
                 f'stroke="var(--grid)" stroke-width="1"/>')
        p.append(f'<text x="{PAD_L - 10}" y="{y + 4:.1f}" text-anchor="end" '
                 f'font-size="11" fill="var(--muted)">{value:.0f}</text>')

    # Markers first, so the trace draws over them.
    #
    # Line style carries the harmonic order: solid for a fundamental, dashed for its
    # harmonics, dotted for the sidebands. That is what lets every candidate's
    # harmonics be drawn without the chart reading as an unexplained picket fence —
    # an unlabelled dashed line is legible from its style alone.
    lead_c = cands[leader]
    marks: list[tuple[float, str, str]] = []
    for name, c in cands.items():
        lead = name == leader
        for n in range(1, len(c["harmonic_snr_db"]) + 1):
            hz = c["freq_hz"] * n
            if hz > x_max:
                continue
            kind = ("lead" if lead else "other") if n == 1 else \
                   ("lead_h" if lead else "other_h")
            marks.append((hz, name if n == 1 else f"{name} {n}x", kind))
    for sign in (-1, 1):                      # sidebands flank the leader unlabelled
        hz = lead_c["freq_hz"] + sign * shaft
        if 0 < hz <= x_max:
            marks.append((hz, "", "side"))

    STYLE = {                                  # width, opacity, dash, colour
        "lead":    (1.8, 0.90, "", "var(--accent)"),
        "lead_h":  (1.2, 0.50, ' stroke-dasharray="6 4"', "var(--accent)"),
        "side":    (1.0, 0.42, ' stroke-dasharray="1.5 3"', "var(--accent)"),
        "other":   (1.3, 0.55, "", "var(--ink-2)"),
        "other_h": (1.0, 0.28, ' stroke-dasharray="6 4"', "var(--ink-2)"),
    }
    for hz, label, kind in sorted(marks):
        width, opacity, dash, colour = STYLE[kind]
        x = X(hz)
        p.append(f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" '
                 f'y2="{PAD_T + plot_h}" stroke="{colour}" '
                 f'stroke-width="{width}" opacity="{opacity}"{dash}/>')

    # Trace.
    pts = " ".join(f"{X(i * step):.1f},{Y(v):.1f}" for i, v in enumerate(db))
    p.append(f'<polyline points="{pts}" fill="none" stroke="var(--ink)" '
             f'stroke-width="1.1" stroke-linejoin="round"/>')

    # Every drawn line gets a label, name and frequency on one line, alternating rows
    # so neighbours do not collide. Sidebands are the exception: they sit within a
    # shaft rate of the leader, so a label there would overlap it.
    axis_y = PAD_T + plot_h
    p.append(f'<line x1="{PAD_L}" y1="{axis_y}" x2="{W - PAD_R}" y2="{axis_y}" '
             f'stroke="var(--rule)" stroke-width="1"/>')
    # Only fundamentals and the leader's harmonics get text. The rest are legible from
    # their style, and labelling them collides — FTF's three orders sit 12 Hz apart.
    labelled = [m for m in sorted(marks) if m[1] and m[2] in ("lead", "other", "lead_h")]
    for i, (hz, label, kind) in enumerate(labelled):
        lead = kind == "lead"
        p.append(
            f'<text x="{X(hz):.1f}" y="{axis_y + 16 + (i % 2) * 14}" '
            f'text-anchor="middle" font-size="11" '
            f'fill="{"var(--accent)" if kind != "other" else "var(--ink-2)"}" '
            f'font-weight="{600 if lead else 400}">'
            f'{_e(label)} <tspan fill="var(--muted)">{hz:.1f}</tspan></text>')

    # A frequency axis of its own, so a peak can be located without a marker next to it.
    step = next(s for s in (10, 20, 50, 100, 200, 500, 1000) if x_max / s <= 8)
    tick = 0.0
    while tick <= x_max:
        p.append(f'<text x="{X(tick):.1f}" y="{axis_y + 47}" text-anchor="middle" '
                 f'font-size="10.5" fill="var(--muted)">{tick:.0f}</text>')
        p.append(f'<line x1="{X(tick):.1f}" y1="{axis_y}" x2="{X(tick):.1f}" '
                 f'y2="{axis_y + 4}" stroke="var(--rule)" stroke-width="1"/>')
        tick += step

    # Axis titles: the y one rotated up its own side, where it sits beside the numbers
    # it describes. The x range used to be spelled out here because there was nothing
    # else to read position from; the tick row says it now.
    mid_y = PAD_T + plot_h / 2
    p.append(f'<text x="15" y="{mid_y:.1f}" text-anchor="middle" font-size="11.5" '
             f'fill="var(--ink-2)" transform="rotate(-90 15 {mid_y:.1f})">'
             f'dB over noise floor</text>')
    p.append(f'<text x="{PAD_L + plot_w / 2:.1f}" y="{h - 6}" text-anchor="middle" '
             f'font-size="11.5" fill="var(--ink-2)">Hz</text>')
    p.append("</svg>")
    return "".join(p)


def _rule(colour: str, dash: str = "", width: float = 1.8) -> str:
    """A short sample of a chart line, for the legend. Showing the stroke beats naming
    it — "dashed" is a word the reader has to map back onto the picture."""
    return (f'<svg viewBox="0 0 26 10" width="26" height="10" aria-hidden="true">'
            f'<line x1="1" y1="5" x2="25" y2="5" stroke="{colour}" '
            f'stroke-width="{width}"{dash}/></svg>')


def _legend(leader: str) -> str:
    items = [
        (_rule("var(--accent)"), f"{leader} (leading)"),
        (_rule("var(--accent)", ' stroke-dasharray="6 4"', 1.2), "its harmonics"),
        (_rule("var(--accent)", ' stroke-dasharray="1.5 3"', 1.0), "shaft sidebands"),
        (_rule("var(--ink-2)", "", 1.3), "other candidates"),
        (_rule("var(--ink-2)", ' stroke-dasharray="6 4"', 1.0), "their harmonics"),
    ]
    return ('<div class="legend">'
            + "".join(f"<span>{svg}{_e(text)}</span>" for svg, text in items)
            + "</div>")


def _waveform_legend(leader: str, period_s: float) -> str:
    tick = _rule("var(--accent)", ' stroke-dasharray="3 4"', 1.0)
    return (f'<div class="legend"><span>{tick}expected {_e(leader)} spacing — '
            f'{period_s * 1000:.1f} ms between impacts</span></div>')


def _waveform_svg(x: np.ndarray, rate: float, period_s: float | None = None) -> str:
    """A short window of the signal, sample for sample.

    The full recording is useless here: at 12 kHz an inner-race impact lands every 74
    samples, and thinning 121k samples into 900 columns puts two impacts in every
    column, so the min/max band fills solid and shows only peak amplitude. A window a
    few impact periods wide shows the impacts themselves, and their spacing is the
    diagnosis made visible.

    `period_s` is the leading candidate's period; the window is sized to show about
    eight of them, taken from the middle of the recording."""
    h, plot_h = 200, 200 - 42
    plot_w = W - PAD_L - PAD_R

    span = min(max((period_s or 0.006) * 8, 0.01), x.size / rate)
    n = max(int(span * rate), 32)
    start = max((x.size - n) // 2, 0)
    seg = x[start:start + n]
    amp = float(max(np.abs(seg).max(), 1e-12))

    def Y(v: float) -> float:
        return 17 + plot_h * (1 - (v + amp) / (2 * amp))

    def X(i: float) -> float:
        return PAD_L + plot_w * i / max(seg.size - 1, 1)

    p = [f'<svg viewBox="0 0 {W} {h}" role="img" '
         f'aria-label="{seg.size / rate * 1000:.0f} ms of the signal">']
    p.append(f'<line x1="{PAD_L}" y1="{Y(0):.1f}" x2="{W - PAD_R}" y2="{Y(0):.1f}" '
             f'stroke="var(--grid)" stroke-width="1"/>')

    # Ticks at the expected impact spacing: if the diagnosis is right, a burst sits at
    # each one. This is the one place the reader can check the frequency by eye.
    if period_s:
        t = 0.0
        while t < seg.size / rate:
            gx = X(t * rate)
            p.append(f'<line x1="{gx:.1f}" y1="17" x2="{gx:.1f}" '
                     f'y2="{17 + plot_h}" stroke="var(--accent)" stroke-width="1" '
                     f'opacity=".30" stroke-dasharray="3 4"/>')
            t += period_s

    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(seg))
    p.append(f'<polyline points="{pts}" fill="none" stroke="var(--ink)" '
             f'stroke-width="1" stroke-linejoin="round"/>')

    p.append(f'<text x="{PAD_L - 10}" y="21" text-anchor="end" font-size="11" '
             f'fill="var(--muted)">{amp:+.2f}</text>')
    p.append(f'<text x="{PAD_L - 10}" y="{17 + plot_h + 4:.0f}" text-anchor="end" '
             f'font-size="11" fill="var(--muted)">{-amp:+.2f}</text>')
    p.append(f'<text x="{PAD_L + plot_w / 2:.1f}" y="{h - 6}" text-anchor="middle" '
             f'font-size="11.5" fill="var(--ink-2)">'
             f'{seg.size / rate * 1000:.0f} ms from the middle of the recording</text>')
    p.append("</svg>")
    return "".join(p)


# TABLES ==========================================================================

def _tiles(result: dict) -> str:
    cands = result["candidates"]
    leader = max(cands, key=lambda k: cands[k]["snr_db"])
    c = cands[leader]
    kurt = result["features"]["kurtosis"]
    tiles = [
        ("kurtosis", f"{kurt:.2f}", "impacts present" if kurt > 3.5 else "near Gaussian"),
        ("crest", f"{result['features']['crest']:.2f}", ""),
        ("rms", f"{_fmt(result['features']['rms'], 3)}", f"peak {_fmt(result['features']['peak'], 2)}"),
        ("leading candidate", f"{leader} {c['snr_db']:.1f} dB",
         f"{c['harmonics_found']} harmonics, {c['sidebands_found']} sidebands"),
    ]
    cells = "".join(f'<div class="tile"><div class="k">{_e(k)}</div>'
                    f'<div class="n">{_e(v)}</div>'
                    f'<div class="k">{_e(sub)}</div></div>' for k, v, sub in tiles)
    return f'<div class="tiles">{cells}</div>'


def _candidates_table(result: dict) -> str:
    cands = result["candidates"]
    leader = max(cands, key=lambda k: cands[k]["snr_db"])
    order = sorted(cands, key=lambda k: -cands[k]["snr_db"])
    top = max(c["snr_db"] for c in cands.values()) or 1.0
    rows = []
    for name in order:
        c = cands[name]
        lead = name == leader
        harm = " ".join(
            f'<span style="color:var(--{"ink" if v >= result["present_threshold_db"] else "muted"})">'
            f"{v:.0f}</span>" for v in c["harmonic_snr_db"])
        bar = f'<span class="bar" style="width:{max(c["snr_db"], 0) / top * 90:.0f}px"></span>'
        gap = abs(c["peak_at_hz"] - c["freq_hz"])
        rows.append(
            f'<tr><td class="l{" lead" if lead else ""}">'
            f'{"◆ " if lead else ""}{_e(name)}</td>'
            f"<td>{c['freq_hz']:.2f}</td><td>{c['peak_at_hz']:.2f}</td>"
            f"<td>{gap:+.2f}</td>"
            f"<td>{c['snr_db']:.1f} {bar}</td><td>{harm}</td>"
            f"<td>{c['harmonics_found']} of {len(c['harmonic_snr_db'])}</td>"
            f"<td>{c['sidebands_found']} of 2</td></tr>")
    return (
        "<table><thead><tr><th class='l'>candidate</th><th>predicted Hz</th>"
        "<th>found at</th><th>offset</th><th>SNR dB</th><th>harmonics 1x 2x 3x</th>"
        "<th>present</th><th>sidebands</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>")


def _bands_table(result: dict) -> str:
    alts = result["band"]["alternatives"]
    chosen = result["band"]["band_hz"]
    top = max((a["snr_db"] for a in alts), default=1.0) or 1.0
    leaders = {a["leader"] for a in alts}
    rows = []
    for a in alts:
        here = a["band_hz"] == chosen
        bar = f'<span class="bar" style="width:{max(a["snr_db"], 0) / top * 110:.0f}px"></span>'
        rows.append(
            f'<tr><td class="l">{a["band_hz"][0]:.0f} – {a["band_hz"][1]:.0f}'
            f'{" ★" if here else ""}</td><td>{_e(a["leader"])}</td>'
            f'<td>{a["snr_db"]:.1f} {bar}</td>'
            f'<td class="l">{"chosen" if here else ""}</td></tr>')
    verdict = (f"{len(alts)} of {len(alts)} bands agree on {next(iter(leaders))} — "
               f"the result does not depend on the filter choice."
               if len(leaders) == 1 else
               f"bands disagree ({', '.join(sorted(leaders))}) — the evidence is weak "
               f"whatever the headline dB says.")
    return ("<table><thead><tr><th class='l'>band (Hz)</th><th>leader</th>"
            "<th>SNR dB</th><th class='l'></th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            f'<div class="agree">{_e(verdict)}</div>')


# PAGE ============================================================================

def render(result: dict, samples: np.ndarray, reading: str = "") -> str:
    """The full page. `reading` is the caller's interpretation, shown as theirs."""
    band = tuple(result["band"]["band_hz"])
    freqs, mags = vibration.envelope_spectrum(samples, result["rate"], band)
    src = result.get("source", {})
    name = src.get("name") or source_name(src.get("path", "capture"))
    cands = result["candidates"]
    leader = max(cands, key=lambda k: cands[k]["snr_db"])
    period_s = 1.0 / cands[leader]["freq_hz"]
    title = f"Bearing analysis — {name}"

    body = [
        '<div class="wrap">',
        f"<h1>{_e(title)}</h1>",
        f'<p class="sub">{_e(src.get("channel", "?"))} · '
        f'{_e(result["bearing"]["name"])} · {result["rpm"]:.0f} rpm '
        f'({_e(src.get("rpm_from", "given"))}) · {result["rate"] / 1000:.1f} kHz · '
        f'{result["duration_s"]:.1f} s · band {band[0]:.0f}–{band[1]:.0f} Hz</p>',
    ]

    if reading:
        body.append(f'<div class="reading">{_e(reading)}'
                    f'<span class="who">Interpretation, written against the metrics '
                    f'below. The analysis itself returns evidence only.</span></div>')

    body += [
        _tiles(result),
        "<h2>Envelope spectrum</h2>",
        '<div class="card">',
        _legend(leader),
        f'<div class="chart">{_spectrum_svg(result, freqs, mags)}</div>',
        "</div>",
        "<h2>Candidates</h2>",
        f'<div class="card">{_candidates_table(result)}'
        f'<div class="agree">◆ leads. <b>offset</b> is predicted minus found — a large '
        f'one means the rpm is wrong. Harmonics and sidebands count above '
        f'{result["present_threshold_db"]:.0f} dB.</div></div>',
        "<h2>Band sensitivity</h2>",
        f'<div class="card">{_bands_table(result)}</div>',
        "<h2>Waveform</h2>",
        f'<div class="card">{_waveform_legend(leader, period_s)}'
        f'<div class="chart">'
        f'{_waveform_svg(np.asarray(samples, dtype=float).ravel(), result["rate"], period_s)}'
        f"</div></div>",
        "<h2>How to read this</h2>",
        '<div class="card"><table class="prose"><tbody>'
        + "".join(f'<tr><td><b>{_e(k)}</b></td><td>{_e(v)}</td></tr>'
                  for k, v in result["indicators"].items())
        + "</tbody></table></div>",
        "<details><summary>Full analysis JSON</summary>"
        f"<pre>{_e(json.dumps(result, indent=2))}</pre></details>",
        "</div>",
    ]

    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_e(title)}</title>"
            f"<style>{reports._CSS}{_EXTRA_CSS}</style></head>"
            f"<body>{''.join(body)}<script>{reports._JS}</script></body></html>")


# MCP SURFACE =====================================================================

def install(mcp: FastMCP) -> None:
    """Register the report tool."""

    @mcp.tool()
    def vibration_report(
        path: str,
        channel: str | None = None,
        rpm: float | None = None,
        bearing: vibration.BearingName = "skf6205",
        geometry: vibration.Bearing | None = None,
        band_hz: list[float] | None = None,
        reading: str = "",
    ) -> dict:
        """Render one vibration analysis as a standalone HTML report and upload it to
        SystemLink. Same arguments as analyze_vibration, plus `reading`.

        `reading` is your interpretation of the metrics — the sentence a person should
        see first — and it is shown as an interpretation, attributed, above the
        evidence. Run analyze_vibration first, decide what the numbers support, then
        pass that here. Leave it empty and the page shows evidence with no conclusion.

        The page is self-contained: envelope spectrum with every predicted frequency
        marked, the candidate table, a band-sensitivity check showing whether the
        result survives a different filter, the raw waveform, and the indicator table."""
        loaded = vibration.from_tdms(path, channel)
        speed = rpm if rpm is not None else loaded["rpm"]
        if speed is None:
            raise ValueError(f"{Path(path).name} records no rpm — pass rpm explicitly")

        result = vibration.analyze(
            loaded["samples"], loaded["rate"], speed, bearing=bearing,
            geometry=geometry, band_hz=tuple(band_hz) if band_hz else None)
        real_name = source_name(loaded["path"])
        result["source"] = {"path": loaded["path"], "name": real_name,
                            "channel": loaded["channel"],
                            "channels": loaded["channels"],
                            "rpm_from": "argument" if rpm is not None else "file"}
        page = render(result, loaded["samples"], reading)

        cands = result["candidates"]
        leader = max(cands, key=lambda k: cands[k]["snr_db"])
        name = systemlink.name_for("vibration-report", _subject(real_name),
                                   loaded["channel"], ext="html")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / name
            out.write_text(page, encoding="utf-8")
            uploaded = systemlink.upload_file(str(out), name=name, tags={
                "kind": "vibration-report",
                "source": loaded["properties"].get("source", "daqmx"),
                "created_by": "vibration_report",
                "source_file": real_name,
                "channel": loaded["channel"],
                "bearing": bearing,
                "rpm": str(speed),
                "leading_candidate": leader,
                "leading_snr_db": str(cands[leader]["snr_db"]),
            })

        return {"file_id": uploaded["file_id"], "filename": uploaded["filename"],
                "size_bytes": len(page.encode("utf-8")),
                "leading_candidate": leader,
                "leading_snr_db": cands[leader]["snr_db"],
                "url": f"{systemlink.SERVER_URI}/nifile/v1/service-groups/Default"
                       f"/files/{uploaded['file_id']}/data"}
