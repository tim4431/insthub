"""What `insthub gui` knows about the servers and devices, and how it finds out.
There is no Qt here: the window calls these on its worker threads (everything
that may block) or with what they returned (everything else), and shows it.

    Backend          finds the servers (the registry folder, devices.toml, systemd) and
                     asks each how it is; one connection per server; every method may block
    Server           a server as the window knows it: where it is, its process and service,
                     what it said when last asked, how fast its streams go, previews
    server_state()   (status, word, sentence) of a server, and device_state() of a device:
                     the status is GOOD, BUSY, BAD or IDLE, and the word says the same
    shrink()         the last frame of a stream, made small enough for a tile
    program_name()   what to call a program connected to a server ('experiment.py',
                     'AI agent (Claude Code)')
    spawn(), server_file(), find_python()    starting servers, and finding their files
"""
from __future__ import annotations

import dataclasses
import math
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path

import numpy as np

from . import registry, service
from .client import Client, _SocketTransport, connect, discover
from .device import summarize
from .errors import BusError

PROBE_TIMEOUT = 3.0  #: seconds a server may take to answer before it is shown as unreachable
ACTION_TIMEOUT = 120.0
PREVIEW_POINTS = 160  #: a trace preview keeps the lowest and highest value of this many slices
PREVIEW_PIXELS = 160  #: the long side of an image preview
PREVIEW_LIMIT = 4_000_000  #: elements; a larger frame gets no preview (over TCP: an eighth of it)
GOOD, BUSY, BAD, IDLE = "good", "busy", "bad", "idle"  #: statuses (widgets.STATUS has their colours)


# ---------------------------------------------------------------- the blocking half (worker threads only)
class Backend:
    """Looks things up and talks to servers.  Every method may block; the
    window calls them on worker threads.  One connection per server address."""

    def __init__(self):
        self._clients: dict[str, Client] = {}
        self._lock = threading.Lock()

    def scan(self) -> dict:
        """Running servers, the hand-written table of remote devices, installed services."""
        try:
            remote, remote_error = registry.static_devices(), None
        except (OSError, ValueError) as e:
            remote, remote_error = {}, f"devices.toml: {e}"
        try:
            units = service.units()
        except BusError:
            units = None  # no systemd user services here
        return {"servers": discover(), "remote": remote, "remote_error": remote_error, "services": units}

    def client(self, address: str) -> Client:
        with self._lock:
            client = self._clients.get(address)
        if client is not None and not client.closed:
            return client
        if address.startswith("inproc://"):
            client = connect(address)
        else:
            client = Client(_SocketTransport(address, connect_timeout=2.0))
        with self._lock:
            other = self._clients.get(address)
            if other is not None and not other.closed:  # another worker was quicker
                client.close()
                return other
            self._clients[address] = client
        return client

    def probe(self, address: str) -> dict:
        """What one server says about itself, its devices and their state."""
        transport = self.client(address)._t
        start = time.perf_counter()
        info = transport.request("", "info", (), {}).result(PROBE_TIMEOUT)
        latency = time.perf_counter() - start
        devices = transport.request("", "devices", (), {}).result(PROBE_TIMEOUT)
        pending = {name: transport.request(name, "$state", (), {}) for name in devices}
        states = {}
        for name, future in pending.items():
            try:
                states[name] = future.result(PROBE_TIMEOUT)
            except Exception:  # noqa: BLE001 - e.g. the device went away in between
                states[name] = {}
        own = None
        sock = getattr(transport, "_sock", None)
        if sock is not None and sock.family != getattr(socket, "AF_UNIX", None):
            try:
                own = "%s:%s" % sock.getsockname()[:2]
            except OSError:
                pass
        clients = None if "clients" not in info else []  # None: a server older than this window
        for c in info.get("clients", ()):
            if c.get("pid") == os.getpid() or (own and c.get("peer") == own):
                continue  # this window's own connection
            program = program_name(c["pid"]) if c.get("pid") else (c.get("peer") or "?")
            clients.append({**c, "program": program, "agent": program.startswith(AGENT)})
        return {"info": info, "devices": devices, "states": states, "clients": clients, "latency": latency}

    def call(self, address: str, device: str, method: str, *args, kwargs: dict | None = None,
             timeout: float = ACTION_TIMEOUT):
        return self.client(address)._t.request(device, method, args, kwargs or {}).result(timeout)

    def latest(self, address: str, device: str, stream: str) -> tuple | None:
        """The last frame of a stream, made small: (seq, time, kind, data, unit), or None.
        `$latest` is answered by the server's connection thread: it neither waits for the
        device nor makes it acquire anything."""
        got = self.client(address)._t.request(device, "$latest", (stream,), {}).result(PROBE_TIMEOUT)
        if got is None:
            return None
        seq, stamp, data, meta = got
        return (seq, stamp, *shrink(data, meta if isinstance(meta, dict) else {}))

    def forget(self, address: str) -> None:
        with self._lock:
            client = self._clients.pop(address, None)
        if client is not None:
            client.close()

    def close(self) -> None:
        for address in list(self._clients):
            self.forget(address)


