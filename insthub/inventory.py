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
    spawn(), Launched    starting a server or a window for a device in the background, its output
                         in a log file (server_log, ui_log); what it said if it failed
    server_file(), server_python()  the file a server runs, and the Python it runs in
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
from .client import PROCESS_TOKEN, Client, connect, discover
from .device import busy_with, summarize
from .errors import BusError

PROBE_TIMEOUT = 3.0  #: seconds a server may take to answer before it is shown as unreachable
ACTION_TIMEOUT = 120.0
PREVIEW_POINTS = 160  #: a trace preview keeps the lowest and highest value of this many slices
PREVIEW_PIXELS = 160  #: the long side of an image preview
PREVIEW_LIMIT = 4_000_000  #: elements; a larger frame gets no preview (over TCP: an eighth of it)
GOOD, BUSY, BAD, IDLE, PAUSED = "good", "busy", "bad", "idle", "paused"  #: statuses (widgets.STATUS: colours)


# ---------------------------------------------------------------- the blocking half (worker threads only)
class Backend:
    """Looks things up and talks to servers.  Every method may block; the
    window calls them on worker threads.  One connection per server address."""

    def __init__(self):
        self._clients: dict[str, Client] = {}
        self._lock = threading.Lock()

    def scan(self) -> dict:
        """Running servers, the servers on other computers that devices.toml names (with the
        devices it lists there: registry.remote_addresses), installed services."""
        try:
            remote, remote_error = registry.remote_addresses(), None
        except BusError as e:  # (it names the file)
            remote, remote_error = {}, str(e)
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
        client = connect(address, connect_timeout=2.0)
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
            args = args[2:] if args[0] in ("-X", "-W", "--check-hash-based-pycs") else args[1:]  # its options
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


def server_log(name: str) -> Path:
    """Where the output of a server this window started goes (the runtime folder, so it is gone at logout)."""
    return registry.runtime_dir() / f"{name}.log"


VIEW_LOG = "_view"  #: the log of a device's live view: no UI is named so (their names start with a letter or digit)


def ui_log(device: str, ui: str | None = None) -> Path:
    """Where the output of a window opened for a device goes: a UI of its extension (by its name),
    else its live view.  A folder for each device, beside the servers' logs: ui/DEVICE/UI.log, and
    ui/DEVICE/_view.log.  Raises ValueError for a name that is not one (a device's name may have
    come over the network)."""
    registry.check_name(device, "device name")
    name = VIEW_LOG if ui is None else registry.check_name(ui, "UI name")
    folder = registry.runtime_dir() / "ui" / device
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{name}.log"


@dataclasses.dataclass(eq=False)
class Launched:
    """A program started in the background by spawn(): a server, or a window for a device.  Its
    output goes to `log`, from `offset` on (before it: earlier runs'); `title` names it in messages
    ("lab", "Scope panel for scope")."""

    process: subprocess.Popen
    log: Path
    title: str
    cwd: str | Path | None = None  #: the folder it runs in (a restart starts it there again)
    quiet: bool = False  #: its end is not news: it is being stopped on purpose
    offset: int = 0  #: where its output starts in the log

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def failure(self, wait: float = 0.0) -> str | None:
        """What it said last, if it ended with an error, waiting up to `wait` seconds for it to end:
        the last lines of its own output in the log, else its exit code or the signal that ended it.
        An error is an exit code above 0 (an exception, or `error: ...` from the insthub command),
        or a signal other than those that stop a program on purpose (STOPPED): Qt aborts when it
        cannot show a window (no platform plugin) or a panel's slot raises, and a vendor library may
        crash.  None while it runs, once it ended well or was stopped, or when quiet."""
        try:
            code = self.process.wait(wait) if wait else self.process.poll()
        except subprocess.TimeoutExpired:
            return None
        if not code or self.quiet or -code in STOPPED:
            return None
        try:
            lines = [line for line in tail(self.log, 40, self.offset).splitlines() if line.strip()]
        except BusError:  # the log is gone (the runtime folder was cleaned up)
            lines = []
        return "\n".join(lines[-12:]) or (f"exit code {code}" if code > 0 else f"ended by {_signal_name(-code)}")


#: The signals that stop a program on purpose (Stop, closing a session, kill): its end is no error.
STOPPED = frozenset(getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGKILL", "SIGHUP")
                    if hasattr(signal, name))


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def spawn(command: list[str], log: Path, title: str, cwd: str | Path | None = None,
          env: dict | None = None) -> Launched:
    """Start a program in the background, in a session of its own so that it outlives the program
    that starts it (the window, `insthub open`).  Its output goes to the file `log` (server_log,
    ui_log), which a server's Logs button shows, and Launched.failure() reads when it fails.  The
    log keeps what earlier runs wrote: each run starts with a line that says when, and what ran.
    `env`: its environment (default: this program's)."""
    with open(log, "ab") as out:
        out.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}: {shlex.join(map(str, command))}\n".encode())
        out.flush()
        offset = out.tell()
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=cwd,
                                   start_new_session=True, env={**(os.environ if env is None else env),
                                                                "PYTHONUNBUFFERED": "1"})
    return Launched(process, Path(log), title, cwd, offset=offset)


