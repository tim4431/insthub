"""Client side: connections, device proxies, futures and stream subscriptions.

    scope = insthub.device("scope")            # found by name
    scope.set_timebase(1e-3)                  # blocking call
    fut = scope.get_waveform.future(1)        # returns at once; fut.result() / await fut
    scope.live.notify(True)                   # fire and forget
    scope.center = 1e9                        # remote property
    sub = scope.subscribe("ch1")              # stream; sub.get(), sub.poll(), for frame in sub

The same code runs against a device in this process (no socket, objects passed
by reference), on this machine (Unix socket) or on another one (TCP).
"""
from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
import weakref
from concurrent.futures import Future, InvalidStateError

from . import codec, registry, wire
from . import server as _server
from .device import Frame
from .errors import (
    BusError,
    ConnectionLost,
    DeviceNotFound,
    SubscriptionClosed,
    UnknownCommand,
    build_exception,
)

_log = logging.getLogger("insthub")


# ---------------------------------------------------------------- futures
class CallFuture(Future):
    """The pending result of a call: a `concurrent.futures.Future` that can
    also be awaited from asyncio."""

    where = ""

    def result(self, timeout: float | None = None):
        try:
            return super().result(timeout)
        except TimeoutError:
            if self.done():  # the command itself raised a TimeoutError
                raise
            raise TimeoutError(f"{self.where}: no reply within {timeout:g} s (the command may still be "
                               "running on the device)") from None

    def __await__(self):
        return asyncio.wrap_future(self).__await__()


def _resolve(fut: Future, ok: bool, value) -> None:
    try:
        if ok:
            fut.set_result(value)
        else:
            fut.set_exception(value)
    except InvalidStateError:  # the caller cancelled it
        pass


class _FutureReply:
    __slots__ = ("fut",)

    def __init__(self, fut):
        self.fut = fut

    def __call__(self, ok, value) -> None:
        _resolve(self.fut, ok, value)


def gather(*futures: Future, timeout: float | None = None) -> list:
    """Wait for several calls; returns their results in order.  The first
    exception is raised.  `timeout` bounds the total wait."""
    end = None if timeout is None else time.monotonic() + timeout
    return [f.result(None if end is None else max(0.0, end - time.monotonic())) for f in futures]


