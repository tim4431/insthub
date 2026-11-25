"""Devices: the server-side objects that own one hardware session each.

A `Device` subclass marks what clients may use:

    class Synth(Device):
        def open(self):                       # runs on the device thread
            self.port = serial.Serial(...)
        def close(self):
            self.port.close()

        @command(params={"mhz": {"unit": "MHz", "min": 10, "max": 24000}})
        def set_frequency(self, mhz: float): ...

        @prop(unit="dBm", step=0.5)           # advice for UIs and agents (META_KEYS)
        def power(self) -> float: ...
        @power.setter
        def power(self, dbm): ...

        @command(dangerous=True)              # agents and the window ask before running it
        def zero_calibration(self): ...

        @periodic(0.5)                        # runs on the device thread when it is idle
        def _poll(self):
            self.publish("temperature", self.read_temperature())

Each device has ONE worker thread.  Commands, property access and periodic
tasks all run on it, one at a time, so driver code never needs a lock and a
slow instrument never delays another device.  `wrap()` hosts an existing driver
class without writing a subclass.

A device describes itself (`describe()`): its commands and properties, their
metadata and which are dangerous, and what it is (`extension`, `simulated`).
What a client needs to know comes from there, so that a generic page or an AI
agent can use a device it has never seen.
"""
from __future__ import annotations

import enum
import inspect
import logging
import queue
import threading
import time
import typing

import numpy as np

from . import codec
from .errors import BusError, DeviceNotConnected, DevicePaused, UnknownCommand, did_you_mean

_STOP = object()
_WAKE = object()
_log = logging.getLogger("insthub")


# ---------------------------------------------------------------- metadata
#: The words that describe a property's value or a command parameter: its unit, its limits,
#: the step a spin box takes, and the values to choose from.  Advice for UIs and agents, never
#: enforced: an instrument may accept "auto", and it clamps a value by itself.
META_KEYS = ("unit", "min", "max", "step", "choices")


def _checked_meta(meta: dict, where: str) -> dict:
    """Metadata as describe() carries it, with choices as a list (an Enum or a Literal gives its
    values).  A key that is not one of META_KEYS, a limit that is not a number and choices that
    are not a list are a TypeError.  None values stay: merged, they remove a key (see _merged)."""
    checked = {}
    for key, value in meta.items():
        if key not in META_KEYS:
            raise TypeError(f"{where}: {key!r} is not one of {', '.join(META_KEYS)}" + did_you_mean(key, META_KEYS))
        if value is not None and key in ("min", "max", "step") and not _is_number(value):
            raise TypeError(f"{where}: {key} should be a number, not {value!r}")
        if value is not None and key == "choices":
            allowed = _allowed(value)
            if allowed is None and (isinstance(value, (str, bytes, dict)) or not hasattr(value, "__iter__")):
                raise TypeError(f"{where}: choices should be a list, not {value!r}")
            value = list(value) if allowed is None else allowed
        checked[key] = value
    return checked


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _allowed(annotation) -> list | None:
    """The values a `Literal[...]` or an Enum allows (an Enum's: its members' values, which is
    what a client can send); None for anything else, and for an Enum without members (a base
    class such as enum.Enum, which says what kind of value it is, not which)."""
    if typing.get_origin(annotation) is typing.Literal:
        return list(typing.get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum) and len(annotation):
        return [member.value for member in annotation]
    return None


def _inferred(annotation) -> dict:
    """The metadata an annotation gives by itself: the choices of a Literal or an Enum.  Not of
    a Flag: its members combine (READ | WRITE), so they are not the values to choose one of."""
    if isinstance(annotation, type) and issubclass(annotation, enum.Flag):
        return {}
    allowed = _allowed(annotation)
    return {} if allowed is None else {"choices": allowed}


def _merged(*layers: dict) -> dict:
    """Layers of metadata, a later one winning; a None value removes its key."""
    merged = {}
    for layer in layers:
        merged.update(layer)
    return {key: value for key, value in merged.items() if value is not None}


