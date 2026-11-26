"""Generic live viewer (`insthub view DEVICE`): plots whatever a device publishes.

One panel per stream, chosen from the data:

    1-D array, dict of 1-D arrays, few-row 2-D array   ->  line plot (x from meta t0/dt or x0/dx)
    2-D array, HxWx3 array                             ->  image
    number                                             ->  strip chart of the recent values
    anything else                                      ->  text

The "state" stream is shown as a table beside the plots.  Streams that appear
later are picked up.  Use it as it is, or as an example of `insthub.qt`.
"""
from __future__ import annotations

import collections
import time

import numpy as np

from .device import busy_with, summarize
from .qt import QtCore, QtGui, QtWidgets, application, deliver, run, watch  # isort: skip (binding before pyqtgraph)

import pyqtgraph as pg  # isort: skip

# Colours: a validated categorical palette (fixed order, colour-blind-safe between
# neighbours) and chart chrome, each chosen separately for light and dark surfaces.
_THEMES = {
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
              "axis": "#c3c2b7",
              "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]},
    "dark": {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
             "axis": "#383835",
             "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"]},
}
_GOOD, _CRITICAL = "#0ca30c", "#d03b3b"
_MAX_SERIES = 8
_STRIP_POINTS = 600


def _theme() -> dict:
    window = application().palette().color(QtGui.QPalette.ColorRole.Window)
    return _THEMES["dark" if window.lightness() < 128 else "light"]


def _short(value) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (list, tuple)) and len(value) <= 6 and all(isinstance(v, (int, float, str)) for v in value):
        return ", ".join(_short(v) for v in value)
    if isinstance(value, (np.ndarray, dict, list, tuple, bytes, bytearray)):
        return summarize(value)
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


