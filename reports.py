"""Static HTML reports rendered from run records.

A report is rendered on demand and uploaded to SystemLink, which is where the rest of
the test data already lives and which renders the HTML in its own file viewer. Nothing
is kept locally: a second copy on the lab box would be one more thing to keep in step
with the record it was built from.

Charts are hand-written inline SVG. The alternative was inlining a charting library at
roughly 3 MB per report, or linking a CDN and losing the self-contained property; the
forms here are a column chart and a grid, which are a few dozen lines of geometry.

Nothing here imports nidaqmx — a report is built from the database and the plan, so it
can be generated on a machine with no hardware attached.
"""
import html
import json
import tempfile
from pathlib import Path
from typing import Callable

from mcp.server.fastmcp import FastMCP

import plans
import run_store
import systemlink

# Pass/fail is a *status*, not a series, so it uses the reserved status palette. Red
# and green are indistinguishable to a deutan viewer (measured CVD dE 4.1), so colour
# never carries the verdict alone: every mark also has a shape and a glyph, and every
# row states the word.
_CSS = """
:root {
  color-scheme: light;
  --surface: #fcfcfb; --plane: #f9f9f7; --card: #ffffff;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --rule: #e6e5df;
  --good: #0ca30c; --critical: #d03b3b; --accent: #2a78d6;
  --band: rgba(42,120,214,.10);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface: #1a1a19; --plane: #0d0d0d; --card: #1f1f1e;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --rule: #2c2c2a;
    --good: #0ca30c; --critical: #d03b3b; --accent: #3987e5;
    --band: rgba(57,135,229,.16);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface: #1a1a19; --plane: #0d0d0d; --card: #1f1f1e;
  --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --rule: #2c2c2a;
  --good: #0ca30c; --critical: #d03b3b; --accent: #3987e5;
  --band: rgba(57,135,229,.16);
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--plane); color: var(--ink);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1040px; margin: 0 auto; padding: 32px 20px 72px; }
h1 { font-size: 21px; margin: 0 0 4px; letter-spacing: -.01em; }
h2 { font-size: 15px; margin: 34px 0 12px; letter-spacing: -.005em; }
.sub { color: var(--ink-2); font-size: 13.5px; margin: 0; }
.card {
  background: var(--card); border: 1px solid var(--rule);
  border-radius: 10px; padding: 18px 20px; margin-top: 14px;
}
.verdict {
  display: inline-flex; align-items: center; gap: 9px;
  font-weight: 650; font-size: 15px; padding: 7px 14px; border-radius: 999px;
  border: 1.5px solid currentColor;
}
.verdict.pass { color: var(--good); }
.verdict.fail { color: var(--critical); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: 10px; margin-top: 16px; }
.tile { background: var(--card); border: 1px solid var(--rule); border-radius: 10px; padding: 13px 15px; }
.tile .n { font-size: 23px; font-weight: 640; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
.tile .k { font-size: 11.5px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-top: 2px; }
.meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 8px 26px; margin-top: 14px; font-size: 13.5px; }
.meta div { color: var(--ink-2); }
.meta b { color: var(--ink); font-weight: 560; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 7px 11px; border-bottom: 1px solid var(--rule); white-space: nowrap; }
th:first-child, td:first-child, th.l, td.l { text-align: left; }
th { color: var(--muted); font-weight: 560; font-size: 11.5px; text-transform: uppercase; letter-spacing: .045em; }
tbody tr:hover { background: color-mix(in srgb, var(--accent) 6%, transparent); }
.pill { display: inline-flex; align-items: center; gap: 5px; font-weight: 600; font-size: 12px; }
.pill.pass { color: var(--good); }
.pill.fail, .pill.error { color: var(--critical); }
.chart { position: relative; }
svg { display: block; width: 100%; height: auto; overflow: visible; }
.tip {
  position: absolute; pointer-events: none; opacity: 0; transition: opacity .1s;
  background: var(--ink); color: var(--surface); font-size: 12px; line-height: 1.45;
  padding: 7px 10px; border-radius: 7px; white-space: pre; z-index: 5;
  font-variant-numeric: tabular-nums;
}
.legend { display: flex; gap: 18px; margin: 0 0 10px; font-size: 12.5px; color: var(--ink-2); align-items: center; flex-wrap: wrap; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.note { color: var(--muted); font-size: 12.5px; margin-top: 9px; }
details { margin-top: 12px; }
summary { cursor: pointer; color: var(--ink-2); font-size: 13.5px; }
pre { background: var(--surface); border: 1px solid var(--rule); border-radius: 8px;
      padding: 13px; overflow-x: auto; font-size: 12px; line-height: 1.5; }
.log { font-size: 13px; }
.log .lvl { display: inline-block; min-width: 46px; font-size: 11px; text-transform: uppercase;
            letter-spacing: .04em; color: var(--muted); }
.log .error .lvl { color: var(--critical); }
"""