# ---------------------------------------------------------------- subscriptions
class Subscription:
    """A stream subscription.

    By default it holds only the newest frame: a reader that is slower than the
    publisher skips frames instead of falling behind (`dropped` counts them).
    With `queue=N` it keeps the newest N in order."""

    def __init__(self, transport, device: str, stream: str, queue: int, retained: bool, callback):
        self.device, self.stream = device, stream
        self._transport = transport
        self._queue, self._retained = int(queue), bool(retained)
        self._frames: collections.deque = collections.deque(maxlen=max(1, self._queue))
        self._cond = threading.Condition(threading.Lock())
        self._wakers: list = []
        self._last_seq = 0
        self._id = 0
        self._sink = None
        self.latest: Frame | None = None  #: the most recent frame received, read or not
        self.received = 0  #: frames that arrived here
        self.dropped = 0  #: frames published but never handed to you
        self.closed = False
        self._callback = callback
        transport.subscribe(self)
        if callback is not None:
            threading.Thread(target=self._pump, name=f"insthub:sub:{device}.{stream}", daemon=True).start()

    def _deliver(self, frame: Frame) -> None:  # reader thread, or the publisher's thread in-process
        with self._cond:
            if self.closed:
                return
            if self._last_seq and frame.seq > self._last_seq + 1:
                self.dropped += frame.seq - self._last_seq - 1  # skipped on the server (slow link)
            self._last_seq = frame.seq
            frames = self._frames
            if len(frames) == frames.maxlen:
                self.dropped += 1  # overwritten here before it was read
            frames.append(frame)
            self.latest = frame
            self.received += 1
            self._cond.notify()
            wakers = self._wakers
            if wakers:
                self._wakers = []
        for wake in wakers:
            wake()

    def get(self, timeout: float | None = None) -> Frame:
        """The next unread frame, waiting for one if necessary."""
        with self._cond:
            frames = self._frames
            if not frames:
                end = None if timeout is None else time.monotonic() + timeout
                while not frames:
                    if self.closed:
                        raise SubscriptionClosed(f"subscription to {self.device}.{self.stream} is closed")
                    left = None if end is None else end - time.monotonic()
                    if left is not None and left <= 0:
                        raise TimeoutError(f"no frame on {self.device}.{self.stream} within {timeout:g} s")
                    self._cond.wait(left)
            return frames.popleft()

    def poll(self) -> Frame | None:
        """The next unread frame, or None.  Never waits: what a GUI timer calls."""
        with self._cond:
            return self._frames.popleft() if self._frames else None

    async def aget(self) -> Frame:
        """`get()` for asyncio."""
        loop = asyncio.get_running_loop()
        while True:
            with self._cond:
                if self._frames:
                    return self._frames.popleft()
                if self.closed:
                    raise SubscriptionClosed(f"subscription to {self.device}.{self.stream} is closed")
                waiter = loop.create_future()
                self._wakers.append(lambda w=waiter: loop.call_soon_threadsafe(_wake, w))
            await waiter

    def close(self) -> None:
        with self._cond:
            if self.closed:
                return
            self.closed = True
            self._cond.notify_all()
            wakers, self._wakers = self._wakers, []
        for wake in wakers:
            try:
                wake()
            except RuntimeError:  # its event loop is gone
                pass
        self._transport.unsubscribe(self)

    def _pump(self) -> None:
        while True:
            try:
                frame = self.get()
            except SubscriptionClosed:
                return
            try:
                self._callback(frame)
            except Exception:
                _log.exception("callback of %s.%s failed", self.device, self.stream)

    def __iter__(self):
        return self

    def __next__(self) -> Frame:
        try:
            return self.get()
        except SubscriptionClosed:
            raise StopIteration from None

    def __aiter__(self):
        return self

    async def __anext__(self) -> Frame:
        try:
            return await self.aget()
        except SubscriptionClosed:
            raise StopAsyncIteration from None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self.closed else f"received={self.received} dropped={self.dropped}"
        return f"<Subscription {self.device}.{self.stream} {state}>"


def _wake(waiter) -> None:
    if not waiter.done():
        waiter.set_result(None)


class _LocalSubSink:
    """In-process sink.  Holds the Subscription weakly: one that is dropped
    without `close()` detaches itself at the next publish."""

    local = True
    __slots__ = ("_ref",)

    def __init__(self, sub: Subscription):
        self._ref = weakref.ref(sub)

    def deliver(self, frame: Frame) -> bool:
        sub = self._ref()
        if sub is None:
            return True  # tells the publisher to remove this sink
        sub._deliver(frame)
        return False


# ---------------------------------------------------------------- transports
class _LocalTransport:
    """Calls into a server living in this process: no socket, no copies."""

    def __init__(self, server: _server.Server):
        self._server = server
        self.address = f"inproc://{server.name}"

    @property
    def closed(self) -> bool:
        return not self._server._running

    def info(self) -> dict:
        return self._server._hello()

    def request(self, device, method, args, kwargs) -> CallFuture:
        fut = CallFuture()
        fut.where = f"{device}.{method}"
        self._server._dispatch(device, method, args, kwargs, _FutureReply(fut), None)
        return fut

    def notify(self, device, method, args, kwargs) -> None:
        self._server._dispatch(device, method, args, kwargs, _server._discard, None)

    def subscribe(self, sub: Subscription) -> None:
        rt = self._server._runtime(sub.device)
        sub._sink = (rt, _LocalSubSink(sub))
        rt.add_sink(sub.stream, sub._sink[1], sub._retained)

    def unsubscribe(self, sub: Subscription) -> None:
        if sub._sink is not None:
            rt, sink = sub._sink
            rt.remove_sink(sub.stream, sink)

    def close(self) -> None:
        pass