class _StreamView:
    """One stream's panel.  The renderer is chosen when the first frame arrives."""

    def __init__(self, layout: pg.GraphicsLayout, row: int, name: str, theme: dict):
        self.name, self.theme = name, theme
        self.plot = layout.addPlot(row=row, col=0)
        self.plot.setMenuEnabled(False)
        for side in ("left", "bottom"):
            axis = self.plot.getAxis(side)
            axis.setPen(pg.mkPen(theme["axis"]))
            axis.setTextPen(pg.mkPen(theme["muted"]))
        self.kind: str | None = None
        self._labels: tuple | None = None
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.image: pg.ImageItem | None = None
        self.text: pg.TextItem | None = None
        self.history: collections.deque = collections.deque(maxlen=_STRIP_POINTS)
        self.latest = None
        self.readout = ""
        self._mark, self.rate = None, 0.0
        self.cursor = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(theme["muted"], width=1))
        self.cursor.setVisible(False)
        self.plot.addItem(self.cursor, ignoreBounds=True)
        self._text("no data yet")
        self._title()

    def _title(self) -> None:
        parts = [f"<span style='color:{self.theme['ink']}'>{self.name}</span>"]
        if self.latest is not None:
            parts.append(f"{_short(self.latest.data)}")
            parts.append(f"{self.rate:.1f} Hz")
        if self.readout:
            parts.append(self.readout)
        text = "  ·  ".join(parts)
        if len(self.curves) > 1:  # the legend: a colour swatch beside each name, in the title row, never over the data
            text += "<br>" + " &nbsp; ".join(
                f"<span style='color:{curve.opts['pen'].color().name()}'>&#9644;</span> {name}"
                for name, curve in self.curves.items())
        self.plot.setTitle(text, color=self.theme["ink2"], size="10pt")

    # -- data -----------------------------------------------------------------
    def update(self, frame) -> None:
        self.latest = frame
        if self._mark is None or frame.seq < self._mark[0]:
            self._mark = (frame.seq, frame.time)
        elif frame.time - self._mark[1] >= 1.0:  # the publisher's rate, also when the display skips frames
            self.rate = (frame.seq - self._mark[0]) / (frame.time - self._mark[1])
            self._mark = (frame.seq, frame.time)
        data, meta = frame.data, frame.meta
        if isinstance(data, np.ndarray) and data.ndim == 1:
            self._lines({self.name: data}, meta)
        elif isinstance(data, np.ndarray) and data.ndim == 2 and data.shape[0] <= _MAX_SERIES < data.shape[1]:
            self._lines({f"{self.name}[{i}]": row for i, row in enumerate(data)}, meta)
        elif isinstance(data, np.ndarray) and (data.ndim == 2 or (data.ndim == 3 and data.shape[2] in (3, 4))):
            self._image(data)
        elif isinstance(data, dict) and data and all(isinstance(v, np.ndarray) and v.ndim == 1 for v in data.values()):
            self._lines({str(k): v for k, v in data.items()}, meta)
        elif isinstance(data, (int, float)) and not isinstance(data, bool):
            self.history.append((frame.time, float(data)))
            t, y = np.array(self.history).T
            self._lines({self.name: y}, {"unit": meta.get("unit")}, x=t - time.time(), xlabel="seconds ago")
            self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        else:
            self._text(data)
        self._title()

    def _set_kind(self, kind: str) -> None:
        if self.kind == kind:
            return
        self.kind = kind
        for item in (*self.curves.values(), self.image, self.text):
            if item is not None:
                self.plot.removeItem(item)
        self.curves, self.image, self.text, self._labels = {}, None, None, None
        lines = kind == "lines"
        self.plot.showGrid(x=lines, y=lines, alpha=0.25)
        self.plot.setAspectLocked(kind == "image")
        self.plot.invertY(kind == "image")
        for side in ("left", "bottom"):
            self.plot.showAxis(side, kind != "text")

    def _lines(self, series: dict, meta: dict, x=None, xlabel: str | None = None, xunit: str | None = None) -> None:
        self._set_kind("lines")
        if x is None:
            n = len(next(iter(series.values())))
            if "dt" in meta:
                x, xlabel, xunit = meta.get("t0", 0.0) + meta["dt"] * np.arange(n), "time", "s"
            elif "dx" in meta:
                x, xlabel, xunit = (meta.get("x0", 0.0) + meta["dx"] * np.arange(n), meta.get("xlabel", "x"),
                                    meta.get("xunit"))
        if (xlabel, xunit, meta.get("unit")) != self._labels:  # units let the axis pick its own SI prefix (ms, mV)
            self._labels = (xlabel, xunit, meta.get("unit"))
            self.plot.setLabel("bottom", xlabel or "sample", units=xunit, color=self.theme["muted"])
            self.plot.setLabel("left", "", units=meta.get("unit"), color=self.theme["muted"])
        for name, y in list(series.items())[:_MAX_SERIES]:
            curve = self.curves.get(name)
            if curve is None:  # a colour belongs to a series for good: assigned once, in fixed order
                color = self.theme["series"][len(self.curves) % _MAX_SERIES]
                curve = self.curves[name] = self.plot.plot(pen=pg.mkPen(color, width=2), antialias=True)
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
            if x is not None and len(x) == len(y):
                curve.setData(x, y)
            else:
                curve.setData(y)

    def _image(self, data: np.ndarray) -> None:
        self._set_kind("image")
        if self.image is None:
            self.image = pg.ImageItem(axisOrder="row-major", autoDownsample=True)
            self.plot.addItem(self.image)
        if data.ndim == 2:
            step = max(1, data.shape[0] // 256), max(1, data.shape[1] // 256)
            sample = data[::step[0], ::step[1]]
            lo, hi = float(sample.min()), float(sample.max())
            self.image.setImage(data, levels=(lo, hi if hi > lo else lo + 1))  # one hue, dark to light
        else:
            self.image.setImage(data)

    def _text(self, data) -> None:
        self._set_kind("text")
        if self.text is None:
            self.text = pg.TextItem(color=self.theme["ink"], anchor=(0.5, 0.5))
            self.plot.addItem(self.text)
        self.text.setText(_short(data))

    # -- hover ----------------------------------------------------------------
    def hover(self, scene_pos) -> None:
        box = self.plot.vb
        if self.latest is None or not self.plot.sceneBoundingRect().contains(scene_pos):
            self.cursor.setVisible(False)
            if self.readout:
                self.readout = ""
                self._title()
            return
        point = box.mapSceneToView(scene_pos)
        if self.kind == "lines":
            self.cursor.setVisible(True)
            self.cursor.setPos(point.x())
            values = []
            for name, curve in self.curves.items():
                xs, ys = curve.getData()
                if xs is None or not len(xs):
                    continue
                i = int(np.clip(np.searchsorted(xs, point.x()), 0, len(xs) - 1))
                values.append(f"{name if len(self.curves) > 1 else 'y'} = {ys[i]:.5g}")
            self.readout = f"x = {point.x():.5g}   " + "   ".join(values)
        elif self.kind == "image" and isinstance(self.latest.data, np.ndarray):
            col, row = int(point.x()), int(point.y())
            data = self.latest.data
            inside = 0 <= row < data.shape[0] and 0 <= col < data.shape[1]
            self.readout = f"({col}, {row}) = {data[row, col]}" if inside else ""
        self._title()


class DevicePanel(QtWidgets.QWidget):
    """Status, state table and live plots of one device (a DeviceProxy)."""

    def __init__(self, device, fps: float = 30.0, parent=None):
        super().__init__(parent)
        self.device, self.bus, self.fps = device, device.bus, fps
        self.theme = theme = _theme()
        self.views: dict[str, _StreamView] = {}
        self.watchers = []

        self.title = QtWidgets.QLabel(f"<b>{self.bus.name}</b>")
        self.status = QtWidgets.QLabel("...")
        self.status.setWordWrap(True)
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["state", "value"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setVisible(False)
        self.pause = QtWidgets.QCheckBox("Pause plots")
        side = QtWidgets.QVBoxLayout()
        for widget in (self.title, self.status, self.table, self.pause):
            side.addWidget(widget)
        side.addStretch(1)
        side_box = QtWidgets.QWidget()
        side_box.setLayout(side)
        side_box.setMaximumWidth(300)

        self.canvas = pg.GraphicsLayoutWidget()
        self.canvas.setBackground(theme["surface"])
        self._hover = pg.SignalProxy(self.canvas.scene().sigMouseMoved, rateLimit=60, slot=self._on_mouse)
        self.empty = pg.LabelItem("nothing published yet", color=theme["muted"])
        self.canvas.addItem(self.empty, row=0, col=0)

        split = QtWidgets.QSplitter()
        split.addWidget(side_box)
        split.addWidget(self.canvas)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 840])
        box = QtWidgets.QHBoxLayout(self)
        box.addWidget(split)

        self.watchers.append(watch(self.bus.subscribe("state"), self._on_state, fps=10, parent=self))
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)
        self._refresh()

    # -- periodic refresh, without ever blocking the GUI on the device ---------
    def _refresh(self) -> None:
        deliver(self.bus.future("$describe"), self._on_describe, self._on_error)

    def _on_error(self, exc) -> None:
        self.status.setText(f"<span style='color:{_CRITICAL}'>&#10005;</span> unreachable: {exc}")

    def _on_describe(self, info: dict) -> None:
        st = info["status"]
        if st["connected"]:
            text = f"<span style='color:{_GOOD}'>&#9679;</span> connected"
        else:
            text = f"<span style='color:{_CRITICAL}'>&#10005;</span> not connected: {st['error'] or 'not opened yet'}"
        if st["busy"]:
            text += f"<br>running: {busy_with(st)}" + (f" (+{st['queued']} queued)" if st["queued"] else "")
        self.title.setText(f"<b>{info['name']}</b> &nbsp; {info['type']}")
        self.status.setText(text)
        for name in sorted(info["streams"]):
            if name != "state" and name not in self.views:
                self._add_stream(name)

    def _add_stream(self, name: str) -> None:
        if self.empty is not None:
            self.canvas.removeItem(self.empty)
            self.empty = None
        view = self.views[name] = _StreamView(self.canvas.ci, len(self.views), name, self.theme)
        self.watchers.append(watch(self.bus.subscribe(name), lambda frame, v=view: self._on_frame(v, frame),
                                   fps=self.fps, parent=self))

    def _on_frame(self, view: _StreamView, frame) -> None:
        if not self.pause.isChecked():
            view.update(frame)

    def _on_state(self, frame) -> None:
        state = frame.data if isinstance(frame.data, dict) else {"state": frame.data}
        self.table.setVisible(bool(state))
        self.table.setRowCount(len(state))
        for row, (key, value) in enumerate(state.items()):
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(key)))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(_short(value)))

    def _on_mouse(self, event) -> None:
        for view in self.views.values():
            view.hover(event[0])

    def close_streams(self) -> None:
        self._timer.stop()
        for watcher in self.watchers:
            watcher.stop()
        self.watchers = []


class Viewer(QtWidgets.QMainWindow):
    """A window with one `DevicePanel` per device (tabs when there are several)."""

    def __init__(self, *devices, fps: float = 30.0):
        super().__init__()
        self.panels = [DevicePanel(dev, fps) for dev in devices]
        self.setWindowTitle("insthub - " + ", ".join(p.bus.name for p in self.panels))
        if len(self.panels) == 1:
            self.setCentralWidget(self.panels[0])
        else:
            tabs = QtWidgets.QTabWidget()
            for panel in self.panels:
                tabs.addTab(panel, panel.bus.name)
            self.setCentralWidget(tabs)
        self.resize(1100, 700)

    def closeEvent(self, event) -> None:
        for panel in self.panels:
            panel.close_streams()
        super().closeEvent(event)


def main(*devices, fps: float = 30.0) -> int:
    """Open the viewer on some devices and run until the window is closed."""
    app = application()
    window = Viewer(*devices, fps=fps)
    window.show()
    return run(app)