_JS = """
(function () {
  var tip = document.querySelector('.tip');
  if (!tip) return;
  document.querySelectorAll('[data-tip]').forEach(function (el) {
    el.addEventListener('mousemove', function (e) {
      var host = el.closest('.chart').getBoundingClientRect();
      tip.textContent = el.getAttribute('data-tip');
      tip.style.opacity = 1;
      var x = e.clientX - host.left + 14, y = e.clientY - host.top - 12;
      if (x + tip.offsetWidth > host.width) x = e.clientX - host.left - tip.offsetWidth - 14;
      tip.style.left = x + 'px';
      tip.style.top = y + 'px';
    });
    el.addEventListener('mouseleave', function () { tip.style.opacity = 0; });
  });
})();
"""

PASS_GLYPH, FAIL_GLYPH = "✓", "✕"      # check, cross


def _e(text) -> str:
    return html.escape(str(text), quote=True)


def _fmt(value, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


# ---------- data shaping ----------

def _limited_steps(points: list[dict]) -> list[dict]:
    """One row per point for the first step that carries a limit — the measurement the
    plan is actually testing. Points with no limits are stimulus-only and have nothing
    to plot."""
    rows = []
    for point in points:
        for step in point["steps"]:
            if step.get("limits"):
                limit = step["limits"][0]
                rows.append({
                    "index": point["point_index"],
                    "axes": point["axis_values"],
                    "step_id": step["step_id"],
                    "result": step["result"],
                    **limit,
                })
                break
        else:
            worst = next((s for s in point["steps"] if s["result"] != "PASS"), None)
            if worst is not None:
                rows.append({"index": point["point_index"],
                             "axes": point["axis_values"], "step_id": worst["step_id"],
                             "result": worst["result"], "error": worst.get("error")})
    return rows


def _bounds(row: dict) -> tuple[float | None, float | None]:
    """The band the measurement had to land in, as (min, max); None on a side the limit
    leaves open.

    Every limit op is really a range; only the way it is written down differs. Deriving
    one pair of numbers here means the table and the chart cannot disagree about what
    was allowed, and neither has to know how the limit was spelled."""
    op, limit = row.get("op"), row.get("limit")
    if op in ("lt", "lte") and isinstance(limit, (int, float)):
        return None, float(limit)
    if op in ("gt", "gte") and isinstance(limit, (int, float)):
        return float(limit), None
    if op == "between" and isinstance(limit, dict):
        return limit.get("min"), limit.get("max")
    ref, allowed = row.get("reference"), row.get("allowed")
    if isinstance(ref, (int, float)) and isinstance(allowed, (int, float)):
        return ref - allowed, ref + allowed
    return None, None


def _passing_range(row: dict) -> str:
    """The range as text. A one-sided op keeps its comparator, so a bound is never
    mistaken for the endpoint of a closed band.

    Six digits, not the default four: a 0.05% limit on 1 V has its bounds in the fifth,
    and a range printed as "0.9995 – 1" is a wrong number, not a rounded one."""
    lo, hi = _bounds(row)
    if lo is None and hi is None:
        return "—"
    if lo is None:
        return f"{'<' if row.get('op') == 'lt' else '≤'} {_fmt(hi, 6)}"
    if hi is None:
        return f"{'>' if row.get('op') == 'gt' else '≥'} {_fmt(lo, 6)}"
    return f"{_fmt(lo, 6)} – {_fmt(hi, 6)}"


def _target(row: dict) -> float | None:
    """The value the point was aiming at: the middle of a closed range, or the bound
    itself when only one side is specified — in both cases, the zero the error is
    measured from."""
    lo, hi = _bounds(row)
    if lo is not None and hi is not None:
        ref = row.get("reference")
        return float(ref) if isinstance(ref, (int, float)) else (lo + hi) / 2
    return hi if lo is None else lo


def _error_pct(row: dict) -> float | None:
    """The reading's error as a share of what the limit allowed: 100% is the edge of the
    passing range, and the sign says which side of target it fell.

    None when the limit is one-sided — there is no half-width to divide by, and inventing
    one would make a bound look like a band. The table and the chart both hinge on this,
    so it is computed once."""
    lo, hi = _bounds(row)
    measured, target = row.get("measured"), _target(row)
    if lo is None or hi is None or not isinstance(measured, (int, float)):
        return None
    half = (hi - lo) / 2
    return 100.0 * (measured - target) / half if half else None


def _axis_label(rows: list[dict]) -> str:
    names = list(rows[0]["axes"]) if rows and rows[0]["axes"] else []
    return names[0] if names else "point"


# ---------- charts ----------

# Geometry shared by the two per-point charts. They differ only in what they put inside
# the frame, so the frame is built once.
_W, _H = 940, 320
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 76, 62, 20, 52
_PLOT_W, _PLOT_H = _W - _PAD_L - _PAD_R, _H - _PAD_T - _PAD_B
_LEFT, _RIGHT, _FLOOR = _PAD_L, _W - _PAD_R, _PAD_T + _PLOT_H


def _series(rows: list[dict]) -> list[dict]:
    """The rows a per-point chart can draw, each carrying its error from target — in the
    reading's own units and as a share of the passing range — and that range expressed
    around the same zero. A row with no limit is stimulus-only and has nothing to place
    on either axis."""
    pts = []
    for row in rows:
        measured, target = row.get("measured"), _target(row)
        if not isinstance(measured, (int, float)) or target is None:
            continue
        lo, hi = _bounds(row)
        pts.append({"row": row, "err": measured - target, "pct": _error_pct(row),
                    "x": next(iter(row["axes"].values()), row["index"]),
                    "lo": None if lo is None else lo - target,
                    "hi": None if hi is None else hi - target})
    return pts


def _path(pairs) -> str:
    return "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pairs)


def _frame(pts: list[dict], top: float, bottom: float, key: str, unit: str,
           y_label: str, y_fmt=lambda v: _fmt(v, 3), ticks=None):
    """Gridlines, tick labels and axis titles, plus the mappings the marks need.

    `ticks` is (value, draw_line) pairs; a tick with no line labels a line the caller
    draws itself, so the axis can read 0% and ±100% without those lines being doubled.
    Defaults to five evenly spaced gridlines.

    Returns the frame's SVG in two halves — what goes under the marks and what goes
    over them — so a caller cannot accidentally paint an axis label beneath a dot."""
    def y_of(value: float) -> float:
        return _PAD_T + _PLOT_H * (top - value) / (top - bottom)

    slot = _PLOT_W / len(pts)
    xs = [_PAD_L + slot * (i + .5) for i in range(len(pts))]

    if ticks is None:
        ticks = [(bottom + (top - bottom) * f, True) for f in (0, .25, .5, .75, 1)]

    under = []
    for value, with_line in ticks:
        y = y_of(value)
        if with_line:
            under.append(f'<line x1="{_LEFT}" y1="{y:.1f}" x2="{_RIGHT}" y2="{y:.1f}" '
                         f'stroke="var(--grid)" stroke-width="1"/>')
        under.append(f'<text x="{_LEFT - 10}" y="{y + 4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="var(--muted)">{y_fmt(value)}</text>')

    over = []
    for i, (cx, p) in enumerate(zip(xs, pts)):
        if len(pts) <= 24 or i % max(len(pts) // 12, 1) == 0:
            over.append(f'<text x="{cx:.1f}" y="{_H - _PAD_B + 26:.1f}" '
                        f'text-anchor="middle" font-size="11" fill="var(--muted)">'
                        f'{_fmt(p["x"], 3)}</text>')
    over.append(f'<text x="{_PAD_L + _PLOT_W/2:.1f}" y="{_H - 6}" text-anchor="middle" '
                f'font-size="11.5" fill="var(--ink-2)">{_e(key)}'
                f'{(" (" + _e(unit) + ")") if unit else ""}</text>')
    over.append(f'<text transform="translate(16 {_PAD_T + _PLOT_H/2:.0f}) rotate(-90)" '
                f'text-anchor="middle" font-size="11.5" fill="var(--ink-2)">'
                f'{_e(y_label)}</text>')
    return y_of, xs, slot, under, over


def _marks(pts: list[dict], xs: list[float], slot: float, y_of, tip_of,
           field: str = "err") -> list[str]:
    """The readings themselves: a trace through them, a mark per point, a hit target.
    `field` names the quantity this chart plots, which is what its y scale was built on.

    The verdict must survive greyscale and a red-green viewer, so shape carries it as
    well as colour — a filled dot passed, a cross did not."""
    parts = [f'<path d="{_path([(x, y_of(p[field])) for x, p in zip(xs, pts)])}" '
             f'fill="none" stroke="var(--ink-2)" stroke-width="1.25" opacity=".33"/>']
    for cx, p in zip(xs, pts):
        failed = p["row"]["result"] != "PASS"
        colour = "var(--critical)" if failed else "var(--good)"
        y = y_of(p[field])
        if failed:
            parts.append(f'<text x="{cx:.1f}" y="{y + 5:.1f}" text-anchor="middle" '
                         f'font-size="14" fill="{colour}">{FAIL_GLYPH}</text>')
        else:
            parts.append(f'<circle cx="{cx:.1f}" cy="{y:.1f}" r="4" fill="{colour}" '
                         f'opacity=".9"/>')
        # Hit target wider than the mark.
        parts.append(f'<rect x="{cx - slot/2:.1f}" y="{_PAD_T}" width="{slot:.1f}" '
                     f'height="{_PLOT_H}" fill="transparent" data-tip="{_e(tip_of(p))}"/>')
    return parts


def _envelope_chart(pts: list[dict], rows: list[dict], unit: str) -> str:
    """Each reading's distance from what it was aiming at, inside the range it had to
    stay in.

    Not measured-against-setpoint: a tracking sweep plots as a 45-degree line where a 1%
    error at 5 V is a pixel of thickness, so the one thing under test is the one thing
    invisible. Subtracting the target puts every point on a common zero, which makes the
    shaded band the pass criterion itself — a dot outside it is the failure, in the same
    units the table quotes. A percentage tolerance widens with the setpoint, and that
    flare is the shape of the limit rather than an artefact of the plot."""
    # Zero is always in frame: it is the line the whole chart is read against.
    values = [0.0] + [p["err"] for p in pts]
    values += [p[k] for p in pts for k in ("lo", "hi") if p[k] is not None]
    span = (max(values) - min(values)) or 1.0
    top, bottom = max(values) + span * .18, min(values) - span * .18

    key = _axis_label(rows)
    y_of, xs, slot, under, over = _frame(
        pts, top, bottom, key, unit,
        f"measured − target{(' (' + unit + ')') if unit else ''}")

    # An open side runs off the plot rather than pretending to a bound it never had.
    def edge(side: str, open_y: float) -> list[tuple[float, float]]:
        ys = [y_of(p[side]) if p[side] is not None else open_y for p in pts]
        return [(_LEFT, ys[0])] + list(zip(xs, ys)) + [(_RIGHT, ys[-1])]

    upper, lower = edge("hi", _PAD_T), edge("lo", _FLOOR)
    band = [f'<path d="{_path(upper)} L {_path(reversed(lower))[2:]} Z" '
            f'fill="var(--band)" stroke="none"/>']
    band += [f'<path d="{_path(pairs)}" fill="none" stroke="var(--accent)" '
             f'stroke-width="1.25" stroke-dasharray="5 3" opacity=".85"/>'
             for pairs in (upper, lower)]
    band.append(f'<text x="{_RIGHT + 6}" y="{upper[-1][1] + 4:.1f}" font-size="11" '
                f'fill="var(--accent)">passing</text>')
    band.append(f'<line x1="{_LEFT}" y1="{y_of(0):.1f}" x2="{_RIGHT}" '
                f'y2="{y_of(0):.1f}" stroke="var(--ink-2)" stroke-width="1.25" '
                f'opacity=".7"/>')
    band.append(f'<text x="{_RIGHT + 6}" y="{y_of(0) + 4:.1f}" font-size="11" '
                f'fill="var(--ink-2)">target</text>')

    def tip_of(p: dict) -> str:
        return (f"{key} = {_fmt(p['x'])}{(' ' + unit) if unit else ''}\n"
                f"{p['row']['result']}   measured {_fmt(p['row'].get('measured'), 6)}\n"
                f"error {p['err']:+.4g}   passing {_passing_range(p['row'])}")

    return (f'<svg viewBox="0 0 {_W} {_H}" role="img" aria-label="Error from target at '
            f'each point, against the passing range">'
            + "".join(under + band + _marks(pts, xs, slot, y_of, tip_of) + over)
            + "</svg>")


def _normalized_chart(pts: list[dict], rows: list[dict], unit: str) -> str:
    """The same errors as a share of what each point was allowed: ±100% is the edge.

    Dividing by the allowance is what makes points at 0.5 V and 5 V comparable when the
    tolerance between them has grown tenfold — the physical-units view puts them on one
    scale set by the widest limit, which flattens the readings against zero and hides
    exactly the structure worth seeing. Here the fine detail is the whole plot, and the
    price is that the edge often sits off-scale, so it is stated in words when it does."""
    key = _axis_label(rows)
    pcts = [p["pct"] for p in pts]
    extent = max(abs(v) for v in pcts + [0.0]) or 1.0
    # Show the edges when the readings come anywhere near them; otherwise let the data
    # set the scale and say in words where the edges went.
    values = pcts + [0.0] + ([100.0, -100.0] if extent * 1.8 >= 100 else [])
    span = (max(values) - min(values)) or 1.0
    top, bottom = max(values) + span * .18, min(values) - span * .18
    edges_shown = top >= 100 and bottom <= -100

    # The axis says the only five numbers worth naming: the two extremes of the scale,
    # the target, and the two edges. Each of the inner three already has a line of its
    # own below, so the tick only labels it — an evenly spaced grid on top of those
    # would be four more lines saying nothing.
    ticks = [(top, True), (bottom, True), (0.0, False)]
    ticks += [(e, False) for e in (100.0, -100.0) if edges_shown]
    y_of, xs, slot, under, over = _frame(
        pts, top, bottom, key, unit, "error (% of what the limit allowed)",
        y_fmt=lambda v: f"{v:+.3g}%" if v else "0%", ticks=ticks)

    scale = []
    if edges_shown:
        scale.append(f'<rect x="{_LEFT}" y="{y_of(100):.1f}" width="{_PLOT_W}" '
                     f'height="{y_of(-100) - y_of(100):.1f}" fill="var(--band)"/>')
        for edge in (100.0, -100.0):
            scale.append(f'<line x1="{_LEFT}" y1="{y_of(edge):.1f}" x2="{_RIGHT}" '
                         f'y2="{y_of(edge):.1f}" stroke="var(--accent)" '
                         f'stroke-width="1.25" stroke-dasharray="5 3" opacity=".85"/>')
    else:
        # The range is off-scale, which is itself the finding: nothing came close to it.
        scale.append(f'<rect x="{_LEFT}" y="{_PAD_T}" width="{_PLOT_W}" '
                     f'height="{_PLOT_H}" fill="var(--band)"/>')
        scale.append(f'<text x="{_RIGHT - 8}" y="{_PAD_T + 15}" text-anchor="end" '
                     f'font-size="11" fill="var(--accent)">'
                     f'passing range ±100% — off scale, nothing past '
                     f'{extent:+.3g}%</text>')
    scale.append(f'<line x1="{_LEFT}" y1="{y_of(0):.1f}" x2="{_RIGHT}" y2="{y_of(0):.1f}" '
                 f'stroke="var(--ink-2)" stroke-width="1.25" opacity=".7"/>')

    def tip_of(p: dict) -> str:
        return (f"{key} = {_fmt(p['x'])}{(' ' + unit) if unit else ''}\n"
                f"{p['row']['result']}   measured {_fmt(p['row'].get('measured'), 6)}\n"
                f"error {p['pct']:+.3g}% of the passing range\n"
                f"{_fmt(p['err'])} from target   passing {_passing_range(p['row'])}")

    return (f'<svg viewBox="0 0 {_W} {_H}" role="img" aria-label="Error at each point as '
            f'a percentage of the allowance, where 100 percent is the limit">'
            + "".join(under + scale
                      + _marks(pts, xs, slot, y_of, tip_of, "pct") + over)
            + "</svg>")


def _sweep_chart(rows: list[dict], unit: str = "") -> dict:
    """The sweep's chart, in whichever frame the limits allow.

    Normalized when every point has a two-sided limit, because a percentage of the
    allowance is the more useful axis and the only one on which points with different
    tolerances are comparable. A one-sided limit has no half-width to divide by, so
    those plans keep the physical-units view rather than being left without a chart."""
    pts = _series(rows)
    if not pts:
        return {"svg": "", "title": "", "note": ""}
    if all(p["pct"] is not None for p in pts):
        return {"svg": _normalized_chart(pts, rows, unit),
                "title": "Error chart",
                "note": "Each reading's distance from what it was aiming at, as a share "
                        "of what its limit allowed. 0% is target and ±100% are the edges "
                        "of the passing range; the sign says which side it fell."}
    return {"svg": _envelope_chart(pts, rows, unit),
            "title": "Error vs passing range",
            "note": "Each reading minus the value it was aiming at. The band is the "
                    "range it had to stay inside — it widens with the setpoint when the "
                    "limit is a percentage."}


def _shmoo_grid(rows: list[dict]) -> str:
    """Pass/fail over two axes — the shape a shmoo exists to produce."""
    if not rows or len(rows[0]["axes"]) < 2:
        return ""
    names = list(rows[0]["axes"])
    xs = sorted({r["axes"][names[1]] for r in rows})
    ys = sorted({r["axes"][names[0]] for r in rows})
    by_cell = {(r["axes"][names[0]], r["axes"][names[1]]): r for r in rows}

    cell, pad_l, pad_t, pad_b = 54, 96, 30, 46
    w = pad_l + cell * len(xs) + 20
    h = pad_t + cell * len(ys) + pad_b

    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Shmoo pass/fail grid">']
    for xi, xv in enumerate(xs):
        parts.append(f'<text x="{pad_l + cell*(xi+.5):.1f}" y="{pad_t - 10}" '
                     f'text-anchor="middle" font-size="11" fill="var(--muted)">'
                     f'{_fmt(xv, 3)}</text>')
    for yi, yv in enumerate(ys):
        parts.append(f'<text x="{pad_l - 12}" y="{pad_t + cell*(yi+.5) + 4:.1f}" '
                     f'text-anchor="end" font-size="11" fill="var(--muted)">'
                     f'{_fmt(yv, 3)}</text>')
        for xi, xv in enumerate(xs):
            row = by_cell.get((yv, xv))
            if row is None:
                continue
            failed = row["result"] != "PASS"
            colour = "var(--critical)" if failed else "var(--good)"
            x, y = pad_l + cell * xi, pad_t + cell * yi
            tip = (f"{names[0]} = {_fmt(yv)}\n{names[1]} = {_fmt(xv)}\n"
                   f"{row['result']}   margin {_fmt(row.get('margin'))}")
            # 2px surface gap between cells so adjacent fills stay separable.
            parts.append(f'<rect x="{x+1}" y="{y+1}" width="{cell-2}" height="{cell-2}" '
                         f'rx="4" fill="{colour}" opacity=".85" data-tip="{_e(tip)}"/>')
            parts.append(f'<text x="{x + cell/2:.1f}" y="{y + cell/2 + 5:.1f}" '
                         f'text-anchor="middle" font-size="15" fill="var(--surface)" '
                         f'pointer-events="none">'
                         f'{PASS_GLYPH if not failed else FAIL_GLYPH}</text>')
    parts.append(f'<text x="{pad_l + cell*len(xs)/2:.1f}" y="{h - 8}" text-anchor="middle" '
                 f'font-size="11.5" fill="var(--ink-2)">{_e(names[1])}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------- plan diagram ----------

def _step_lines(step: dict, defaults: dict) -> tuple[str, str | None]:
    """A step as one line of prose, plus the limit it checks if it carries one."""
    args = {**defaults, **step.get("args", {})}
    target = (args.get("channels") or args.get("channel") or args.get("lines")
              or args.get("counter") or args.get("target") or "")
    if isinstance(target, list):
        target = ", ".join(str(t) for t in target)
    value = next((f" = {args[k]}" for k in ("volts", "value", "frequency")
                  if k in args), "")
    head = f"{step['action']} {target}{value}".strip()

    limits = step.get("limits") or []
    if not limits:
        return head, None
    # Reuse the phrasing validation already reads back, so the diagram and the
    # plain-language summary can never describe the same limit differently.
    return head, plans._limit_phrase(plans.Limit.model_validate(limits[0]))


def _plan_diagram(plan: dict, point_count: int) -> str:
    """The plan as a flow, because the shape is what JSON hides.

    What matters is not the list of steps but the mechanism around them: what repeats
    and how many times, where the settle falls, which step carries the check, and that
    cleanup runs on the way out whether the loop finished or aborted. Those are the
    arrows, so those get the labels."""
    defaults = plan.get("defaults", {})
    setup, steps = plan.get("setup", []), plan.get("steps", [])
    cleanup = plan.get("cleanup", [])
    loop = plan.get("loop", {}) or {}
    axes, settle = loop.get("axes", []), loop.get("settle_ms", 0)

    W, cx = 860, 430
    box_w, inner_w = 560, 500
    parts, y = [], 22

    def box(top: float, height: float, width: float, title: str,
            sub: str | None = None, dashed: bool = False) -> None:
        x = cx - width / 2
        parts.append(
            f'<rect x="{x:.0f}" y="{top:.0f}" width="{width}" height="{height}" rx="8" '
            f'fill="var(--surface)" stroke="currentColor" stroke-width="1.25" '
            f'{"stroke-dasharray=\'5 4\' " if dashed else ""}opacity=".92"/>')
        ty = top + (height / 2 + 5 if sub is None else height / 2 - 5)
        parts.append(f'<text x="{cx}" y="{ty:.0f}" text-anchor="middle" font-size="13" '
                     f'fill="currentColor">{_e(title)}</text>')
        if sub:
            parts.append(f'<text x="{cx}" y="{ty + 19:.0f}" text-anchor="middle" '
                         f'font-size="11.5" fill="var(--good)">{PASS_GLYPH} {_e(sub)}</text>')

    def arrow(top: float, height: float, label: str = "") -> None:
        parts.append(f'<line x1="{cx}" y1="{top:.0f}" x2="{cx}" y2="{top + height - 9:.0f}" '
                     f'stroke="currentColor" stroke-width="1.25" marker-end="url(#pa)"/>')
        if label:
            parts.append(f'<text x="{cx + 11}" y="{top + height / 2 + 4:.0f}" font-size="11" '
                         f'fill="var(--ink-2)">{_e(label)}</text>')

    if setup:
        box(y, 44, box_w,
            f"setup · {', '.join(_step_lines(s, defaults)[0] for s in setup)}")
        y += 44
        arrow(y, 36, "once, before the loop")
        y += 36

    frame_top = y
    y += 40 if axes else 12                      # room for the loop header

    for i, step in enumerate(steps):
        head, limit = _step_lines(step, defaults)
        height = 44 if limit is None else 60
        box(y, height, inner_w, head, limit)
        y += height
        if i < len(steps) - 1:
            drives = step["action"] in ("set_voltage", "set_digital", "set_waveform",
                                        "pulse")
            arrow(y, 34, f"settle {settle} ms" if (drives and settle) else "then")
            y += 34

    if axes:
        y += 14
        frame_h = y - frame_top
        span = " × ".join(
            f"{a['name']} {_fmt(a.get('from'), 3)}→{_fmt(a.get('to'), 3)}"
            f"{(' ' + a['unit']) if a.get('unit') else ''}"
            if a.get("values") is None else
            f"{a['name']} over {len(a['values'])} values"
            for a in axes)
        parts.insert(0,
            f'<rect x="{cx - box_w/2:.0f}" y="{frame_top:.0f}" width="{box_w}" '
            f'height="{frame_h:.0f}" rx="10" fill="var(--band)" '
            f'stroke="var(--accent)" stroke-width="1.5"/>')
        parts.insert(1,
            f'<text x="{cx - box_w/2 + 16:.0f}" y="{frame_top + 25:.0f}" font-size="12" '
            f'fill="var(--accent)" font-weight="600">'
            f'repeat {point_count}× · {_e(span)}</text>')
        # Back-edge: the thing that makes it a loop rather than a list.
        bx = cx + inner_w / 2 + 14
        parts.append(
            f'<path d="M {cx + inner_w/2:.0f} {y - 26:.0f} H {bx:.0f} '
            f'V {frame_top + 52:.0f} H {cx + inner_w/2:.0f}" fill="none" '
            f'stroke="var(--accent)" stroke-width="1.25" stroke-dasharray="4 3" '
            f'marker-end="url(#pa2)"/>')
        parts.append(f'<text x="{bx + 7:.0f}" y="{(frame_top + y)/2:.0f}" font-size="11" '
                     f'fill="var(--accent)">next point</text>')

    if cleanup:
        arrow(y, 42)
        y += 42
        heads = ", ".join(_step_lines(s, defaults)[0] for s in cleanup)
        box(y, 44, box_w, f"cleanup · {heads}", dashed=True)
        y += 44

    height = y + 22
    svg = (f'<svg viewBox="0 0 {W} {height:.0f}" role="img" '
           f'aria-label="Flow of the plan as executed: '
           f'{len(setup)} setup, {len(steps)} steps repeated {point_count} times, '
           f'{len(cleanup)} cleanup" style="max-width:100%;height:auto">'
           f'<defs>'
           f'<marker id="pa" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
           f'markerHeight="7" orient="auto-start-reverse">'
           f'<path d="M0 0 L10 5 L0 10 z" fill="currentColor"/></marker>'
           f'<marker id="pa2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
           f'markerHeight="7" orient="auto-start-reverse">'
           f'<path d="M0 0 L10 5 L0 10 z" fill="var(--accent)"/></marker>'
           f'</defs>{"".join(parts)}</svg>')

    return f'<figure style="margin:0">{svg}</figure>'


# ---------- page ----------

def _tiles(record: dict) -> str:
    s, p = record["summary"], record["progress"]
    tiles = [
        (p["point_count"] or 0, "points"),
        (s["steps_passed"], "steps passed"),
        (s["steps_failed"], "failed"),
        (s["steps_errored"], "errored"),
    ]
    return ('<div class="tiles">' + "".join(
        f'<div class="tile"><div class="n">{v}</div><div class="k">{k}</div></div>'
        for v, k in tiles) + "</div>")


def _result_pill(result: str) -> str:
    cls = "pass" if result == "PASS" else "fail"
    glyph = PASS_GLYPH if result == "PASS" else FAIL_GLYPH
    return f'<span class="pill {cls}">{glyph} {result}</span>'


def _points_table(rows: list[dict], unit: str) -> str:
    key = _axis_label(rows)
    head = (f"<tr><th class='l'>#</th><th class='l'>{_e(key)}"
            f"{(' (' + _e(unit) + ')') if unit else ''}</th>"
            "<th>measured</th><th>target</th><th>passing range</th>"
            "<th>error (%)</th><th class='l'>result</th></tr>")
    body = []
    for row in rows:
        axis_value = next(iter(row["axes"].values()), row["index"])
        pct = _error_pct(row)
        body.append(
            f"<tr><td class='l'>{row['index']}</td>"
            f"<td class='l'>{_fmt(axis_value)}</td>"
            f"<td>{_fmt(row.get('measured'), 6)}</td>"
            f"<td>{_fmt(_target(row), 6)}</td>"
            f"<td>{_e(_passing_range(row))}</td>"
            f"<td>{'—' if pct is None else f'{pct:+.3g}'}</td>"
            f"<td class='l'>{_result_pill(row['result'])}</td></tr>")
    return f'<div class="scroll"><table><thead>{head}</thead><tbody>{"".join(body)}</tbody></table></div>'


def _log_block(events: list[dict]) -> str:
    return '<div class="card log">' + "".join(
        f'<div class="{_e(e["level"])}"><span class="lvl">{_e(e["level"])}</span> '
        f'{_e(e["message"])}</div>' for e in events) + "</div>"


def render(record: dict, plan: dict) -> str:
    rows = _limited_steps(record.get("points", []))
    axes = plan.get("loop", {}).get("axes", [])
    unit = axes[0].get("unit", "") if axes else ""
    shape = {0: "functional", 1: "sweep"}.get(len(axes), "shmoo")

    passed = record["verdict"] == "PASS"
    title = plan.get("title") or record["plan_id"]

    if shape == "shmoo":
        section = {"svg": _shmoo_grid(rows), "title": "Shmoo", "note": ""}
    elif shape == "sweep":
        section = _sweep_chart(rows, unit)
    else:
        section = {"svg": "", "title": "", "note": ""}

    body = [
        '<div class="wrap">',
        f"<h1>{_e(title)}</h1>",
        f'<p class="sub">{_e(record["plan_id"])} · {_e(record["run_id"])}</p>',
        f'<div style="margin-top:15px"><span class="verdict {"pass" if passed else "fail"}">'
        f'{PASS_GLYPH if passed else FAIL_GLYPH} {record["verdict"] or record["status"]}'
        f"</span></div>",
        _tiles(record),
        '<div class="meta">',
        f'<div>status <b>{_e(record["status"])}</b></div>',
        f'<div>started <b>{_e(record.get("started_at") or "—")}</b></div>',
        f'<div>ended <b>{_e(record.get("ended_at") or "—")}</b></div>',
        "</div>",
    ]

    if section["svg"]:
        shmoo = shape == "shmoo"
        legend = ('<div class="legend">'
                  f'<span style="color:var(--good)">'
                  f'{PASS_GLYPH if shmoo else "●"} pass</span>'
                  f'<span style="color:var(--critical)">{FAIL_GLYPH} fail</span>'
                  + ("" if shmoo else '<span style="color:var(--accent)">▨ passing '
                                      'range</span>') + "</div>")
        body += [
            f'<h2>{_e(section["title"])}</h2>',
            '<div class="card">', legend,
            f'<div class="chart">{section["svg"]}<div class="tip"></div></div>',
            f'<p class="note">{_e(section["note"])}</p>' if section["note"] else "",
            "</div>",
        ]

    if rows:
        body += ["<h2>Test Results</h2>", '<div class="card">',
                 _points_table(rows, unit), "</div>"]

    body += [
        "<h2>Test plan</h2>",
        '<div class="card">'
        + _plan_diagram(plan, record["progress"]["point_count"] or 1) + "</div>",
    ]

    if record.get("log"):
        body += ["<h2>Event log</h2>", _log_block(record["log"])]

    # The plan JSON is the appendix: exact, occasionally needed, and never what a reader
    # is here for. It sits below everything, collapsed and outside any card.
    body += [
        "<details><summary>Show the underlying JSON</summary>"
        f"<pre>{_e(json.dumps(plan, indent=2))}</pre></details>",
        "</div>",
    ]

    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_e(title)} — {_e(record['run_id'])}</title>"
            f"<style>{_CSS}</style></head><body>{''.join(body)}"
            f"<script>{_JS}</script></body></html>")


# ---------- MCP surface ----------

def install(mcp: FastMCP, *, build_record: Callable) -> None:
    """Register the report tool."""

    @mcp.tool()
    def generate_report(run_id: str) -> dict:
        """Render a run as a standalone HTML report and upload it to SystemLink.

        The report is self-contained — charts, styles and data are all inline — so
        SystemLink's file viewer renders it directly, and it stays readable if it is
        later downloaded and opened on its own.

        It is tagged with the run id, plan id, verdict and status, so reports are
        findable by metadata rather than only by filename. Nothing is written to local
        disk; the returned `file_id` is the report's identity from here on.

        A sweep gets a margin-to-limit chart, a shmoo gets a pass/fail grid, and any
        run gets its failures, the full point table, the event log, and the plan
        exactly as executed."""
        record = build_record(run_id, "all")
        plan = json.loads(run_store.get(run_id)["plan_json"])
        page = render(record, plan)

        name = systemlink.name_for("test-report", record["plan_id"], ext="html")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_text(page, encoding="utf-8")
            uploaded = systemlink.upload_file(str(path), name=name, tags={
                "kind": "test-report",
                "source": "daqmx",
                "created_by": "generate_report",
                "run_id": run_id,
                "plan_id": record["plan_id"],
                "verdict": record["verdict"] or "",
                "status": record["status"],
            })

        return {"run_id": run_id, "verdict": record["verdict"],
                "status": record["status"], "summary": record["summary"],
                "file_id": uploaded["file_id"], "filename": uploaded["filename"],
                "size_bytes": len(page.encode("utf-8")),
                "url": f"{systemlink.SERVER_URI}/nifile/v1/service-groups/Default"
                       f"/files/{uploaded['file_id']}/data"}
