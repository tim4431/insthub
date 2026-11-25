"""The manager window (`insthub gui`): the servers and devices on this computer,
how each one is reached, and the controls to look after them.

    left     every server, found the way clients find them (the registry
             folder), the installed services that are not running, and the
             servers of other computers listed in devices.toml; under each
             server its devices, with a status dot
    right    for a device: is the instrument connected (and if not, why), what
             it is doing, its streams and periodic tasks, which programs watch
             it.  Connect now / Release / Live view.
             For a server: its process, addresses, service and the programs
             connected to it.  Start / Stop / Restart / Logs.

The GUI thread never waits for a server: discovery, probes and actions run on
worker threads and come back through `qt.deliver`.  The window is an ordinary
client and needs only Qt; live plots open as a separate `insthub view` process.
`insthub gui --install` adds it to the applications menu (see launcher.py).
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import launcher, registry, service
from .client import Client, _SocketTransport, connect, discover
from .errors import BusError
from .qt import QtCore, QtGui, QtWidgets, application, deliver, run

REFRESH_MS = 1000
HIDDEN_REFRESH_MS = 3000  #: while only the tray icon is watching
PROBE_TIMEOUT = 3.0  #: seconds a server may take to answer before it is shown as unreachable
ACTION_TIMEOUT = 120.0

# Status colours: readable on light and dark window backgrounds.  The text beside
# each dot says the same thing, so colour is never the only signal.
_GOOD, _BUSY, _BAD, _IDLE = "#0ca30c", "#c98500", "#d03b3b", "#898781"
_ORIGIN_ORDER = {"local": 0, "service": 1, "remote": 2}
_ROLE = QtCore.Qt.ItemDataRole.UserRole
QAction = getattr(QtGui, "QAction", None) or QtWidgets.QAction  # QtWidgets in Qt 5


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

    def call(self, address: str, device: str, method: str, *args, timeout: float = ACTION_TIMEOUT):
        return self.client(address)._t.request(device, method, args, {}).result(timeout)

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


# ---------------------------------------------------------------- the model
@dataclasses.dataclass(eq=False)
class _Server:
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


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def _clock(stamp: float) -> str:
    t = time.localtime(stamp)
    today = time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d")
    return time.strftime("%H:%M:%S" if today else "%Y-%m-%d %H:%M", t)


def _esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _badge(color: str, text: str) -> str:
    return f"<span style='color:{color}'>&#9679;</span>&nbsp;{_esc(text)}"


def _route(address: str, origin: str) -> str:
    kind = address.partition("://")[0]
    if kind == "ipc":
        return "Unix socket"
    if kind == "inproc":
        return "inside this program"
    return "TCP, from devices.toml" if origin == "remote" else "TCP"


def _server_state(s: _Server) -> tuple[str, str, str]:
    """(colour, word, sentence)"""
    if s.origin == "service":
        state, sub = s.service["state"], s.service.get("sub")
        if state == "active":
            return _BUSY, "no server yet", ("The service runs, but no insthub server has announced itself from it "
                                            "yet. Logs shows its output.")
        if state == "activating":
            if sub == "auto-restart":
                return _BUSY, "restarting", "It stopped with an error; systemd starts it again in a moment."
            return _BUSY, "starting", "Starting."
        if state == "deactivating":
            return _BUSY, "stopping", "Stopping."
        if state == "failed":
            return _BAD, "failed", "Stopped with an error. Logs shows why."
        return _IDLE, "stopped", "Installed as a service, not running."
    if s.error:
        return _BAD, "unreachable", f"Does not answer: {s.error}"
    if s.probe is None:
        return _IDLE, "checking", "Asking it..."
    return _GOOD, "running", "Running."


def _device_state(d: dict | None) -> tuple[str, str, str]:
    """(colour, word, sentence) for a device's describe(); None when the server does not answer."""
    if d is None:
        return _IDLE, "unknown", "Unknown: its server does not answer."
    st = d["status"]
    if st["busy"]:
        queued = f" (+{st['queued']} waiting)" if st["queued"] else ""
        word = "connecting" if st["busy"] == "$open" else f"busy: {st['busy']}"
        sentence = (f"Connected since {_clock(st['since'])} ({_ago(time.time() - st['since'])})."
                    if st["connected"] and st.get("since") else "Opening the instrument session...")
        return _BUSY, word + queued, sentence
    if st["connected"]:
        since = f" since {_clock(st['since'])} ({_ago(time.time() - st['since'])})" if st.get("since") else ""
        return _GOOD, "connected", f"Connected{since}."
    error = st.get("error")
    if error is None:
        return _IDLE, "not opened yet", "Not opened yet: it opens on first use."
    if error == "released":
        return _IDLE, "released", ("Released: the session is closed so that other software can use the "
                                   "instrument. The next command or periodic task opens it again.")
    return _BAD, "not connected", f"Not connected: {error}"


def _server_details(s: _Server) -> str:
    if s.origin == "remote":
        return f"{s.address} (devices.toml)"
    if s.origin == "service":
        return f"{service.PREFIX}{s.name}"
    routes = []
    for address in s.addresses:
        kind = address.partition("://")[0]
        if kind == "ipc":
            routes.append("Unix socket")
        elif kind == "tcp":
            routes.append(f"TCP :{address.rpartition(':')[2]}")
    parts = [f"pid {s.pid}", " + ".join(routes) or "in this program"]
    if s.service:
        parts.append("service")
    return " · ".join(parts)


_icons: dict = {}


def _dot(color: str) -> QtGui.QIcon:
    """A status dot: filled, or a ring for 'nothing to report'."""
    icon = _icons.get(color)
    if icon is None:
        pix = QtGui.QPixmap(28, 28)
        pix.setDevicePixelRatio(2.0)
        pix.fill(QtGui.QColor(0, 0, 0, 0))
        p = QtGui.QPainter(pix)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        if color == _IDLE:
            p.setPen(QtGui.QPen(QtGui.QColor(color), 1.5))
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        else:
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(color))
        p.drawEllipse(QtCore.QRectF(2.5, 2.5, 9, 9))
        p.end()
        icon = _icons[color] = QtGui.QIcon(pix)
    return icon


# ---------------------------------------------------------------- the right-hand pages
def _table(*headers: str) -> QtWidgets.QTableWidget:
    table = QtWidgets.QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    table.setWordWrap(False)
    table.setShowGrid(False)
    header = table.horizontalHeader()
    header.setStretchLastSection(True)
    header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
    header.setDefaultAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
    return table


def _fill(table: QtWidgets.QTableWidget, rows: list) -> None:
    """Show rows of text, reusing the cells so that selection and scrolling survive a refresh."""
    blocked = table.blockSignals(True)
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            item = table.item(r, c)
            if item is None:
                item = QtWidgets.QTableWidgetItem()
                item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
                table.setItem(r, c, item)
            text = "" if value is None else str(value)
            if item.text() != text:
                item.setText(text)
                item.setToolTip(text if len(text) > 40 else "")
    table.blockSignals(blocked)