class _SocketTransport:
    """One socket to a server.  Any thread may call; one reader thread routes
    replies to futures and stream frames to subscriptions.  After the server
    goes away the next call reconnects, and live subscriptions are restored in
    the background."""

    def __init__(self, address: str, connect_timeout: float = 5.0):
        self.address = address
        self._lock = threading.Lock()  # socket writes, ids, the pending table
        self._connect_lock = threading.Lock()
        self._sock = None
        self._pending: dict[int, CallFuture] = {}
        self._next_id = 1
        self._subs: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        self._gone: set[int] = set()
        self._reconnecting = False
        self.closed = False
        self.hello: dict = {}
        self._connect(connect_timeout)

    def info(self) -> dict:
        return self.request("", "info", (), {}).result(10.0)

    # -- connection ----------------------------------------------------------
    def _connect(self, timeout: float) -> None:
        try:
            sock = wire.open_socket(self.address, timeout)
        except OSError as e:
            raise ConnectionLost(f"cannot connect to {self.address}: {e}") from None
        try:
            sock.sendall(wire.MAGIC)
            reader = wire.FrameReader(sock)
            reader.read_magic()
            mtype, _, body, buffers = reader.read()
            if mtype != wire.HELLO:
                raise ConnectionError("server did not say hello")
            self.hello = codec.decode(body, buffers)
            sock.settimeout(None)
        except (OSError, EOFError) as e:
            sock.close()
            raise ConnectionLost(f"handshake with {self.address} failed: {e}") from None
        pending: dict[int, CallFuture] = {}
        with self._lock:
            self._sock, self._pending = sock, pending
        threading.Thread(target=self._read, args=(sock, reader, pending),
                         name=f"insthub:client:{self.address}", daemon=True).start()

    def _reconnect_now(self) -> None:
        with self._connect_lock:
            if self._sock is not None or self.closed:
                return
            self._connect(3.0)
            subs = [sub for sub in list(self._subs.values()) if not sub.closed]
        for sub in subs:  # restore the subscriptions of the previous connection
            try:
                self._request_subscribe(sub, retry=False).add_done_callback(_log_failure)
            except ConnectionLost:  # gone again already: the next reconnect restores them
                break

    def _reconnect_later(self) -> None:
        delay = 0.2
        while True:
            while self._sock is None and not self.closed and len(self._subs):
                try:
                    self._reconnect_now()
                except ConnectionLost:
                    time.sleep(delay)
                    delay = min(delay * 1.5, 2.0)
            with self._lock:  # _lost() decides under the same lock whether to start another of us
                if self._sock is not None or self.closed or not len(self._subs):
                    self._reconnecting = False
                    return

    def _read(self, sock, reader, pending) -> None:
        subs, decode = self._subs, codec.decode
        reason: object = "closed"
        try:
            while True:
                mtype, tag, body, buffers = reader.read()
                if mtype == wire.REPLY:
                    fut = pending.pop(tag, None)
                    if fut is not None:
                        try:
                            value = decode(body, buffers)
                        except Exception as e:
                            _resolve(fut, False, e)
                        else:
                            _resolve(fut, True, value)
                elif mtype == wire.DATA:
                    sub = subs.get(tag)
                    if sub is None:
                        self._forget(tag)
                        continue
                    seq, stamp, data, meta = decode(body, buffers)
                    sub._deliver(Frame(sub.stream, seq, stamp, data, meta))
                elif mtype == wire.ERROR:
                    fut = pending.pop(tag, None)
                    if fut is not None:
                        names, message, tb = decode(body)
                        _resolve(fut, False, build_exception(names, message, tb, fut.where))
        except (EOFError, OSError) as e:
            reason = e
        except Exception as e:
            _log.exception("connection to %s failed", self.address)
            reason = e
        finally:
            self._lost(sock, pending, reason)

    def _lost(self, sock, pending, reason) -> None:
        with self._lock:
            if self._sock is sock:
                self._sock = None
            orphans = list(pending.values())
            pending.clear()
        try:
            sock.close()
        except OSError:
            pass
        for fut in orphans:
            _resolve(fut, False, ConnectionLost(f"{fut.where}: connection to {self.address} lost ({reason}); "
                                                "the command may or may not have run"))
        if not self.closed and len(self._subs):
            with self._lock:
                start, self._reconnecting = not self._reconnecting, True
            if start:
                threading.Thread(target=self._reconnect_later, name="insthub:reconnect", daemon=True).start()

    # -- sending -------------------------------------------------------------
    def _send(self, body, buffers, fut, retry: bool = True) -> None:
        """Write one CALL.  Reconnects once if the server has gone since the
        last call; a message is only ever re-sent if none of it was accepted."""
        for attempt in (0, 1) if retry else (1,):
            with self._lock:
                sock = self._sock
                if sock is not None:
                    if fut is None:
                        tag = 0
                    else:
                        tag = self._next_id
                        self._next_id = tag + 1
                        self._pending[tag] = fut
                    try:
                        wire.send_all(sock, wire.frame(wire.CALL, tag, body, buffers))
                        return
                    except OSError:
                        self._pending.pop(tag, None)
                        self._sock = None
            if sock is not None:
                _shutdown(sock)
            if self.closed:
                raise ConnectionLost(f"the connection to {self.address} was closed")
            if attempt == 0:
                self._reconnect_now()
        raise ConnectionLost(f"cannot send to {self.address}")

    def request(self, device, method, args, kwargs, retry: bool = True) -> CallFuture:
        body, buffers = codec.encode([device, method, codec.lift(list(args)) if args else [],
                                      codec.lift(kwargs) if kwargs else kwargs])
        fut = CallFuture()
        fut.where = f"{device}.{method}"
        self._send(body, buffers, fut, retry)
        return fut

    def notify(self, device, method, args, kwargs) -> None:
        body, buffers = codec.encode([device, method, codec.lift(list(args)) if args else [],
                                      codec.lift(kwargs) if kwargs else kwargs])
        self._send(body, buffers, None)

    # -- subscriptions ---------------------------------------------------------
    def _request_subscribe(self, sub: Subscription, retry: bool = True) -> CallFuture:
        return self.request(sub.device, "$subscribe", (sub.stream, sub._id, sub._queue, sub._retained), {}, retry)

    def subscribe(self, sub: Subscription) -> None:
        with self._lock:
            sub._id = self._next_id
            self._next_id += 1
        self._subs[sub._id] = sub
        try:
            self._request_subscribe(sub).result(10.0)
        except BaseException:
            self._subs.pop(sub._id, None)
            raise

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.pop(sub._id, None)
        self._forget(sub._id)

    def _forget(self, sub_id: int) -> None:
        """Tell the server to stop sending a subscription nobody holds any more."""
        if sub_id in self._gone:
            return
        if len(self._gone) > 4096:
            self._gone.clear()
        self._gone.add(sub_id)
        try:
            self.notify("", "unsubscribe", (sub_id,), {})
        except ConnectionLost:
            pass

    def close(self) -> None:
        self.closed = True
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            _shutdown(sock)
        for sub in list(self._subs.values()):
            sub.close()


