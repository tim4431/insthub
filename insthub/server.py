"""Server: hosts devices and serves them to clients.

Clients in the same process reach the devices directly (no socket, no
serialization); others connect over a Unix socket (default, same machine) or
TCP (opt-in, other machines).

Threads: one per device (see device.py), one per listener, one reader per
connection, and one writer per connection that is started only when a send
would block.  Replies and stream frames are normally written straight from the
thread that produced them with a non-blocking send; whatever does not fit in
the socket buffer is handed to the writer.  A client that reads slowly
therefore never stalls a device, and stream frames it has not been sent yet are
replaced by newer ones (per subscription, unless it asked for a queue).
"""
from __future__ import annotations

import atexit
import collections
import logging
import os
import signal
import socket
import threading
import time
import weakref

from . import codec, registry, wire
from .device import Device, _Cmd, wrap
from .errors import BusError, DeviceNotFound, UnknownCommand, describe_exception

_log = logging.getLogger("insthub")

#: Local clients use a Unix socket where the platform has them.  Elsewhere
#: (Windows) the server listens on a loopback TCP port instead; clients find it
#: through the registry file in both cases.
_UNIX_SOCKETS = hasattr(socket, "AF_UNIX") and os.name != "nt"

_local_lock = threading.Lock()
_local_servers: dict[str, Server] = {}
_started: weakref.WeakSet = weakref.WeakSet()


def local_servers() -> list[Server]:
    """Servers running in this process."""
    with _local_lock:
        return list(_local_servers.values())


@atexit.register
def _stop_all() -> None:
    for server in list(_started):
        server.stop()


# ---------------------------------------------------------------- sending
class _Slot:
    """Frames of one subscription waiting to be written (at most `maxlen`)."""

    __slots__ = ("pending", "maxlen", "queued", "dropped")

    def __init__(self, maxlen: int):
        self.pending: collections.deque = collections.deque()
        self.maxlen = maxlen
        self.queued = False
        self.dropped = 0


class _Sender:
    """Outgoing side of one connection.  Thread-safe; `post*` never block."""

    def __init__(self, sock: socket.socket, on_error):
        self._sock = sock
        self._on_error = on_error
        self._cond = threading.Condition(threading.Lock())
        self._fifo: collections.deque = collections.deque()  # must-deliver, in order: replies, unfinished frames
        self._ready: collections.deque = collections.deque()  # slots with pending stream frames
        self._busy = False  # the writer thread is inside a blocking send
        self._closed = False
        self._thread: threading.Thread | None = None

    def post(self, segs: list) -> None:
        """Send a message that must arrive (a reply)."""
        failed = False
        with self._cond:
            if self._closed:
                return
            if wire.CAN_TRY_SEND and not (self._busy or self._fifo or self._ready):
                try:
                    segs = wire.try_send(self._sock, segs)
                except OSError:
                    self._closed = failed = True
                else:
                    if not segs:
                        return
            if not failed:
                self._fifo.append(segs)
                self._kick()
        if failed:
            self._on_error()

    def post_stream(self, slot: _Slot, segs: list) -> None:
        """Send a stream frame; if the connection is backed up, keep only the
        newest `slot.maxlen` frames of this subscription."""
        failed = False
        with self._cond:
            if self._closed:
                return
            if wire.CAN_TRY_SEND and not (self._busy or self._fifo or self._ready):
                try:
                    rest = wire.try_send(self._sock, segs)
                except OSError:
                    self._closed = failed = True
                else:
                    if not rest:
                        return
                    if rest is not segs:  # partly on the wire: it has to be finished, ahead of all else
                        self._fifo.append(rest)
                        self._kick()
                        return
            if not failed:
                pending = slot.pending
                if len(pending) >= slot.maxlen:
                    pending.popleft()
                    slot.dropped += 1
                pending.append(segs)
                if not slot.queued:
                    slot.queued = True
                    self._ready.append(slot)
                self._kick()
        if failed:
            self._on_error()

    def discard(self, slot: _Slot) -> None:
        with self._cond:
            slot.pending.clear()

    def _kick(self) -> None:  # lock held
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="insthub:writer", daemon=True)
            self._thread.start()
        else:
            self._cond.notify()

    def _run(self) -> None:
        cond = self._cond
        while True:
            with cond:
                self._busy = False
                segs = None
                while segs is None:
                    if self._closed:
                        return
                    if self._fifo:
                        segs = self._fifo.popleft()
                    elif self._ready:
                        slot = self._ready.popleft()
                        if slot.pending:
                            segs = slot.pending.popleft()
                        if slot.pending:
                            self._ready.append(slot)
                        else:
                            slot.queued = False
                    else:
                        cond.wait()
                self._busy = True
            try:
                wire.send_all(self._sock, segs)
            except OSError:
                with cond:
                    self._closed = True
                self._on_error()
                return

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._fifo.clear()
            self._ready.clear()
            self._cond.notify_all()