class _Page(QtWidgets.QWidget):
    """Title, a column of facts, a row of buttons, and tabs of tables."""

    def __init__(self):
        super().__init__()
        self.title = QtWidgets.QLabel()
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() * 1.45)
        font.setBold(True)
        self.title.setFont(font)
        self.badge = QtWidgets.QLabel()
        self.subtitle = self._label()
        head = QtWidgets.QHBoxLayout()
        head.addWidget(self.title)
        head.addSpacing(12)
        head.addWidget(self.badge)
        head.addStretch(1)
        self.form = QtWidgets.QFormLayout()
        self.form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        self.form.setHorizontalSpacing(18)
        self.form.setVerticalSpacing(7)
        self.buttons = QtWidgets.QHBoxLayout()
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(18, 14, 18, 12)
        box.addLayout(head)
        box.addWidget(self.subtitle)
        box.addSpacing(10)
        box.addLayout(self.form)
        box.addSpacing(10)
        box.addLayout(self.buttons)
        box.addSpacing(6)
        box.addWidget(self.tabs, 1)
        box.addStretch(0)  # takes the space instead of the tabs when they are hidden

    def _label(self) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel()
        label.setWordWrap(True)
        label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        return label

    def muted(self, text: str) -> str:
        """Secondary text: halfway between the text and the background, in light and dark themes alike."""
        palette = self.palette()
        ink, paper = palette.color(QtGui.QPalette.ColorRole.WindowText), palette.color(QtGui.QPalette.ColorRole.Window)
        mix = [round(0.55 * a + 0.45 * b) for a, b in zip(ink.getRgb()[:3], paper.getRgb()[:3], strict=True)]
        return f"<span style='color:#{mix[0]:02x}{mix[1]:02x}{mix[2]:02x}'>{text}</span>"

    def row(self, name: str) -> QtWidgets.QLabel:
        label = self._label()
        self.form.addRow(name, label)
        return label

    def show_row(self, field: QtWidgets.QLabel, text: str | None) -> None:
        """Set a fact, or hide its row when there is nothing to say."""
        visible = bool(text)
        field.setText(text or "")
        field.setVisible(visible)
        self.form.labelForField(field).setVisible(visible)

    def button(self, text: str, tip: str, slot) -> QtWidgets.QPushButton:
        button = QtWidgets.QPushButton(text)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        self.buttons.addWidget(button)
        return button


class _Overview(QtWidgets.QLabel):
    """What the right-hand side shows when nothing is selected."""

    def __init__(self):
        super().__init__()
        self.setWordWrap(True)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
        self.setContentsMargins(22, 18, 22, 18)
        self.setTextFormat(QtCore.Qt.TextFormat.RichText)

    def show_summary(self, servers: list[_Server], scanned: bool) -> None:
        if not scanned:
            self.setText("<p>Looking for servers...</p>")
        elif not servers:
            self.setText(
                "<h3>No insthub server is running on this computer</h3>"
                "<p>Start one with <code>insthub serve lab.toml</code> or <code>python my_server.py</code>, "
                "or keep one running in the background with <b>Add service...</b> above.</p>"
                "<p>To try things without hardware, run <code>insthub sim</code> in a terminal.</p>")
        else:
            self.setText("<p>Select a server or a device on the left to see how it is connected and to manage it."
                         "</p><p>A <b>server</b> is a program that holds the connections to its instruments; "
                         "a <b>device</b> is one instrument on it.</p>")


class _ServerPage(_Page):
    def __init__(self, window: Manager):
        super().__init__()
        self.window, self.server = window, None
        self.state = self.row("State")
        self.process = self.row("Process")
        self.reach = self.row("Reached at")
        self.latency = self.row("Answers in")
        self.service = self.row("Service")
        self.command = self.row("Command")
        self.devices = self.row("Devices")
        self.start = self.button("Start", "Start the service", lambda: window.service_action(self.server, "start"))
        self.stop = self.button("Stop", "Stop the server; its devices close their instruments", self._stop)
        self.restart = self.button("Restart", "Stop and start again, e.g. after changing its code or config",
                                   lambda: window.service_action(self.server, "restart"))
        self.logs = self.button("Logs", "What the server printed", lambda: window.show_logs(self.server))
        self.remove = self.button("Remove service", "Stop it and no longer start it at login",
                                  lambda: window.service_action(self.server, "remove"))
        self.buttons.addStretch(1)
        self.clients = _table("Program", "Process", "Connected since", "Watching")
        self.tabs.addTab(self.clients, "Connected programs")

    def _stop(self) -> None:
        if self.server.service:
            self.window.service_action(self.server, "stop")
        else:
            self.window.stop_server(self.server)

    def show_server(self, s: _Server) -> None:
        self.server = s
        color, word, sentence = _server_state(s)
        self.title.setText(_esc(s.name))
        self.badge.setText(_badge(color, word))
        self.subtitle.setText(self.muted({"local": "Server on this computer",
                                          "service": "Background service, not running",
                                          "remote": "Server on another computer, listed in devices.toml"}[s.origin]))
        self.show_row(self.state, _esc(sentence))
        info = s.probe["info"] if s.probe else {}
        if s.origin == "local" and s.pid:
            started = f" · started {_clock(s.started)}, {_ago(time.time() - s.started)} ago" if s.started else ""
            self.show_row(self.process, f"pid {s.pid}{started}")
        elif info.get("pid"):
            self.show_row(self.process, f"pid {info['pid']} on that computer")
        else:
            self.show_row(self.process, None)
        self.show_row(self.reach, "<br>".join(
            f"<code>{_esc(a)}</code> &nbsp;{self.muted(_route(a, s.origin))}" for a in s.addresses))
        self.show_row(self.latency, f"{s.probe['latency'] * 1e3:.2f} ms" if s.probe else None)
        u = s.service
        if u:
            detail = f" ({u['sub']})" if u.get("sub") not in (None, "", "running", "dead", u["state"]) else ""
            boot = "starts at login" if u["enabled"] else "does not start at login"
            self.show_row(self.service, f"{service.PREFIX}{_esc(u['name'])} · {_esc(u['state'])}{detail} · {boot}")
            self.show_row(self.command, f"<code>{_esc(u['command'])}</code>")
        elif s.origin == "local":
            self.show_row(self.service, self.muted("none: started by hand, so it stops when its terminal closes"))
            self.show_row(self.command, None)
        else:
            self.show_row(self.service, None)
            self.show_row(self.command, None)
        if s.probe:
            devices = s.probe["devices"].values()
            connected = sum(1 for d in devices if d["status"]["connected"])
            self.show_row(self.devices, f"{len(devices)} · {connected} connected")
        else:
            self.show_row(self.devices, ", ".join(_esc(n) for n in s.declared) or None)
        running = s.origin == "local"
        mine = s.pid == os.getpid()
        self.start.setVisible(bool(u) and not running)
        self.stop.setVisible((bool(u) and running) or (running and not u and bool(s.pid) and not mine))
        for button in (self.restart, self.logs, self.remove):
            button.setVisible(bool(u))
        clients = (s.probe["clients"] if s.probe else None) or []
        _fill(self.clients, [[c["program"], f"pid {c['pid']}" if c.get("pid") else (c.get("peer") or ""),
                              _clock(c["since"]) if c.get("since") else "",
                              ", ".join(c.get("subscriptions", ())) or "-"] for c in clients])
        self.tabs.setTabText(0, f"Connected programs ({len(clients)})")
        self.tabs.setVisible(s.probe is not None and s.probe["clients"] is not None)