AGENT = "AI agent"  #: how a program connected through `insthub mcp` is named


def program_name(pid: int) -> str:
    """A short name for a process on this computer (Linux), else 'pid N'.  An `insthub mcp`
    is named after the agent that started it: 'AI agent (Claude Code)'."""
    argv = _cmdline(pid)
    if not argv:
        return f"pid {pid}"
    name = command_name(argv)
    if name.split()[:2] == ["insthub", "mcp"]:
        parent = _parent(pid)
        return f"{AGENT} ({agent_name(_cmdline(parent) if parent else [])})"
    return name


def agent_name(argv: list[str]) -> str:
    """What to call the program that started an `insthub mcp`, from its command line."""
    if not argv:
        return "unknown"
    return {"claude": "Claude Code", "claude-desktop": "Claude Desktop"}.get(os.path.basename(argv[0]),
                                                                            command_name(argv))


def _cmdline(pid: int) -> list[str]:
    try:
        return [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
    except OSError:
        return []


def _parent(pid: int) -> int | None:
    try:  # "pid (name) state ppid ...": the name may contain anything, so split after its last ")"
        return int(Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[1]) or None
    except (OSError, ValueError, IndexError):
        return None


def mcp_command() -> list[str]:
    """How an agent starts this installation's `insthub mcp`: by full path, as docs/agents.md advises."""
    script = Path(sys.executable).with_name("insthub.exe" if os.name == "nt" else "insthub")
    return [str(script), "mcp"] if script.is_file() else [sys.executable, "-m", "insthub", "mcp"]


def command_name(argv: list[str]) -> str:
    """'experiment.py', 'insthub view scope', 'Jupyter kernel': what a person would call a command line."""
    exe, args = os.path.basename(argv[0]), argv[1:]
    if exe.startswith("python"):
        while args and args[0].startswith("-") and args[0] not in ("-m", "-c"):
            args = args[1:]  # interpreter options
        if args[:1] == ["-m"] and len(args) > 1:
            if args[1] == "ipykernel_launcher":
                return "Jupyter kernel"
            args = [args[1].split(".")[0], *args[2:]]
        elif args[:1] == ["-c"]:
            args = ["python -c ..."]
        elif args:
            args = [os.path.basename(args[0]), *args[1:]]
        else:
            args = ["python"]
    else:
        args = [exe, *args]
    if args[0] == "insthub" and "-a" in args[:-1]:  # `insthub -a ADDRESS view scope` -> `insthub view scope`
        i = args.index("-a")
        args = args[:i] + args[i + 2:]
    text = " ".join(args)
    return text if len(text) <= 60 else text[:57] + "..."


def terminate(pid: int) -> None:
    """Ask a server on this computer to stop (it closes its instruments first)."""
    os.kill(pid, signal.SIGTERM)


def find_python(target: str | Path) -> str:
    """The interpreter a server file most likely needs: a virtualenv in its folder or the one above, else ours."""
    folder = Path(target).expanduser().resolve().parent
    inside = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    for base in (folder, folder.parent):
        for venv in (".venv", "venv", "env"):
            python = base.joinpath(venv, *inside)
            if python.is_file():
                return str(python)
    return sys.executable


def server_log(name: str) -> Path:
    """Where the output of a server this window started goes (the runtime folder, so it is gone at logout)."""
    return registry.runtime_dir() / f"{name}.log"


def spawn(command: list[str], name: str, cwd: str | Path | None = None) -> subprocess.Popen:
    """Start a server in the background, in a session of its own so that it outlives this
    window; its output goes to server_log(name), which its Logs button shows."""
    with open(server_log(name), "ab") as out:
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                cwd=cwd, start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})