def _shutdown(sock) -> None:
    try:
        sock.shutdown(2)  # SHUT_RDWR: the reader thread wakes up and cleans up
    except OSError:
        pass


def _log_failure(fut: Future) -> None:
    exc = fut.exception()
    if exc is not None:
        _log.warning("could not restore a subscription: %s", exc)


# ---------------------------------------------------------------- proxies
class RemoteMethod:
    """A command of a remote device.  Call it, or use `.future()` (don't wait),
    `.notify()` (don't even want the result) or `await .aio()`."""

    def __init__(self, transport, device, name, timeout, info):
        self._t, self._device, self._name, self._timeout = transport, device, name, timeout
        self.__name__ = name
        self.__doc__ = f"{name}{info.get('signature', '(...)')}\n\n{info.get('doc', '')}".rstrip()

    def __call__(self, *args, **kwargs):
        return self._t.request(self._device, self._name, args, kwargs).result(self._timeout)

    def future(self, *args, **kwargs) -> CallFuture:
        """Start the call and return at once.  `fut.result(timeout)` waits for it;
        calls to one device still run in the order they were made."""
        return self._t.request(self._device, self._name, args, kwargs)

    def notify(self, *args, **kwargs) -> None:
        """Send the call without asking for a reply (errors are logged by the server)."""
        self._t.notify(self._device, self._name, args, kwargs)

    async def aio(self, *args, **kwargs):
        """The call as a coroutine."""
        return await asyncio.wrap_future(self._t.request(self._device, self._name, args, kwargs))

    def __repr__(self) -> str:
        return f"<command {self._device}.{self.__doc__.splitlines()[0]}>"


