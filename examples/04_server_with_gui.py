"""One process that owns an instrument AND shows it: the server runs on
background threads, the Qt GUI in the main thread talks to it in-process
(frames arrive by reference, nothing is copied or serialized).  Other programs
can use the same device at the same time over the bus.

    python examples/04_server_with_gui.py        # elsewhere: insthub call scope set_signal 2500
"""
import numpy as np
import pyqtgraph as pg

import insthub
from insthub import qt, sim
from insthub.qt import QtWidgets

server = insthub.Server("bench-scope", [sim.SimScope("scope", points=2000)]).start()
scope = server.device("scope")                       # same API as insthub.device("scope") from another process

app = qt.application()
window = QtWidgets.QWidget()
window.setWindowTitle("scope")
layout = QtWidgets.QVBoxLayout(window)
plot = pg.PlotWidget()
plot.setLabel("bottom", "time", units="s")
plot.setLabel("left", "", units="V")
curve = plot.plot(pen=pg.mkPen("#3987e5", width=2))
readout = QtWidgets.QLabel("Vpp: ...")
freq = QtWidgets.QDoubleSpinBox(minimum=10, maximum=1e5, value=1000, suffix=" Hz", decimals=0, singleStep=100)
for widget in (plot, readout, freq):
    layout.addWidget(widget)


def show(frame):                                     # GUI thread, newest frame only, at most 30 times a second
    curve.setData(frame.meta["dt"] * np.arange(len(frame.data)), frame.data)


def measure():                                       # a button or timer must never wait for the instrument:
    qt.deliver(scope.measure.future("VPP"), lambda v: readout.setText(f"Vpp: {v:.3f} V"))


watcher = qt.watch(scope.subscribe("ch1"), show, fps=30)
freq.valueChanged.connect(lambda hz: scope.set_signal.notify(hz))
timer = qt.QtCore.QTimer(window)
timer.timeout.connect(measure)
timer.start(500)

window.resize(800, 500)
window.show()
qt.run(app)
watcher.stop()
server.stop()