# ---------------------------------------------------------------- decorators
def command(fn=None, *, concurrent: bool = False, needs_open: bool = True, dangerous: bool = False,
            params: dict[str, dict] | None = None):
    """Expose a method to clients.

    concurrent=True: the method only touches thread-safe state (a cached value,
    a flag), so it runs at once on the caller's thread instead of waiting for
    the device thread.  It must not use the hardware session.
    needs_open=False: do not (re)open the hardware session before running it.
    dangerous=True: running it can harm the instrument, the sample or a person
    (a laser's fire, a supply's output).  AI agents need a tool of its own to run
    it, and the window asks first.
    params: metadata of its parameters, {name: {"unit": "MHz", "min": 10}} (see
    META_KEYS).  A parameter annotated with a Literal[...] or an Enum gets its
    choices by itself."""

    def mark(f):
        where = f"@command of {getattr(f, '__name__', f)}"
        f._insthub_command = {"concurrent": concurrent, "needs_open": needs_open and not concurrent,
                              "dangerous": bool(dangerous),
                              "params": {p: _checked_meta(m, f"{where}, parameter {p}") for p, m in
                                         (params or {}).items()}}
        return f

    return mark(fn) if fn is not None else mark


class prop(property):
    """A property that clients can read (and write, if it has a setter) as a
    plain attribute of the proxy: `dev.power`, `dev.power = -10`.

        @prop                                 # or @prop(unit="dBm", min=-40, max=20)
        def power(self) -> float: ...

    Keyword arguments are its metadata (see META_KEYS); a getter annotated to
    return a Literal[...] or an Enum gives the choices by itself.  dangerous=True:
    WRITING it can harm the instrument, the sample or a person, so AI agents need
    a tool of its own to write it, and the window asks first."""

    def __init__(self, fget=None, fset=None, fdel=None, doc=None, *, dangerous: bool = False, **meta):
        super().__init__(fget, fset, fdel, doc)
        self._doc = doc  #: as given: a copy made by .setter keeps it, or else takes the getter's
        self.dangerous = bool(dangerous)
        self.meta = _merged(_checked_meta(meta, "@prop"))

    def __call__(self, fget) -> prop:
        """`@prop(unit="Hz")`: the getter comes after the options."""
        if self.fget is not None:
            raise TypeError(f"property {self.fget.__name__!r} is not callable")
        return self.getter(fget)

    # property's own copies (power.setter ...) would drop the metadata
    def getter(self, fget) -> prop:
        return self._with(fget=fget)

    def setter(self, fset) -> prop:
        return self._with(fset=fset)

    def deleter(self, fdel) -> prop:
        return self._with(fdel=fdel)

    def _with(self, **function) -> prop:
        parts = {"fget": self.fget, "fset": self.fset, "fdel": self.fdel, **function}
        return type(self)(**parts, doc=self._doc, dangerous=self.dangerous, **self.meta)


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
    __slots__ = ("concurrent", "dangerous", "doc", "fn", "name", "needs_open", "params", "sig", "signature",
                 "while_paused")

    def __init__(self, name, fn, signature="(...)", doc="", concurrent=False, needs_open=True, while_paused=False,
                 dangerous=False, params=None, sig=None):
        self.name, self.fn, self.signature, self.doc = name, fn, signature, doc
        self.concurrent, self.needs_open = concurrent, needs_open
        self.while_paused = while_paused  #: runs even while the device is paused (only $pause and $resume)
        self.dangerous = dangerous
        self.params: dict[str, dict] = params or {}  #: metadata of its parameters, by name
        self.sig: inspect.Signature | None = sig  #: what it takes (None: cannot be told)

    def info(self) -> dict:
        """What describe() says about it: the optional keys only when they say something."""
        info = {"signature": self.signature, "doc": self.doc, "concurrent": self.concurrent}
        if self.params:
            info["params"] = self.params
        if self.dangerous:
            info["dangerous"] = True
        return info


