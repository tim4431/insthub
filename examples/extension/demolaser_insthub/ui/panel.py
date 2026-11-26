"""The demo laser's panel: the window of one laser, which insthub opens in a program of its own
(`[ui.panel]` in insthub-extension.toml: `insthub open laser panel`, or **Open → Laser panel** on
the device's page in the window).

    make(device, parent=None) -> QWidget     device: an ordinary proxy, as insthub.device("laser") gives

It shows the power as a slider and the temperature, and leaves the other settings and commands to
insthub's generic controls (insthub.controls), the same ones as the window's device page.  It
never blocks the GUI thread: frames arrive through qt.watch, and the slider's values go out
through qt.follow, one call at a time with the newest value, so dragging it never builds up a
backlog.  qt.follow also decides what the slider shows: the power the laser reports (read every
second, see `poll` in device.py) and, after a change, the power the laser took, which may not be
the one asked for, but never an old report over a value on its way.  It reaches the laser only
through the bus, so a script could do all it does.  insthub's own panel for its simulated scope,
insthub/sim_ui.py, does more of the same (docs/guide.md#writing-a-panel).
"""
from __future__ import annotations

from insthub import qt
from insthub.controls import Controls

QtCore, QtWidgets = qt.QtCore, qt.QtWidgets


def make(device, parent=None) -> QtWidgets.QWidget:
    """The panel of a demo laser: what `panel = "demolaser_insthub.ui.panel:make"` names."""
    return LaserPanel(device, parent)


class LaserPanel(QtWidgets.QWidget):
    """The power, as a slider; the temperature; below, the laser's other settings and commands."""

    def __init__(self, device, parent=None):
        super().__init__(parent)
        self.device = device
        # The slider's range and step are the laser's own: what its description says about power,
        # which the proxy already has (no call).
        described = device.bus.describe(refresh=False)
        meta = described["properties"]["power"].get("meta", {})
        self.step = meta.get("step", 0.1)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setRange(round(meta.get("min", 0) / self.step), round(meta.get("max", 100) / self.step))
        self.reports = device.bus.subscribe("power")  #: the power the laser reports (the newest only)
        # set_power returns the power the laser took.  A report read just before it took it (it reads its
        # power every second) may come in with the answer: `reports=` drops it, not shown after the answer.
        self.set_power = qt.follow(device.set_power.future, self._show_power, reports=self.reports,
                                   error=self._failed, owner=self)
        self.slider.valueChanged.connect(self._send)
        self.power, self.temperature, self.message = (QtWidgets.QLabel("") for _ in range(3))
        self.power.setMinimumWidth(self.power.fontMetrics().horizontalAdvance("000.0 mW"))

        power = QtWidgets.QHBoxLayout()
        power.addWidget(self.slider, 1)
        power.addWidget(self.power)
        form = QtWidgets.QFormLayout()
        form.addRow("Power", power)
        form.addRow("Temperature", self.temperature)
        if described.get("simulated"):  # (so that nobody takes what it shows for a real laser's)
            form.addRow("", QtWidgets.QLabel("Simulated: no laser is connected."))
        self.controls = Controls(device, include=["emission", "wavelength", "mode", "pulse", "identify"])
        box = QtWidgets.QVBoxLayout(self)
        box.addLayout(form)
        box.addWidget(self.message)
        box.addWidget(self.controls, 1)

        self.watchers = [qt.watch(self.reports, lambda frame: self.set_power.report(frame.data), fps=5, parent=self),
                         qt.watch(device.bus.subscribe("temperature"), self._temperature, fps=5, parent=self)]

    def _send(self, steps: int) -> None:
        self.message.clear()  # (what failed before is old news)
        self.set_power(mw=steps * self.step)

    def _show_power(self, mw: float) -> None:
        """Show a power the laser has (qt.follow says when): on the slider and as a number."""
        blocked = self.slider.blockSignals(True)  # (showing a value is not sending it)
        self.slider.setValue(round(mw / self.step))
        self.slider.blockSignals(blocked)
        self.power.setText(f"{mw:g} mW")

    def _failed(self, exc) -> None:
        """A call failed, so the laser kept its power, which qt.follow shows again: say why."""
        self.message.setText(f"failed: {exc}")

    def _temperature(self, frame) -> None:
        self.temperature.setText(f"{frame.data:.2f} °C")

    def closeEvent(self, event) -> None:
        for watcher in self.watchers:  # (a panel inside an app may close long before the program ends)
            watcher.stop()
        self.watchers = []
        super().closeEvent(event)