class _RemoteSink:
    """Stream consumer on the other end of a connection."""

    local = False
    __slots__ = ("sender", "sub_id", "slot")

    def __init__(self, sender: _Sender, sub_id: int, queue: int):
        self.sender = sender
        self.sub_id = sub_id
        self.slot = _Slot(max(1, int(queue)))

    def post(self, encoded) -> None:
        body, buffers = encoded
        self.sender.post_stream(self.slot, wire.frame(wire.DATA, self.sub_id, body, buffers))


class _RemoteReply:
    __slots__ = ("conn", "tag")

    def __init__(self, conn, tag):
        self.conn, self.tag = conn, tag

    def __call__(self, ok, value) -> None:
        if ok:
            try:
                body, buffers = codec.encode(codec.lift(value))
            except Exception as e:  # noqa: BLE001 - e.g. the command returned something unsendable
                value = e
            else:
                self.conn.sender.post(wire.frame(wire.REPLY, self.tag, body, buffers))
                return
        body, _ = codec.encode(describe_exception(value))
        self.conn.sender.post(wire.frame(wire.ERROR, self.tag, body))


def _discard(ok, value) -> None:
    """Reply sink of a notify: nobody is waiting, so failures are only logged."""
    if not ok:
        _log.warning("notify failed: %s: %s", type(value).__name__, value)


