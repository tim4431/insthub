"""Qt glue (optional; needs PyQt6, PySide6 or PyQt5).

A GUI must never block on an instrument and must only touch widgets from the
GUI thread.  Five helpers cover that:

    watch(sub, slot)        every display tick, hand the newest frame of a
                            subscription to `slot(frame)` in the GUI thread
    start(call, *args)      start a call without waiting in the GUI thread, not
                            even for a server that has gone away (reconnecting
                            to it can take seconds); returns its future
    deliver(future, slot)   when a call finishes, hand its result to
                            `slot(result)` in the GUI thread
    coalesce(call)          a sender for a setting that changes faster than the
                            instrument takes it (a slider): one call at a time,
                            with the newest values
    follow(call, show)      the same, for a control that also shows the setting
                            as the device reports it (and takes it): never an
                            old report over a value on its way

    scope = insthub.device("scope")
    watch(scope.subscribe("ch1"), lambda frame: curve.setData(frame.data))
    deliver(start(scope.measure.future, "VPP"), lambda vpp: label.setText(f"{vpp:.3f} V"))
    timebase = coalesce(scope.set_timebase.future)
    slider.valueChanged.connect(lambda v: timebase(seconds_per_div=v * 1e-6))

The GUI can live in the server process (pass `server.device("scope")`: frames
then arrive by reference, no copy) or anywhere else on the bus; the code is the
same.  `insthub.viewer` is a ready-made viewer built on these, `insthub.controls`
the settings and commands of any device, and `insthub.sim_ui` an example of a
panel: `make(device, parent)` -> a widget, which `device_window(make, device)`
puts in a window of its own.
"""
from __future__ import annotations

import importlib
import logging
import queue
import signal
import sys
import threading
import weakref
from concurrent.futures import InvalidStateError

from .client import CallFuture
from .errors import ConnectionLost

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
    raise ImportError("insthub.qt needs a Qt binding: pip install 'insthub[gui]' (pyqtgraph + PyQt6)")


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


def _pass_on(source, target: CallFuture) -> None:
    """What a started call came to (`source`, done) becomes the result of the future start() gave."""
    try:
        if source.cancelled():
            target.cancel()
        elif source.exception() is not None:
            target.set_exception(source.exception())
        else:
            target.set_result(source.result())
    except InvalidStateError:  # its caller cancelled it: nobody wants it any more
        pass


class _Starter:
    """Starts the calls of one connection on a thread of its own, one after another in the order they
    were made (see start()).  The thread ends once it has had nothing to do for a while."""

    IDLE = 10.0  #: seconds without a call before the thread ends (the next call starts another)

    def __init__(self, name: str, one_server: bool = True):
        self._name, self._one_server = name, one_server  # (one_server: its calls all go to one server)
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._running = False

    def put(self, call, args: tuple, kwargs: dict) -> CallFuture:
        future = CallFuture()
        self._jobs.put((future, call, args, kwargs))
        with self._lock:  # (a thread that is ending looks at the queue under the same lock)
            if not self._running:
                self._running = True
                threading.Thread(target=self._run, name=f"insthub:qt-start:{self._name}", daemon=True).start()
        return future

    def _run(self) -> None:
        while True:
            try:
                job = self._jobs.get(timeout=self.IDLE)
            except queue.Empty:
                with self._lock:
                    if self._jobs.empty():
                        self._running = False
                        return
                continue
            self._start(*job)

    def _start(self, future: CallFuture, call, args: tuple, kwargs: dict) -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            started = call(*args, **kwargs)
            future.where = getattr(started, "where", "")
            started.add_done_callback(lambda done: _pass_on(done, future))
        except Exception as e:  # noqa: BLE001 - one that cannot start fails like one that ran (and the thread goes on)
            if not future.done():
                future.set_exception(e)
            if isinstance(e, ConnectionLost) and self._one_server:  # its server is gone: those that waited
                self._fail_waiting(e)  # for it fail alike, at once, instead of each trying (and waiting) again

    def _fail_waiting(self, exc: ConnectionLost) -> None:
        while True:
            try:
                future = self._jobs.get_nowait()[0]
            except queue.Empty:
                return
            if future.set_running_or_notify_cancel():
                future.set_exception(ConnectionLost(str(exc)))


_starters: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # connection -> its _Starter
_elsewhere = _Starter("other", one_server=False)  # for calls whose connection is not known
_starters_lock = threading.Lock()


