"""The UI of insthub's own extension: a panel for its simulated scope (`[ui.scope]` in
insthub-extension.toml), and the example to copy when writing one (docs/guide.md#writing-a-panel).

A panel is a function, `make(device, parent=None) -> QWidget`, for one device, an ordinary
proxy.  It never blocks the GUI thread: frames arrive through `qt.watch`, results through
`qt.deliver`.  It reaches the instrument only through the device's commands and streams, so a
script could do all it does.  It stops what it watches when its widget closes.  `insthub open
scope` shows it in a window of its own (`qt.device_window`), in a program of its own.
"""
from __future__ import annotations

import numpy as np

from .qt import QtWidgets, deliver, watch  # isort: skip (the Qt binding before pyqtgraph)

import pyqtgraph as pg  # isort: skip

COLORS = {"ch1": "#2a78d6", "ch2": "#eb6834"}  #: told apart on light and dark backgrounds alike


def scope(device, parent=None) -> QtWidgets.QWidget:
    """The panel of a SimScope: what the manifest's `panel = "insthub.sim_ui:scope"` names."""
    return ScopePanel(device, parent)


class ScopePanel(QtWidgets.QWidget):
    """Both channels, live; the timebase; acquisition on or off; a measurement on demand."""

    def __init__(self, device, parent=None):
        super().__init__(parent)
        self.device = device
        self.live = QtWidgets.QCheckBox("Live")
        self.live.toggled.connect(lambda on: self._send(device.live.future(on)))
        self.timebase = QtWidgets.QDoubleSpinBox()
        self.timebase.setRange(0.001, 10_000)  # the device takes 1 ns to 10 s per division
        self.timebase.setDecimals(3)
        self.timebase.setSuffix(" ms/div")
        self.timebase.setKeyboardTracking(False)  # sent on Enter or an arrow, not at every key
        self.timebase.valueChanged.connect(lambda ms: self._send(device.set_timebase.future(ms / 1000)))
        measure = QtWidgets.QPushButton("Measure Vpp")
        measure.clicked.connect(self.measure)
        self.result = QtWidgets.QLabel("")
        controls = QtWidgets.QHBoxLayout()
        for widget in (self.live, QtWidgets.QLabel("Timebase"), self.timebase, measure, self.result):
            controls.addWidget(widget)
        controls.addStretch(1)

        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "time", units="s")
        self.plot.setLabel("left", "", units="V")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.curves = {ch: self.plot.plot(pen=pg.mkPen(color, width=2)) for ch, color in COLORS.items()}
        box = QtWidgets.QVBoxLayout(self)
        box.addLayout(controls)
        box.addWidget(self.plot, 1)

        self.watchers = [watch(device.bus.subscribe(ch), lambda frame, ch=ch: self._trace(ch, frame), parent=self)
                         for ch in self.curves]
        self.watchers.append(watch(device.bus.subscribe("state"), self._state, fps=5, parent=self))

    def _trace(self, ch: str, frame) -> None:
        t = frame.meta.get("t0", 0.0) + frame.meta.get("dt", 1.0) * np.arange(len(frame.data))
        self.curves[ch].setData(t, frame.data)

    def _state(self, frame) -> None:
        """Show the timebase and whether it acquires as the device says, also when a script changed them."""
        state = frame.data
        for widget, show, value in ((self.timebase, self.timebase.setValue, state.get("timebase", 0) * 1000),
                                    (self.live, self.live.setChecked, bool(state.get("live")))):
            blocked = widget.blockSignals(True)  # (showing a value is not sending it)
            show(value)
            widget.blockSignals(blocked)

    def measure(self) -> None:
        deliver(self.device.measure.future("VPP"), lambda vpp: self.result.setText(f"Vpp {vpp:.3f} V"), self._failed)

    def _send(self, future) -> None:
        deliver(future, error=self._failed)

    def _failed(self, exc) -> None:
        self.result.setText(f"failed: {exc}")

    def closeEvent(self, event) -> None:
        for watcher in self.watchers:  # (a panel inside an app may close long before the program ends)
            watcher.stop()
        self.watchers = []
        super().closeEvent(event)