class Bus:
    """`dev.bus`: everything about a remote device that is not one of its own
    commands.  Most of it is also reachable directly (`dev.subscribe(...)`)
    unless the device has a command of the same name."""

    #: names that also work on the proxy itself when the device does not define them
    SHORTCUTS = frozenset({"subscribe", "latest", "state", "describe", "status", "help", "with_timeout",
                           "release", "reopen", "call", "task"})

    def __init__(self, transport, name: str, timeout, info: dict):
        self._t, self.name, self.timeout, self._info = transport, name, timeout, info

    @property
    def address(self) -> str:
        return self._t.address

    def describe(self, refresh: bool = True) -> dict:
        """Commands, properties, streams, tasks and status of the device."""
        if refresh:
            self._info.update(self._t.request(self.name, "$describe", (), {}).result(self.timeout or 10.0))
        return self._info

    def status(self) -> dict:
        """Is the hardware connected, what is it doing, last error."""
        return self._t.request(self.name, "$status", (), {}).result(self.timeout or 10.0)

    def state(self) -> dict:
        """The device's current state dict (also the stream "state")."""
        return self._t.request(self.name, "$state", (), {}).result(self.timeout or 10.0)

    def subscribe(self, stream: str, callback=None, *, queue: int = 0, retained: bool = True) -> Subscription:
        """Subscribe to a stream.

        callback   called with every frame on a thread of its own; without it,
                   read frames with `get()`, `poll()` or by iterating.
        queue      0: keep only the newest frame (a slow reader skips frames).
                   N: keep the newest N in order (for recording).
        retained   start with the last frame published before subscribing."""
        return Subscription(self._t, self.name, stream, queue, retained, callback)

    def latest(self, stream: str) -> Frame | None:
        """The last frame published on a stream (None if there was none), without subscribing."""
        got = self._t.request(self.name, "$latest", (stream,), {}).result(self.timeout or 10.0)
        return None if got is None else Frame(stream, *got)

    def call(self, command: str, *args, **kwargs):
        """Call a command by name."""
        return self._t.request(self.name, command, args, kwargs).result(self.timeout)

    def future(self, command: str, *args, **kwargs) -> CallFuture:
        return self._t.request(self.name, command, args, kwargs)

    def get(self, prop: str):
        return self._t.request(self.name, "$get", (prop,), {}).result(self.timeout)

    def set(self, prop: str, value) -> None:
        self._t.request(self.name, "$set", (prop, value), {}).result(self.timeout)

    def task(self, name: str, enabled: bool | None = None, interval: float | None = None) -> dict:
        """Start / stop a periodic task of the device or change its interval."""
        return self._t.request(self.name, "$task", (name, enabled, interval), {}).result(self.timeout or 10.0)

    def release(self) -> dict:
        """Close the hardware session (e.g. to use the vendor's software).  The next command reopens it."""
        return self._t.request(self.name, "$release", (), {}).result(self.timeout)

    def reopen(self) -> dict:
        """Open the hardware session now, without waiting for the reconnect interval."""
        return self._t.request(self.name, "$open", (), {}).result(self.timeout)

    def with_timeout(self, timeout: float | None) -> DeviceProxy:
        """A proxy of the same device whose blocking calls give up after `timeout` seconds."""
        return DeviceProxy(self._t, self.name, timeout, self._info)

    def help(self) -> str:
        """Printable list of commands, properties and streams."""
        info = self.describe()
        lines = [f"{info['name']}: {info['type']} at {self.address}"]
        if info.get("doc"):
            lines.append(info["doc"])
        for title, key in (("commands", "commands"), ("properties", "properties")):
            if info[key]:
                lines.append(f"\n{title}:")
            for name, item in sorted(info[key].items()):
                first = (item.get("doc") or "").split("\n")[0]
                tail = "  (read-only)" if key == "properties" and not item["settable"] else ""
                lines.append(f"  {name}{item.get('signature', '')}{tail}" + (f"\n      {first}" if first else ""))
        if info["streams"]:
            lines.append("\nstreams:")
            for name, item in sorted(info["streams"].items()):
                what = item["kind"] or "nothing published yet"
                lines.append(f"  {name}: {what}" + (f"  - {item['doc']}" if item.get("doc") else ""))
        return "\n".join(lines)


