"""The manager window (`insthub gui`): the servers and devices on this computer,
how each one is reached, and the controls to look after them and set them up.

    sidebar      Devices, Servers, AI agents, Settings; how many of each, what is broken
    Devices      a tile per device, in a section per server: its status, a small live
                 preview of what it publishes (else the figures of its state), its streams
    a device     is the instrument connected (and if not, why), what it is doing, which
                 programs watch it; its properties to read and change, its streams, its
                 periodic tasks (a switch each), its state, and its commands to run.
                 Connect now / Release / Pause and Resume / Live view / Open <a UI of its
                 extension> (a menu of them, for several) / Edit in its config.
    Servers      a card per server: running here, installed as a service, or on another
                 computer (devices.toml)
    a server     its process, addresses, service, devices and the programs connected to it.
                 Start / Stop / Restart / Logs / Remove; Starts at login; Edit its config
                 (forms.ServerDialog, which changes the file in place, comments kept).
    AI agents    giving Claude Code or another agent the instruments; which are connected
    Settings     appearance, previews, notifications, start at login, the files insthub uses,
                 your extensions (folders added and removed; what is wrong with each)
    Add          a new server (its config file is written for you; the driver list offers
                 the devices of your extensions), a server file to keep
                 running as a service (at login or not), devices on another computer

It is made of four layers, each knowing only the ones below it:

    manager.py    this window: the refresh loop, navigation, actions, the tray, main()
    pages.py      what each page shows          forms.py    the dialogs that set things up
    inventory.py  what is known and how it is found out (no Qt)
    widgets.py    the look: colours, icons, tiles (knows nothing of insthub)

The GUI thread never waits for a server: discovery, probes and actions run on
worker threads and come back through `qt.deliver`.  The window is an ordinary
client and needs only Qt.  It never subscribes to a stream: a tile's preview is
the stream's last frame (`$latest`), so watching does not make an instrument
acquire.  Live plots open as a separate `insthub view` process, and a device's
UIs (from its extension's manifest) as programs run with the extension's Python:
the window never imports an extension's code.  Their output goes to log files,
shown when one fails.  `insthub gui --install` adds the window to the
applications menu (see launcher.py).
"""
from __future__ import annotations

import collections
import importlib.util
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import extensions, forms, launcher, pages, registry, service
from . import widgets as ui
from .client import PROCESS_TOKEN, log_text
from .device import busy_with
from .inventory import (
    BAD,
    BUSY,
    GOOD,
    IDLE,
    PREVIEW_LIMIT,
    Backend,
    Server,
    clock,
    device_state,
    elements,
    paused_by,
    paused_here,
    preview_stream,
    server_file,
    server_log,
    server_state,
    spawn,
    tail,
    terminate,
    ui_log,
)
from .qt import BINDING, QtCore, QtGui, QtWidgets, application, deliver, run

REFRESH_MS = 1000
HIDDEN_REFRESH_MS = 3000  #: while only the tray icon is watching
HISTORY = 90  #: the values the preview of a number remembers
UIS_FRESH = 30.0  #: seconds what a device's extension offers is believed before it is looked up again

_ORIGIN_ORDER = {"local": 0, "service": 1, "remote": 2}
QAction = getattr(QtGui, "QAction", None) or QtWidgets.QAction  # QtWidgets in Qt 5
Qt = QtCore.Qt


def _qimage(a: np.ndarray) -> QtGui.QImage:
    h, w = a.shape[:2]
    if a.ndim == 2:
        image = QtGui.QImage(a.tobytes(), w, h, w, QtGui.QImage.Format.Format_Grayscale8)
    else:
        image = QtGui.QImage(a.tobytes(), w, h, 3 * w, QtGui.QImage.Format.Format_RGB888)
    return image.copy()  # its own pixels: the bytes above are gone after this


_icons: dict = {}