class _DevicePage(_Page):
    def __init__(self, window: Manager):
        super().__init__()
        self.window, self.server, self.name = window, None, None
        self.instrument = self.row("Instrument")
        self.activity = self.row("Activity")
        self.requests = self.row("Requests")
        self.via = self.row("Server")
        self.watchers = self.row("Watched by")
        self.connect_now = self.button("Connect now", "Open the instrument session now, without waiting for a command",
                                       lambda: window.device_action(self.server, self.name, "$open"))
        self.release = self.button(
            "Release", "Close the instrument session so that other software (e.g. the vendor's) can use the "
                       "instrument. The next command or periodic task opens it again; stop the periodic tasks "
                       "below to keep it closed.",
            lambda: window.device_action(self.server, self.name, "$release"))
        self.live = self.button("Live view", "Live plots of its streams, in a window of their own",
                                lambda: window.live_view(self.server, self.name))
        self.buttons.addStretch(1)
        self.streams = _table("Stream", "Data", "Rate", "Last frame", "Watchers", "Description")
        self.tasks = _table("Periodic task", "Every", "Running", "Runs", "Errors", "Last error")
        self.tasks.itemChanged.connect(self._task_toggled)
        self.state = _table("Key", "Value")
        self.commands = _table("Name", "Arguments", "Description")
        for table, title in ((self.streams, "Streams"), (self.tasks, "Periodic tasks"), (self.state, "State"),
                             (self.commands, "Commands")):
            self.tabs.addTab(table, title)

    def show_device(self, s: _Server, name: str) -> None:
        changed = (s, name) != (self.server, self.name)
        self.server, self.name = s, name
        d = s.device(name)
        color, word, sentence = _device_state(d)
        self.title.setText(_esc(name))
        self.badge.setText(_badge(color, word))
        doc = f" · {_esc(d['doc'])}" if d and d.get("doc") else ""
        what = _esc(d["type"]) if d else ("device listed in devices.toml" if s.origin == "remote" else "device")
        self.subtitle.setText(self.muted(what + doc))
        self.show_row(self.instrument, _badge(color, sentence))
        reachable = d is not None
        if d:
            st = d["status"]
            if st["busy"]:
                waiting = f", {st['queued']} more waiting" if st["queued"] else ""
                activity = f"running <code>{_esc(st['busy'])}</code>{waiting}"
            else:
                activity = "idle"
            self.show_row(self.activity, activity)
            self.show_row(self.requests, f"{st['calls']:,} handled · {st['errors']:,} failed")
        else:
            self.show_row(self.activity, None)
            self.show_row(self.requests, None)
        route = _route(s.address, s.origin) if s.address else ""
        pid = f" (pid {s.pid})" if s.origin == "local" and s.pid else ""
        self.show_row(self.via, f"{_esc(s.name)}{pid} · {self.muted(route)}")
        watching = {}
        reported = bool(s.probe) and s.probe["clients"] is not None
        for c in s.probe["clients"] if reported else ():
            streams = [sub.partition("/")[2] for sub in c.get("subscriptions", ()) if sub.startswith(name + "/")]
            if streams:
                watching[c["program"]] = watching.get(c["program"], []) + streams
        nobody = "no other program" if reported else "not reported (the server runs an older insthub)"
        self.show_row(self.watchers, "; ".join(f"{_esc(p)} ({', '.join(map(_esc, v))})" for p, v in watching.items())
                      or (self.muted(nobody) if reachable else None))
        connected = bool(d and d["status"]["connected"])
        self.connect_now.setEnabled(reachable and not connected)
        self.release.setEnabled(reachable and connected)
        live_ok, why = self.window.can_view(s)
        self.live.setEnabled(reachable and live_ok)
        self.live.setToolTip("Live plots of its streams, in a window of their own" if live_ok else why)

        now = time.time()
        streams = {n: st for n, st in d["streams"].items() if n != "state"} if d else {}  # state: its own tab
        _fill(self.streams, [[n, st["kind"] or "nothing yet", _rate(s.rates.get((name, n))),
                              f"{_ago(now - st['time'])} ago" if st["time"] else "never",
                              st["subscribers"], st["doc"]] for n, st in sorted(streams.items())])
        tasks = d["tasks"] if d else {}
        _fill(self.tasks, [[n, f"{t['interval']:g} s" if t["interval"] else "continuously",
                            "on" if t["enabled"] else "off",
                            f"{t['runs']:,}", t["errors"], t["last_error"] or ""] for n, t in sorted(tasks.items())])
        blocked = self.tasks.blockSignals(True)
        for r, (_, t) in enumerate(sorted(tasks.items())):
            item = self.tasks.item(r, 2)
            item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.CheckState.Checked if t["enabled"] else QtCore.Qt.CheckState.Unchecked)
        self.tasks.blockSignals(blocked)
        state = s.probe["states"].get(name, {}) if s.probe else {}
        _fill(self.state, [[k, _short(v)] for k, v in state.items()])
        commands = [[n, c["signature"], (c["doc"] or "").split("\n")[0]] for n, c in sorted(d["commands"].items())] \
            if d else []
        commands += [[n, "property" if p["settable"] else "property, read-only", (p["doc"] or "").split("\n")[0]]
                     for n, p in sorted(d["properties"].items())] if d else []
        _fill(self.commands, commands)
        for i, (title, count) in enumerate((("Streams", len(streams)), ("Periodic tasks", len(tasks)),
                                            ("State", len(state)), ("Commands", len(commands)))):
            self.tabs.setTabText(i, f"{title} ({count})")
        if changed and d:  # open on the tab that has something to show
            self.tabs.setCurrentIndex(next((i for i, n in enumerate((streams, tasks, state, commands)) if n), 0))

    def _task_toggled(self, item: QtWidgets.QTableWidgetItem) -> None:
        if item.column() != 2 or self.server is None:
            return
        task = self.tasks.item(item.row(), 0).text()
        on = item.checkState() == QtCore.Qt.CheckState.Checked
        self.window.task_action(self.server, self.name, task, on)


def _rate(per_second: float | None) -> str:
    if not per_second:
        return "-" if per_second == 0 else ""
    return f"{per_second:.0f} /s" if per_second >= 10 else f"{per_second:.2g} /s"


def _short(value) -> str:
    text = repr(value) if not isinstance(value, str) else value
    return text if len(text) <= 80 else text[:77] + "..."


