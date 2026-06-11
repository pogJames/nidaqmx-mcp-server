import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from app import analysis
from app.state import LoadedFile, state


class FakeChannel:
    def __init__(self, y, dt):
        self._y = np.asarray(y, dtype=float)
        self._dt = float(dt)

    def __getitem__(self, key):
        return self._y[key]

    def __len__(self):
        return len(self._y)

    def time_track(self):
        return np.arange(len(self._y)) * self._dt

    @property
    def properties(self):
        return {}


class FakeGroup:
    def __init__(self, name, channels):
        self._channels = channels
        self.name = name

    def __getitem__(self, name):
        return self._channels[name]

    def channels(self):
        return list(self._channels.values())


class FakeTdms:
    def __init__(self, groups):
        self._groups = groups

    def __getitem__(self, name):
        return self._groups[name]

    def groups(self):
        return list(self._groups.values())


def make_wave(peak_t, n=1000, dt=0.001, width=0.02):
    t = np.arange(n) * dt
    return np.exp(-((t - peak_t) / width) ** 2)


# Two Gaussian pulses with peaks at t=0.3 and t=0.5 (true offset = -0.2).
chA = FakeChannel(make_wave(0.3), 0.001)
chB = FakeChannel(make_wave(0.5), 0.001)
state.files["f_a"] = LoadedFile("f_a", "<fake-a>", FakeTdms({"g": FakeGroup("g", {"ai0": chA})}))
state.files["f_b"] = LoadedFile("f_b", "<fake-b>", FakeTdms({"g": FakeGroup("g", {"ai0": chB})}))

import server  # noqa: E402

traces = [
    {"file_id": "f_a", "group": "g", "channel": "ai0"},
    {"file_id": "f_b", "group": "g", "channel": "ai0"},
]

peaks = server.first_peak(traces)
print("peaks:")
for p in peaks:
    print(f"  {p['file_id']}: t={p['time']:.4f}, val={p['value']:.4f}")

assert abs(peaks[0]["time"] - 0.3) < 0.01, f"A peak time wrong: {peaks[0]['time']}"
assert abs(peaks[1]["time"] - 0.5) < 0.01, f"B peak time wrong: {peaks[1]['time']}"
print("[OK] first_peak detects peaks correctly")

result = server.overlay(traces, align=True)
print(f"overlay result: {result}")
print(f"traces after overlay (looking for t_offset to be applied to trace B):")
for t in traces:
    print(f"  {t}")

fig = state.current_figure
xa = np.array(fig["data"][0]["x"])
xb = np.array(fig["data"][1]["x"])
print(f"trace A x range: [{xa[0]:.4f}, {xa[-1]:.4f}]")
print(f"trace B x range: [{xb[0]:.4f}, {xb[-1]:.4f}]")

# A peaks at 0.3, B's native peak is at 0.5. After align with A as anchor,
# B should be shifted by -0.2, i.e. xb should be ~[-0.2, 0.799] and B's
# peak (originally at 0.5) should now land at ~0.3 in plot coordinates.
expected_xb_start = -0.2
actual_xb_start = xb[0]
diff = abs(actual_xb_start - expected_xb_start)
print(f"\nexpected xb[0] ~= {expected_xb_start}, actual = {actual_xb_start:.4f}, diff = {diff:.4f}")

if diff < 0.01:
    print("[OK] align=True correctly applied offset to trace B")
else:
    print("[FAIL] align=True did NOT shift trace B")
    print("    → offsets are likely written to the wrong dict (peaks instead of traces).")