class DeviceProxy:
    """A remote device that reads like the local driver object: its commands
    are methods, its properties are attributes.  Bus features (streams, status,
    ...) are under `.bus`."""

    def __init__(self, transport, name: str, timeout: float | None = None, info: dict | None = None):
        if info is None:
            info = transport.request(name, "$describe", (), {}).result(timeout or 10.0)
        d = self.__dict__
        d["_t"], d["_name"], d["_timeout"] = transport, name, timeout
        d["_commands"], d["_props"] = info["commands"], info["properties"]
        d["_refreshed"] = time.monotonic()
        d["bus"] = Bus(transport, name, timeout, info)

    def __getattr__(self, attr: str):
        d = self.__dict__
        commands = d.get("_commands")
        if commands is None or attr.startswith("__"):
            raise AttributeError(attr)
        for attempt in (0, 1):
            info = commands.get(attr)
            if info is not None:
                method = d[attr] = RemoteMethod(d["_t"], d["_name"], attr, d["_timeout"], info)
                return method
            if attr in d["_props"]:
                return d["_t"].request(d["_name"], "$get", (attr,), {}).result(d["_timeout"])
            if attr in Bus.SHORTCUTS:
                return getattr(d["bus"], attr)
            if attempt or time.monotonic() - d["_refreshed"] < 1.0:
                break
            # Unknown name: the server may have been restarted with new code since we asked.
            d["_refreshed"] = time.monotonic()
            try:
                fresh = d["_t"].request(d["_name"], "$describe", (), {}).result(d["_timeout"] or 10.0)
            except Exception:
                break
            commands.clear()
            commands.update(fresh["commands"])
            d["_props"].clear()
            d["_props"].update(fresh["properties"])
        raise AttributeError(f"device {d['_name']!r} has no command or property {attr!r} "
                             f"(print(dev.bus.help()) lists them)")

    def __setattr__(self, attr: str, value) -> None:
        props = self.__dict__["_props"]
        if attr not in props:
            raise AttributeError(f"device {self._name!r} has no property {attr!r} to set")
        if not props[attr]["settable"]:
            raise AttributeError(f"property {attr!r} of device {self._name!r} is read-only")
        self._t.request(self._name, "$set", (attr, value), {}).result(self._timeout)

    def __dir__(self):
        return sorted({"bus", *self._commands, *self._props, *Bus.SHORTCUTS})

    def __repr__(self) -> str:
        return (f"<insthub device {self._name!r} at {self._t.address}: {len(self._commands)} commands, "
                f"{len(self._props)} properties>")