class _LogDialog(QtWidgets.QDialog):
    """The recent output of a service, refreshed while the dialog is open."""

    def __init__(self, window: Manager, name: str):
        super().__init__(window)
        self.window, self.name = window, name
        self._loading, self._last = False, None
        self.setWindowTitle(f"{service.PREFIX}{name}: log")
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self.follow = QtWidgets.QCheckBox("Follow new output")
        self.follow.setChecked(True)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.hide)
        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self.follow)
        bottom.addStretch(1)
        bottom.addWidget(close)
        box = QtWidgets.QVBoxLayout(self)
        box.addWidget(self.text)
        box.addLayout(bottom)
        self.resize(940, 520)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.reload)
        self._timer.start(2000)

    def reload(self) -> None:
        if self._loading or not self.isVisible():
            return
        self._loading = True
        self.window.submit(self.window.actions, lambda: service.journal(self.name, 500), self._show, self._failed)

    def _show(self, text: str) -> None:
        self._loading = False
        if text == self._last:
            return
        self._last = text
        bar = self.text.verticalScrollBar()
        keep = None if self.follow.isChecked() else bar.value()
        self.text.setPlainText(text)
        bar.setValue(bar.maximum() if keep is None else keep)

    def _failed(self, exc) -> None:
        self._loading = False
        self.text.setPlainText(f"cannot read the log: {exc}")


class _AddServiceDialog(QtWidgets.QDialog):
    """Which server to keep running, under what name, with which Python."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Add a background service")
        self.target, self.name, self.python = QtWidgets.QLineEdit(), QtWidgets.QLineEdit(), QtWidgets.QLineEdit()
        self.target.setPlaceholderText("a .toml config or a .py server script")
        self.target.textChanged.connect(self._guess)
        form = QtWidgets.QFormLayout()
        form.addRow("Server", self._with_browse(self.target, self._browse_target))
        form.addRow("Name", self.name)
        form.addRow("Python", self._with_browse(self.python, self._browse_python))
        note = QtWidgets.QLabel("It starts now and at every login, and is restarted if it crashes. Python must be "
                                "the interpreter that has the driver's packages: a virtualenv next to the server "
                                "file is picked automatically.")
        note.setWordWrap(True)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                             | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Install and start")
        buttons.accepted.connect(self._check)
        buttons.rejected.connect(self.reject)
        box = QtWidgets.QVBoxLayout(self)
        box.addLayout(form)
        box.addWidget(note)
        box.addWidget(buttons)
        self.resize(620, 0)

    @staticmethod
    def _with_browse(edit, slot) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        browse = QtWidgets.QPushButton("Browse...")
        browse.clicked.connect(slot)
        layout.addWidget(browse)
        return row

    def _browse_target(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Server to keep running", str(Path.home()),
                                                        "insthub servers (*.toml *.py)")
        if path:
            self.target.setText(path)

    def _browse_python(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Python interpreter", str(Path(self.python.text()).parent))
        if path:
            self.python.setText(path)

    def _guess(self, text: str) -> None:
        path = Path(text.strip()).expanduser()
        if path.suffix in (".toml", ".py") and path.is_file():
            self.name.setText(path.stem)
            self.python.setText(find_python(path))

    def _check(self) -> None:
        target, name, python = self.values()
        problem = None
        if not Path(target).expanduser().is_file():
            problem = "Choose the server's .toml config or .py script."
        elif not os.access(python, os.X_OK):
            problem = f"{python} is not a program."
        else:
            try:
                registry.check_name(name, "service name")
            except ValueError as e:
                problem = str(e)
        if problem:
            QtWidgets.QMessageBox.warning(self, "Add a background service", problem)
        else:
            self.accept()

    def values(self) -> tuple[str, str, str]:
        return self.target.text().strip(), self.name.text().strip(), self.python.text().strip() or sys.executable


def _claude(*args: str) -> subprocess.CompletedProcess:
    """Run Claude Code's command line (`claude mcp ...`)."""
    return subprocess.run(["claude", *args], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)


class _AgentsDialog(QtWidgets.QDialog):
    """AI agents on the bus: giving one access through `insthub mcp`, and which are connected now."""

    def __init__(self, window: Manager):
        super().__init__(window)
        self.window = window
        self._asking = False
        self.setWindowTitle("AI agents")
        command = mcp_command()
        self.add = ["mcp", "add", "--scope", "user", "insthub", "--", *command]
        intro = QtWidgets.QLabel(
            "An AI agent such as Claude Code can use the instruments through <code>insthub mcp</code>: list them, "
            "read their documentation, call commands, set properties and take live traces. While it does, it is "
            "listed among the programs connected to a server, and the tray says when one starts. "
            "docs/agents.md explains the rest.")
        intro.setWordWrap(True)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        self.setup = QtWidgets.QPushButton("Set up")
        self.setup.setToolTip("Runs: " + shlex.join(["claude", *self.add]))
        self.setup.clicked.connect(lambda: self._run("Setting up insthub for Claude Code", self.add))
        self.remove = QtWidgets.QPushButton("Remove")
        self.remove.setToolTip("Runs: claude mcp remove --scope user insthub")
        self.remove.clicked.connect(lambda: self._run("Removing insthub from Claude Code",
                                                      ["mcp", "remove", "--scope", "user", "insthub"]))
        claude = QtWidgets.QHBoxLayout()
        claude.addWidget(self.status, 1)
        claude.addWidget(self.setup)
        claude.addWidget(self.remove)
        self.connected = QtWidgets.QLabel()
        self.connected.setWordWrap(True)
        form = QtWidgets.QFormLayout()
        form.addRow("Claude Code", claude)
        form.addRow("Its command", self._copyable(shlex.join(["claude", *self.add])))
        form.addRow("Other agents", self._copyable(json.dumps(
            {"mcpServers": {"insthub": {"command": command[0], "args": command[1:]}}})))
        form.addRow("Connected now", self.connected)
        close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.hide)
        box = QtWidgets.QVBoxLayout(self)
        box.addWidget(intro)
        box.addSpacing(8)
        box.addLayout(form)
        box.addWidget(close)
        self.resize(780, 0)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._show_connected)
        self._timer.start(1000)

    def _copyable(self, text: str) -> QtWidgets.QWidget:
        line = QtWidgets.QLineEdit(text)
        line.setReadOnly(True)
        line.setCursorPosition(0)
        copy = QtWidgets.QPushButton("Copy")
        copy.clicked.connect(lambda: (QtWidgets.QApplication.clipboard().setText(text),
                                      self.window.statusBar().showMessage("Copied", 3000)))
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(line, 1)
        layout.addWidget(copy)
        return row

    def check(self) -> None:
        """Ask Claude Code whether it has insthub (in a worker: it starts `insthub mcp` to check it)."""
        self._show_connected()
        if shutil.which("claude") is None:
            self.status.setText("not installed here (there is no <code>claude</code> command); other agents take "
                                "the settings below")
            self.setup.hide()
            self.remove.hide()
            return
        if self._asking:
            return
        self._asking = True
        self.status.setText("asking...")
        self.window.submit(self.window.actions, lambda: _claude("mcp", "get", "insthub").returncode == 0,
                           self._answered, self._failed)

    def _answered(self, registered: bool) -> None:
        self._asking = False
        self.status.setText(_badge(_GOOD, "set up, for all your projects") if registered else "not set up yet")
        self.setup.setVisible(not registered)
        self.remove.setVisible(registered)

    def _failed(self, exc) -> None:
        self._asking = False
        self.status.setText(f"cannot ask it: {_esc(exc)}")

    def _run(self, doing: str, args: list[str]) -> None:
        def run():
            result = _claude(*args)
            if result.returncode != 0:
                raise BusError((result.stderr or result.stdout).strip() or f"exit status {result.returncode}")

        self.window.act(doing, run, f"{doing}: done", after=self.check)

    def _show_connected(self) -> None:
        if not self.isVisible():
            return
        agents = self.window.agents()
        self.connected.setText("<br>".join(f"{_esc(name)} (pid {pid}), using {', '.join(map(_esc, servers))}"
                                           for pid, (name, servers) in agents.items()) or "none right now")