def tail(path: Path, lines: int = 500, start: int = 0) -> str:
    """The last lines of a text file (of what comes after the first `start` bytes)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(start, f.tell() - 256_000))
            text = f.read().decode(errors="replace")
    except OSError as e:
        raise BusError(f"cannot read {path}: {e}") from None
    return "\n".join(text.splitlines()[-lines:])


def _argv(s: Server) -> tuple[list[str], str | None]:
    """(the command line of a server: that of its service, else that of its process on this
    computer; the folder that process runs in, if known); ([], None) if neither is known."""
    if s.service:
        return service.command_of(s.service), None
    if s.origin == "local" and s.pid and s.pid != os.getpid():
        try:
            cwd = os.readlink(f"/proc/{s.pid}/cwd")
        except OSError:
            cwd = None
        return _cmdline(s.pid), cwd
    return [], None


def server_python(s: Server) -> Path | None:
    """The Python a server runs in: for one running here, what its registry file says; else (a
    service that is not running, or a server of an older insthub) from its command line, the
    Python it starts, or that of a virtualenv whose script it starts (.venv/bin/insthub); None
    if unknown."""
    if s.origin == "local" and s.python:
        return Path(s.python)
    argv, _ = _argv(s)
    if not argv:
        return None
    first = Path(argv[0])
    if first.name.startswith("python"):
        return first
    sibling = first.with_name("python")  # a script of a virtualenv (.venv/bin/insthub): its python
    return sibling if sibling.is_file() else None


def server_file(s: Server) -> Path | None:
    """The .toml config or .py script a server runs: from its service, else from its command line."""
    if s.service:
        return service.target(s.service)
    argv, cwd = _argv(s)
    arg = service.served(argv)
    if arg is None:
        return None
    path = Path(arg) if os.path.isabs(arg) or cwd is None else Path(cwd) / arg
    return path if path.is_file() else None


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
    python: str | None = None  #: the Python it runs in, as its registry file says (a server running here)
    entry: dict | None = None  #: its registry entry (a server running here): what its probes judge its code by
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
        """The address the window talks to: the Unix socket, else TCP, else in-process.  Not the order
        of client._nearest, which prefers a server's address in this process: the window hands this
        address to the programs it starts for a device (its live view, a UI), which only a socket
        reaches."""
        for prefix in ("ipc://", "tcp://", "inproc://"):
            for address in self.addresses:
                if address.startswith(prefix):
                    return address
        return None

    def device(self, name: str) -> dict | None:
        return self.probe["devices"].get(name) if self.probe else None

    def device_names(self) -> list[str]:
        return list(self.probe["devices"]) if self.probe else list(self.declared)

    @property
    def outdated(self) -> list[str]:
        """Why it runs older code than its folders hold, as its last probe found out (see
        extensions.outdated); none if it does not, or that is not known."""
        return (self.probe or {}).get("outdated") or []

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
        restarts = service.restarts_text(s.service)
        told = f" ({restarts})" if restarts else ""
        if state == "active":
            return BUSY, "no server yet", ("The service runs, but no insthub server has announced itself from it "
                                           "yet. Logs shows its output.")
        if state == "activating":
            if sub == "auto-restart":
                return BUSY, "restarting", (f"It stopped with an error{told}; systemd starts it again in a "
                                            "moment.")
            return BUSY, "starting", "Starting."
        if state == "deactivating":
            return BUSY, "stopping", "Stopping."
        if state == "failed":
            return BAD, "failed", f"Stopped with an error{told}. Logs shows why."
        return IDLE, "stopped", "Installed as a service, not running."
    if s.error:
        return BAD, "unreachable", f"Does not answer: {s.error}"
    if s.probe is None:
        return IDLE, "checking", "Asking it..."
    return GOOD, "running", "Running."


def paused_here(paused: dict, local: bool = True) -> bool:
    """Whether this window paused the device (status["paused"]), on this computer or another."""
    return paused.get("token") == PROCESS_TOKEN or bool(local and paused.get("pid") == os.getpid())


def paused_by(paused: dict, local: bool = True) -> str:
    """Who paused a device, from its status["paused"]: 'this window', what the program called
    itself ('insthub pause', 'experiment.py'), its name from its process, or its computer."""
    pid, peer, label = paused.get("pid"), paused.get("peer"), paused.get("by")
    if paused_here(paused, local):
        return "this window"
    host = peer.rpartition(":")[0] if peer else ""
    where = f" on {host}" if host and host not in ("127.0.0.1", "::1", "localhost") else ""
    if label:
        return label + where
    if pid and local:
        name = program_name(pid)
        return name if not name.startswith("pid ") else f"a program that has ended ({name})"
    if peer:
        return f"a program{where or ' on this computer'}"
    return "somebody"


def device_state(d: dict | None, local: bool = True) -> tuple[str, str, str]:
    """(status, word, sentence) of a device, from its describe(); None: its server does not answer.
    `local`: its server runs on this computer (so the pid of who paused it means a program here)."""
    if d is None:
        return IDLE, "unknown", "Unknown: its server does not answer."
    st = d["status"]
    paused = st.get("paused")  # None from a server older than this window
    if paused:
        who = paused_by(paused, local)
        if st["busy"] == "$pause":  # the pause itself, closing the session (a slow close() shows here)
            return BUSY, "pausing", f"Pausing (asked by {who}): closing the instrument session."
        what = busy_with(st)
        if what or st["connected"]:  # only the pause itself closes the session: until then, it is pausing
            return BUSY, "pausing", f"Pausing (asked by {who}): " + (
                f"waiting for {what} to finish." if what else
                "the session is closed once what it is doing (a periodic task, say) has finished.")
        return PAUSED, "paused", (f"Paused since {clock(paused['since'])} by {who}: none of its commands, "
                                  "properties or periodic tasks run, and the instrument is left alone, until it "
                                  "is resumed.")
    if st["busy"]:
        queued = f" (+{st['queued']} waiting)" if st["queued"] else ""
        word = "connecting" if st["busy"] in ("$open", "$resume") else \
            "releasing" if st["busy"] == "$release" else f"busy: {st['busy']}"
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