class Client:
    """A connection to one server: `client.device("scope")`, `client["scope"]` or `client.scope`."""

    def __init__(self, transport, timeout: float | None = None):
        self._t = transport
        self.timeout = timeout
        self._proxies: dict[tuple, DeviceProxy] = {}

    @property
    def address(self) -> str:
        return self._t.address

    @property
    def closed(self) -> bool:
        return self._t.closed

    def info(self) -> dict:
        """Server name, pid, addresses and device names."""
        return self._t.info()

    def devices(self) -> list[str]:
        return list(self.info()["devices"])

    def describe(self) -> dict:
        """`describe()` of every device, by name."""
        return self._t.request("", "devices", (), {}).result(self.timeout or 10.0)

    def ping(self) -> float:
        """Round-trip time of an empty call, in seconds."""
        start = time.perf_counter()
        self._t.request("", "ping", (), {}).result(self.timeout or 10.0)
        return time.perf_counter() - start

    def device(self, name: str, timeout: float | None = None) -> DeviceProxy:
        """The proxy of a device.  `timeout` bounds its blocking calls (default: this client's)."""
        timeout = self.timeout if timeout is None else timeout
        proxy = self._proxies.get((name, timeout))
        if proxy is None:
            proxy = self._proxies[name, timeout] = DeviceProxy(self._t, name, timeout)
        return proxy

    def __getitem__(self, name: str) -> DeviceProxy:
        return self.device(name)

    def __getattr__(self, name: str) -> DeviceProxy:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self.device(name)
        except DeviceNotFound as e:
            raise AttributeError(str(e)) from None

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<insthub.Client {self.address}>"


# ---------------------------------------------------------------- entry points
def connect(address: str, *, timeout: float | None = None) -> Client:
    """Connect to a server by address: "tcp://host:port", "ipc:///path/to.sock"
    or "inproc://server-name".  `timeout` is the default for blocking calls
    (None: wait as long as the device takes)."""
    kind, target = wire.parse_address(address)
    if kind == "inproc":
        with _server._local_lock:
            srv = _server._local_servers.get(target)
        if srv is None:
            raise ConnectionLost(f"no server named {target!r} is running in this process")
        return Client(_LocalTransport(srv), timeout)
    return Client(_SocketTransport(address), timeout)


_pool: dict[str, Client] = {}
_pool_lock = threading.Lock()


def _pooled(address: str) -> Client:
    with _pool_lock:
        client = _pool.get(address)
        if client is None or client.closed:
            client = _pool[address] = connect(address)
        return client


def locate(name: str) -> str:
    """Address of the server hosting the device `name`: this process first, then
    the servers running on this machine, then the static table (registry.py)."""
    for srv in _server.local_servers():
        if name in srv._devices:
            return f"inproc://{srv.name}"
    entries = registry.find(name)
    if len(entries) > 1:
        raise DeviceNotFound(f"device name {name!r} is ambiguous: servers "
                             f"{[e['server'] for e in entries]} all have one; pass address=")
    if entries:
        addresses = entries[0]["addresses"]
        return next((a for a in addresses if a.startswith("ipc://")), addresses[0])
    static = registry.static_devices()
    if name in static:
        return static[name]
    known = sorted({d for s in registry.servers() for d in s.get("devices", ())} | set(static))
    raise DeviceNotFound(f"no device {name!r} on the bus (known devices: {', '.join(known) or 'none'}; "
                         "is its server running?)")


def device(name: str, address: str | None = None, *, timeout: float | None = None) -> DeviceProxy:
    """A proxy for the device `name`, wherever it is hosted.  Connections are
    shared: asking for ten devices of one server opens one socket."""
    return _pooled(address or locate(name)).device(name, timeout=timeout)


def discover() -> list[dict]:
    """The servers visible from here: in this process and on this machine."""
    found = [{**srv._hello(), "addresses": [f"inproc://{srv.name}", *srv.addresses]}
             for srv in _server.local_servers()]
    names = {f["server"] for f in found}
    found += [s for s in registry.servers() if s.get("server") not in names]
    return found


def close_all() -> None:
    """Close the shared connections opened by `device()`."""
    with _pool_lock:
        clients = list(_pool.values())
        _pool.clear()
    for client in clients:
        client.close()


__all__ = ["BusError", "UnknownCommand", "CallFuture", "Client", "DeviceProxy", "RemoteMethod", "Subscription",
           "close_all", "connect", "device", "discover", "gather", "locate"]