class _Prop:
    __slots__ = ("dangerous", "doc", "get", "meta", "name", "set", "via")

    def __init__(self, name, get, set_, doc="", meta=None, dangerous=False, via=None):
        self.name, self.get, self.set, self.doc = name, get, set_, doc
        self.meta: dict = meta or {}
        self.dangerous = dangerous  #: writing it is
        self.via: str | None = via  #: the command that writes it too (set_X, of an accessor pair)

    def info(self) -> dict:
        info = {"doc": self.doc, "settable": self.set is not None}
        if self.meta:
            info["meta"] = self.meta
        if self.dangerous:
            info["dangerous"] = True
        return info


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


def _signatures(fn, drop_first: bool = False) -> tuple:
    """fn's signature as written, which clients are shown, and with its annotations evaluated
    (for the choices of a Literal or an Enum), both without the first parameter if drop_first
    (self, of a function of a class).  The second is the first when an annotation cannot be
    evaluated (a name imported only for type checkers); both are None if fn has none that can
    be read (some functions written in C)."""
    try:
        written = inspect.signature(fn)
    except (TypeError, ValueError):
        return None, None
    try:
        typed = inspect.signature(fn, eval_str=True)
    except Exception:  # noqa: BLE001 - whatever evaluating it raised: keep the annotations as written
        typed = written
    if drop_first:
        written, typed = (s.replace(parameters=list(s.parameters.values())[1:]) for s in (written, typed))
    return written, typed


def _shown(sig: inspect.Signature | None) -> str:
    """A signature as clients see it, "(mhz: float) -> None": string annotations (from
    `from __future__ import annotations`) without quotes."""
    if sig is None:
        return "(...)"
    params = [p.replace(annotation=_Bare(p.annotation)) if isinstance(p.annotation, str) else p
              for p in sig.parameters.values()]
    ret = sig.return_annotation
    return str(sig.replace(parameters=params, return_annotation=_Bare(ret) if isinstance(ret, str) else ret))


def _required(sig: inspect.Signature) -> list[str]:
    """The parameters a call must give."""
    return [p.name for p in sig.parameters.values()
            if p.default is p.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]


def _doc(obj) -> str:
    return inspect.cleandoc(getattr(obj, "__doc__", None) or "")


def _command(name: str, fn, call, drop_first: bool = False, opts: dict | None = None) -> _Cmd:
    """The table entry of a command: `call` runs it; `fn`, the function (or the bound method),
    says what it takes; `opts` is what @command says.  A parameter's metadata: the choices of
    its Literal or Enum annotation, then what @command(params=...) says, which may name only
    parameters it has (else UnknownCommand)."""
    opts = dict(opts or {})
    declared = opts.pop("params", {})
    sig, typed = _signatures(fn, drop_first)
    if typed is None:  # what it takes cannot be told, nor what params names
        params = {p: meta for p, m in declared.items() if (meta := _merged(m))}
        return _Cmd(name, call, _shown(sig), _doc(fn), params=params, **opts)
    unknown = [p for p in declared if p not in typed.parameters]
    if unknown:
        raise UnknownCommand(f"{name}{_shown(sig)} has no parameter {unknown[0]!r}"
                             + did_you_mean(unknown[0], typed.parameters))
    params = {p.name: meta for p in typed.parameters.values()
              if (meta := _merged(_inferred(p.annotation), declared.get(p.name, {})))}
    return _Cmd(name, call, _shown(sig), _doc(fn), params=params, sig=typed, **opts)


def _declared(obj, attr: str, kind, default=None):
    """What a device (or a driver) declares as `attr` (extension, simulated, poll, dangerous), if
    it is of that kind.  Read without running anything: a subclass may use the name for a command
    or a property of its own, which then is no declaration (and its getter must not run here)."""
    value = inspect.getattr_static(obj, attr, default)
    return value if isinstance(value, kind) else default


#: What `dangerous` may be: True, a name, or names.
_MARKS = (bool, str, tuple, list, set, frozenset)


