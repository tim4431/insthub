"""Devices: the server-side objects that own one hardware session each.

A `Device` subclass marks what clients may use:

    class Synth(Device):
        def open(self):                       # runs on the device thread
            self.port = serial.Serial(...)
        def close(self):
            self.port.close()

        @command
        def set_frequency(self, mhz: float): ...

        @prop
        def power(self) -> float: ...
        @power.setter
        def power(self, dbm): ...

        @periodic(0.5)                        # runs on the device thread when it is idle
        def _poll(self):
            self.publish("temperature", self.read_temperature())

Each device has ONE worker thread.  Commands, property access and periodic
tasks all run on it, one at a time, so driver code never needs a lock and a
slow instrument never delays another device.  `wrap()` hosts an existing driver
class without writing a subclass.
"""
from __future__ import annotations

import inspect
import logging
import queue
import threading
import time

import numpy as np

from . import codec
from .errors import BusError, DeviceNotConnected, UnknownCommand

_STOP = object()
_WAKE = object()
_log = logging.getLogger("insthub")


# ---------------------------------------------------------------- decorators
def command(fn=None, *, concurrent: bool = False, needs_open: bool = True):
    """Expose a method to clients.

    concurrent=True: the method only touches thread-safe state (a cached value,
    a flag), so it runs at once on the caller's thread instead of waiting for
    the device thread.  It must not use the hardware session.
    needs_open=False: do not (re)open the hardware session before running it."""

    def mark(f):
        f._insthub_command = {"concurrent": concurrent, "needs_open": needs_open and not concurrent}
        return f

    return mark(fn) if fn is not None else mark


class prop(property):
    """A property that clients can read (and write, if it has a setter) as a
    plain attribute of the proxy: `dev.power`, `dev.power = -10`."""


def periodic(interval: float, *, enabled: bool = True):
    """Run a method every `interval` seconds on the device thread, between
    commands.  interval=0 means "again as soon as it returns" (an acquisition
    loop).  Control it at run time through `self.tasks[name]`."""

    def mark(f):
        f._insthub_periodic = (float(interval), bool(enabled))
        return f

    return mark


# ---------------------------------------------------------------- data model
class Frame:
    """One published value of a stream."""

    __slots__ = ("stream", "seq", "time", "data", "meta")

    def __init__(self, stream: str, seq: int, time_: float, data, meta: dict):
        self.stream = stream
        self.seq = seq  #: 1, 2, 3 ... per stream; gaps mean frames you did not get
        self.time = time_  #: publisher's time.time()
        self.data = data
        self.meta = meta

    @property
    def age(self) -> float:
        """Seconds since it was published (meaningful if the clocks agree)."""
        return time.time() - self.time

    def __repr__(self) -> str:
        return f"Frame({self.stream!r} #{self.seq}, {summarize(self.data)}, meta={self.meta})"


def summarize(value) -> str:
    """Short description of a value for listings: 'float32[1000]', 'dict(3)', 'float'."""
    if isinstance(value, np.ndarray):
        return f"{value.dtype}[{'x'.join(map(str, value.shape))}]"
    if isinstance(value, (bytes, bytearray)):
        return f"bytes[{len(value)}]"
    if isinstance(value, (dict, list, tuple, set)):
        return f"{type(value).__name__}({len(value)})"
    return type(value).__name__


class Task:
    """A periodic method of a device.  `start()`, `stop()` and `interval` may be
    used from any thread."""

    def __init__(self, runtime, name: str, fn, interval: float, enabled: bool):
        self._rt = runtime
        self.name = name
        self.fn = fn
        self.interval = interval
        self.enabled = enabled
        self.next_due = 0.0
        self.runs = 0
        self.errors = 0
        self.last_error: str | None = None

    def start(self, interval: float | None = None) -> None:
        if interval is not None:
            self.interval = float(interval)
        self.next_due = time.monotonic()
        self.enabled = True
        self._rt.wake()

    def stop(self) -> None:
        self.enabled = False

    def __repr__(self) -> str:
        return f"Task({self.name!r}, interval={self.interval}, enabled={self.enabled}, runs={self.runs})"