def tail(path: Path, lines: int = 500) -> str:
    """The last lines of a text file."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256_000))
            text = f.read().decode(errors="replace")
    except OSError as e:
        raise BusError(f"cannot read {path}: {e}") from None
    return "\n".join(text.splitlines()[-lines:])


def server_file(s: Server) -> Path | None:
    """The .toml config or .py script a server runs: from its service, else from its command line."""
    argv, cwd = [], None
    if s.service:
        try:
            argv = shlex.split(s.service["command"])
        except ValueError:
            argv = []
    elif s.origin == "local" and s.pid and s.pid != os.getpid():
        argv = _cmdline(s.pid)
        try:
            cwd = os.readlink(f"/proc/{s.pid}/cwd")
        except OSError:
            cwd = None
    for arg in reversed(argv[1:]):
        if arg.endswith((".toml", ".py")):
            path = Path(arg) if os.path.isabs(arg) or cwd is None else Path(cwd) / arg
            return path if path.is_file() else None
    return None


# ---------------------------------------------------------------- previews (worker threads)
def _envelope(a) -> np.ndarray:
    """At most 2 x PREVIEW_POINTS values that draw like the whole trace: the lowest and highest of each slice."""
    a = np.asarray(a)
    a = np.abs(a) if a.dtype.kind == "c" else a
    a = a.astype(np.float32, copy=False).ravel()
    if a.size <= 2 * PREVIEW_POINTS:
        return a.copy()
    per = a.size // PREVIEW_POINTS
    blocks = a[:per * PREVIEW_POINTS].reshape(PREVIEW_POINTS, per)
    out = np.empty(2 * PREVIEW_POINTS, np.float32)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # a slice of only NaN
        out[0::2], out[1::2] = np.nanmin(blocks, axis=1), np.nanmax(blocks, axis=1)
    return out


def _thumbnail(a: np.ndarray) -> np.ndarray:
    """An image made small (every n-th pixel) and scaled to 8 bits between its 1st and 99.5th percentile."""
    step = max(1, math.ceil(max(a.shape[:2]) / PREVIEW_PIXELS))
    small = np.asarray(a[::step, ::step])
    if small.ndim == 3:
        small = small[..., :3]
    if small.dtype == np.uint8:
        return np.ascontiguousarray(small)
    f = np.nan_to_num(small.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    lo, hi = np.percentile(f, (1, 99.5)) if f.size else (0.0, 1.0)
    if hi <= lo:
        hi = lo + 1.0
    return np.ascontiguousarray(np.clip((f - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8))


def _brief(value) -> str:
    text = summarize(value) if isinstance(value, (np.ndarray, bytes, bytearray, dict, list, tuple)) else str(value)
    return text if len(text) <= 40 else text[:37] + "..."


def shrink(data, meta: dict) -> tuple[str, object, str]:
    """What a tile shows of a frame: ("trace", [1-D float32 arrays]), ("image", a small uint8
    array), ("number", float) or ("text", str); and the unit from its meta."""
    unit = str(meta.get("unit") or "")
    if dataclasses.is_dataclass(data) and not isinstance(data, type):
        data = {f.name: getattr(data, f.name) for f in dataclasses.fields(data)}
    if isinstance(data, dict):
        arrays = [v for v in data.values() if isinstance(v, np.ndarray) and v.ndim == 1 and v.size > 1]
        if arrays:
            return "trace", [_envelope(v) for v in arrays[:3]], unit
        return "text", ", ".join(f"{k} {_brief(v)}" for k, v in list(data.items())[:4]), unit
    if isinstance(data, np.ndarray):
        if data.dtype.kind not in "biufc":
            return "text", summarize(data), unit
        if data.size == 1:
            return "number", float(np.abs(data.reshape(-1)[0])), unit
        if data.ndim == 1:
            return "trace", [_envelope(data)], unit
        if data.ndim == 2 and data.shape[0] <= 8 < data.shape[1]:  # a few rows of samples: traces
            return "trace", [_envelope(row) for row in data[:3]], unit
        if data.ndim == 2 or (data.ndim == 3 and data.shape[2] in (3, 4)):
            return "image", _thumbnail(data.real if data.dtype.kind == "c" else data), unit
        return "text", summarize(data), unit
    if isinstance(data, (bool, np.bool_, str)):
        return "text", str(data)[:200], unit
    if isinstance(data, (int, float, np.integer, np.floating)):
        return "number", float(data), unit
    return "text", summarize(data), unit


def elements(kind: str | None) -> int:
    """Elements of a stream's last frame from its summary ('uint16[480x640]'); 0 if not an array."""
    if not kind or not kind.endswith("]") or "[" not in kind:
        return 0
    try:
        return math.prod(int(n) for n in kind[kind.index("[") + 1:-1].split("x"))
    except ValueError:
        return 0


def preview_stream(d: dict) -> str | None:
    """The stream a tile shows: the first one (but "state") that has published something."""
    return next((n for n, st in d["streams"].items() if n != "state" and st["time"] is not None), None)