def start(call, /, *args, **kwargs) -> CallFuture:
    """Start `call(*args, **kwargs)` without waiting for it in the GUI thread, and return the future
    of its result at once: `deliver(start(scope.measure.future, "VPP"), show)`.

    `call` is a command's `.future` (`scope.measure.future`) or `device.bus.future`.  Starting a call
    takes no time while its server is there; to one that has gone away, the proxy first reconnects,
    which can take seconds (or fail).  start() does that on a thread of the device's connection, not
    in the GUI thread, and calls started through it still run in the order they were made.  If the
    server cannot be reached, the future fails with ConnectionLost, and so do the calls waiting
    behind it to start.  `call` runs on that thread: it must not touch widgets."""
    bound = getattr(call, "func", call)  # (a functools.partial of one)
    connection = getattr(getattr(bound, "__self__", None), "_t", None)  # what RemoteMethod and Bus send on
    if connection is None:
        return _elsewhere.put(call, args, kwargs)
    with _starters_lock:
        starter = _starters.get(connection)
        if starter is None:
            starter = _starters[connection] = _Starter(getattr(connection, "address", "?"))
    return starter.put(call, args, kwargs)


class _Courier(QtCore.QObject):
    arrived = Signal(object)

    def __init__(self):
        super().__init__()
        self.arrived.connect(self._hand_over)

    def _hand_over(self, item) -> None:
        future, slot, error, owner = item
        if owner is not None and _deleted(owner):
            return
        try:
            result = future.result()
        except Exception as e:  # noqa: BLE001 - reported to the GUI's handler, not raised into Qt
            (error or _warn)(e)
            return
        if slot is not None:
            slot(result)


def _warn(exc: Exception) -> None:
    """What happens to a failed call whose GUI gave no function for it."""
    _log.warning("call failed: %s: %s", type(exc).__name__, exc)


def _deleted(obj) -> bool:
    """Whether a Qt object was deleted: its Python object can outlive it, and then raises."""
    try:
        obj.objectName()
    except RuntimeError:
        return True
    return False


_courier: _Courier | None = None


def deliver(future, slot=None, error=None, *, owner=None) -> None:
    """When `future` (from start(), or `dev.command.future(...)`) completes, call
    `slot(result)`, or `error(exception)`, in the GUI thread.  `owner`: the
    widget they show the result in; once it is deleted (a panel inside a larger
    window can be, before the instrument answers), nothing is called.  Call this
    from the GUI thread."""
    global _courier
    if _courier is None:
        _courier = _Courier()  # lives in the GUI thread; emit() from other threads is queued to it
    courier = _courier
    future.add_done_callback(lambda f: courier.arrived.emit((f, slot, error, owner)))


def _merged(waiting: dict, new: dict) -> dict:
    return {**waiting, **new}


class _Coalesced:
    """The sender made by coalesce()."""

    def __init__(self, call, done, error, merge, owner):
        self._call, self._done, self._error, self._merge = call, done, error or _warn, merge or _merged
        self._owner = owner
        self._waiting: dict | None = None
        self.busy = False  #: whether a call is on its way (the instrument has not answered it yet)

    def __call__(self, **values) -> None:
        if self.busy:
            self._waiting = values if self._waiting is None else self._merge(self._waiting, values)
        else:
            self._start(values)

    def _start(self, values: dict) -> None:
        self.busy = True  # (from now on, also while a server that has gone is tried again: one call at a time)
        deliver(start(self._call, **values), self._answered, self._failed, owner=self._owner)

    def _next(self) -> bool:
        """The call on its way has ended: send what waits, if anything does (and say whether)."""
        self.busy, values, self._waiting = False, self._waiting, None
        if values is not None:
            self._start(values)
        return values is not None

    def _answered(self, result) -> None:
        if not self._next() and self._done is not None:  # (a result that newer values replace is not shown)
            self._done(result)

    def _failed(self, exc: Exception) -> None:
        self._next()  # what waits goes out first: `busy` then says whether a newer call is on its way
        self._error(exc)