# ---------------------------------------------------------------- the window
class Manager(QtWidgets.QMainWindow):
    """The window.  With `tray=True` it also has a tray icon, and closing the
    window only hides it: it keeps watching from the tray until Quit."""

    def __init__(self, backend: Backend | None = None, refresh_ms: int = REFRESH_MS, tray: bool = False):
        super().__init__()
        self.backend = backend or Backend()
        self._refresh_ms = refresh_ms
        self._quitting = False
        self._health: dict[tuple, str] = {}  # what was fine and what was broken at the last look
        self._agents_told: set[int] = set()  # agents (by pid) the tray has announced
        self._agents_dialog: _AgentsDialog | None = None
        self.probes = ThreadPoolExecutor(8, thread_name_prefix="insthub-gui-probe")
        self.actions = ThreadPoolExecutor(4, thread_name_prefix="insthub-gui-action")
        self.servers: dict[str, _Server] = {}
        self._items: dict[tuple, QtWidgets.QTreeWidgetItem] = {}
        self._scanning = False
        self._scanned = False
        self._closed = False
        self._systemd = False
        self._remote_error: str | None = None  # devices.toml cannot be read
        self._viewers: list = []
        self._logs: dict[str, _LogDialog] = {}

        self.setWindowTitle("insthub")
        self.resize(1120, 680)
        bar = self.addToolBar("Main")
        bar.setMovable(False)
        self.add_service = self._action(bar, "Add service...", self.add_service_dialog,
                                        "Keep a server running in the background (systemd user service)")
        self._action(bar, "AI agents...", self.agents_dialog,
                     "Let AI agents such as Claude Code use the instruments (insthub mcp)")
        self._action(bar, "Refresh", self.refresh, "Look again now (F5)", QtGui.QKeySequence.StandardKey.Refresh)
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self.summary = QtWidgets.QLabel()
        self.summary.setContentsMargins(0, 0, 10, 0)
        bar.addWidget(self.summary)
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QtGui.QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.quit)
        self.addAction(quit_action)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Status", "Details"])
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QtCore.QSize(14, 14))
        header = self.tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.tree.currentItemChanged.connect(lambda *_: self.show_selected())

        self.overview = _Overview()
        self.server_page = _ServerPage(self)
        self.device_page = _DevicePage(self)
        self.pages = QtWidgets.QStackedWidget()
        for page in (self.overview, self.server_page, self.device_page):
            self.pages.addWidget(page)
        split = QtWidgets.QSplitter()
        split.addWidget(self.tree)
        split.addWidget(self.pages)
        split.setStretchFactor(1, 1)
        split.setSizes([470, 650])
        split.setChildrenCollapsible(False)
        self.setCentralWidget(split)
        self.where = QtWidgets.QLabel()
        self.where.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.statusBar().addPermanentWidget(self.where)

        self.tray = _Tray(self) if tray else None
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(refresh_ms)
        self.refresh()

    def _action(self, bar, text, slot, tip, shortcut=None) -> QAction:
        action = QAction(text, self)
        action.setToolTip(tip)
        action.triggered.connect(slot)
        if shortcut is not None:
            action.setShortcut(shortcut)
        bar.addAction(action)
        return action

    # -- plumbing ------------------------------------------------------------
    def submit(self, pool, fn, done, failed=None) -> None:
        """Run `fn` on a worker thread; `done(result)` or `failed(exception)` then runs in the GUI thread."""
        def ok(result):
            if not self._closed:
                done(result)

        def error(exc):
            if not self._closed:
                (failed or self._report)(exc)

        try:
            future = pool.submit(fn)
        except RuntimeError:  # shutting down
            return
        deliver(future, ok, error)

    def _report(self, exc) -> None:
        self.statusBar().showMessage(f"{type(exc).__name__}: {exc}", 8000)

    def act(self, doing: str, fn, done, after=None) -> None:
        """A user's action: say what is happening, then what came of it.  `done` is the
        message (or makes it from the result); `after()` runs once it is over, either way."""
        self.statusBar().showMessage(f"{doing}...")

        def ok(result):
            self.statusBar().showMessage(done if not callable(done) else done(result), 6000)
            self.refresh()
            if after is not None:
                after()

        def failed(exc):
            self.statusBar().clearMessage()
            QtWidgets.QMessageBox.warning(self, "insthub", f"{doing} failed.\n\n{exc}")
            self.refresh()
            if after is not None:
                after()

        self.submit(self.actions, fn, ok, failed)

    def confirm(self, title: str, text: str, action: str) -> bool:
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Question, title, text,
                                    QtWidgets.QMessageBox.StandardButton.Cancel, self)
        yes = box.addButton(action, QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is yes

    # -- looking -------------------------------------------------------------
    def refresh(self) -> None:
        self._viewers = [p for p in self._viewers if p.poll() is None]  # reap closed viewers
        if not self._scanning:
            self._scanning = True
            self.submit(self.probes, self.backend.scan, self._on_scan, self._on_scan_failed)

    def _on_scan_failed(self, exc) -> None:
        self._scanning = False
        self._report(exc)

    def _on_scan(self, found: dict) -> None:
        self._scanning, self._scanned = False, True
        self._remote_error = found["remote_error"]
        units = found["services"]
        self._systemd = units is not None
        by_pid = {u["pid"]: u for u in units or () if u["pid"]}
        fresh: dict[str, _Server] = {}
        taken, local_addresses = set(), set()
        for info in found["servers"]:
            key = f"local:{info['server']}"
            s = self.servers.get(key) or _Server(key, info["server"], "local")
            if s.pid not in (None, info.get("pid")):  # restarted: what we knew is stale
                s.probe, s.error, s.rates = None, None, {}
            s.pid, s.started = info.get("pid"), info.get("started")
            s.addresses, s.declared = list(info.get("addresses", ())), list(info.get("devices", ()))
            s.service = by_pid.get(s.pid)
            if s.service:
                taken.add(s.service["name"])
            fresh[key] = s
            local_addresses.update(s.addresses)
        for u in units or ():
            if u["name"] not in taken:
                key = f"service:{u['name']}"
                s = self.servers.get(key) or _Server(key, u["name"], "service")
                s.service = u
                fresh[key] = s
        grouped: dict[str, list] = {}
        for name, address in found["remote"].items():
            grouped.setdefault(address, []).append(name)
        for address, names in grouped.items():
            if address in local_addresses:
                continue
            key = f"remote:{address}"
            s = self.servers.get(key) or _Server(key, address.partition("://")[2], "remote", [address])
            s.declared = sorted(names)
            fresh[key] = s
        for key, old in self.servers.items():
            if old.address and (key not in fresh or fresh[key].address != old.address):
                self.submit(self.actions, lambda a=old.address: self.backend.forget(a), lambda _: None)
        self.servers = fresh
        self._sync_tree()
        self.show_selected()
        self._summarize()
        self._watch_health()
        for s in fresh.values():
            if s.address and not s.probing and s.origin != "service":
                self._probe(s)

    def _probe(self, s: _Server) -> None:
        s.probing = True
        address = s.address

        def done(result):
            s.probing = False
            if self.servers.get(s.key) is not s:
                return
            if s.probe is not None and self.tray is not None:  # an agent that arrived since the last look
                before = {c["pid"] for c in s.probe["clients"] or () if c["agent"]}
                for c in result["clients"] or ():
                    if c["agent"] and c["pid"] not in before and c["pid"] not in self._agents_told:
                        self._agents_told.add(c["pid"])
                        self.tray.alert("An AI agent is using the instruments",
                                        f"{c['program']} connected to {s.name}.", ("server", s.key))
            now = time.monotonic()
            if s.probe and now > s.probed_at:
                for dev, d in result["devices"].items():
                    old = s.probe["devices"].get(dev)
                    for stream, st in d["streams"].items():
                        before = old["streams"].get(stream) if old else None
                        if before is not None and st["seq"] >= before["seq"]:
                            s.rates[(dev, stream)] = (st["seq"] - before["seq"]) / (now - s.probed_at)
            s.probe, s.probed_at, s.error = result, now, None
            if s.origin == "remote":
                s.name = result["info"].get("server") or s.name
            self._changed(s)

        def failed(exc):
            s.probing = False
            if self.servers.get(s.key) is not s:
                return
            s.probe, s.error, s.rates = None, str(exc) or type(exc).__name__, {}
            self._changed(s)

        self.submit(self.probes, lambda: self.backend.probe(address), done, failed)

    def _changed(self, s: _Server) -> None:
        item = self._items.get(("server", s.key))
        if item is not None:
            self._paint(s, item)
        key = self._selected()
        if key and key[1] == s.key:
            self.show_selected()
        self._summarize()
        self._watch_health()

    # -- the tree ------------------------------------------------------------
    def _selected(self) -> tuple | None:
        item = self.tree.currentItem()
        return item.data(0, _ROLE) if item is not None else None

    def _sync_tree(self) -> None:
        for key in [k for k in self._items if k[0] == "server" and k[1] not in self.servers]:
            item = self._items.pop(key)
            for child in [k for k in self._items if k[0] == "device" and k[1] == key[1]]:
                del self._items[child]
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        ordered = sorted(self.servers.values(), key=lambda s: (_ORIGIN_ORDER[s.origin], s.name.lower()))
        for index, s in enumerate(ordered):
            item = self._items.get(("server", s.key))
            if item is None:
                item = self._items["server", s.key] = QtWidgets.QTreeWidgetItem()
                item.setData(0, _ROLE, ("server", s.key))
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
                self.tree.insertTopLevelItem(min(index, self.tree.topLevelItemCount()), item)
                item.setExpanded(True)
            self._paint(s, item)
        if self.tree.currentItem() is None and self.tree.topLevelItemCount() == 1:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _paint(self, s: _Server, item: QtWidgets.QTreeWidgetItem) -> None:
        color, word, sentence = _server_state(s)
        item.setText(0, s.name)
        item.setText(1, word)
        item.setIcon(1, _dot(color))
        item.setToolTip(1, sentence)
        item.setText(2, _server_details(s))
        names = list(s.probe["devices"]) if s.probe else s.declared
        for key in [k for k in self._items if k[0] == "device" and k[1] == s.key and k[2] not in names]:
            item.removeChild(self._items.pop(key))
        for index, name in enumerate(names):
            child = self._items.get(("device", s.key, name))
            if child is None:
                child = self._items["device", s.key, name] = QtWidgets.QTreeWidgetItem()
                child.setData(0, _ROLE, ("device", s.key, name))
                item.insertChild(min(index, item.childCount()), child)
            d = s.device(name)
            color, word, sentence = _device_state(d)
            child.setText(0, name)
            child.setText(1, word)
            child.setIcon(1, _dot(color))
            child.setToolTip(1, sentence)
            watchers = d["status"]["subscribers"] if d else 0
            child.setText(2, (d["type"] if d else "") + (f" · {watchers} watching" if watchers else ""))

    def show_selected(self) -> None:
        key = self._selected()
        s = self.servers.get(key[1]) if key else None
        if s is not None and key[0] == "server":
            self.server_page.show_server(s)
            self.pages.setCurrentWidget(self.server_page)
        elif s is not None and key[0] == "device":
            self.device_page.show_device(s, key[2])
            self.pages.setCurrentWidget(self.device_page)
        else:
            self.overview.show_summary(list(self.servers.values()), self._scanned)
            self.pages.setCurrentWidget(self.overview)

    def _summarize(self) -> None:
        devices = [s.device(n) for s in self.servers.values() if s.probe for n in s.probe["devices"]]
        down = sum(1 for d in devices if not d["status"]["connected"] and d["status"]["error"] not in (None, "released"))
        running = sum(1 for s in self.servers.values() if s.origin != "service")
        text = f"{running} server{'s' * (running != 1)} · {len(devices)} device{'s' * (len(devices) != 1)}"
        agents = len(self.agents())
        if agents:
            text += f" · {agents} AI agent{'s' * (agents != 1)} connected"
        if down:
            text += f" · <span style='color:{_BAD}'>{down} not connected</span>"
        self.summary.setText(text)
        self.add_service.setEnabled(self._systemd)
        self.where.setText(self._remote_error or f"runtime folder: {registry.runtime_dir()}")

    def agents(self) -> dict[int, tuple[str, list[str]]]:
        """The AI agents connected right now: pid -> (name, the servers it uses)."""
        found: dict[int, tuple[str, list[str]]] = {}
        for s in self.servers.values():
            for c in (s.probe["clients"] if s.probe else None) or ():
                if c["agent"]:
                    found.setdefault(c["pid"], (c["program"], []))[1].append(s.name)
        return found

    # -- managing ------------------------------------------------------------
    def agents_dialog(self) -> None:
        if self._agents_dialog is None:
            self._agents_dialog = _AgentsDialog(self)
        self._agents_dialog.show()
        self._agents_dialog.raise_()
        self._agents_dialog.check()

    def device_action(self, s: _Server, device: str, method: str) -> None:
        address = s.address
        doing = {"$open": f"Connecting {device}", "$release": f"Releasing {device}"}[method]

        def result(status):
            if status["connected"]:
                return f"{device}: connected"
            return f"{device}: {status['error']}" if status["error"] == "released" else \
                f"{device}: not connected ({status['error']})"

        self.act(doing, lambda: self.backend.call(address, device, method), result)

    def task_action(self, s: _Server, device: str, task: str, on: bool) -> None:
        address = s.address
        self.act(f"{'Starting' if on else 'Stopping'} {device}.{task}",
                 lambda: self.backend.call(address, device, "$task", task, on, None, timeout=10.0),
                 f"{device}.{task}: {'running' if on else 'stopped'}")

    def can_view(self, s: _Server) -> tuple[bool, str]:
        if not s.address or s.address.startswith("inproc://"):
            return False, "Only for servers in other programs"
        if importlib.util.find_spec("pyqtgraph") is None:
            return False, "Live plots need pyqtgraph: pip install 'insthub[gui]'"
        return True, ""

    def live_view(self, s: _Server, device: str) -> None:
        command = [sys.executable, "-m", "insthub", "-a", s.address, "view", device]
        try:
            self._viewers.append(subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL, start_new_session=True))
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "insthub", f"Cannot open the live view.\n\n{e}")
            return
        self.statusBar().showMessage(f"Opening the live view of {device}...", 4000)

    def stop_server(self, s: _Server) -> None:
        if not self.confirm(f"Stop {s.name}?",
                            f"Stop the server “{s.name}”?\n\nIts devices close their instruments, and programs "
                            "using them get errors until it runs again. It was started by hand, so start it the "
                            "same way.", "Stop"):
            return
        pid = s.pid
        self.act(f"Stopping {s.name}", lambda: terminate(pid), f"{s.name}: asked to stop")

    def service_action(self, s: _Server, what: str) -> None:
        name = s.service["name"]
        unit = f"{service.PREFIX}{name}"
        questions = {
            "stop": ("Stop", f"Stop the service {unit}?\n\nIts devices close their instruments. It starts again "
                             "at the next login, or with Start."),
            "restart": ("Restart", f"Restart {unit}?\n\nPrograms using it get an error for calls under way, then "
                                   "reconnect by themselves."),
            "remove": ("Remove", f"Remove the service {unit}?\n\nIt is stopped and no longer starts at login. The "
                                 "server's own files are not touched."),
        }
        if what in questions:
            action, text = questions[what]
            if not self.confirm(f"{action} {unit}?", text, action):
                return
        doing = {"start": "Starting", "stop": "Stopping", "restart": "Restarting", "remove": "Removing"}[what]
        done = {"start": "started", "stop": "stopped", "restart": "restarted", "remove": "removed"}[what]
        self.act(f"{doing} {unit}", lambda: getattr(service, what)(name), f"{unit} {done}")

    def show_logs(self, s: _Server) -> None:
        name = s.service["name"]
        dialog = self._logs.get(name)
        if dialog is None:
            dialog = self._logs[name] = _LogDialog(self, name)
        dialog.show()
        dialog.raise_()
        dialog.reload()

    def add_service_dialog(self) -> None:
        dialog = _AddServiceDialog(self)
        if not dialog.exec():
            return
        target, name, python = dialog.values()

        def install():
            service.install(target, name, python=python)
            return service.linger_hint()

        def done(hint):
            message = f"{service.PREFIX}{name} installed and started"
            return message + (f"; it starts at login (at boot: {hint})" if hint else "")

        self.act(f"Installing {service.PREFIX}{name}", install, done)

    # -- the tray -------------------------------------------------------------
    def bring_up(self, select: tuple | None = None) -> None:
        """Show the window in front, optionally with a server or device selected."""
        self.setWindowState(self.windowState() & ~QtCore.Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        if select is not None and select in self._items:
            self.tree.setCurrentItem(self._items[select])

    def _watch_health(self) -> None:
        """Keep the tray up to date, and say so when something that worked breaks."""
        now: dict[tuple, tuple] = {}  # key -> (colour, headline, explanation, what to select)
        for s in self.servers.values():
            color, word, sentence = _server_state(s)
            if s.origin == "service":
                now["service", s.name] = (color, f"{service.PREFIX}{s.name}: {word}", sentence, ("server", s.key))
                continue
            now["server", s.key] = (color, f"{s.name}: {word}", sentence, ("server", s.key))
            if s.service:
                now["service", s.service["name"]] = (_GOOD, "", "", None)
            for name in s.probe["devices"] if s.probe else ():
                color, word, sentence = _device_state(s.device(name))
                now["device", s.key, name] = (color, f"{name} on {s.name}: {word}", sentence, ("device", s.key, name))
        if self.tray is not None:
            for key, (color, headline, sentence, select) in now.items():
                if color == _BAD and self._health.get(key) in (_GOOD, _BUSY):  # it worked until just now
                    self.tray.alert(headline, sentence, select)
            problems = [headline for color, headline, _, _ in now.values() if color == _BAD]
            agents = [f"{name} on {', '.join(servers)}" for name, servers in self.agents().values()]
            self.tray.refresh(sorted(self.servers.values(), key=lambda s: (_ORIGIN_ORDER[s.origin], s.name.lower())),
                              problems, agents)
        self._health = {key: value[0] for key, value in now.items()}

    def showEvent(self, event) -> None:
        self._timer.setInterval(self._refresh_ms)
        self.refresh()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._timer.setInterval(max(self._refresh_ms, HIDDEN_REFRESH_MS))  # only the tray is looking
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        if self.tray is not None and not self._quitting:
            event.ignore()  # to the tray; the window keeps watching
            self.hide()
            self.tray.hint_once()
            return
        self._shutdown()
        super().closeEvent(event)

    def quit(self) -> None:
        """Close the window for good (the servers keep running)."""
        self._quitting = True
        self._shutdown()
        self.close()
        app = QtWidgets.QApplication.instance()
        if app is not None and self.tray is not None:  # without a tray, closing the last window quits by itself
            app.quit()

    def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        if self.tray is not None:
            self.tray.hide()
        for dialog in self._logs.values():
            dialog.close()
        self.probes.shutdown(wait=False, cancel_futures=True)
        self.actions.shutdown(wait=False, cancel_futures=True)
        threading.Thread(target=self.backend.close, daemon=True).start()


# ---------------------------------------------------------------- the tray icon
_LIGHTS = {"good": "#33d17a", "bad": "#ed333b"}  # the desktop's own green and red
_NODES = ((3.2, 3.2), (12.8, 3.2), (3.2, 12.8), (12.8, 12.8))


def _tray_icon(status: str | None) -> QtGui.QIcon:
    """A symbolic hub, white line art like the other icons of the top bar.  The
    lower-right node doubles as a status light: green while all is well, red
    while something is broken, absent while no server runs."""
    icon = QtGui.QIcon()
    ink = QtGui.QColor("#f2f2f2")
    for size in (16, 22, 24, 32, 48, 64):  # drawn at each size, so that it stays crisp
        pix = QtGui.QPixmap(size, size)
        pix.fill(QtGui.QColor(0, 0, 0, 0))
        p = QtGui.QPainter(pix)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.scale(size / 16, size / 16)  # designed on a 16 x 16 grid
        pen = QtGui.QPen(ink)
        pen.setWidthF(1.5)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for x, y in _NODES:
            p.drawLine(QtCore.QPointF(8, 8), QtCore.QPointF(x, y))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawEllipse(QtCore.QPointF(8, 8), 2.7, 2.7)
        for x, y in _NODES:
            p.drawEllipse(QtCore.QPointF(x, y), 1.9, 1.9)
        if status in _LIGHTS:
            light = QtCore.QPointF(12.4, 12.4)
            p.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Clear)  # a gap around the light
            p.drawEllipse(light, 4.4, 4.4)
            p.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setBrush(QtGui.QColor(_LIGHTS[status]))
            p.drawEllipse(light, 3.2, 3.2)
        p.end()
        icon.addPixmap(pix)
    return icon