class _Cmd:
    __slots__ = ("name", "fn", "signature", "doc", "concurrent", "needs_open")

    def __init__(self, name, fn, signature="(...)", doc="", concurrent=False, needs_open=True):
        self.name, self.fn, self.signature, self.doc = name, fn, signature, doc
        self.concurrent, self.needs_open = concurrent, needs_open


class _Prop:
    __slots__ = ("name", "get", "set", "doc")

    def __init__(self, name, get, set_, doc=""):
        self.name, self.get, self.set, self.doc = name, get, set_, doc


class _Stream:
    __slots__ = ("name", "doc", "seq", "last", "sinks", "lock")

    def __init__(self, name, doc=""):
        self.name = name
        self.doc = doc
        self.seq = 0
        self.last: Frame | None = None
        self.sinks: tuple = ()
        self.lock = threading.Lock()


class _Bare(str):
    """A string annotation (from `from __future__ import annotations`) that prints without quotes."""

    def __repr__(self) -> str:
        return str(self)


def _signature(fn, drop_first: bool = False) -> str:
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())[1 if drop_first else 0:]
        params = [p.replace(annotation=_Bare(p.annotation)) if isinstance(p.annotation, str) else p for p in params]
        ret = sig.return_annotation
        return str(sig.replace(parameters=params, return_annotation=_Bare(ret) if isinstance(ret, str) else ret))
    except (TypeError, ValueError, IndexError):
        return "(...)"


def _doc(obj) -> str:
    return inspect.cleandoc(getattr(obj, "__doc__", None) or "")


