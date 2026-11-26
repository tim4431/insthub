"""The UI of insthub's own extension: a panel for its simulated scope (`[ui.scope]` in
insthub-extension.toml), and the example to copy when writing one (docs/guide.md#writing-a-panel).

A panel is a function, `make(device, parent=None) -> QWidget`, for one device, an ordinary
proxy.  It never blocks the GUI thread: frames arrive through `qt.watch`, calls start through
`qt.start` and their results come back through `qt.deliver`, and the timebase, which a spin box's
arrows change faster than an instrument may take it, goes out through `qt.coalesce`.  Each result
names the panel as its `owner`, so one that comes after the panel was deleted (closed inside a
larger app) is dropped.  What it does not show in a way of its own, the generic controls do
(`controls.Controls`: the device's other commands).  It reaches the instrument only through the
device's commands and streams, so a script could do all it does, and it takes the limits of what
it sets from the device's own description.  It never disagrees with the device for long: it shows
what the device reports, also a value the device took differently from what was sent (rounded)
and one it refused.  It stops what it watches when its widget closes.  `insthub open scope` shows
it in a window of its own (`qt.device_window`), in a program of its own.
"""
from __future__ import annotations

import numpy as np

from .qt import QtCore, QtWidgets, coalesce, deliver, start, watch  # isort: skip (the Qt binding before pyqtgraph)

import pyqtgraph as pg  # isort: skip

from .controls import Controls

COLORS = {"ch1": "#2a78d6", "ch2": "#eb6834"}  #: told apart on light and dark backgrounds alike


def scope(device, parent=None) -> QtWidgets.QWidget:
    """The panel of a SimScope: what the manifest's `panel = "insthub.sim_ui:scope"` names."""
    return ScopePanel(device, parent)


class ScopePanel(QtWidgets.QWidget):
    """Both channels, live; the timebase; acquisition on or off; a measurement on demand; below, the
    scope's other commands."""

    def __init__(self, device, parent=None):
        super().__init__(parent)
        self.device = device
        self.reported: dict = {}  #: the scope's state as it reported it last (what the controls show)
        self.live = QtWidgets.QCheckBox("Live")
        self.live.toggled.connect(lambda on: self._send(device.live.future, on))
        # The timebase as the device takes it: the limits are its own (what describe() says about
        # set_timebase, which the proxy already has: no call), shown with SI prefixes, from ns to s.
        # Sent on Enter, an arrow or the wheel, not at every key; while the scope is still taking one
        # value, the newest waits (coalesce), so holding an arrow down never builds up a backlog.
        limits = device.bus.describe(refresh=False)["commands"]["set_timebase"].get("params", {}).get(
            "seconds_per_div", {})
        self.timebase = pg.SpinBox(value=1e-3, bounds=(limits.get("min"), limits.get("max")), suffix="s/div",
                                   siPrefix=True, dec=True, step=1, minStep=1e-12, decimals=4)
        self.set_timebase = coalesce(device.set_timebase.future, self._timebase_set, self._failed, owner=self)
        self.timebase.sigValueChanging.connect(lambda box, seconds: self.set_timebase(seconds_per_div=seconds))
        measure = QtWidgets.QPushButton("Measure Vpp")
        measure.clicked.connect(self.measure)
        self.result = QtWidgets.QLabel("")
        bar = QtWidgets.QHBoxLayout()
        for widget in (self.live, QtWidgets.QLabel("Timebase"), self.timebase, measure, self.result):
            bar.addWidget(widget)
        bar.addStretch(1)

        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "time", units="s")
        self.plot.setLabel("left", "", units="V")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.curves = {ch: self.plot.plot(pen=pg.mkPen(color, width=2)) for ch, color in COLORS.items()}
        self.controls = Controls(device, exclude=["live", "set_timebase"])  # (those two are above)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        split.addWidget(self.plot)
        split.addWidget(self.controls)
        split.setSizes([2000, 1000])  # two thirds for the traces
        box = QtWidgets.QVBoxLayout(self)
        box.addLayout(bar)
        box.addWidget(split, 1)

        self.watchers = [watch(device.bus.subscribe(ch), lambda frame, ch=ch: self._trace(ch, frame), parent=self)
                         for ch in self.curves]
        self.watchers.append(watch(device.bus.subscribe("state"), self._state, fps=5, parent=self))

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(900, 720)  # (the size its window opens at: room for the traces and the commands)

    def _trace(self, ch: str, frame) -> None:
        t = frame.meta.get("t0", 0.0) + frame.meta.get("dt", 1.0) * np.arange(len(frame.data))
        self.curves[ch].setData(t, frame.data)

    def _state(self, frame) -> None:
        """The scope's state, also when a script changed it: shown, and kept."""
        self.reported = frame.data
        self._show_reported()

    def _show_reported(self) -> None:
        """Show whether the scope acquires and its timebase, as it reported them last.  Not the
        timebase while one is on its way to it: until it has taken it, it may still report the one
        before.  What it reports meanwhile is kept (`reported`) and shown once the call has ended
        (_timebase_set, _failed), so even a value that the scope took differently from what was sent
        is shown."""
        shown = []
        if "live" in self.reported:
            shown.append((self.live, self.live.setChecked, bool(self.reported["live"])))
        if "timebase" in self.reported and not self.set_timebase.busy:
            shown.append((self.timebase, self.timebase.setValue, self.reported["timebase"]))
        for widget, show, value in shown:
            blocked = widget.blockSignals(True)  # (showing a value is not sending it)
            show(value)
            widget.blockSignals(blocked)

    def _timebase_set(self, taken: float) -> None:
        """The last timebase sent is set: show the one the scope took, which set_timebase returns (a
        real scope may round what it is sent).  For a setter that returns nothing, just call
        _show_reported()."""
        self.reported = {**self.reported, "timebase": taken}  # (a copy: a frame's data is not ours to change)
        self._show_reported()

    def measure(self) -> None:
        deliver(start(self.device.measure.future, "VPP"), lambda vpp: self.result.setText(f"Vpp {vpp:.3f} V"),
                self._failed, owner=self)

    def _send(self, call, *args) -> None:
        deliver(start(call, *args), error=self._failed, owner=self)

    def _failed(self, exc) -> None:
        """A call failed: say so, and show what the scope reported last again, not what it refused."""
        self.result.setText(f"failed: {exc}")
        self._show_reported()

    def closeEvent(self, event) -> None:
        for watcher in self.watchers:  # (a panel inside an app may close long before the program ends)
            watcher.stop()
        self.watchers = []
        super().closeEvent(event)