def coalesce(call, done=None, error=None, merge=None, *, owner=None) -> _Coalesced:
    """A sender for a setting that changes faster than the instrument takes it, such as a slider
    or a spin box's arrows held down: `send = coalesce(device.set_timebase.future)`, then
    `send(seconds_per_div=v)` for each new value.

    `call(**values)` starts a call and returns its future; it is started as start() starts one,
    off the GUI thread, so it must not touch widgets.  At most one call is on its way at a time;
    the values sent meanwhile wait, merged into the next call: a later value of a key replaces an
    earlier one, or `merge(waiting, new) -> dict` decides, for keys that go together (a span that
    a new center invalidates, say).  So the instrument gets the newest values as fast as it takes
    them, and never works through a backlog of old ones.

    `done(result)` runs in the GUI thread with the result of a call that nothing waits behind,
    `error(exception)` with each call that fails (without it, a warning is logged), neither once
    `owner` is deleted, as for deliver().  `send.busy` says whether a call is on its way: it is
    False in `done`, and in `error` unless values that waited went out.  A control that also shows
    the device's own value uses follow(), which builds on this.  Use the sender from the GUI
    thread."""
    return _Coalesced(call, done, error, merge, owner)


class _Followed:
    """The sender made by follow()."""

    def __init__(self, call, show, reports, error, merge, owner):
        self._show, self._reports, self._error = show, reports, error or _warn
        self._send = _Coalesced(call, self._taken, self._failed, merge, owner)
        self.value = None  #: what the device reported last, or took (None: nothing yet)

    @property
    def busy(self) -> bool:
        """Whether a call is on its way (as coalesce()'s `busy`)."""
        return self._send.busy

    def __call__(self, **values) -> None:
        self._send(**values)

    def report(self, value) -> None:
        """What the device reports now (from a stream, or its state): kept, and shown unless a
        call is on its way, which the device may not have taken yet."""
        self.value = value
        if not self.busy:
            self._show(value)

    def _taken(self, result) -> None:
        if self._reports is not None:  # a report not shown yet may be older than the answer: dropped
            self._reports.poll()
        if result is not None:
            self.value = result
        if self.value is not None:
            self._show(self.value)

    def _failed(self, exc: Exception) -> None:
        self._error(exc)
        if not self.busy and self.value is not None:  # (unless newer values went out: they are shown then)
            self._show(self.value)


def follow(call, show, *, reports=None, error=None, merge=None, owner=None) -> _Followed:
    """A sender for a setting, as coalesce() makes, for a control that also shows the setting as the
    device has it: a slider that shows the power a laser reports, and sends the one it is moved to.
    `send = follow(laser.set_power.future, show_power)`; `send(mw=v)` for each new value, and
    `send.report(value)` with each value the device reports (from a stream or its state, through
    watch()).

    It never disagrees with the device for long, and never shows an old value over one on its way:
    `show(value)` runs in the GUI thread with each value reported while no call is on its way (while
    one is, the device may still report the value before, so it is kept, not shown), and once the
    last call has ended with what the device took (the call's result, if it returns one, as a
    setter that rounds does; else what it reported last) or, if it failed, with what it reported
    last (it kept that), after `error(exception)` (without it, a warning is logged).  `reports`: the
    subscription that report() is fed from, for a value the device reads on its own (polls): what
    it read just before it took the new value may come in just before the answer, and is dropped
    then, not shown over it.  `merge` and `owner` are coalesce()'s.  `send.busy` says whether a call
    is on its way, `send.value` what the device reported last, or took.  Use it from the GUI thread
    (docs/guide.md#guis)."""
    return _Followed(call, show, reports, error, merge, owner)


def application():
    """The QApplication, created if there is none yet.

    Ctrl-C and SIGTERM end the event loop instead of killing the process, so
    the code after `run()` (stopping the server, closing instruments) still
    runs."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    if not getattr(app, "_insthub_ready", False):
        app._insthub_ready = True
        try:  # (queued: a quit() that comes before the loop runs would be lost, and the program never end)
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: QtCore.QTimer.singleShot(0, app.quit))
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


class _DeviceWindow(QtWidgets.QMainWindow):
    """The window of device_window(): closing it closes its panel first."""

    def closeEvent(self, event) -> None:
        panel = self.centralWidget()
        if panel is not None and not panel.close():  # its closeEvent stops what it watches (or says no)
            event.ignore()
            return
        super().closeEvent(event)


def device_window(make, device, title: str | None = None):
    """A window of its own around a panel: `make(device, parent)` returns the widget of a panel for
    one device (a proxy; see docs/guide.md#writing-a-panel).  The window is titled after the panel
    (`title`) and the device.  Keep it, show() it and run(), as `insthub panel DEVICE
    MODULE:FUNCTION` does in a program of its own.  Closing it closes the panel's widget, whose
    closeEvent stops its watchers."""
    application()
    window = _DeviceWindow()
    window.setCentralWidget(make(device, window))
    window.setWindowTitle(f"{title} - {device.bus.name}" if title else device.bus.name)
    return window