# ---------------------------------------------------------------- runtime
class _Runtime:
    """Everything the bus keeps per device: the worker thread and its queue,
    the command table, streams, state and periodic tasks."""

    def __init__(self, dev: Device):
        self.dev = dev
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self.thread: threading.Thread | None = None
        self.running = False
        self.opened = False
        self.opened_at: float | None = None
        self.last_error: str | None = None
        self.last_attempt = float("-inf")
        self.open_failed = False
        self.busy: str | None = None
        self.calls = 0
        self.errors = 0
        declared = dev.streams if isinstance(dev.streams, dict) else dict.fromkeys(dev.streams, "")
        self.streams: dict[str, _Stream] = {name: _Stream(name, doc) for name, doc in declared.items()}
        self.streams_lock = threading.Lock()
        self.state: dict = {}
        self.state_lock = threading.Lock()
        self.commands, self.props = dev._insthub_tables()
        self.tasks: dict[str, Task] = {}
        for name in dir(type(dev)):
            spec = getattr(inspect.getattr_static(type(dev), name), "_insthub_periodic", None)
            if spec is not None:
                self.tasks[name] = Task(self, name, getattr(dev, name), *spec)
        self._last_task_log = 0.0

    # -- lifecycle (any thread) ------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, name=f"insthub:{self.dev.name}", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if not self.running:
            return
        self.running = False
        self.queue.put(_STOP)
        if self.thread is not threading.current_thread():
            self.thread.join(timeout)
            if self.thread.is_alive():
                _log.warning("%s: device thread still busy (%s) after %.0f s; leaving it",
                             self.dev.name, self.busy, timeout)

    def wake(self) -> None:
        if self.running:
            self.queue.put(_WAKE)

    def submit(self, cmd: _Cmd, args, kwargs, reply) -> None:
        """Run a command: inline for concurrent ones and for calls made from the
        device thread itself (a device calling its own proxy), else queued."""
        if cmd.concurrent:
            try:
                ok, result = True, cmd.fn(*args, **kwargs)
            except Exception as e:
                ok, result = False, e
            reply(ok, result)
        elif threading.current_thread() is self.thread:
            self._execute(cmd, args, kwargs, reply)
        elif not self.running:
            reply(False, BusError(f"device {self.dev.name!r} is not running (server stopped?)"))
        else:
            self.queue.put((cmd, args, kwargs, reply))

    # -- device thread ----------------------------------------------------
    def _run(self) -> None:
        dev = self.dev
        if dev.open_on_start:
            try:
                self.ensure_open()
            except Exception as e:
                dev.log.warning("not connected at start: %s", e)
        q = self.queue
        tasks = list(self.tasks.values())
        try:
            while True:
                delay = self._task_delay(tasks) if tasks else None
                try:
                    if delay is None:
                        item = q.get()
                    elif delay <= 0:
                        item = q.get_nowait()
                    else:
                        item = q.get(True, delay)
                except queue.Empty:
                    item = None
                if item is _STOP:
                    break
                if not self.running:  # stopping: what has not started yet is cancelled, not run
                    if item is not None and item is not _WAKE:
                        item[3](False, BusError(f"device {dev.name!r} stopped before running {item[0].name}"))
                    continue
                if item is not None and item is not _WAKE:
                    self._execute(*item)
                if tasks:
                    self._run_due(tasks)
        finally:
            self.running = False
            self._close("server stopped")
            while True:  # nobody will run what is still queued: tell the callers
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item is not _STOP and item is not _WAKE:
                    item[3](False, BusError(f"device {dev.name!r} stopped before running {item[0].name}"))

    def _execute(self, cmd: _Cmd, args, kwargs, reply) -> None:
        outer, self.busy = self.busy, cmd.name
        try:
            if cmd.needs_open and not self.opened:
                self.ensure_open()
            result = cmd.fn(*args, **kwargs)
        except Exception as e:
            self.errors += 1
            self._after_error(e)
            ok, result = False, e
        else:
            ok = True
        finally:
            self.busy = outer
            self.calls += 1
        try:
            reply(ok, result)
        except Exception:
            self.dev.log.exception("could not deliver the result of %s", cmd.name)

    def ensure_open(self, force: bool = False) -> None:
        if self.opened:
            return
        dev = self.dev
        now = time.monotonic()
        wait = dev.reconnect_interval - (now - self.last_attempt)
        if wait > 0 and not force:
            if self.open_failed:  # the instrument is not there: fail fast instead of queueing callers up
                raise DeviceNotConnected(
                    f"{dev.name}: not connected ({self.last_error}); next attempt in {wait:.1f} s")
            time.sleep(wait)  # it worked until just now: give the instrument a moment, then reopen
        self.last_attempt = time.monotonic()
        try:
            dev.open()
        except Exception as e:
            self.open_failed = True
            self.last_error = f"{type(e).__name__}: {e}"
            try:
                dev.close()
            except Exception:
                pass
            raise DeviceNotConnected(f"{dev.name}: cannot open: {self.last_error}") from e
        self.opened = True
        self.open_failed = False
        self.opened_at = time.time()
        self.last_error = None
        dev.log.info("connected")

    def _close(self, reason: str) -> None:
        if not self.opened:
            return
        self.opened = False
        self.opened_at = None
        try:
            self.dev.close()
        except Exception as e:
            self.dev.log.debug("close failed: %s", e)

    def release(self, reason: str = "released") -> None:
        self._close(reason)
        self.last_error = reason
        self.last_attempt = float("-inf")  # the next command may reopen at once

    def _after_error(self, exc: Exception) -> None:
        """A link-level failure leaves the session in an unknown state: drop it
        so that the next command starts from a fresh connection."""
        if self.opened and isinstance(exc, self.dev.link_errors):
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.dev.log.warning("link error (%s); dropping the session", self.last_error)
            self._close("link error")

    def _task_delay(self, tasks) -> float | None:
        due = None
        for t in tasks:
            if t.enabled and (due is None or t.next_due < due):
                due = t.next_due
        return None if due is None else due - time.monotonic()

    def _run_due(self, tasks) -> None:
        for t in tasks:
            now = time.monotonic()
            if not t.enabled or t.next_due > now:
                continue
            t.next_due = max(t.next_due + t.interval, now)
            try:
                if not self.opened:
                    self.ensure_open()
                t.fn()
                t.runs += 1
            except DeviceNotConnected:
                t.next_due = now + max(t.interval, self.dev.reconnect_interval, 0.05)
            except Exception as e:
                t.errors += 1
                t.last_error = f"{type(e).__name__}: {e}"
                t.next_due = now + max(t.interval, 0.2)
                self._after_error(e)
                if now - self._last_task_log > 5:
                    self._last_task_log = now
                    self.dev.log.warning("periodic task %s failed: %s", t.name, t.last_error, exc_info=True)

    # -- streams (any thread) ---------------------------------------------
    def stream(self, name: str) -> _Stream:
        st = self.streams.get(name)
        if st is None:
            with self.streams_lock:
                st = self.streams.setdefault(name, _Stream(name))
        return st

    def publish(self, name: str, data, timestamp, meta: dict) -> None:
        st = self.streams.get(name) or self.stream(name)
        with st.lock:
            st.seq += 1
            frame = st.last = Frame(name, st.seq, timestamp or time.time(), data, meta)
            encoded = None
            for sink in st.sinks:
                if sink.local:  # in this process: hand over the Frame itself
                    if sink.deliver(frame):  # True: its subscription is gone
                        st.sinks = tuple(s for s in st.sinks if s is not sink)
                else:
                    if encoded is None:  # serialized once, however many remote subscribers
                        encoded = codec.encode([frame.seq, frame.time, codec.lift(data), meta])
                    sink.post(encoded)

    def add_sink(self, name: str, sink, retained: bool) -> None:
        st = self.stream(name)
        with st.lock:
            st.sinks += (sink,)
            last = st.last
            if retained and last is not None:
                if sink.local:
                    sink.deliver(last)
                else:
                    sink.post(codec.encode([last.seq, last.time, codec.lift(last.data), last.meta]))

    def remove_sink(self, name: str, sink) -> None:
        st = self.streams.get(name)
        if st is not None:
            with st.lock:
                st.sinks = tuple(s for s in st.sinks if s is not sink)

    # -- introspection (any thread) ---------------------------------------
    def status(self) -> dict:
        return {"connected": self.opened, "error": self.last_error, "since": self.opened_at,
                "busy": self.busy, "queued": self.queue.qsize(), "calls": self.calls, "errors": self.errors,
                "subscribers": sum(len(s.sinks) for s in list(self.streams.values()))}

    def describe(self) -> dict:
        dev = self.dev
        streams = {}
        for name, st in list(self.streams.items()):
            last = st.last
            streams[name] = {"seq": st.seq, "time": last.time if last else None, "doc": st.doc,
                             "kind": summarize(last.data) if last else None, "subscribers": len(st.sinks)}
        return {
            "name": dev.name,
            "type": dev._insthub_type(),
            "doc": _doc(dev._insthub_doc()).split("\n\n")[0],
            "commands": {c.name: {"signature": c.signature, "doc": c.doc, "concurrent": c.concurrent}
                         for c in self.commands.values()},
            "properties": {p.name: {"doc": p.doc, "settable": p.set is not None} for p in self.props.values()},
            "streams": streams,
            "tasks": {t.name: {"interval": t.interval, "enabled": t.enabled, "runs": t.runs,
                               "errors": t.errors, "last_error": t.last_error} for t in self.tasks.values()},
            "status": self.status(),
        }