def _dot(status: str) -> QtGui.QIcon:
    """A status dot (for the tray menu): filled, or a ring for 'nothing to report'."""
    icon = _icons.get(status)
    if icon is None:
        pix = QtGui.QPixmap(28, 28)
        pix.setDevicePixelRatio(2.0)
        pix.fill(QtGui.QColor(0, 0, 0, 0))
        p = QtGui.QPainter(pix)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        ink = QtGui.QColor(ui.STATUS[status])
        if status == IDLE:
            p.setPen(QtGui.QPen(ink, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(ink)
        p.drawEllipse(QtCore.QRectF(2.5, 2.5, 9, 9))
        p.end()
        icon = _icons[status] = QtGui.QIcon(pix)
    return icon


# ---------------------------------------------------------------- the window
class Manager(QtWidgets.QMainWindow):
    """The window.  With `tray=True` it also has a tray icon, and closing the
    window only hides it: it keeps watching from the tray until Quit.
    `style_app`: also give the application the window's palette (when the
    window is the whole program, as with `insthub gui`)."""

    def __init__(self, backend: Backend | None = None, refresh_ms: int = REFRESH_MS, tray: bool = False,
                 style_app: bool = False):
        super().__init__()
        self.backend = backend or Backend()
        self.settings = QtCore.QSettings("insthub", "gui")
        self._refresh_ms = refresh_ms
        self._style_app = style_app
        self._quitting = False
        self._health: dict[tuple, str] = {}  # what was fine and what was broken at the last look
        self._agents_told: set[int] = set()  # agents (by pid) the tray has announced
        self.probes = ThreadPoolExecutor(8, thread_name_prefix="insthub-gui-probe")
        self.actions = ThreadPoolExecutor(4, thread_name_prefix="insthub-gui-action")
        self.servers: dict[str, Server] = {}
        self._scanning = False
        self._scanned = False
        self._closed = False
        self._systemd = False
        self._remote_error: str | None = None  # devices.toml cannot be read
        self._local_ids: set[tuple] = set()  # registry.identity() of the servers here, at the last scan
        self._here: dict[str, tuple] = {}  # address in devices.toml -> identity of the server here it reached
        self._spawned: list = []  # programs this window started: servers, windows for devices (inventory.Launched)
        self._uis: dict[str, tuple] = {}  # server address -> what its devices can open here (see _look_up_uis)
        self._uis_asking: set[str] = set()  # server addresses whose devices' UIs a worker is finding out
        self._logs: dict[str, pages.LogDialog] = {}
        self._detail: tuple | None = None  # ("device", key, name) or ("server", key) while one is shown
        self.login_pending: dict[str, bool] = {}  # service -> the Starts at login asked, until it is done
        self.restarting: dict[str, dict] = {}  # server key -> a restart under way (see restart_server)
        self._follow: tuple | None = None  # (server key, device or None): the page to show again once its restart is over
        self._back: tuple | str = "devices"  # where the device page goes back to

        self.setWindowTitle("insthub")
        self.resize(1220, 800)
        self.setMinimumSize(780, 540)
        ui.use(ui.theme(str(self.settings.value("theme", "system"))))  # before any widget picks a colour
        self._build_add_menu()
        self.sidebar = pages.Sidebar(self)
        self.summary = self.sidebar.summary
        self.devices_page = pages.DevicesPage(self)
        self.servers_page = pages.ServersPage(self)
        self.device_page = pages.DevicePage(self)
        self.server_page = pages.ServerPage(self)
        self.agents_page = pages.AgentsPage(self)
        self.settings_page = pages.SettingsPage(self)
        self.pages = QtWidgets.QStackedWidget()
        for page in (self.devices_page, self.servers_page, self.device_page, self.server_page, self.agents_page,
                     self.settings_page):
            self.pages.addWidget(page)
        central = QtWidgets.QWidget()
        frame = QtWidgets.QHBoxLayout(central)
        frame.setContentsMargins(0, 0, 0, 0)
        frame.setSpacing(0)
        frame.addWidget(self.sidebar)
        frame.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        self.toast = ui.Toast(central)
        for keys, slot in ((QtGui.QKeySequence.StandardKey.Refresh, self.refresh),
                           (QtGui.QKeySequence.StandardKey.Quit, self.quit),
                           (QtGui.QKeySequence.StandardKey.Find, self.find),
                           (QtGui.QKeySequence.StandardKey.Back, self.go_back), ("Esc", self.go_back)):
            action = QAction(self)
            action.setShortcut(QtGui.QKeySequence(keys))
            action.triggered.connect(lambda *_, slot=slot: slot())
            self.addAction(action)

        self.tray = _Tray(self) if tray else None
        self.apply_theme()
        hints = QtWidgets.QApplication.instance().styleHints()
        if hasattr(hints, "colorSchemeChanged"):  # Qt 6.5+: follow the desktop when it switches
            hints.colorSchemeChanged.connect(self._desktop_switched)
        self.show_page("devices")
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(refresh_ms)
        self.refresh()

    def _build_add_menu(self) -> None:
        menu = self.add_menu = QtWidgets.QMenu(self)
        menu.setToolTipsVisible(True)

        def item(icon, text, tip, slot):
            action = menu.addAction(ui.icon(icon), text)
            action.setToolTip(tip)
            action.triggered.connect(lambda *_: slot())
            return action

        self.new_server = item("plus", "New server...", "Choose drivers for your instruments; insthub writes the "
                               "config file and starts the server", self.new_server_dialog)
        self.add_to_server = item("chip", "Add a device to a server...", "Another instrument in a server that runs "
                                  "from a .toml config: written into it, then the server restarts",
                                  self.add_device_dialog)
        self.add_service = item("servers", "Keep a server running...", "Run an existing .toml config or .py "
                                "server in the background, at login or only when you start it (systemd user "
                                "service)", self.add_service_dialog)
        self.add_remote = item("globe", "Devices on another computer...", "Ask a server on another computer which "
                               "devices it has and list them in devices.toml", self.remote_dialog)
        menu.addSeparator()
        self.add_sim = item("live", "Simulated instruments", "Start insthub sim: a scope, a generator, a camera and "
                            "a spectrum analyzer, without hardware", self.start_simulator)

    def add_button(self) -> QtWidgets.QPushButton:
        button = ui.button("Add", "plus", kind="primary", tip="A new server, a background service, or devices on "
                           "another computer")
        button.setMenu(self.add_menu)
        return button

    @property
    def previews_on(self) -> bool:
        return self.settings.value("previews", True, type=bool)

    def _desktop_switched(self, *_) -> None:
        if not self._closed and self.settings.value("theme", "system") == "system":
            self.apply_theme()

    def apply_theme(self) -> None:
        """Colour the window as the settings (or the desktop) say, and redraw it."""
        t = ui.theme(str(self.settings.value("theme", "system")))
        ui.use(t)
        size = self.font().pointSizeF()
        self.setStyleSheet(ui.stylesheet(t, size if size > 0 else 10.0))
        if self._style_app:
            QtWidgets.QApplication.instance().setPalette(ui.palette(t))
        for widget in self.findChildren(QtWidgets.QWidget):
            widget.update()
        self.render()

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

    def say(self, text: str, ms: int = 5000) -> None:
        """A short message at the bottom of the window (ms=0: until the next one)."""
        self.toast.show_message(text, ms)

    def _report(self, exc) -> None:
        self.say(ui.error_text(exc), 8000)

    def act(self, doing: str, fn, done, after=None) -> None:
        """A user's action: say what is happening, then what came of it.  `done` is the
        message (or makes it from the result); `after()` runs once it is over, either way."""
        self.say(f"{doing}...", 0)

        def ok(result):
            self.say(done if not callable(done) else done(result), 6000)
            self.refresh()
            if after is not None:
                after()

        def failed(exc):
            self.toast.hide()
            QtWidgets.QMessageBox.warning(self, "insthub", f"{doing} failed.\n\n{exc}")
            self.refresh()
            if after is not None:
                after()

        self.submit(self.actions, fn, ok, failed)

    def confirm(self, title: str, text: str, action: str) -> bool:
        return ui.confirm(self, title, text, action)

    def copy(self, text: str) -> None:
        QtWidgets.QApplication.clipboard().setText(text)
        self.say("Copied", 2500)

    # -- looking -------------------------------------------------------------
    def refresh(self) -> None:
        ended = [p for p in self._spawned if not p.running]  # (once: one that ends meanwhile is seen next time)
        self._spawned = [p for p in self._spawned if p not in ended]
        for p in ended:  # a server or a window this window started that failed (at start, often) says why
            failure = p.failure()
            if failure:
                self.say(f"{p.title} ended with an error: {failure.splitlines()[-1]} (its log: {p.log})", 15000)
                box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Warning, "insthub",
                                            f"{p.title} ended with an error.", QtWidgets.QMessageBox.StandardButton.Ok,
                                            self)  # all of what it said at the end (a config's every problem)
                box.setInformativeText(f"{failure}\n\nIts log: {p.log}")
                box.setModal(False)
                box.show()
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
        fresh: dict[str, Server] = {}
        taken, local_addresses = set(), set()
        for info in found["servers"]:
            key = f"local:{info['server']}"
            s = self.servers.get(key) or Server(key, info["server"], "local")
            if s.pid not in (None, info.get("pid")):  # restarted: what we knew is stale
                s.forget_live_data()
            s.pid, s.started = info.get("pid"), info.get("started")
            s.addresses, s.declared = list(info.get("addresses", ())), list(info.get("devices", ()))
            s.python = info["python"] if isinstance(info.get("python"), str) else None  # (another insthub's: none)
            s.entry = info
            s.service = by_pid.get(s.pid)
            if s.service:
                taken.add(s.service["name"])
            fresh[key] = s
            local_addresses.update(s.addresses)
        self._local_ids = {registry.identity(info) for info in found["servers"]}
        for u in units or ():
            if u["name"] not in taken:
                key = f"service:{u['name']}"
                s = self.servers.get(key) or Server(key, u["name"], "service")
                s.service = u
                fresh[key] = s
        for address, names in found["remote"].items():
            if address in local_addresses or self._here.get(address) in self._local_ids:
                continue  # a server of this computer, which has its card (devices.toml may name it by an IP of ours)
            key = f"remote:{address}"
            s = self.servers.get(key) or Server(key, address.partition("://")[2], "remote", [address])
            s.declared = sorted(names)
            fresh[key] = s
        for key, old in self.servers.items():
            if old.address and (key not in fresh or fresh[key].address != old.address):
                self._forget_address(old.address)
        self.servers = fresh
        self.add_service.setEnabled(self._systemd)
        self.add_sim.setEnabled(not any(s.name == "sim" for s in fresh.values() if s.origin == "local"))
        self.render()
        self._watch_health()
        for s in fresh.values():
            if s.address and not s.probing and s.origin != "service":
                self._probe(s)

    def _forget_address(self, address: str) -> None:
        """Close the connection to an address no card shows any more, and forget its devices' UIs."""
        self.submit(self.actions, lambda: self.backend.forget(address), lambda _: None)
        self._uis.pop(address, None)

    def _probe(self, s: Server) -> None:
        s.probing = True
        address, entry = s.address, s.entry

        def ask():  # (on a worker) what it says, and why it runs older code than its folders hold, if it does
            result = self.backend.probe(address)
            return {**result, "outdated": extensions.outdated(entry, result["devices"])}

        def done(result):
            s.probing = False
            if self.servers.get(s.key) is not s:
                return
            if s.origin == "remote" and registry.identity(result["info"]) in self._local_ids:
                self._here[address] = registry.identity(result["info"])  # a server of this computer after all
                self.servers.pop(s.key)
                self._forget_address(address)
                self.render()
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
            self._look_up_uis(s)  # its devices' UIs found out before a right-click on a tile asks for them
            self._fetch_previews(s)
            self._changed(s)

        def failed(exc):
            s.probing = False
            if self.servers.get(s.key) is not s:
                return
            s.probe, s.error, s.rates = None, str(exc) or type(exc).__name__, {}
            self._changed(s)

        self.submit(self.probes, ask, done, failed)

    def _fetch_previews(self, s: Server) -> None:
        """Fetch the last frame of each device's first stream, where a new one was published."""
        if not self.previews_on or not self.isVisible() or not s.address or not s.probe:
            return
        limit = PREVIEW_LIMIT // 8 if s.address.startswith("tcp://") else PREVIEW_LIMIT
        for name, d in s.probe["devices"].items():
            stream = preview_stream(d)
            if stream is None:
                s.previews.pop(name, None)
                continue
            st = d["streams"][stream]
            if name in s.fetching or s.shown.get(name) == (stream, st["seq"]) or elements(st["kind"]) > limit:
                continue
            s.fetching.add(name)

            def done(result, name=name, stream=stream):
                s.fetching.discard(name)
                if result is None or self.servers.get(s.key) is not s:
                    return
                seq, stamp, kind, data, unit = result
                history = ()
                if kind == "number":
                    values = s.history.setdefault((name, stream), collections.deque(maxlen=HISTORY))
                    values.append(data)
                    history = tuple(values)
                s.previews[name] = ui.Preview(kind, _qimage(data) if kind == "image" else data, unit, stream,
                                              stamp, history)
                s.shown[name] = (stream, seq)
                self._changed(s, health=False)

            def failed(exc, name=name):
                s.fetching.discard(name)

            self.submit(self.probes, lambda a=s.address, n=name, st=stream: self.backend.latest(a, n, st),
                        done, failed)

    def _changed(self, s: Server, health: bool = True) -> None:
        self.render()
        if health:
            self._watch_health()

    # -- showing -------------------------------------------------------------
    def _ordered(self) -> list[Server]:
        return sorted(self.servers.values(), key=lambda s: (_ORIGIN_ORDER[s.origin], s.name.lower()))

    def render(self) -> None:
        """Bring the sidebar and the page on show up to date (the others catch up when shown)."""
        servers = self._ordered()
        page = self.pages.currentWidget()
        for key, mark in list(self.restarting.items()):  # a restart is over once its new process is seen
            proc, back = mark["proc"], self._restarted(mark)
            if back is None and not (proc is not None and not proc.running):
                continue
            del self.restarting[key]  # (a new process that ended at once: refresh() says why)
            if back is None:
                continue
            follow, self._follow = (self._follow, None) if self._follow and self._follow[0] == key else (None, self._follow)
            if follow is not None and page is (self.servers_page if follow[1] is None else self.devices_page):
                if follow[1] is not None and follow[1] in self.servers[back].device_names():
                    self.open_device(back, follow[1])  # its page again, where the restart left it (renders)
                else:
                    self.open_server(back)
                return
            if self._detail and self._detail[1] == key and back != key:  # on show, under a new key (a service
                self._detail = (self._detail[0], back, *self._detail[2:])  # started, a rename): it follows
        self.sidebar.sync(servers, len(self.agents()), self._scanned)
        if page is self.devices_page:
            self.devices_page.sync(servers, self._scanned, self.previews_on)
        elif page is self.servers_page:
            self.servers_page.sync(servers, self._scanned)
        elif page is self.device_page:
            s = self.servers.get(self._detail[1])
            if s is None or self._detail[2] not in s.device_names():
                key, name = self._detail[1], self._detail[2]
                self.show_page("devices")
                if key in self.restarting:  # it comes back on its own once the server is up again
                    self._follow = (key, name)
            else:
                self.device_page.show_device(s, self._detail[2])
        elif page is self.server_page:
            s = self.servers.get(self._detail[1])
            if s is None:
                key = self._detail[1]
                self.show_page("servers")
                if key in self.restarting:  # it comes back on its own once the server is up again
                    self._follow = (key, None)
            else:
                self.server_page.show_server(s)
        elif page is self.agents_page:
            self.agents_page.show_connected()

    def show_page(self, name: str) -> None:
        """One of the pages of the sidebar: "devices", "servers", "agents" or "settings"."""
        self._detail, self._follow = None, None  # (another page chosen: no restart takes it back)
        self.pages.setCurrentWidget({"devices": self.devices_page, "servers": self.servers_page,
                                     "agents": self.agents_page, "settings": self.settings_page}[name])
        self.sidebar.select(name)
        if name == "agents":
            self.agents_page.check()
        elif name == "settings":
            self.settings_page.sync()
        self.render()

    def open_device(self, key: str, name: str) -> None:
        s = self.servers.get(key)
        if s is None:
            return
        self._follow = None
        if s.address in self._uis:  # its UIs looked up again (manifests and your list of extensions change)
            self._uis[s.address] = -math.inf, *self._uis[s.address][1:]
        from_server = self.pages.currentWidget() is self.server_page
        self._back = ("server", key) if from_server else "devices"
        self._detail = ("device", key, name)
        self.device_page.header.back.setText(s.name if from_server else "Devices")
        self.pages.setCurrentWidget(self.device_page)
        self.sidebar.select("servers" if from_server else "devices")
        self.render()

    def _restarted(self, mark: dict) -> str | None:
        """The key of the server a restart started, once a scan sees it (another pid than before;
        under its key, or the new process's pid, or its service's name)."""
        for key, s in self.servers.items():
            if s.origin != "local" or s.pid is None or s.pid == mark["old"]:
                continue
            if key == mark["key"] or (mark["proc"] is not None and s.pid == mark["proc"].pid) or \
                    (mark["unit"] is not None and (s.service or {}).get("name") == mark["unit"]):
                return key
        return None

    def open_server(self, key: str) -> None:
        if key not in self.servers:
            return
        self._follow = None
        self._detail = ("server", key)
        self.pages.setCurrentWidget(self.server_page)
        self.sidebar.select("servers")
        self.render()

    def go_back(self) -> None:
        if self._detail is None:
            return
        if self._detail[0] == "device" and self._back != "devices" and self._back[1] in self.servers:
            self.open_server(self._back[1])
        else:
            self.show_page("devices" if self._detail[0] == "device" else "servers")

    def _selected(self) -> tuple | None:
        """What is shown: ("device", server key, name), ("server", key), or None for a list."""
        return self._detail

    def find(self) -> None:
        self.show_page("devices")
        self.devices_page.search.setFocus()
        self.devices_page.search.selectAll()

    def agents(self) -> dict[int, tuple[str, list[str]]]:
        """The AI agents connected right now: pid -> (name, the servers it uses)."""
        found: dict[int, tuple[str, list[str]]] = {}
        for s in self.servers.values():
            for c in (s.probe["clients"] if s.probe else None) or ():
                if c["agent"]:
                    found.setdefault(c["pid"], (c["program"], []))[1].append(s.name)
        return found

    def device_menu(self, key: str, name: str, pos) -> None:
        s = self.servers.get(key)
        if s is None:
            return
        d = s.device(name)
        connected = bool(d and d["status"]["connected"])
        paused = bool(d and d["status"].get("paused"))
        menu = QtWidgets.QMenu(self)
        menu.setToolTipsVisible(True)

        def item(icon, text, slot, enabled=True, tip=""):
            action = menu.addAction(ui.icon(icon), text)
            action.setEnabled(enabled)
            action.setToolTip(tip)
            action.triggered.connect(lambda *_: slot())

        item("forward", "Open", lambda: self.open_device(key, name))
        live_ok, why = self.can_view(s, d)
        item("live", "Live view", lambda: self.live_view(s, name), live_ok, why)
        ext, uis, _ = self.device_uis(s, name) or (None, [], None)
        open_ok, why = self.can_open(s)
        for ui_ in uis:
            item("window", f"Open {ui_.title}", lambda ui_=ui_: self.open_ui(s, name, ext, ui_), open_ok,
                 why or f"Its extension's {ui_.kind}, in a window of its own")
        menu.addSeparator()
        can_pause, why = self.can_pause(d)
        if paused:
            item("start", "Resume", lambda: self.device_action(s, name, "$resume"), tip=pages.RESUME_TIP)
        else:
            item("pause", "Pause", lambda: self.device_action(s, name, "$pause"), can_pause, why or pages.PAUSE_TIP)
        item("power", "Connect now", lambda: self.device_action(s, name, "$open"),
             d is not None and not connected and not paused)
        item("release", "Release", lambda: self.device_action(s, name, "$release"), connected, pages.RELEASE_TIP)
        menu.addSeparator()
        config = server_file(s)
        if config is not None and config.suffix == ".toml":
            item("edit", f"Edit in {config.name}...", lambda: self.edit_config(key, name))
        item("copy", "Copy its name", lambda: self.copy(name))
        menu.exec(pos)

    def server_menu(self, key: str, pos) -> None:
        s = self.servers.get(key)
        if s is None:
            return
        menu = QtWidgets.QMenu(self)
        menu.addAction(ui.icon("forward"), "Open").triggered.connect(lambda *_: self.open_server(key))
        running, u = s.origin == "local", s.service
        if u and not running:
            menu.addAction(ui.icon("start"), "Start").triggered.connect(lambda *_: self.service_action(s, "start"))
        if running and (u or self.started_here(s.pid)):  # a service, or a server this window started
            menu.addAction(ui.icon("refresh"), "Restart").triggered.connect(
                lambda *_: self.restart_server(s, ask=True))
        if u and running:
            menu.addAction(ui.icon("stop"), "Stop").triggered.connect(lambda *_: self.service_action(s, "stop"))
        elif running and s.pid and s.pid != os.getpid():
            menu.addAction(ui.icon("stop"), "Stop").triggered.connect(lambda *_: self.stop_server(s))
        if self.log_source(s):
            menu.addAction(ui.icon("logs"), "Logs").triggered.connect(lambda *_: self.show_logs(s))
        config = server_file(s)
        if config is not None and config.suffix == ".toml":
            menu.addSeparator()
            menu.addAction(ui.icon("edit"), f"Edit {config.name}...").triggered.connect(
                lambda *_: self.edit_config(key))
        menu.exec(pos)

    # -- managing ------------------------------------------------------------
    def show_agents(self) -> None:
        self.show_page("agents")

    def device_action(self, s: Server, device: str, method: str) -> None:
        address = s.address
        doing = {"$open": f"Connecting {device}", "$release": f"Releasing {device}", "$pause": f"Pausing {device}",
                 "$resume": f"Resuming {device}"}[method]
        d = s.device(device) or {"status": {}}
        st = d["status"]
        if method == "$resume" and st.get("paused") and not paused_here(st["paused"], s.origin == "local"):
            who = paused_by(st["paused"], s.origin == "local")
            if not self.confirm(f"Resume {device}?", f"{who} paused {device} at {clock(st['paused']['since'])}, "
                                "perhaps to use the instrument by hand. Resume it anyway? Its commands run again, "
                                "and it reconnects if it was connected.", "Resume"):
                return
        if method == "$pause":
            users = sorted({c["program"] for c in (s.probe or {}).get("clients") or ()
                            if any(sub.startswith(device + "/") for sub in c.get("subscriptions", ()))})
            busy = busy_with(st) if st.get("busy") != "$pause" else None
            if (users or busy) and not self.confirm(
                    f"Pause {device}?", (f"{', '.join(users)} {'is' if len(users) == 1 else 'are'} watching it. "
                                         if users else "") + (f"It is busy with {busy}; the pause waits for it. "
                                                              if busy else "") +
                    "While it is paused, every program's calls to it fail. Pause it?", "Pause"):
                return
        def call():
            if method not in ("$pause", "$resume"):
                return self.backend.call(address, device, method)
            args = ("insthub gui", PROCESS_TOKEN) if method == "$pause" else ()
            try:  # its reply waits for the command under way: do not wait for a long one here
                return self.backend.call(address, device, method, *args, timeout=5.0)
            except TimeoutError:
                return None  # it takes effect once that command has finished: the tile says so

        def result(status):
            if status is None:
                running = busy_with(st) or "what it is doing (a periodic task, say)"
                return f"{device}: pausing; it is closed once {running} finishes" if method == "$pause" else \
                    f"{device}: resuming once {running} finishes" if busy_with(st) else \
                    f"{device}: resuming; opening the instrument takes a while"
            if status.get("paused"):
                return f"{device}: paused; its commands fail until you resume it"
            if status["connected"]:
                return f"{device}: connected" + (" again" if method == "$resume" else "")
            if status["error"] is None:
                return f"{device}: resumed; it opens on first use" if method == "$resume" else f"{device}: not opened"
            return f"{device}: {status['error']}" if status["error"] == "released" else \
                f"{device}: not connected ({status['error']})"

        self.act(doing, call, result)

    def task_action(self, s: Server, device: str, task: str, on: bool) -> None:
        address = s.address
        self.act(f"{'Starting' if on else 'Stopping'} {device}.{task}",
                 lambda: self.backend.call(address, device, "$task", task, on, None, timeout=10.0),
                 f"{device}.{task}: {'running' if on else 'stopped'}")

    @staticmethod
    def can_open(s: Server) -> tuple[bool, str]:
        """Whether a program of its own (a live view, a UI) can reach the server, and if not, why."""
        if not s.address or s.address.startswith("inproc://"):
            return False, "Only for servers in other programs"
        return True, ""

    def can_view(self, s: Server, d: dict | None = None) -> tuple[bool, str]:
        """Whether Live view makes sense, and if not, why (with `d`: for this device)."""
        reachable, why = self.can_open(s)
        if not reachable:
            return False, why
        if importlib.util.find_spec("pyqtgraph") is None:
            return False, "Live plots need pyqtgraph: pip install 'insthub[gui]'"
        if d is not None and not any(n != "state" for n in d["streams"]):
            return False, "It publishes no streams to plot"
        if d is not None and d["status"].get("paused"):
            return False, "It is paused: it publishes nothing until it is resumed"
        return True, ""

    @staticmethod
    def can_pause(d: dict | None) -> tuple[bool, str]:
        if d is None:
            return False, "Its server does not answer"
        if "paused" not in d["status"]:
            return False, "Its server runs an older insthub, which cannot pause a device"
        return True, ""

    def live_view(self, s: Server, device: str) -> None:
        command = [sys.executable, "-m", "insthub", "-a", s.address, "view", device]
        self._start(f"the live view of {device}",
                    lambda: spawn(command, ui_log(device), f"The live view of {device}"))

    def device_uis(self, s: Server, device: str) -> tuple | None:
        """What a device can open here, as extensions.device_uis() says: (its extension, its UIs, why
        it has none); None until a worker has found out (see _look_up_uis)."""
        d = s.device(device)
        wanted = extensions.named(d) if d else None
        if wanted is None or not s.address:
            return None, [], None
        self._look_up_uis(s)
        _, extension_of, found = self._uis.get(s.address, (None, {}, {}))
        return found.get(device) if extension_of.get(device) == wanted else None

    @staticmethod
    def _with_extensions(s: Server) -> dict[str, dict]:
        """The devices of a server that name an extension (name -> what it said about itself)."""
        devices = s.probe["devices"] if s.probe else {}
        return {name: d for name, d in devices.items() if extensions.named(d)}

    def _look_up_uis(self, s: Server) -> None:
        """Find out on a worker what the devices of a server that name an extension can open here
        (extensions.server_uis: it reads manifests and the registry, and may ask systemd), for all of
        them at once: unless that was found out for the same devices and extensions less than
        UIS_FRESH seconds ago (manifests and your list of extensions change), or is under way.
        What was known stays on show meanwhile; the window renders again if anything changed."""
        address, devices = s.address, self._with_extensions(s)
        extension_of = {name: d["extension"] for name, d in devices.items()}
        asked, known, _ = self._uis.get(address, (-math.inf, None, {}))
        if not devices or address in self._uis_asking or (
                known == extension_of and time.monotonic() - asked <= UIS_FRESH):
            return
        self._uis_asking.add(address)
        asked = time.monotonic()

        def found(result):
            self._uis_asking.discard(address)
            before = self._uis.get(address)
            self._uis[address] = asked, extension_of, result
            if before is None or before[1:] != (extension_of, result):
                self.render()

        self.submit(self.probes, lambda: extensions.server_uis(devices, address), found,
                    lambda exc: found(dict.fromkeys(devices, (None, [], ui.error_text(exc)))))

    def open_ui(self, s: Server, device: str, ext, ui_) -> None:
        """Start a UI of a device's extension (extensions.launch), with the extension's Python."""
        self._start(f"{ui_.title} for {device}", lambda: extensions.launch(ext, ui_.name, device, s.address))

    def _start(self, what: str, start) -> None:
        """Start a window for a device in a program of its own (`start()` -> inventory.Launched); if
        it fails, refresh() shows what it said."""
        try:
            self._spawned.append(start())
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.warning(self, "insthub", f"Cannot open {what}.\n\n{e}")
            return
        self.say(f"Opening {what}...", 4000)

    def stop_server(self, s: Server) -> None:
        if not self.confirm(f"Stop {s.name}?",
                            f"Stop the server “{s.name}”?\n\nIts devices close their instruments, and programs "
                            "using them get errors until it runs again. It is not a service, so nothing starts it "
                            "again by itself.", "Stop"):
            return
        pid = s.pid
        self.act(f"Stopping {s.name}", lambda: terminate(pid), f"{s.name}: asked to stop")

    def service_action(self, s: Server, what: str) -> None:
        name = s.service["name"]
        unit = f"{service.PREFIX}{name}"
        questions = {
            "stop": ("Stop", f"Stop the service {unit}?\n\nIts devices close their instruments. It starts again "
                             "with Start" + (", or the next time your services start (at login, or at boot with "
                                             "lingering)." if s.service.get("enabled") else ".")),
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

    def forget_remote(self, s: Server) -> None:
        names = list(s.declared)
        if not self.confirm("Remove from devices.toml?",
                            f"Stop listing {', '.join(names)} (on {s.address}) in devices.toml?\n\nPrograms on this "
                            "computer then no longer find them by name. The server itself is not affected.",
                            "Remove"):
            return
        self.act("Editing devices.toml", lambda: registry.edit_static_devices(remove=names),
                 lambda path: f"removed from {path}")

    def log_source(self, s: Server) -> tuple | None:
        """Where a server's log is, as (key, title, load: what reads it, on a worker thread): its
        service's journal, else the output of a server this window started, else what the server
        keeps of its own log (the server call "logs": for one started in a terminal, a program that
        hosts its own, one on another computer).  None for a server whose insthub is too old for it."""
        if s.service:
            name = s.service["name"]
            return name, f"{service.PREFIX}{name}: log", lambda: service.journal(name, 500)
        path = server_log(s.name)
        if s.origin == "local" and path.is_file():
            return str(path), f"{s.name}: output", lambda: tail(path)
        address = s.address
        if address and "logs" in ((s.probe or {}).get("info") or {}).get("features", ()):
            return (f"logs:{address}", f"{s.name}: log",
                    lambda: log_text(self.backend.client(address).logs(500)))
        return None

    def show_logs(self, s: Server) -> None:
        source = self.log_source(s)
        if source is None:
            return
        key, title, load = source
        dialog = self._logs.get(key)
        if dialog is None:
            dialog = self._logs[key] = pages.LogDialog(self, title, load)
        dialog.show()
        dialog.raise_()
        dialog.reload()

    def start_server(self, command: list[str], name: str, cwd: str | Path | None = None) -> None:
        """Start a server in the background (it outlives the window; Stop on its page ends it)."""
        try:
            proc = spawn(command, server_log(name), name, cwd)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "insthub", f"Cannot start {name}.\n\n{e}")
            return
        self._spawned.append(proc)
        self.say(f"Starting {name}...", 4000)
        QtCore.QTimer.singleShot(800, self.refresh)

    def restart_server(self, s: Server, after: str = "", ask: bool = False, name: str | None = None) -> None:
        """Start a server again, to use a changed config: its service, or one this window started.
        `ask`: confirm first (a click on Restart; not after Save, which asked already)."""
        done = f"{after}; " if after else ""
        if s.key in self.restarting:
            self.say(f"{s.name} is restarting", 4000)
            return
        proc = next((p for p in self._spawned if p.pid == s.pid and p.running), None)
        if not s.service and proc is None:
            self.say(f"{done}restart {s.name} where you started it, to use the changes", 8000)
            return
        if ask and not self.confirm(f"Restart {s.name}?", f"Restart {s.name}?\n\nPrograms using it get an error "
                                    "for calls under way, then reconnect by themselves.", "Restart"):
            return
        key = s.key
        mark = {"key": key, "old": s.pid, "proc": None, "unit": (s.service or {}).get("name")}
        self.restarting[key] = mark  # one restart at a time; the page comes back once it is over

        def over():
            if self.restarting.get(key) is mark:  # (not a later restart's)
                del self.restarting[key]
                self.render()

        QtCore.QTimer.singleShot(30_000, over)  # it did not come back in time: stop waiting for it
        if s.service:
            name = s.service["name"]
            what = "restart" if s.origin == "local" else "start"
            failed = []

            def run():
                try:
                    getattr(service, what)(name)
                except BaseException:
                    failed.append(True)
                    raise

            self.act(f"{'Restarting' if what == 'restart' else 'Starting'} {service.PREFIX}{name}", run,
                     f"{done}{service.PREFIX}{name} {what}ed", after=lambda: over() if failed else None)
            return
        command, name, cwd = list(proc.process.args), name or s.name, proc.cwd
        proc.quiet = True  # (its end is not news)

        def stop_and_start():  # all on the worker: if the window is quit meanwhile, the server still starts
            proc.process.terminate()  # SIGTERM: the server closes its instruments, then ends
            proc.process.wait(30)
            return spawn(command, server_log(name), name, cwd)

        def started(new):
            self._spawned.append(new)
            mark["proc"] = new
            self.say(f"{done}{name} restarted", 5000)
            QtCore.QTimer.singleShot(800, self.refresh)

        def failed(exc):
            self.say(f"Cannot restart {name}: {ui.error_text(exc)}", 8000)
            over()

        self.say(f"Restarting {name}...", 0)
        self.submit(self.actions, stop_and_start, started, failed)

    def set_login(self, s: Server, on: bool) -> None:
        """Start a service at every login, or no longer."""
        name = s.service["name"]
        if name in self.login_pending:  # the last change is not done yet
            return
        self.login_pending[name] = on  # what the switch shows until systemd has done it (and a scan saw it)
        shown = getattr(self.server_page, "server", None)
        if shown is not None and (shown.service or {}).get("name") == name:
            self.server_page.login.setEnabled(False)  # now, not at the next refresh: no second click meanwhile

        def over():
            if self.login_pending.get(name) == on:
                self.login_pending.pop(name, None)

        failed = []

        def change():
            try:
                (service.enable if on else service.disable)(name)
            except BaseException:
                failed.append(True)
                raise

        def after():  # done: what the switch shows comes from the scans again (at once if it was refused)
            if failed:
                over()
                self.render()
            else:
                QtCore.QTimer.singleShot(2 * self._refresh_ms + 300, over)

        self.act(f"{'Enabling' if on else 'Disabling'} {service.PREFIX}{name} at login", change,
                 f"{service.PREFIX}{name} " + ("starts at every login" if on else "no longer starts at login"),
                 after=after)

    def add_device_dialog(self) -> None:
        """Add a device to a server that runs from a .toml config (which one, if there are several)."""
        editable = [(s, f) for s in self._ordered() if (f := server_file(s)) is not None and f.suffix == ".toml"]
        if not editable:
            QtWidgets.QMessageBox.information(
                self, "Add a device", "No server here runs from a .toml config file that this window can change "
                "(a .py server is changed in its code). New server... makes one.")
            return
        key = editable[0][0].key
        if len(editable) > 1:
            labels = [f"{s.name}   ({f})" for s, f in editable]
            choice, ok = QtWidgets.QInputDialog.getItem(self, "Add a device", "To which server?", labels, 0, False)
            if not ok:
                return
            key = editable[labels.index(choice)][0].key
        self.edit_config(key, add=True)

    def edit_config(self, key: str, select: str | None = None, add: bool = False) -> None:
        """Change a server's config file in the window (its comments are kept); `add`: with a new
        device in it already, to fill in."""
        s = self.servers.get(key)
        path = server_file(s) if s is not None else None
        if path is None or path.suffix != ".toml":
            self.say("Its server does not run from a .toml config file that this computer can open", 6000)
            return
        try:
            dialog = forms.ServerDialog(self, path, server=s, select=select, add=add)
        except (OSError, ValueError, TypeError, UnicodeDecodeError) as e:
            QtWidgets.QMessageBox.warning(self, "insthub", f"Cannot read {path}.\n\n{e}")
            return
        dialog.exec()

    def started_here(self, pid: int | None) -> bool:
        """Whether this window started that process (and it still runs)."""
        return pid is not None and any(p.pid == pid and p.running for p in self._spawned)

    def start_simulator(self) -> None:
        self.start_server([sys.executable, "-m", "insthub", "sim"], "sim")

    def new_server_dialog(self) -> None:
        forms.ServerDialog(self).exec()

    def add_service_dialog(self) -> None:
        forms.ServiceDialog(self).exec()

    def remote_dialog(self) -> None:
        forms.RemoteDialog(self).exec()

    # -- the tray -------------------------------------------------------------
    def bring_up(self, select: tuple | None = None) -> None:
        """Show the window in front, optionally with a server or device shown."""
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        if select is not None and select[1] in self.servers:
            if select[0] == "server":
                self.open_server(select[1])
            else:
                self.open_device(select[1], select[2])

    def _watch_health(self) -> None:
        """Keep the tray up to date, and say so when something that worked breaks."""
        now: dict[tuple, tuple] = {}  # key -> (status, headline, explanation, what to select)
        for s in self.servers.values():
            status, word, sentence = server_state(s)
            if s.origin == "service":
                now["service", s.name] = (status, f"{service.PREFIX}{s.name}: {word}", sentence, ("server", s.key))
                continue
            now["server", s.key] = (status, f"{s.name}: {word}", sentence, ("server", s.key))
            if s.service:
                now["service", s.service["name"]] = (GOOD, "", "", None)
            for name in s.probe["devices"] if s.probe else ():
                status, word, sentence = device_state(s.device(name), s.origin == "local")
                now["device", s.key, name] = (status, f"{name} on {s.name}: {word}", sentence, ("device", s.key, name))
        if self.tray is not None:
            for key, (status, headline, sentence, select) in now.items():
                if status == BAD and self._health.get(key) in (GOOD, BUSY):  # it worked until just now
                    self.tray.alert(headline, sentence, select)
            problems = [headline for status, headline, _, _ in now.values() if status == BAD]
            agents = [f"{name} on {', '.join(servers)}" for name, servers in self.agents().values()]
            self.tray.refresh(self._ordered(), problems, agents)
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
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for x, y in _NODES:
            p.drawLine(QtCore.QPointF(8, 8), QtCore.QPointF(x, y))
        p.setPen(Qt.PenStyle.NoPen)
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

    def refresh(self, servers: list[Server], problems: list[str], agents: list[str]) -> None:
        running = sum(1 for s in servers if s.origin != "service")
        status = "bad" if problems else ("good" if running else None)
        if status != self.status:
            self.status = status
            self.setIcon(self._icons[status])
        devices = sum(len(s.probe["devices"]) for s in servers if s.probe)
        tip = f"insthub: {running} server{'s' * (running != 1)}, {devices} device{'s' * (devices != 1)}"
        self.setToolTip(tip + "".join(f"\n{line}" for line in (*problems[:6], *agents)))
        rows = tuple((s.key, s.name, *server_state(s)[:2], len(s.probe["devices"]) if s.probe else None)
                     for s in servers)
        if (rows, agents) != self._rows:  # rebuild only on a change: the menu may be open
            self._rows = rows, agents
            self._build(rows, agents)

    def _build(self, rows: tuple, agents: list[str]) -> None:
        menu = self.menu
        menu.clear()
        menu.addAction("Show insthub").triggered.connect(lambda: self.window.bring_up())
        menu.addSeparator()
        for key, name, status, word, devices in rows[:12]:
            count = f" · {devices} device{'s' * (devices != 1)}" if devices is not None else ""
            action = menu.addAction(_dot(status), f"{name}: {word}{count}")
            action.triggered.connect(lambda _=False, key=key: self.window.bring_up(("server", key)))
        if not rows:
            menu.addAction("No server is running").setEnabled(False)
        for agent in agents:
            menu.addAction(agent).triggered.connect(lambda: (self.window.bring_up(), self.window.show_agents()))
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
    ui.desktop_dark()  # note the desktop's palette before the window replaces it
    app.setStyle("Fusion")  # draws the same on every desktop, and follows the stylesheet closely
    tray = QtWidgets.QSystemTrayIcon.isSystemTrayAvailable()
    app.setQuitOnLastWindowClosed(not tray)
    window = Manager(tray=tray, style_app=True)

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
