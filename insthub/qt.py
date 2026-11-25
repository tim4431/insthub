"""Qt glue (optional; needs PyQt6, PySide6 or PyQt5).

A GUI must never block on an instrument and must only touch widgets from the
GUI thread.  Two helpers cover that:

    watch(sub, slot)        every display tick, hand the newest frame of a
                            subscription to `slot(frame)` in the GUI thread
    deliver(future, slot)   when a call finishes, hand its result to
                            `slot(result)` in the GUI thread

    scope = insthub.device("scope")
    watch(scope.subscribe("ch1"), lambda frame: curve.setData(frame.data))
    deliver(scope.measure.future("VPP"), lambda vpp: label.setText(f"{vpp:.3f} V"))

The GUI can live in the server process (pass `server.device("scope")`: frames
then arrive by reference, no copy) or anywhere else on the bus; the code is the
same.  `insthub.viewer` is a ready-made viewer built on these.
"""
from __future__ import annotations

import importlib
import logging
import signal
import sys

_log = logging.getLogger("insthub")
_BINDINGS = ("PyQt6", "PySide6", "PyQt5", "PySide2")


def _load():
    # Use the binding the application already imported, so that we share its QApplication.
    for name in sorted(_BINDINGS, key=lambda n: f"{n}.QtWidgets" not in sys.modules):
        try:
            return (name, importlib.import_module(f"{name}.QtCore"), importlib.import_module(f"{name}.QtGui"),
                    importlib.import_module(f"{name}.QtWidgets"))
        except ImportError:
            continue
    raise ImportError("insthub.qt needs a Qt binding, which insthub installs (PyQt6) but this Python does not "
                      "have: pip install PyQt6 pyqtgraph")


BINDING, QtCore, QtGui, QtWidgets = _load()
Signal = getattr(QtCore, "pyqtSignal", None) or QtCore.Signal


class Watcher(QtCore.QObject):
    """Polls a subscription from a QTimer.  Made by `watch()`."""

    def __init__(self, subscription, slot, fps: float = 30.0, parent=None):
        super().__init__(parent)
        self.subscription = subscription
        self._slot = slot
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(max(1, int(1000 / fps)))
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        poll = self.subscription.poll
        while (frame := poll()) is not None:  # one frame for a latest-only subscription; a queue is drained
            self._slot(frame)

    def stop(self) -> None:
        self._timer.stop()
        self.subscription.close()


def watch(subscription, slot, *, fps: float = 30.0, parent=None) -> Watcher:
    """Call `slot(frame)` in the GUI thread with the newest frame of a
    subscription, at most `fps` times a second.  A stream faster than the
    display is thinned out for free: a default (latest-only) subscription just
    skips what was never shown.  Keep the returned Watcher; `stop()` ends it."""
    return Watcher(subscription, slot, fps, parent)


class _Courier(QtCore.QObject):
    arrived = Signal(object)

    def __init__(self):
        super().__init__()
        self.arrived.connect(self._hand_over)

    def _hand_over(self, item) -> None:
        future, slot, error = item
        try:
            result = future.result()
        except Exception as e:  # noqa: BLE001 - reported to the GUI's handler, not raised into Qt
            if error is not None:
                error(e)
            else:
                _log.warning("call failed: %s: %s", type(e).__name__, e)
            return
        if slot is not None:
            slot(result)


_courier: _Courier | None = None


def deliver(future, slot=None, error=None) -> None:
    """When `future` (from `dev.command.future(...)`) completes, call
    `slot(result)`, or `error(exception)`, in the GUI thread.  Call this from
    the GUI thread."""
    global _courier
    if _courier is None:
        _courier = _Courier()  # lives in the GUI thread; emit() from other threads is queued to it
    courier = _courier
    future.add_done_callback(lambda f: courier.arrived.emit((f, slot, error)))


def application():
    """The QApplication, created if there is none yet.

    Ctrl-C and SIGTERM end the event loop instead of killing the process, so
    the code after `run()` (stopping the server, closing instruments) still
    runs."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    if not getattr(app, "_insthub_ready", False):
        app._insthub_ready = True
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: app.quit())
        except ValueError:  # not the main thread: leave signal handling to the application
            pass
        app._insthub_tick = QtCore.QTimer(app)  # Python only notices a signal while Python code runs
        app._insthub_tick.timeout.connect(lambda: None)
        app._insthub_tick.start(200)
    return app


def run(app=None) -> int:
    """Run the Qt event loop until the last window closes."""
    app = app or application()
    return app.exec() if hasattr(app, "exec") else app.exec_()