# ---------------------------------------------------------------- Device
class Device:
    """Base class of a device hosted on the bus.  See the module docstring."""

    #: Name on the bus.  Set by `__init__`, by `Server.add(dev, name)`, or defaults to the class name.
    name: str | None = None
    #: Minimum time between two attempts to open the hardware session.  After a
    #: link error the next command waits this out and reopens; while the
    #: instrument cannot be opened at all, commands fail at once instead.
    reconnect_interval = 1.0
    #: Exceptions that mean "the link is broken": the session is closed and the
    #: next command reopens it.  Add your library's I/O error here (VisaIOError, ...).
    link_errors: tuple = (OSError, EOFError)
    #: Open the session when the server starts (else on first use).
    open_on_start = True
    #: Streams this device publishes, {name: description}.  Optional (a stream
    #: exists as soon as it is published or subscribed to), but declaring them
    #: lets viewers and `insthub info` find a stream that only produces data
    #: while somebody is subscribed.
    streams: dict | tuple = ()

    def __init__(self, name: str | None = None):
        if name is not None:
            self.name = name

    # -- to override -------------------------------------------------------
    def open(self) -> None:
        """Open the hardware session.  Called on the device thread: at start,
        and again before the next command after a link error.  Raise on failure."""

    def close(self) -> None:
        """Close the hardware session.  Must cope with a half-open or dead one."""

    # -- for use by subclasses ----------------------------------------------
    @property
    def log(self) -> logging.Logger:
        return logging.getLogger(f"insthub.{self.name or type(self).__name__}")

    @property
    def connected(self) -> bool:
        """Whether the hardware session is open."""
        return self._rt.opened

    def publish(self, stream: str, data, *, timestamp: float | None = None, copy: bool = False, **meta) -> None:
        """Publish `data` on a stream.  Subscribers in this process get the very
        object; remote ones get it sent straight from its memory, possibly after
        this call returns.  So do not modify an array after publishing it; pass
        copy=True if you reuse an acquisition buffer.  Keyword arguments become
        the frame's `meta` dict.  Costs about a microsecond with no subscriber."""
        if copy and isinstance(data, np.ndarray):
            data = data.copy()
        self._rt.publish(stream, data, timestamp, meta)

    def subscribers(self, stream: str | None = None) -> int:
        """How many subscriptions are attached (to one stream, or to any).  Lets a
        periodic task skip an expensive readout nobody is looking at."""
        rt = self._rt
        if stream is not None:
            st = rt.streams.get(stream)
            return len(st.sinks) if st else 0
        return sum(len(s.sinks) for s in list(rt.streams.values()))

    @property
    def state(self) -> dict:
        """The device's state dict (settings worth showing: frequency, mode, ...).
        Read-only snapshot; change it with `set_state`."""
        return self._rt.state

    def set_state(self, **values) -> None:
        """Merge values into the state and publish the whole dict on the stream
        "state" if anything changed."""
        rt = self._rt
        with rt.state_lock:
            old = rt.state
            try:
                if all(k in old and (old[k] is v or bool(old[k] == v)) for k, v in values.items()):
                    return
            except Exception:
                pass
            new = rt.state = {**old, **values}
            rt.publish("state", new, None, {})

    @property
    def tasks(self) -> dict[str, Task]:
        """The periodic tasks by method name: `self.tasks["_acquire"].start(0.05)`."""
        return self._rt.tasks

    # -- plumbing ------------------------------------------------------------
    @property
    def _rt(self) -> _Runtime:
        try:
            return self.__dict__["_insthub_rt"]
        except KeyError:
            rt = self.__dict__["_insthub_rt"] = _Runtime(self)
            return rt

    def _insthub_type(self) -> str:
        return type(self).__name__

    def _insthub_doc(self):
        return type(self)

    def _insthub_tables(self) -> tuple[dict, dict]:
        """(commands, properties) exposed by this device."""
        cls = type(self)
        commands, props = {}, {}
        for name in dir(cls):
            if name.startswith("_"):
                continue
            attr = inspect.getattr_static(cls, name)
            if isinstance(attr, prop):
                props[name] = _Prop(name, _Getter(self, name), _Setter(self, name) if attr.fset else None, _doc(attr))
                continue
            opts = getattr(attr, "_insthub_command", None)
            if opts is not None:
                bound = getattr(self, name)
                commands[name] = _Cmd(name, bound, _signature(bound), _doc(attr), **opts)
        return commands, props

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