class _Tray(QtWidgets.QSystemTrayIcon):
    """The icon in the top bar: how things are at a glance, a menu, and a
    notification when something that worked breaks."""

    def __init__(self, window: Manager):
        super().__init__(window)
        self.window = window
        self.settings = QtCore.QSettings("insthub", "gui")
        self._icons = {status: _tray_icon(status) for status in (None, "good", "bad")}
        self._logo = QtGui.QIcon(str(launcher.ICON))  # for notifications, which show it large
        self.status: str | None = None
        self._rows: tuple | None = None
        self._clicked: tuple | None = None  # what the last notification was about
        self.menu = QtWidgets.QMenu()
        self.setContextMenu(self.menu)
        self.setIcon(self._icons[None])
        self.setToolTip("insthub")
        self.activated.connect(self._activated)
        self.messageClicked.connect(lambda: window.bring_up(self._clicked))
        self.refresh([], [], [])
        self.show()

    def _activated(self, reason) -> None:
        if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger:  # a left click, where the desktop sends one
            if self.window.isVisible() and self.window.isActiveWindow():
                self.window.hide()
            else:
                self.window.bring_up()

    @property
    def notify(self) -> bool:
        return self.settings.value("notify", True, type=bool)

    def refresh(self, servers: list[_Server], problems: list[str], agents: list[str]) -> None:
        running = sum(1 for s in servers if s.origin != "service")
        status = "bad" if problems else ("good" if running else None)
        if status != self.status:
            self.status = status
            self.setIcon(self._icons[status])
        devices = sum(len(s.probe["devices"]) for s in servers if s.probe)
        tip = f"insthub: {running} server{'s' * (running != 1)}, {devices} device{'s' * (devices != 1)}"
        self.setToolTip(tip + "".join(f"\n{line}" for line in (*problems[:6], *agents)))
        rows = tuple((s.key, s.name, *_server_state(s)[:2], len(s.probe["devices"]) if s.probe else None)
                     for s in servers)
        if (rows, agents) != self._rows:  # rebuild only on a change: the menu may be open
            self._rows = rows, agents
            self._build(rows, agents)

    def _build(self, rows: tuple, agents: list[str]) -> None:
        menu = self.menu
        menu.clear()
        menu.addAction("Show insthub").triggered.connect(lambda: self.window.bring_up())
        menu.addSeparator()
        for key, name, color, word, devices in rows[:12]:
            count = f" · {devices} device{'s' * (devices != 1)}" if devices is not None else ""
            action = menu.addAction(_dot(color), f"{name}: {word}{count}")
            action.triggered.connect(lambda _=False, key=key: self.window.bring_up(("server", key)))
        if not rows:
            menu.addAction("No server is running").setEnabled(False)
        for agent in agents:
            menu.addAction(agent).triggered.connect(lambda: self.window.agents_dialog())
        menu.addSeparator()
        notify = menu.addAction("Notify me when something breaks")
        notify.setCheckable(True)
        notify.setChecked(self.notify)
        notify.toggled.connect(lambda on: self.settings.setValue("notify", on))
        if sys.platform.startswith("linux"):
            login = menu.addAction("Start at login, in the tray")
            login.setCheckable(True)
            login.setChecked(launcher.autostart())
            login.toggled.connect(lambda on: launcher.set_autostart(on))
        menu.addSeparator()
        menu.addAction("Quit (servers keep running)").triggered.connect(lambda: self.window.quit())

    def alert(self, headline: str, sentence: str, select: tuple | None) -> None:
        if self.notify:
            self._clicked = select
            self.showMessage(headline, sentence, self._logo, 10000)

    def hint_once(self) -> None:
        """The first time the window is closed, say where it went."""
        if not self.settings.value("told-about-tray", False, type=bool):
            self.settings.setValue("told-about-tray", True)
            self.showMessage("insthub is still here", "It keeps watching from the tray icon. Your servers run "
                             "on regardless; Quit in the tray menu closes the window for good.",
                             self._logo, 8000)