# ---------------------------------------------------------------- the model
@dataclasses.dataclass(eq=False)
class Server:
    """A server as the window knows it.  `key` is "local:NAME", "service:NAME" or "remote:ADDRESS"."""

    key: str
    name: str
    origin: str  #: "local" (running here), "service" (installed, not running) or "remote" (devices.toml)
    addresses: list = dataclasses.field(default_factory=list)
    pid: int | None = None
    started: float | None = None
    declared: list = dataclasses.field(default_factory=list)  #: device names known before asking the server
    service: dict | None = None
    probe: dict | None = None
    probed_at: float = 0.0
    error: str | None = None
    probing: bool = False
    rates: dict = dataclasses.field(default_factory=dict)  #: (device, stream) -> frames per second
    previews: dict = dataclasses.field(default_factory=dict)  #: device -> widgets.Preview of the stream it shows
    shown: dict = dataclasses.field(default_factory=dict)  #: device -> (stream, seq) of that preview
    fetching: set = dataclasses.field(default_factory=set)  #: devices whose preview is on its way
    history: dict = dataclasses.field(default_factory=dict)  #: (device, stream) -> recent numbers

    @property
    def address(self) -> str | None:
        """The address the window talks to: the Unix socket, else TCP, else in-process."""
        for prefix in ("ipc://", "tcp://", "inproc://"):
            for address in self.addresses:
                if address.startswith(prefix):
                    return address
        return None

    def device(self, name: str) -> dict | None:
        return self.probe["devices"].get(name) if self.probe else None

    def device_names(self) -> list[str]:
        return list(self.probe["devices"]) if self.probe else list(self.declared)

    def forget_live_data(self) -> None:
        self.probe, self.error, self.rates = None, None, {}
        self.previews, self.shown, self.history = {}, {}, {}


def ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def clock(stamp: float) -> str:
    t = time.localtime(stamp)
    today = time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d")
    return time.strftime("%H:%M:%S" if today else "%Y-%m-%d %H:%M", t)


def route(address: str, origin: str) -> str:
    kind = address.partition("://")[0]
    if kind == "ipc":
        return "Unix socket"
    if kind == "inproc":
        return "inside this program"
    return "TCP, from devices.toml" if origin == "remote" else "TCP"


def server_state(s: Server) -> tuple[str, str, str]:
    """(status, word, sentence) of a server."""
    if s.origin == "service":
        state, sub = s.service["state"], s.service.get("sub")
        if state == "active":
            return BUSY, "no server yet", ("The service runs, but no insthub server has announced itself from it "
                                           "yet. Logs shows its output.")
        if state == "activating":
            if sub == "auto-restart":
                return BUSY, "restarting", "It stopped with an error; systemd starts it again in a moment."
            return BUSY, "starting", "Starting."
        if state == "deactivating":
            return BUSY, "stopping", "Stopping."
        if state == "failed":
            return BAD, "failed", "Stopped with an error. Logs shows why."
        return IDLE, "stopped", "Installed as a service, not running."
    if s.error:
        return BAD, "unreachable", f"Does not answer: {s.error}"
    if s.probe is None:
        return IDLE, "checking", "Asking it..."
    return GOOD, "running", "Running."


def device_state(d: dict | None) -> tuple[str, str, str]:
    """(status, word, sentence) of a device, from its describe(); None: its server does not answer."""
    if d is None:
        return IDLE, "unknown", "Unknown: its server does not answer."
    st = d["status"]
    if st["busy"]:
        queued = f" (+{st['queued']} waiting)" if st["queued"] else ""
        word = "connecting" if st["busy"] == "$open" else f"busy: {st['busy']}"
        sentence = (f"Connected since {clock(st['since'])} ({ago(time.time() - st['since'])})."
                    if st["connected"] and st.get("since") else "Opening the instrument session...")
        return BUSY, word + queued, sentence
    if st["connected"]:
        since = f" since {clock(st['since'])} ({ago(time.time() - st['since'])})" if st.get("since") else ""
        return GOOD, "connected", f"Connected{since}."
    error = st.get("error")
    if error is None:
        return IDLE, "not opened yet", "Not opened yet: it opens on first use."
    if error == "released":
        return IDLE, "released", ("Released: the session is closed so that other software can use the "
                                  "instrument. The next command or periodic task opens it again.")
    return BAD, "not connected", f"Not connected: {error}"


def routes(s: Server) -> str:
    """How it can be reached, in short: 'Unix socket + TCP :8700'."""
    found = []
    for address in s.addresses:
        kind = address.partition("://")[0]
        if kind == "ipc":
            found.append("Unix socket")
        elif kind == "tcp":
            found.append(f"TCP :{address.rpartition(':')[2]}")
    return " + ".join(found)