class _Getter:
    __slots__ = ("obj", "name")

    def __init__(self, obj, name):
        self.obj, self.name = obj, name

    def __call__(self):
        return getattr(self.obj, self.name)


class _Setter(_Getter):
    __slots__ = ()

    def __call__(self, value):
        setattr(self.obj, self.name, value)


# ---------------------------------------------------------------- wrap
class Wrapped(Device):
    """Hosts an ordinary driver object.  Made by `wrap()`.

    Subclass it to add bus features to an existing driver: `@command`, `@prop`
    and `@periodic` methods of the subclass are exposed next to the driver's
    own methods, and reach the driver through `self.target`."""

    def __init__(self, target, name=None, *, args=(), kwargs=None, include=None, exclude=(),
                 attrs=(), close="close"):
        if isinstance(target, type):
            self._cls, self._obj, self._owned = target, None, True
        else:
            self._cls, self._obj, self._owned = type(target), target, False
        self._args, self._kwargs = tuple(args), dict(kwargs or {})
        self._include = None if include is None else set(include)
        self._exclude = set(exclude) | {close}
        self._attrs = tuple(attrs)
        self._close_name = close
        super().__init__(name or self._cls.__name__.lower())

    @property
    def target(self):
        """The driver object (None while the session is closed)."""
        return self._obj

    def open(self) -> None:
        if self._owned:
            self._obj = self._cls(*self._args, **self._kwargs)

    def close(self) -> None:
        if self._owned and self._obj is not None:
            obj, self._obj = self._obj, None
            closer = getattr(obj, self._close_name, None)
            if callable(closer):
                closer()

    def _insthub_type(self) -> str:
        return self._cls.__name__

    def _insthub_doc(self):
        return self._cls

    def _insthub_tables(self):
        cls = self._cls
        commands, props = {}, {}
        own_commands, own_props = Device._insthub_tables(self)  # what a subclass adds; wins over the driver's
        for name in dir(cls):
            if name.startswith("_") or name in self._exclude:
                continue
            if self._include is not None and name not in self._include:
                continue
            attr = inspect.getattr_static(cls, name)
            if isinstance(attr, property):
                props[name] = _Prop(name, _TargetGet(self, name), _TargetSet(self, name) if attr.fset else None,
                                    _doc(attr))
            elif isinstance(attr, staticmethod):
                commands[name] = _Cmd(name, _TargetCall(self, name), _signature(attr.__func__), _doc(attr.__func__))
            elif isinstance(attr, classmethod):
                commands[name] = _Cmd(name, _TargetCall(self, name), _signature(attr.__func__, True),
                                      _doc(attr.__func__))
            elif callable(attr) and not isinstance(attr, type):
                commands[name] = _Cmd(name, _TargetCall(self, name), _signature(attr, True), _doc(attr))
        for name in self._attrs:
            props[name] = _Prop(name, _TargetGet(self, name), _TargetSet(self, name), "")
        if self._include:
            missing = self._include - set(commands) - set(props)
            if missing:
                raise UnknownCommand(f"{cls.__name__} has no public method or property named {sorted(missing)}")
        return {**commands, **own_commands}, {**props, **own_props}