# ---------------------------------------------------------------- starting
def _single_instance(tray_only: bool):
    """Make sure only one window runs: if one does already, bring it up (unless
    started for the tray only) and return None; else return the listener that
    later starts will reach."""
    import importlib

    from .qt import BINDING

    network = importlib.import_module(f"{BINDING}.QtNetwork")
    name = f"insthub-gui-{os.environ.get('USERNAME', 'user')}" if os.name == "nt" \
        else str(registry.runtime_dir() / ".gui.sock")  # a leading dot: never a server's socket name
    probe = network.QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(500):
        if not tray_only:
            probe.write(b"show\n")
            probe.waitForBytesWritten(500)
        probe.disconnectFromServer()
        return None
    network.QLocalServer.removeServer(name)  # left behind by a window that crashed
    listener = network.QLocalServer()
    listener.listen(name)  # if this fails (e.g. the path is too long), run anyway, just without the check
    return listener


def main(tray_only: bool = False) -> int:
    """Open the manager window and run until it is closed (or, with a tray, until Quit)."""
    app = application()
    app.setApplicationName("insthub")
    app.setDesktopFileName(launcher.APP_ID)  # how the desktop matches the window to its launcher icon
    app.setWindowIcon(QtGui.QIcon(str(launcher.ICON)))
    listener = _single_instance(tray_only)
    if listener is None:
        return 0  # already running: it has been brought up
    tray = QtWidgets.QSystemTrayIcon.isSystemTrayAvailable()
    app.setQuitOnLastWindowClosed(not tray)
    window = Manager(tray=tray)

    def knocked():  # another `insthub gui` was started: it wants this window shown
        while (sock := listener.nextPendingConnection()) is not None:
            def read(sock=sock):
                if sock.readAll():
                    window.bring_up()

            sock.readyRead.connect(read)
            sock.disconnected.connect(sock.deleteLater)
            read()  # what arrived before the connection above was made

    listener.newConnection.connect(knocked)
    if not (tray_only and tray):
        window.show()
    return run(app)