# ---------------------------------------------------------------- connection
class _Conn:
    def __init__(self, server: Server, sock: socket.socket, peer):
        self.server, self.sock, self.peer = server, sock, peer
        self.sender = _Sender(sock, self._abort)
        self.subs: dict[int, tuple] = {}  # subscription id -> (runtime, stream name, sink)
        self.closed = False
        self._lock = threading.Lock()

    def run(self) -> None:
        server, sock = self.server, self.sock
        try:
            sock.settimeout(10.0)
            hello, _ = codec.encode(server._hello())
            sock.sendall(wire.MAGIC + wire.frame(wire.HELLO, 0, hello)[0])
            reader = wire.FrameReader(sock)
            reader.read_magic()
            sock.settimeout(None)
            dispatch, decode = server._dispatch, codec.decode
            while True:
                mtype, tag, body, buffers = reader.read()
                if mtype != wire.CALL:
                    raise ConnectionError(f"unexpected message type {mtype}")
                reply = _RemoteReply(self, tag) if tag else _discard
                try:
                    device, method, args, kwargs = decode(body, buffers)
                except Exception as e:  # noqa: BLE001 - the framing is intact, only this call is bad
                    reply(False, e)
                    continue
                dispatch(device, method, args, kwargs, reply, self)
        except (EOFError, OSError):
            pass
        except Exception:  # noqa: BLE001
            _log.exception("connection from %s failed", self.peer)
        finally:
            self.close()
            sock.close()

    def _abort(self) -> None:
        """A send failed.  The caller may hold a stream lock, so do not clean up
        here: wake the reader thread, which then runs `close()`."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
        for rt, stream, sink in list(self.subs.values()):
            rt.remove_sink(stream, sink)
        self.subs.clear()
        self.sender.close()
        self._abort()  # the reader thread wakes up and closes the socket
        self.server._conns.discard(self)


# ---------------------------------------------------------------- server
class Server:
    """Hosts devices.

        server = Server("rf", [Synth("synth"), wrap(SomeDriver, "sa")], tcp=8765)
        server.start()            # returns at once; or server.serve_forever()
        ...
        server.stop()

    name   how clients on this machine find the server (default: the first device's name).
    tcp    None: Unix socket only.  A port number: also listen on 127.0.0.1:port.
           "0.0.0.0:8765": reachable from other machines (no authentication!).
    ipc    listen on the Unix socket `<runtime dir>/<name>.sock` (default True).
           With ipc=False and no tcp, the server is reachable from this process only.
    """

    def __init__(self, name: str | None = None, devices=(), *, tcp: int | str | None = None, ipc: bool = True):
        self.name = registry.check_name(name, "server name") if name else None
        self._tcp, self._ipc = tcp, ipc
        self._devices: dict[str, Device] = {}
        self._listeners: list[socket.socket] = []
        self._conns: set[_Conn] = set()
        self._lock = threading.RLock()
        self._running = False
        self._owns_registry = False
        self._done = threading.Event()
        self.addresses: list[str] = []
        self.started_at: float | None = None
        for dev in devices:
            self.add(dev)

    # -- devices -------------------------------------------------------------
    def add(self, device, name: str | None = None) -> Device:
        """Host a device (also while running).  Anything that is not a `Device`
        is wrapped with `wrap()`."""
        if not isinstance(device, Device):
            device = wrap(device, name)
        if name:
            device.name = name
        if not device.name:
            device.name = type(device).__name__.lower()
        registry.check_name(device.name, "device name")
        with self._lock:
            if device.name in self._devices:
                raise ValueError(f"server {self.name!r} already has a device named {device.name!r}")
            _ = device._rt  # build the command table now: mistakes surface here, not in a thread
            self._devices[device.name] = device
            if self._running:
                device._rt.start()
                self._register()
        return device

    @property
    def devices(self) -> dict[str, Device]:
        return dict(self._devices)

    def device(self, name: str, timeout: float | None = None):
        """An in-process proxy for one of this server's devices (what a GUI
        living in the server process uses)."""
        from .client import connect

        return connect(f"inproc://{self.name}", timeout=timeout).device(name)

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> Server:
        with self._lock:
            if self._running:
                return self
            if not self.name:
                self.name = next(iter(self._devices), None) or f"insthub-{os.getpid()}"
            for other in registry.servers():
                if other.get("server") == self.name and other.get("pid") != os.getpid():
                    raise OSError(f"a server named {self.name!r} is already running (pid {other.get('pid')})")
            with _local_lock:
                if self.name in _local_servers:
                    raise ValueError(f"a server named {self.name!r} is already running in this process")
                _local_servers[self.name] = self
            try:
                if self._ipc and _UNIX_SOCKETS:
                    self._listen_ipc()
                if self._tcp is not None or (self._ipc and not self.addresses):  # no Unix sockets here
                    self._listen_tcp(self._tcp or 0)
            except BaseException:
                self._close_listeners()
                if self._owns_registry:
                    registry.remove(self.name)
                with _local_lock:
                    _local_servers.pop(self.name, None)
                raise
            self._running = True
            self._done.clear()
            self.started_at = time.time()
            for dev in self._devices.values():
                dev._rt.start()
            self._register()
            _started.add(self)
        _log.info("server %r up: %s; devices: %s", self.name, ", ".join(self.addresses),
                  ", ".join(self._devices) or "(none)")
        return self

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            registry.remove(self.name)
            self._owns_registry = False
            self._close_listeners()
            for dev in self._devices.values():
                dev._rt.stop()
            for conn in list(self._conns):
                conn.close()
            with _local_lock:
                _local_servers.pop(self.name, None)
            self.addresses = []
            self._done.set()
        _log.info("server %r stopped", self.name)

    def serve_forever(self) -> None:
        """Start, then block until Ctrl-C / SIGTERM / `stop()`."""
        self.start()
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGTERM, lambda *_: self._done.set())
            except (ValueError, OSError):
                pass
        try:
            while not self._done.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def __enter__(self) -> Server:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- listening -------------------------------------------------------------
    def _listen_ipc(self) -> None:
        path = registry.socket_path(self.name)
        if path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1.0)
            try:
                probe.connect(str(path))
            except OSError:
                path.unlink(missing_ok=True)  # left behind by a crashed server
            else:
                raise OSError(f"a server named {self.name!r} is already running ({path})")
            finally:
                probe.close()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
        except OSError as e:
            sock.close()
            if "too long" not in str(e):
                raise
            # Unix socket paths are limited to ~107 bytes.  Clients find the server
            # through the registry file either way, so a loopback TCP port does the job.
            _log.warning("socket path %s is too long for a Unix socket; using a TCP port on 127.0.0.1 instead "
                         "(set INSTHUB_DIR to a shorter directory to avoid this)", path)
            return
        self._owns_registry = True
        self._listen(sock, f"ipc://{path}")

    def _listen_tcp(self, spec) -> None:
        if isinstance(spec, int) or str(spec).isdigit():
            host, port = "127.0.0.1", int(spec)
        else:
            host, _, port = str(spec).rpartition(":")
            port = int(port)
        sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "posix":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # restart on the same port at once
        elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # On Windows SO_REUSEADDR would let a second server bind the same port unnoticed.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((host, port))
        except BaseException:
            sock.close()
            raise
        shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
        self._listen(sock, f"tcp://{shown}:{sock.getsockname()[1]}")

    def _listen(self, sock: socket.socket, address: str) -> None:
        sock.listen(64)
        sock.settimeout(1.0)
        self._listeners.append(sock)
        self.addresses.append(address)
        threading.Thread(target=self._accept, args=(sock,), name=f"insthub:accept:{address}", daemon=True).start()

    def _accept(self, lsock: socket.socket) -> None:
        while lsock in self._listeners:
            try:
                sock, peer = lsock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            sock.settimeout(None)
            wire.tune(sock)
            conn = _Conn(self, sock, peer)
            self._conns.add(conn)
            threading.Thread(target=conn.run, name="insthub:conn", daemon=True).start()

    def _close_listeners(self) -> None:
        listeners, self._listeners = self._listeners, []
        for sock in listeners:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _hello(self) -> dict:
        return {"server": self.name, "pid": os.getpid(), "protocol": wire.PROTOCOL,
                "devices": sorted(self._devices), "addresses": list(self.addresses), "started": self.started_at}

    def _register(self) -> None:
        if not self.addresses:  # in-process only: nothing for another process to find
            return
        try:
            registry.write(self.name, self._hello())
        except OSError as e:
            _log.warning("could not write the registry entry: %s", e)

    # -- calls -------------------------------------------------------------------
    def _runtime(self, device: str):
        dev = self._devices.get(device)
        if dev is None:
            raise DeviceNotFound(f"no device {device!r} on server {self.name!r} (it has: "
                                 f"{', '.join(sorted(self._devices)) or 'none'})")
        return dev._rt

    def _dispatch(self, device, method, args, kwargs, reply, conn) -> None:
        """Route one call.  `reply(ok, value)` is called exactly once, on
        whichever thread finishes the call.  `conn` is None for in-process callers."""
        try:
            if not device:
                reply(True, self._server_call(method, args, conn))
                return
            rt = self._runtime(device)
            if method[:1] != "$":
                cmd = rt.commands.get(method)
                if cmd is None:
                    raise UnknownCommand(f"{device} has no command {method!r} (see its describe())")
                rt.submit(cmd, args, kwargs, reply)
                return
            handler = _BUILTINS.get(method)
            if handler is None:
                raise UnknownCommand(f"unknown bus command {method!r}")
            handler(rt, args, reply, conn)
        except Exception as e:  # noqa: BLE001
            reply(False, e)

    def _server_call(self, method, args, conn):
        if method == "ping":
            return time.time()
        if method == "unsubscribe":
            old = conn.subs.pop(args[0], None) if conn is not None else None
            if old is not None:
                old[0].remove_sink(old[1], old[2])
                conn.sender.discard(old[2].slot)
            return None
        if method == "info":
            return self._hello()
        if method == "devices":
            return {name: dev._rt.describe() for name, dev in list(self._devices.items())}
        raise UnknownCommand(f"unknown server command {method!r}")

    def __repr__(self) -> str:
        state = ", ".join(self.addresses) if self._running else "stopped"
        return f"<insthub.Server {self.name!r} [{state}] devices={list(self._devices)}>"


# -- bus-level commands every device answers ("$name"; cannot clash with Python names)
def _b_describe(rt, args, reply, conn):
    reply(True, rt.describe())


def _b_status(rt, args, reply, conn):
    reply(True, rt.status())


def _b_state(rt, args, reply, conn):
    reply(True, rt.state)


def _b_latest(rt, args, reply, conn):
    st = rt.streams.get(args[0])
    last = st.last if st else None
    reply(True, None if last is None else [last.seq, last.time, last.data, last.meta])


def _b_get(rt, args, reply, conn):
    p = rt.props.get(args[0])
    if p is None:
        raise UnknownCommand(f"{rt.dev.name} has no property {args[0]!r}")
    rt.submit(_Cmd(args[0], p.get), (), {}, reply)


def _b_set(rt, args, reply, conn):
    p = rt.props.get(args[0])
    if p is None:
        raise UnknownCommand(f"{rt.dev.name} has no property {args[0]!r}")
    if p.set is None:
        raise AttributeError(f"property {args[0]!r} of {rt.dev.name} is read-only")
    rt.submit(_Cmd(args[0], p.set), (args[1],), {}, reply)


def _b_open(rt, args, reply, conn):
    def reopen():
        rt.ensure_open(force=True)
        return rt.status()

    rt.submit(_Cmd("$open", reopen, needs_open=False), (), {}, reply)


def _b_release(rt, args, reply, conn):
    def release():
        rt.release()
        return rt.status()

    rt.submit(_Cmd("$release", release, needs_open=False), (), {}, reply)


def _b_task(rt, args, reply, conn):
    name, enabled, interval = args
    task = rt.tasks.get(name)
    if task is None:
        raise UnknownCommand(f"{rt.dev.name} has no periodic task {name!r} (it has: {sorted(rt.tasks)})")
    if interval is not None:
        task.interval = float(interval)
    if enabled is True:
        task.start()
    elif enabled is False:
        task.stop()
    reply(True, {"interval": task.interval, "enabled": task.enabled})


def _b_subscribe(rt, args, reply, conn):
    if conn is None:
        raise BusError("$subscribe is for socket connections")
    stream, sub_id, queue, retained = args
    old = conn.subs.pop(sub_id, None)
    if old is not None:
        old[0].remove_sink(old[1], old[2])
    sink = _RemoteSink(conn.sender, sub_id, queue)
    conn.subs[sub_id] = (rt, stream, sink)
    rt.add_sink(stream, sink, retained)  # attach first: once the client has the reply, no frame can be missed
    if conn.closed:  # lost the race with a disconnect
        rt.remove_sink(stream, sink)
    reply(True, None)


_BUILTINS = {
    "$describe": _b_describe, "$status": _b_status, "$state": _b_state, "$latest": _b_latest,
    "$get": _b_get, "$set": _b_set, "$open": _b_open, "$release": _b_release, "$task": _b_task,
    "$subscribe": _b_subscribe,
}


def serve(*devices, name: str | None = None, tcp: int | str | None = None, ipc: bool = True) -> None:
    """Host the given devices until Ctrl-C.  Shorthand for `Server(...).serve_forever()`."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Server(name, devices, tcp=tcp, ipc=ipc).serve_forever()