class _TargetCall:
    __slots__ = ("dev", "name")

    def __init__(self, dev, name):
        self.dev, self.name = dev, name

    def __call__(self, *args, **kwargs):
        return getattr(self.dev._obj, self.name)(*args, **kwargs)


class _TargetGet(_TargetCall):
    __slots__ = ()

    def __call__(self):
        return getattr(self.dev._obj, self.name)


class _TargetSet(_TargetCall):
    __slots__ = ()

    def __call__(self, value):
        setattr(self.dev._obj, self.name, value)


def wrap(target, name: str | None = None, *, args=(), kwargs: dict | None = None, include=None, exclude=(),
         attrs=(), close: str = "close", link_errors: tuple | None = None, lazy: bool = False) -> Wrapped:
    """Host an existing driver on the bus without writing a Device subclass.

    target   a class: it is instantiated as `target(*args, **kwargs)` on the
             device thread when the session opens, and re-created after a link
             error (so reconnecting comes for free); or an instance you made
             yourself, which is used as is.
    Every public method becomes a command and every public property a property.
    include / exclude   restrict that to / remove some names.
    attrs    names of plain instance attributes to expose as properties.
    close    name of the method that closes the driver (default "close").
    link_errors   exception classes that mean the connection is broken.
    lazy     open on first use instead of at server start."""
    dev = Wrapped(target, name, args=args, kwargs=kwargs, include=include, exclude=exclude, attrs=attrs, close=close)
    if link_errors is not None:
        dev.link_errors = tuple(link_errors)
    if lazy:
        dev.open_on_start = False
    return dev