def _property(name: str, attr: property, get, set_) -> _Prop:
    """The table entry of a property: `get` and `set_` reach it (set_ only if it has a setter).
    Its metadata: the choices of its getter's Literal or Enum return annotation, then @prop's.
    @prop(dangerous=True) counts only with a setter: reading is never dangerous."""
    typed = _signatures(attr.fget)[1] if attr.fget is not None else None
    inferred = _inferred(typed.return_annotation) if typed is not None else {}
    settable = attr.fset is not None
    return _Prop(name, get, set_ if settable else None, _doc(attr), _merged(inferred, getattr(attr, "meta", {})),
                 settable and getattr(attr, "dangerous", False))


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
        self.paused: dict | None = None  #: while paused: since when, and who paused it (pid, peer, by)
        self._paused_closed = False  #: the device thread has closed the session for this pause
        self._before_pause: tuple = (False, None)  #: (was the session open, last_error) when it was closed
        declared = dev.streams if isinstance(dev.streams, dict) else dict.fromkeys(dev.streams, "")
        self.streams: dict[str, _Stream] = {name: _Stream(name, doc) for name, doc in declared.items()}
        self.streams_lock = threading.Lock()
        self.state: dict = {}
        self.state_lock = threading.Lock()
        self.commands: dict[str, _Cmd] = {}
        self.props: dict[str, _Prop] = {}
        self.tasks: dict[str, Task] = {}
        for name in dir(type(dev)):
            spec = getattr(inspect.getattr_static(type(dev), name), "_insthub_periodic", None)
            if spec is not None:
                self.tasks[name] = Task(self, name, getattr(dev, name), *spec)
        self.overrides: dict[str, dict] = {}  #: what set_meta() changed, by name: kept when the tables are remade
        self._last_task_log = 0.0
        self.build()

    # -- the tables (before the device starts) ----------------------------
    def build(self) -> None:
        """Make the tables of commands and properties from what the device declares, and the tasks of
        its `poll`.  Run when the runtime is made, and again when the device is added to a server, so
        that what is declared in between counts (a config file sets `poll` once the constructor has
        run, and the constructor may have used the runtime already).  Raises UnknownCommand, or a
        TypeError, for a name the device does not have or cannot be used so."""
        dev = self.dev
        commands, props = dev._insthub_tables()
        # The class's marks, then the device's own (a config file's, wrap()'s): one may only add to
        # the other, since a table that marks one more command must not unmark what the author marked.
        for layer in (type(dev), dev):
            for entry in self._dangerous(_declared(layer, "dangerous", _MARKS, ()), commands, props):
                entry.dangerous = True
        for p in props.values():  # an accessor pair writes one setting: what marks one half marks both
            c = commands.get(p.via) if p.via else None
            if c is not None and (c.dangerous or p.dangerous):
                c.dangerous = p.dangerous = True
        for name, changes in self.overrides.items():
            self._meta(commands, props, name, changes)
        self.commands, self.props = commands, props
        self._poll(dict(_declared(dev, "poll", dict, {})))

    def _dangerous(self, names, commands: dict, props: dict) -> list:
        """The entries that `dangerous` names: commands, and properties (writing them).  True: every
        command and every property that can be written.  A name the device does not have is an
        UnknownCommand, a property that cannot be written a TypeError (reading is never dangerous)."""
        if names is True:
            return [*commands.values(), *(p for p in props.values() if p.set is not None)]
        names = (names,) if isinstance(names, str) else names or ()
        unknown = [n for n in names if n not in commands and n not in props]
        if unknown:
            raise UnknownCommand(f"{self.dev.name}: dangerous names {unknown[0]!r}, which is none of its commands "
                                 "and properties" + did_you_mean(unknown[0], [*commands, *props]))
        entries = [commands.get(n) or props[n] for n in names]
        read_only = [e.name for e in entries if isinstance(e, _Prop) and e.set is None]
        if read_only:
            raise TypeError(f"{self.dev.name}: dangerous names {read_only[0]!r}, a property that cannot be written "
                            "(only writing a property can be dangerous)")
        return entries

    def _meta(self, commands: dict, props: dict, name: str, changes: dict) -> None:
        """Merge metadata into the entry `name` names: a property ("power"), or a command's
        parameter ("set_frequency.mhz")."""
        command, dot, param = name.partition(".")
        if not dot:
            p = props.get(name)
            if p is None:
                raise UnknownCommand(f"{self.dev.name} has no property {name!r}" + did_you_mean(name, props))
            p.meta = _merged(p.meta, changes)
            return
        c = commands.get(command)
        if c is None:
            raise UnknownCommand(f"{self.dev.name} has no command {command!r}" + did_you_mean(command, commands))
        if c.sig is not None and param not in c.sig.parameters:
            raise UnknownCommand(f"{self.dev.name}.{command}{c.signature} has no parameter {param!r}"
                                 + did_you_mean(param, c.sig.parameters))
        params = dict(c.params)  # a new dict: describe() may be reading the old one on another thread
        params[param] = _merged(params.get(param, {}), changes)
        c.params = {p: m for p, m in params.items() if m}

    def set_meta(self, name: str, meta: dict) -> None:
        """Device.set_meta(): checked, merged into the tables, and kept for when they are remade."""
        changes = _checked_meta(meta, f"{self.dev.name}.{name}")
        self._meta(self.commands, self.props, name, changes)
        self.overrides[name] = {**self.overrides.get(name, {}), **changes}

    def _poll(self, poll: dict) -> None:
        """A task "poll:NAME" for each entry of the device's `poll` ({NAME: seconds}): it reads the
        property NAME, or calls the command NAME, and publishes the value on the stream NAME.  A task
        made already keeps what was done to it ($task), unless `poll` now declares another interval
        (a config file's after the class's): then it takes that.  One no longer in `poll` goes."""
        for name, seconds in poll.items():
            if name not in self.props:
                c = self.commands.get(name)
                if c is None:
                    raise UnknownCommand(f"{self.dev.name}: poll names {name!r}, which is neither a property nor "
                                         "a command" + did_you_mean(name, [*self.props, *self.commands]))
                if c.sig is not None and _required(c.sig):
                    raise TypeError(f"{self.dev.name}: poll names {name}{c.signature}, which needs arguments")
            seconds, task = float(seconds), self.tasks.get(f"poll:{name}")
            if task is None:
                self.tasks[f"poll:{name}"] = Task(self, f"poll:{name}", _Poll(self, name, seconds), seconds, True)
            elif task.fn.seconds != seconds:
                task.fn.seconds = task.interval = seconds
            self.stream(name)  # listed before its first value (its doc: see _stream_doc)
        for task in [t for t in self.tasks if t.startswith("poll:") and t[5:] not in poll]:
            del self.tasks[task]

    def _stream_doc(self, st: _Stream) -> str:
        """A stream's doc: as declared, or for a stream that a poll task fills, how often it does."""
        task = self.tasks.get(f"poll:{st.name}")
        return st.doc or (f"{st.name}, read every {task.interval:g} s (poll)" if task is not None else "")

    # -- lifecycle (any thread) ------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        if self._paused_closed and self.last_error == "paused":  # a pause ends with the server that ran it
            was_open, before = self._before_pause
            self.last_error = "released" if was_open else before
        self.paused, self._paused_closed = None, False
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
        paused = self.paused
        if cmd.concurrent:
            try:  # (the command or task running when a pause came in may call its own device until it ends)
                if paused is not None and not cmd.while_paused and \
                        (self._paused_closed or threading.current_thread() is not self.thread):
                    raise self._paused_error(paused)
                ok, result = True, cmd.fn(*args, **kwargs)
            except Exception as e:
                ok, result = False, e
            reply(ok, result)
        elif threading.current_thread() is self.thread:
            self._execute(cmd, args, kwargs, reply, nested=True)
        elif not self.running:
            reply(False, BusError(f"device {self.dev.name!r} is not running (server stopped?)"))
        elif paused is not None and not cmd.while_paused:  # at once: not after the command running now
            reply(False, self._paused_error(paused))
        else:
            self.queue.put((cmd, args, kwargs, reply))

    # -- device thread ----------------------------------------------------
    def _run(self) -> None:
        dev = self.dev
        if dev.open_on_start:
            self.busy = "$open"  # (in the status: a pause that comes in meanwhile waits for it)
            try:
                self.ensure_open()
            except Exception as e:
                dev.log.warning("not connected at start: %s", e)
            finally:
                self.busy = None
        q = self.queue
        tasks = list(self.tasks.values())
        started = time.monotonic()
        for t in tasks:  # each runs first now, then an interval later (from 0, it would run twice now)
            t.next_due = max(t.next_due, started)
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

    def _execute(self, cmd: _Cmd, args, kwargs, reply, nested: bool = False) -> None:
        """Run one command on the device thread.  `nested`: called by the command or task running
        now (a device calling its own proxy), which may go on until the pause closes the session."""
        outer, self.busy = self.busy, cmd.name
        try:
            paused = self.paused
            if paused is not None and not cmd.while_paused and (self._paused_closed or not nested):
                raise self._paused_error(paused)
            if cmd.needs_open and not self.opened:
                self.ensure_open(nested=nested)
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

    def ensure_open(self, force: bool = False, nested: bool = False) -> None:
        paused = self.paused
        if paused is not None and (self._paused_closed or not nested):
            raise self._paused_error(paused)
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
            paused = self.paused  # (paused meanwhile: then it is not opened)
            if paused is not None and (self._paused_closed or not nested):
                raise self._paused_error(paused)
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

    # -- pausing ----------------------------------------------------------
    def pause(self, by: dict | None = None) -> None:
        """From now on, refuse every command, property and periodic task (any thread).  The
        session is closed by pause_now() on the device thread, after the command running now."""
        if self.paused is None:
            self.paused = {"since": time.time(), **(by or {})}

    def pause_now(self, by: dict | None = None) -> dict:
        """The device thread's half of a pause: be paused (again, if a resume came in between:
        of several pauses and resumes, the last in the device's queue wins), and close the session."""
        if self.paused is None:
            self.pause(by)
        if not self._paused_closed:  # whether it was open is known only now, after the command that ran
            self._before_pause = (self.opened, self.last_error)
            self._paused_closed = True
        self._close("paused")
        self.last_error = "paused"
        return self.status()

    def resume_now(self) -> dict:
        """Lift a pause (device thread).  The session is opened again at once if it was open when
        it was paused; a device that had not been opened (lazy) is left alone."""
        if self.paused is not None:
            closed, (was_open, before) = self._paused_closed, self._before_pause
            self.paused, self._paused_closed = None, False
            if closed:  # (not closed: resumed before its pause reached the device; nothing to undo)
                self.last_error = "released" if was_open else before
                self.last_attempt = float("-inf")
                if was_open:
                    try:
                        self.ensure_open(force=True)
                    except DeviceNotConnected:
                        pass  # the status says why
        return self.status()

    def _paused_error(self, paused: dict) -> DevicePaused:
        since = time.strftime("%H:%M:%S", time.localtime(paused["since"]))
        who = f" by {paused['by']}" if paused.get("by") else ""
        return DevicePaused(f"{self.dev.name} is paused (since {since}{who}): nothing runs on it until it is "
                            f"resumed (insthub resume {self.dev.name})")

    def _task_delay(self, tasks) -> float | None:
        if self.paused is not None:
            return None  # nothing is due while paused: wait for a request (a resume wakes it)
        due = None
        for t in tasks:
            if t.enabled and (due is None or t.next_due < due):
                due = t.next_due
        return None if due is None else due - time.monotonic()

    def _run_due(self, tasks) -> None:
        for t in tasks:
            if self.paused is not None:  # (asked for while the task before ran: none starts after it)
                return
            now = time.monotonic()
            if not t.enabled or t.next_due > now:
                continue
            t.next_due = max(t.next_due + t.interval, now)
            try:
                if not self.opened:
                    self.busy = "$open"  # (in the status: a pause that comes in meanwhile waits for it)
                    try:
                        self.ensure_open()
                    finally:
                        self.busy = None
                    if self.paused is not None:  # paused while it opened: the task does not run
                        return
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
                "subscribers": sum(len(s.sinks) for s in list(self.streams.values())),
                "paused": dict(paused) if (paused := self.paused) is not None else None}

    def describe(self) -> dict:
        dev = self.dev
        streams = {}
        for name, st in list(self.streams.items()):
            last = st.last
            streams[name] = {"seq": st.seq, "time": last.time if last else None, "doc": self._stream_doc(st),
                             "kind": summarize(last.data) if last else None, "subscribers": len(st.sinks)}
        about = {key: value for key in ("extension", "extension_version") if (value := _declared(dev, key, str))}
        if _declared(dev, "simulated", bool):
            about["simulated"] = True
        return {
            "name": dev.name,
            "type": dev._insthub_type(),
            "doc": _doc(dev._insthub_doc()).split("\n\n")[0],
            **about,
            "commands": {c.name: c.info() for c in self.commands.values()},
            "properties": {p.name: p.info() for p in self.props.values()},
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
    #: The extension the device belongs to: the name a UI and the window know it by
    #: (see docs/design.md#extensions).  insthub compares it, and never interprets it.
    extension: str | None = None
    #: The version of the extension's code the device runs, if it says.
    extension_version: str | None = None
    #: It talks to a simulator, not to an instrument.  Clients mark it SIM, so that
    #: its output is never taken for a measurement.
    simulated = False
    #: Values to read again and again, {name: seconds}: a property, or a command
    #: that needs no argument.  Each value is published on a stream of the same name,
    #: by a periodic task "poll:<name>".
    poll: dict | None = None
    #: What is dangerous to use (beyond what @command and @prop say): names of
    #: commands, and of properties (to write them); True for all of them.  Set on
    #: the device (by a config file, by wrap()), it adds to what the class marks.
    dangerous: bool | tuple = ()

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

    def set_meta(self, name: str, **meta) -> None:
        """Change what clients are told about a property ("power") or a command's parameter
        ("set_frequency.mhz"): unit, min, max, step, choices (META_KEYS).  For example from
        open(), once the instrument has said which model it is.  Merged into what the class
        declares; a value of None removes a key.  Clients see it at their next describe().
        Raises UnknownCommand for a name the device does not have."""
        self._rt.set_meta(name, meta)

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
        """(commands, properties) exposed by this device, as its class marks them."""
        cls = type(self)
        commands, props = {}, {}
        for name in dir(cls):
            if name.startswith("_"):
                continue
            attr = inspect.getattr_static(cls, name)
            if isinstance(attr, prop):
                props[name] = _property(name, attr, _Getter(self, name), _Setter(self, name))
                continue
            opts = getattr(attr, "_insthub_command", None)
            if opts is not None:
                bound = getattr(self, name)
                commands[name] = _command(name, bound, bound, opts=opts)
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


class _Poll:
    """What a poll task runs: read the property `name` (or call the command), publish the value."""

    __slots__ = ("name", "rt", "seconds")

    def __init__(self, rt: _Runtime, name: str, seconds: float):
        self.rt, self.name = rt, name
        self.seconds = seconds  #: the interval `poll` declares (the task's may be changed at run time)

    def __call__(self) -> None:
        rt, name = self.rt, self.name
        p = rt.props.get(name)
        rt.publish(name, p.get() if p is not None else rt.commands[name].fn(), None, {})


# ---------------------------------------------------------------- wrap
class Wrapped(Device):
    """Hosts an ordinary driver object.  Made by `wrap()`.

    Subclass it to add bus features to an existing driver: `@command`, `@prop`
    and `@periodic` methods of the subclass are exposed next to the driver's
    own methods, and reach the driver through `self.target`.  A driver that
    says `simulated = True` makes the device say so too."""

    def __init__(self, target, name=None, *, args=(), kwargs=None, include=None, exclude=(),
                 attrs=(), close="close", accessors=False):
        if isinstance(target, type):
            self._cls, self._obj, self._owned = target, None, True
        else:
            self._cls, self._obj, self._owned = type(target), target, False
        self._args, self._kwargs = tuple(args), dict(kwargs or {})
        self._include = None if include is None else set(include)
        self._exclude = set(exclude) | {close}
        self._attrs = tuple(attrs)
        self._close_name = close
        self._accessors = bool(accessors)
        if _declared(target, "simulated", bool):
            self.simulated = True
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
                props[name] = _property(name, attr, _TargetGet(self, name), _TargetSet(self, name))
            elif isinstance(attr, staticmethod):
                commands[name] = _command(name, attr.__func__, _TargetCall(self, name))
            elif isinstance(attr, classmethod):
                commands[name] = _command(name, attr.__func__, _TargetCall(self, name), drop_first=True)
            elif callable(attr) and not isinstance(attr, type):
                commands[name] = _command(name, attr, _TargetCall(self, name), drop_first=True)
        for name in self._attrs:
            props[name] = _Prop(name, _TargetGet(self, name), _TargetSet(self, name))
        if self._accessors:
            props.update(self._accessor_props(commands, props))
        if self._include:
            missing = self._include - set(commands) - set(props)
            if missing:
                raise UnknownCommand(f"{cls.__name__} has no public method or property named {sorted(missing)}")
        return {**commands, **own_commands}, {**props, **own_props}

    def _accessor_props(self, commands: dict, props: dict) -> dict:
        """accessors=True: a property X for each get_X() that needs no argument and has a set_X(value)
        beside it, unless the driver has a public X already.  A getter alone stays a command: it may
        do more than read (a read-and-clear get_error()), and pages read every property.  The
        property and set_X write one setting, so marking either dangerous marks both (build)."""
        found = {}
        for getter, get in commands.items():
            x = getter[4:]
            put = commands.get(f"set_{x}")
            if (getter.startswith("get_") and x and not x.startswith("_") and put is not None
                    and not hasattr(self._cls, x) and x not in props and get.sig is not None
                    and put.sig is not None and not _required(get.sig) and len(_required(put.sig)) == 1):
                found[x] = _Prop(x, get.fn, put.fn, get.doc, _inferred(get.sig.return_annotation), via=f"set_{x}")
        return found


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
         attrs=(), close: str = "close", accessors: bool = False, link_errors: tuple | None = None,
         lazy: bool = False, extension: str | None = None, extension_version: str | None = None,
         simulated: bool | None = None, poll: dict | None = None, dangerous=None, meta: dict | None = None) -> Wrapped:
    """Host an existing driver on the bus without writing a Device subclass.

    target   a class: it is instantiated as `target(*args, **kwargs)` on the
             device thread when the session opens, and re-created after a link
             error (so reconnecting comes for free); or an instance you made
             yourself, which is used as is.
    Every public method becomes a command and every public property a property.
    include / exclude   restrict that to / remove some names.
    attrs    names of plain instance attributes to expose as properties.
    close    name of the method that closes the driver (default "close").
    accessors   also make a property X of each get_X() that needs no argument
             and its set_X(value); both stay commands too.
    link_errors   exception classes that mean the connection is broken.
    lazy     open on first use instead of at server start.
    extension, extension_version, simulated, poll, dangerous   what the device
             says it is, what it reads periodically and what is dangerous: see
             these attributes of Device.  dangerous=True: every command and
             every property that can be written; names add to what a Wrapped
             subclass marks.  simulated: by default, what the driver says (its
             `simulated`), if it says.
    meta     metadata, {"power": {"unit": "dBm"}, "set_level.dbm": {"min": -40}}:
             see Device.set_meta."""
    dev = Wrapped(target, name, args=args, kwargs=kwargs, include=include, exclude=exclude, attrs=attrs, close=close,
                  accessors=accessors)
    if link_errors is not None:
        dev.link_errors = tuple(link_errors)
    if lazy:
        dev.open_on_start = False
    for attr, value in (("extension", extension), ("extension_version", extension_version),
                        ("simulated", simulated), ("poll", poll), ("dangerous", dangerous)):
        if value is not None:
            setattr(dev, attr, value)
    for entry, values in (meta or {}).items():  # last: it makes the tables, from all of the above
        dev.set_meta(entry, **values)
    return dev
