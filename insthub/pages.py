"""The pages of the manager window (`insthub gui`), and what each shows.  A page
is filled in from what the window knows (inventory.Server) and never waits for
a server: anything it asks for goes through the window's workers.

    Sidebar        the way to each page; how many servers and devices, what is broken
    DevicesPage    a tile per device, in a section per server, with a search
    DevicePage     a device: its connection, its properties (to read and change), streams,
                   periodic tasks, state and commands (to run)
    ServersPage    a card per server
    ServerPage     a server: its process, addresses, service, devices and clients
    AgentsPage     giving AI agents the instruments (insthub mcp); which are connected
    SettingsPage   how the window looks and behaves; the files insthub uses
    LogDialog      a server's output, followed live

device_view() and server_view() say what a tile shows; widgets.py draws it.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__, cli, launcher, registry, service
from . import widgets as ui
from .errors import BusError
from .inventory import (
    BAD,
    GOOD,
    Server,
    ago,
    clock,
    device_state,
    mcp_command,
    route,
    routes,
    server_file,
    server_log,
    server_state,
)
from .qt import BINDING, QtCore, QtGui, QtWidgets

if TYPE_CHECKING:
    from .manager import Manager

Qt = QtCore.Qt
STALE = 10.0  #: seconds without a new frame after which a preview is drawn faded


def open_path(path: str | Path) -> bool:
    """Open a file or folder with the desktop's own program for it."""
    return QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))


def _esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def error_text(exc: BaseException) -> str:
    """An exception as people know it: 'ValueError: ...', also when it came from a server."""
    return f"{getattr(exc, 'remote_type', '') or type(exc).__name__}: {exc}"


def _badge(status: str, text: str) -> str:
    """Rich text: a dot in the colour of a status, then the text."""
    return f"<span style='color:{ui.STATUS[status]}'>&#9679;</span>&nbsp;{_esc(text)}"


# ---------------------------------------------------------------- what tiles show
_KINDS = (("scope", ("scope", "oscillo", "dso", "digitizer")), ("camera", ("cam", "ccd", "cmos", "imag")),
          ("spectrum", ("spectr", "analyz", "fft", "osa")),
          ("synth", ("synth", "generator", "siggen", "awg", "dds")),
          ("bolt", ("psu", "power", "supply", "smu", "volt", "current")))


def _device_icon(name: str, kind: str) -> str:
    """An icon for a device, guessed from its name and class."""
    text = f"{name} {kind}".lower()
    return next((icon for icon, words in _KINDS if any(w in text for w in words)), "chip")


def _label(key: str) -> str:
    text = key.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _figures(state: dict) -> tuple:
    """Up to six (label, value) pairs from a device's state: its plain values."""
    figures = []
    for key, value in state.items():
        plain = isinstance(value, (bool, int, float, str)) or (
            isinstance(value, (list, tuple)) and len(value) <= 4 and all(isinstance(v, (int, float)) for v in value))
        if plain and not (isinstance(value, str) and len(value) > 40):
            figures.append((_label(key), ui.format_value(value)))
        if len(figures) == 6:
            break
    return tuple(figures)


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else many or one + 's'}"


def _rate(per_second: float | None) -> str:
    if not per_second:
        return "-" if per_second == 0 else ""
    return f"{per_second:.0f} /s" if per_second >= 10 else f"{per_second:.2g} /s"


def _caption(s: Server, name: str, d: dict, preview: ui.Preview | None) -> str:
    parts = [f"{stream} {_rate(rate)}" for stream in d["streams"] if stream != "state"
             if (rate := s.rates.get((name, stream)))]
    if not parts and preview is not None and preview.time:
        parts.append(f"{preview.stream} · {ago(preview.age())} ago")
    watchers = d["status"]["subscribers"]
    if watchers:
        parts.append(f"{watchers} watching")
    if not parts:
        parts.append(_count(len(d["commands"]), "command"))
    return " · ".join(parts[:3])


def device_view(s: Server, name: str, previews: bool = True) -> ui.TileView:
    """What a device's tile shows: a preview of its stream if there is one (and `previews`),
    else the figures of its state; why, if it is broken."""
    d = s.device(name)
    status, word, sentence = device_state(d)
    kind = d["type"] if d else ""
    view = ui.TileView(title=name, subtitle=" · ".join(x for x in (kind, s.name) if x), icon=_device_icon(name, kind),
                       status=status, word=word, tip=f"{name}: {sentence}")
    if d is None:
        if s.error:
            view.message = f"Its server does not answer: {s.error}"
        elif s.probe is None:
            view.word, view.message = "checking", "Asking its server..."
        return view
    preview = s.previews.get(name) if previews else None
    if status == BAD:
        view.message = sentence
    elif preview is not None:
        view.preview, view.stale = preview, preview.age() > STALE
    else:
        view.figures = _figures(s.probe["states"].get(name) or {})
        if not view.figures:
            view.message = d["doc"] or (f"{d['type']}: {_count(len(d['commands']), 'command')}, "
                                        f"{_count(len(d['properties']), 'property', 'properties')}.")
    view.caption = _caption(s, name, d, preview)
    return view


def server_view(s: Server) -> ui.TileView:
    """What a server's card shows."""
    status, word, sentence = server_state(s)
    where = {"local": "This computer", "service": "Background service", "remote": "Another computer"}[s.origin]
    via = routes(s) if s.origin == "local" else ""
    view = ui.TileView(title=s.name, subtitle=f"{where} · {via}" if via else where,
                       icon="globe" if s.origin == "remote" else "servers", status=status, word=word, tip=sentence)
    if s.probe:
        devices = list(s.probe["devices"].values())
        connected = sum(1 for d in devices if d["status"]["connected"])
        figures = [("Connected", f"{connected} of {len(devices)}")]
        if s.probe["clients"] is not None:
            figures.append(("Programs", str(len(s.probe["clients"]))))
        figures.append(("Answers in", f"{s.probe['latency'] * 1e3:.2f} ms"))
        view.figures = tuple(figures)
    else:
        view.message = sentence
    parts = [f"pid {s.pid}"] if s.pid and s.origin == "local" else [s.address or ""]
    if s.origin == "local" and s.started:
        parts.append(f"up {ago(time.time() - s.started)}")
    if s.service:
        parts.append("service, starts at login" if s.service["enabled"] else "service")
    view.caption = " · ".join(p for p in parts if p)
    return view


# ---------------------------------------------------------------- building blocks of the pages
def _table(*headers: str) -> QtWidgets.QTableWidget:
    table = QtWidgets.QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(34)
    table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
    table.setWordWrap(False)
    table.setShowGrid(False)
    table.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    header = table.horizontalHeader()
    header.setStretchLastSection(True)
    header.setHighlightSections(False)
    header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
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
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                table.setItem(r, c, item)
            text = "" if value is None else str(value)
            if item.text() != text:
                item.setText(text)
                item.setToolTip(text if len(text) > 40 else "")
    table.blockSignals(blocked)


def _fit(table: QtWidgets.QTableWidget) -> None:
    """Make a table exactly as tall as its rows, for one inside a scrolling page."""
    height = table.horizontalHeader().sizeHint().height() + table.verticalHeader().length() + 4
    table.setFixedHeight(height)


def _boxed(widget: QtWidgets.QWidget, margins=(0, 4, 0, 4)) -> QtWidgets.QFrame:
    frame, layout = ui.card(margins=margins, spacing=0)
    layout.addWidget(widget)
    return frame


def _short(value) -> str:
    text = repr(value) if not isinstance(value, str) else value
    return text if len(text) <= 80 else text[:77] + "..."


def _edit_text(value) -> str:
    """A property's value as it is typed back: text as it is, anything else as JSON."""
    if isinstance(value, str):
        return value
    plain = cli._plain(value)
    return plain if isinstance(plain, str) else json.dumps(plain, default=str)


class _Facts(QtWidgets.QFrame):
    """A card of facts, a name on the left of each; a fact without text hides its row."""

    def __init__(self, title: str | None = None):
        super().__init__()
        self.setProperty("card", True)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(18, 14, 18, 14)
        box.setSpacing(10)
        if title:
            box.addWidget(ui.label(title, "h2"))
        self.form = QtWidgets.QFormLayout()
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setHorizontalSpacing(24)
        self.form.setVerticalSpacing(8)
        box.addLayout(self.form)
        box.addStretch(1)

    def row(self, name: str) -> QtWidgets.QLabel:
        field = ui.label("", wrap=True, select=True)
        field.setTextFormat(Qt.TextFormat.RichText)
        self.form.addRow(ui.label(name, "label"), field)
        return field

    def show_row(self, field: QtWidgets.QLabel, text: str | None) -> None:
        visible = bool(text)
        if field.text() != (text or ""):
            field.setText(text or "")
        field.setVisible(visible)
        self.form.labelForField(field).setVisible(visible)


class _Header(QtWidgets.QWidget):
    """The top of a page: the way back, its title and status, a line below, buttons on the right."""

    def __init__(self, title: str = "", back: str | None = None, on_back=None):
        super().__init__()
        self.back = ui.button(back, "back", kind="link", slot=on_back) if back else None
        self.title = ui.label(title, "h1")
        self.pill = ui.Pill()
        self.pill.hide()
        self.subtitle = ui.label("", "muted", wrap=True, select=True)
        self.subtitle.setTextFormat(Qt.TextFormat.RichText)
        self.actions = QtWidgets.QHBoxLayout()
        self.actions.setSpacing(8)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(self.title)
        top.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addStretch(1)
        top.addLayout(self.actions)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        if self.back is not None:
            box.addWidget(self.back, 0, Qt.AlignmentFlag.AlignLeft)
        box.addLayout(top)
        box.addWidget(self.subtitle)


class _Page(QtWidgets.QWidget):
    """A page: its header, then a body that scrolls (or, with scroll=False, fills the rest)."""

    def __init__(self, title: str = "", back: str | None = None, on_back=None, scroll: bool = True):
        super().__init__()
        self.setObjectName("page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.header = _Header(title, back, on_back)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        top = QtWidgets.QVBoxLayout()
        top.setContentsMargins(28, 22, 28, 14)
        top.addWidget(self.header)
        outer.addLayout(top)
        if scroll:
            self.scroll, self.body = ui.scroll_page()
            outer.addWidget(self.scroll, 1)
        else:
            self.body = QtWidgets.QVBoxLayout()
            self.body.setContentsMargins(28, 4, 28, 22)
            self.body.setSpacing(14)
            outer.addLayout(self.body, 1)


def _sync_tiles(window: Manager, grid: ui.GridFlow, tiles: dict, s: Server, names: list[str]) -> None:
    """Keep one tile per device in `tiles` (name -> Tile), in the order of `names`."""
    for name in [n for n in tiles if n not in names]:
        tile = tiles.pop(name)
        grid.removeWidget(tile)
        tile.deleteLater()
    for name in names:
        if name not in tiles:
            tile = tiles[name] = ui.Tile(parent=grid.parentWidget())
            tile.clicked.connect(lambda key=s.key, name=name: window.open_device(key, name))
            tile.menu_requested.connect(lambda pos, key=s.key, name=name: window.device_menu(key, name, pos))
    grid.set_order([tiles[n] for n in names])


def _where(s: Server) -> str:
    if s.origin == "remote":
        return f"another computer · {s.address}"
    if s.origin == "service":
        return f"background service {service.PREFIX}{s.name}"
    return " · ".join(x for x in ("this computer", routes(s), "service" if s.service else "") if x)


# ---------------------------------------------------------------- Devices and Servers
class _Section(QtWidgets.QWidget):
    """A server's part of the Devices page: a heading, then a tile per device."""

    def __init__(self, window: Manager, key: str):
        super().__init__()
        self.window, self.key = window, key
        self.name = ui.button("", kind="section", tip="Show this server", slot=lambda: window.open_server(key))
        self.pill = ui.Pill()
        self.detail = ui.label("", "muted")
        self.start = ui.button("Start", "start", tip="Start the service",
                               slot=lambda: window.service_action(window.servers[key], "start"))
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(self.name)
        head.addWidget(self.pill)
        head.addWidget(self.detail, 1)
        head.addWidget(self.start)
        self.grid_host = QtWidgets.QWidget()
        self.grid = ui.GridFlow(self.grid_host)
        self.note = ui.label("", "muted", wrap=True)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 6)
        box.setSpacing(10)
        box.addLayout(head)
        box.addWidget(self.grid_host)
        box.addWidget(self.note)
        self.tiles: dict[str, ui.Tile] = {}

    def sync(self, s: Server, query: str, previews: bool) -> bool:
        """Show the server as it is now; returns whether anything in it matches `query`."""
        status, word, sentence = server_state(s)
        self.name.setText(s.name)
        self.pill.set(status, word)
        self.detail.setText(_where(s))
        names = s.device_names()
        _sync_tiles(self.window, self.grid, self.tiles, s, names)
        shown = 0
        for name in names:
            tile = self.tiles[name]
            tile.set_view(device_view(s, name, previews))
            d = s.device(name)
            match = not query or query in f"{name} {d['type'] if d else ''} {s.name}".lower()
            tile.setVisible(match)
            shown += match
        self.grid_host.setVisible(shown > 0)
        if s.origin == "service" or (s.error and not names):
            note = sentence
        elif not names:
            note = "Asking it which devices it has..." if s.probe is None else "It has no devices."
        else:
            note = ""
        self.note.setText(note)
        self.note.setVisible(bool(note) and not query)
        self.start.setVisible(s.origin == "service" and s.service["state"] in ("inactive", "failed"))
        return shown > 0 or not query


class _Welcome(QtWidgets.QFrame):
    """The Devices page while no server runs: what to do."""

    def __init__(self, window: Manager):
        super().__init__()
        self.setProperty("card", True)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(28, 26, 28, 26)
        box.setSpacing(10)
        box.addWidget(ui.label("No instrument server is running", "h2"))
        box.addWidget(ui.label(
            "A server is a small program that holds the connections to its instruments; each instrument on it "
            "is a device. Once one runs, its devices appear here as tiles.", "muted", wrap=True))
        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(8)
        self.sim = ui.button("Try simulated instruments", "live", kind="primary",
                             tip="Start insthub sim: a scope, a generator, a camera and a spectrum analyzer",
                             slot=window.start_simulator)
        self.new = ui.button("New server...", "plus", tip="Write the config of a server for your instruments",
                             slot=window.new_server_dialog)
        buttons.addWidget(self.sim)
        buttons.addWidget(self.new)
        buttons.addStretch(1)
        box.addSpacing(4)
        box.addLayout(buttons)
        box.addSpacing(4)
        box.addWidget(ui.label("Or in a terminal:  insthub serve lab.toml   or   python my_server.py", "small",
                               select=True))


class DevicesPage(_Page):
    """Every device, a tile each, in a section per server."""

    def __init__(self, window: Manager):
        super().__init__("Devices")
        self.window = window
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search devices")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(ui.icon("search", "muted"), QtWidgets.QLineEdit.ActionPosition.LeadingPosition)
        self.search.setMinimumWidth(240)
        self.search.textChanged.connect(lambda *_: window.render())
        self.header.actions.addWidget(self.search)
        self.header.actions.addWidget(window.add_button())
        self.sections: dict[str, _Section] = {}
        self.welcome = _Welcome(window)
        self.welcome.hide()
        self.nothing = ui.label("", "muted")
        self.nothing.hide()
        self.body.addWidget(self.welcome)
        self.body.addWidget(self.nothing)
        self.body.addStretch(1)

    @property
    def tiles(self) -> dict:
        return {(key, name): tile for key, section in self.sections.items() for name, tile in section.tiles.items()}

    def sync(self, servers: list[Server], scanned: bool, previews: bool) -> None:
        query = self.search.text().strip().lower()
        keys = {s.key for s in servers}
        for key in [k for k in self.sections if k not in keys]:
            section = self.sections.pop(key)
            self.body.removeWidget(section)
            section.deleteLater()
        shown = 0
        for index, s in enumerate(servers):
            section = self.sections.get(s.key)
            if section is None:
                section = self.sections[s.key] = _Section(self.window, s.key)
                self.body.insertWidget(index, section)
            elif self.body.indexOf(section) != index:
                self.body.removeWidget(section)
                self.body.insertWidget(index, section)
            visible = section.sync(s, query, previews)
            section.setVisible(visible)
            shown += visible
        self.welcome.setVisible(scanned and not servers)
        self.nothing.setText(f"No device matches “{_esc(query)}”." if query else "")
        self.nothing.setVisible(bool(query) and not shown)
        total = sum(len(s.device_names()) for s in servers)
        broken = sum(1 for s in servers if s.probe for d in s.probe["devices"].values() if device_state(d)[0] == BAD)
        running = sum(1 for s in servers if s.origin != "service")
        text = f"{_count(total, 'device')} on {_count(running, 'server')}" if scanned else "Looking for servers..."
        if total and any(s.probe for s in servers):
            text += f" · <span style='color:{ui.STATUS[BAD]}'>{broken} not connected</span>" if broken \
                else " · all connected"
        self.header.subtitle.setText(text)


class ServersPage(_Page):
    """Every server: a card each."""

    def __init__(self, window: Manager):
        super().__init__("Servers")
        self.window = window
        self.header.actions.addWidget(window.add_button())
        self.grid_host = QtWidgets.QWidget()
        self.grid = ui.GridFlow(self.grid_host, min_width=300)
        self.note = ui.label("", "muted", wrap=True)
        self.body.addWidget(self.grid_host)
        self.body.addWidget(self.note)
        self.body.addStretch(1)
        self.tiles: dict[str, ui.Tile] = {}

    def sync(self, servers: list[Server], scanned: bool) -> None:
        keys = [s.key for s in servers]
        for key in [k for k in self.tiles if k not in keys]:
            tile = self.tiles.pop(key)
            self.grid.removeWidget(tile)
            tile.deleteLater()
        for s in servers:
            tile = self.tiles.get(s.key)
            if tile is None:
                tile = self.tiles[s.key] = ui.Tile(parent=self.grid_host)
                tile.clicked.connect(lambda key=s.key: self.window.open_server(key))
                tile.menu_requested.connect(lambda pos, key=s.key: self.window.server_menu(key, pos))
            tile.set_view(server_view(s))
        self.grid.set_order([self.tiles[k] for k in keys])
        self.note.setText("" if servers or not scanned else
                          "No server is running, none is installed as a service, and devices.toml lists none. "
                          "Add sets one up.")
        self.note.setVisible(not servers)
        running = sum(1 for s in servers if s.origin == "local")
        services = sum(1 for s in servers if s.service)
        remote = sum(1 for s in servers if s.origin == "remote")
        parts = [f"{running} running here"]
        if services:
            parts.append(f"{services} background service{'s' * (services != 1)}")
        if remote:
            parts.append(f"{remote} on other computers")
        problem = self.window._remote_error
        self.header.subtitle.setText(" · ".join(parts) + (f" · {_esc(problem)}" if problem else ""))


class ServerPage(_Page):
    def __init__(self, window: Manager):
        super().__init__("", back="Servers", on_back=lambda: window.show_page("servers"))
        self.window, self.server = window, None
        self.start = ui.button("Start", "start", kind="primary", tip="Start the service",
                               slot=lambda: window.service_action(self.server, "start"))
        self.restart = ui.button("Restart", "refresh", tip="Stop and start again, e.g. after changing its code or "
                                 "config", slot=lambda: window.service_action(self.server, "restart"))
        self.stop = ui.button("Stop", "stop", tip="Stop the server; its devices close their instruments",
                              slot=self._stop)
        self.logs = ui.button("Logs", "logs", tip="What the server printed", slot=lambda: window.show_logs(self.server))
        self.more = ui.button("", "more", kind="ghost", tip="More")
        self.more.setFixedWidth(38)
        menu = QtWidgets.QMenu(self.more)
        self.open_file = menu.addAction(ui.icon("file"), "Open its config file")
        self.open_file.triggered.connect(lambda *_: open_path(server_file(self.server)))
        self.remove = menu.addAction(ui.icon("trash"), "Remove the service...")
        self.remove.setToolTip("Stop it and no longer start it at login")
        self.remove.triggered.connect(lambda *_: window.service_action(self.server, "remove"))
        self.forget = menu.addAction(ui.icon("trash"), "Remove from devices.toml...")
        self.forget.triggered.connect(lambda *_: window.forget_remote(self.server))
        self.more.setMenu(menu)
        for button in (self.start, self.restart, self.stop, self.logs, self.more):
            self.header.actions.addWidget(button)

        self.facts = _Facts()
        self.state = self.facts.row("State")
        self.process = self.facts.row("Process")
        self.reach = self.facts.row("Reached at")
        self.latency = self.facts.row("Answers in")
        self.service = self.facts.row("Service")
        self.command = self.facts.row("Command")
        self.file = self.facts.row("Config")
        self.devices = self.facts.row("Devices")
        self.body.addWidget(self.facts)
        self.devices_title = ui.label("Devices", "h2")
        self.grid_host = QtWidgets.QWidget()
        self.grid = ui.GridFlow(self.grid_host)
        self.tiles: dict[str, ui.Tile] = {}
        self.clients_title = ui.label("Connected programs", "h2")
        self.clients = _table("Program", "Process", "Connected since", "Watching")
        self.clients_box = _boxed(self.clients)
        self.no_clients = ui.label("No other program is connected to it.", "muted")
        for widget in (self.devices_title, self.grid_host, self.clients_title, self.clients_box, self.no_clients):
            self.body.addWidget(widget)
        self.body.addStretch(1)

    def _stop(self) -> None:
        if self.server.service:
            self.window.service_action(self.server, "stop")
        else:
            self.window.stop_server(self.server)

    def show_server(self, s: Server) -> None:
        self.server = s
        status, word, sentence = server_state(s)
        h = self.header
        h.title.setText(s.name)
        h.pill.set(status, word)
        h.pill.show()
        h.subtitle.setText({"local": "Server on this computer", "service": "Background service, not running",
                            "remote": "Server on another computer, listed in devices.toml"}[s.origin])
        f = self.facts
        f.show_row(self.state, _esc(sentence))
        info = s.probe["info"] if s.probe else {}
        if s.origin == "local" and s.pid:
            started = f" · started {clock(s.started)}, {ago(time.time() - s.started)} ago" if s.started else ""
            f.show_row(self.process, f"pid {s.pid}{started}")
        elif info.get("pid"):
            f.show_row(self.process, f"pid {info['pid']} on that computer")
        else:
            f.show_row(self.process, None)
        f.show_row(self.reach, "<br>".join(
            f"<code>{_esc(a)}</code> &nbsp;{ui.muted(route(a, s.origin))}" for a in s.addresses))
        f.show_row(self.latency, f"{s.probe['latency'] * 1e3:.2f} ms" if s.probe else None)
        u = s.service
        if u:
            detail = f" ({u['sub']})" if u.get("sub") not in (None, "", "running", "dead", u["state"]) else ""
            boot = "starts at login" if u["enabled"] else "does not start at login"
            f.show_row(self.service, f"{service.PREFIX}{_esc(u['name'])} · {_esc(u['state'])}{detail} · {boot}")
            f.show_row(self.command, f"<code>{_esc(u['command'])}</code>")
        elif s.origin == "local":
            f.show_row(self.service, ui.muted(
                "none: started from this window, so it runs until it is stopped or you log out"
                if self.window.started_here(s.pid) else "none: started by hand, so it stops when its terminal closes"))
            f.show_row(self.command, None)
        else:
            f.show_row(self.service, None)
            f.show_row(self.command, None)
        config = server_file(s)
        f.show_row(self.file, f"<code>{_esc(config)}</code>" if config else None)
        if s.probe:
            devices = s.probe["devices"].values()
            connected = sum(1 for d in devices if d["status"]["connected"])
            f.show_row(self.devices, f"{len(devices)} · {connected} connected")
        else:
            f.show_row(self.devices, ", ".join(_esc(n) for n in s.declared) or None)

        running = s.origin == "local"
        mine = s.pid == os.getpid()
        self.start.setVisible(bool(u) and not running)
        self.stop.setVisible((bool(u) and running) or (running and not u and bool(s.pid) and not mine))
        self.restart.setVisible(bool(u))
        self.logs.setVisible(bool(u) or (running and server_log(s.name).is_file()))
        self.open_file.setVisible(config is not None)
        self.remove.setVisible(bool(u))
        self.forget.setVisible(s.origin == "remote")
        self.more.setVisible(config is not None or bool(u) or s.origin == "remote")

        names = s.device_names()
        _sync_tiles(self.window, self.grid, self.tiles, s, names)
        for name in names:
            self.tiles[name].set_view(device_view(s, name, self.window.previews_on))
            self.tiles[name].show()
        self.devices_title.setVisible(bool(names))
        self.grid_host.setVisible(bool(names))
        clients = (s.probe["clients"] if s.probe else None) or []
        _fill(self.clients, [[c["program"], f"pid {c['pid']}" if c.get("pid") else (c.get("peer") or ""),
                              clock(c["since"]) if c.get("since") else "",
                              ", ".join(c.get("subscriptions", ())) or "-"] for c in clients])
        _fit(self.clients)
        self.clients_title.setText(f"Connected programs ({len(clients)})")
        reported = s.probe is not None and s.probe["clients"] is not None
        self.clients_title.setVisible(reported)
        self.clients_box.setVisible(reported and bool(clients))
        self.no_clients.setVisible(reported and not clients)


# ---------------------------------------------------------------- a device
class _PropRow(QtWidgets.QWidget):
    """One property: its name and documentation, its value, and (if settable) a way to change it."""

    def __init__(self, panel: _Properties, name: str, info: dict):
        super().__init__()
        self.panel, self.name, self.settable = panel, name, info["settable"]
        self.value, self.loaded = None, False
        title = ui.label(name)
        title.setFont(ui.scaled(title.font(), 1.0, QtGui.QFont.Weight.DemiBold))
        doc = (info.get("doc") or "").split("\n")[0]
        if not self.settable and "read-only" not in doc.lower():
            doc = f"{doc} (read-only)" if doc else "read-only"
        self.edit = QtWidgets.QLineEdit()
        self.edit.setReadOnly(not self.settable)
        self.edit.setPlaceholderText("reading..." if self.settable else "")
        self.edit.returnPressed.connect(self.apply)
        self.edit.textEdited.connect(lambda *_: self.set_button.setEnabled(True))
        self.switch = ui.Switch()
        self.switch.hide()
        self.switch.toggled.connect(lambda on: self.panel.write(self.name, bool(on)))
        self.set_button = ui.button("Set", tip=f"Send the new {name} to the instrument", slot=self.apply)
        self.set_button.setEnabled(False)
        self.set_button.setVisible(self.settable)
        self.message = ui.label("", "small", wrap=True)
        self.message.hide()
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 8, 0, 8)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(2)
        grid.addWidget(title, 0, 0)
        grid.addWidget(ui.label(_esc(doc), "small", wrap=True), 1, 0)
        editor = QtWidgets.QHBoxLayout()
        editor.setSpacing(8)
        editor.addWidget(self.edit, 1)
        editor.addWidget(self.switch)
        editor.addWidget(self.set_button)
        grid.addLayout(editor, 0, 1, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self.message, 2, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 4)

    def show_value(self, value, keep_message: bool = False) -> None:
        self.value, self.loaded = value, True
        boolean = isinstance(value, bool) and self.settable
        self.switch.setVisible(boolean)
        self.edit.setVisible(not boolean)
        self.set_button.setVisible(self.settable and not boolean)
        if boolean:
            self.switch.set_quietly(value)
        else:
            self.edit.setText(_edit_text(value))
            self.edit.setCursorPosition(0)
            self.set_button.setEnabled(False)
        if not keep_message:
            self.say("")

    def show_error(self, exc) -> None:
        if not self.loaded:
            self.edit.setPlaceholderText("could not be read")
        self.say(error_text(exc), error=True)

    def apply(self) -> None:
        if not self.settable:
            return
        text = self.edit.text()
        self.panel.write(self.name, text if isinstance(self.value, str) else cli._value(text.strip()))

    def say(self, text: str, error: bool = False) -> None:
        self.message.setText(_esc(text))
        self.message.setProperty("role", "error" if error else "small")
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)
        self.message.setVisible(bool(text))


class _Properties(QtWidgets.QWidget):
    """A device's properties, read from the instrument when its page opens (and on Reload),
    each with its value; a settable one is changed in place."""

    def __init__(self, page: DevicePage):
        super().__init__()
        self.page = page
        self.rows: dict[str, _PropRow] = {}
        self._for: tuple | None = None
        self.note = ui.label("Read from the instrument when this page opens. Type values as on the command line: "
                             "2500, true, [1, 2] or text.", "small", wrap=True)
        self.reload = ui.button("Reload", "refresh", kind="ghost", tip="Read them again from the instrument",
                                slot=self.read_all)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.note, 1)
        top.addWidget(self.reload)
        self.card, self.list = ui.card(margins=(18, 6, 18, 6), spacing=0)
        self.empty = ui.label("This device has no properties. Its commands are under Commands.", "muted", wrap=True)
        area, content = ui.scroll_page(margins=(0, 0, 0, 0), spacing=10)
        content.addLayout(top)
        content.addWidget(self.card)
        content.addWidget(self.empty)
        content.addStretch(1)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 12, 0, 0)
        box.addWidget(area)

    def load(self, s: Server, name: str, d: dict | None) -> None:
        """Show the properties of this device (and read them), if it is not the one shown already."""
        props = d["properties"] if d else {}
        key = (s.key, name, tuple(props))
        if key == self._for:
            return
        self._for = key
        while (item := self.list.takeAt(0)) is not None:  # the rows (and lines between them) of the one before
            if item.widget() is not None:
                item.widget().deleteLater()
        self.rows = {}
        for index, (prop, info) in enumerate(sorted(props.items())):
            if index:
                line = QtWidgets.QFrame()
                line.setFixedHeight(1)
                line.setProperty("role", "rule")
                self.list.addWidget(line)
            row = self.rows[prop] = _PropRow(self, prop, info)
            self.list.addWidget(row)
        self.card.setVisible(bool(self.rows))
        self.empty.setVisible(d is not None and not self.rows)
        self.note.setVisible(bool(self.rows))
        self.reload.setVisible(bool(self.rows))
        self.read_all()

    def _target(self) -> tuple | None:
        s, name = self.page.server, self.page.name
        return (s.address, name, self._for) if s is not None and s.address else None

    def read_all(self) -> None:
        for prop in self.rows:
            self.read(prop)

    def read(self, prop: str, keep_message: bool = False) -> None:
        """Read a property from the instrument; keep_message: an error said about it stays."""
        target = self._target()
        if target is None:
            return
        address, device, token = target
        window = self.page.window

        def done(value):
            if self._for == token and prop in self.rows:
                self.rows[prop].show_value(value, keep_message)

        def failed(exc):
            if self._for == token and prop in self.rows:
                self.rows[prop].show_error(exc)

        window.submit(window.actions, lambda: window.backend.call(address, device, "$get", prop, timeout=10.0),
                      done, failed)

    def write(self, prop: str, value) -> None:
        target = self._target()
        if target is None:
            return
        address, device, token = target
        window = self.page.window
        self.rows[prop].say("setting...")

        def done(_):
            if self._for == token and prop in self.rows:
                window.say(f"{device}.{prop} = {_short(value)}")
                self.read(prop)

        def failed(exc):
            if self._for == token and prop in self.rows:
                self.rows[prop].show_error(exc)
                self.read(prop, keep_message=True)  # the value it has kept

        window.submit(window.actions, lambda: window.backend.call(address, device, "$set", prop, value), done, failed)


class _Commands(QtWidgets.QWidget):
    """A device's commands, and a line to run one: arguments as on the command line."""

    def __init__(self, page: DevicePage):
        super().__init__()
        self.page = page
        self._names: list[str] = []
        self.table = _table("Command", "Arguments", "Description")
        self.table.itemSelectionChanged.connect(self._chosen)
        self.table.itemDoubleClicked.connect(lambda *_: self.args.setFocus())
        self.chosen = ui.label("Choose a command above, then Run.", "muted", select=True)
        self.args = QtWidgets.QLineEdit()
        self.args.setPlaceholderText("arguments, as on the command line: VPP channel=2 gains=[1, 2]")
        self.args.returnPressed.connect(self.run)
        self.run_button = ui.button("Run", "start", kind="primary", tip="Run the command on the instrument",
                                    slot=self.run)
        self.result = QtWidgets.QPlainTextEdit()
        self.result.setProperty("role", "code")
        self.result.setReadOnly(True)
        self.result.setPlaceholderText("The result appears here.")
        self.result.setMaximumHeight(150)
        self.took = ui.label("", "small")
        line = QtWidgets.QHBoxLayout()
        line.setSpacing(8)
        line.addWidget(self.args, 1)
        line.addWidget(self.run_button)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 12, 0, 0)
        box.setSpacing(8)
        box.addWidget(_boxed(self.table), 1)
        box.addWidget(self.chosen)
        box.addLayout(line)
        box.addWidget(self.result)
        box.addWidget(self.took)

    def load(self, d: dict | None, changed: bool) -> None:
        commands = d["commands"] if d else {}
        self._names = sorted(commands)
        _fill(self.table, [[n, commands[n]["signature"], (commands[n]["doc"] or "").split("\n")[0]]
                           for n in self._names])
        if changed:
            self.table.clearSelection()
            self.args.clear()
            self.result.clear()
            self.took.clear()
            self._chosen()

    def current(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        return self._names[rows[0].row()] if rows and rows[0].row() < len(self._names) else None

    def choose(self, name: str) -> None:
        self.table.selectRow(self._names.index(name))

    def _chosen(self) -> None:
        name = self.current()
        d = self.page.server.device(self.page.name) if self.page.server else None
        if name is None or d is None or name not in d["commands"]:
            self.chosen.setText("Choose a command above, then Run.")
            self.run_button.setEnabled(False)
            return
        c = d["commands"][name]
        self.chosen.setText(f"<b>{_esc(name)}</b>{_esc(c['signature'])}"
                            + (f" &nbsp;{ui.muted(_esc(c['doc'].splitlines()[0]))}" if c["doc"] else ""))
        self.run_button.setEnabled(True)

    def run(self) -> None:
        name, s, device = self.current(), self.page.server, self.page.name
        if name is None or s is None or not s.address:
            return
        try:
            positional, keyword = cli.parse_params(cli.split_arguments(self.args.text()))
        except ValueError as e:
            self.result.setPlainText(f"Cannot read the arguments: {e}")
            return
        window, address, start = self.page.window, s.address, time.perf_counter()
        call = f"{device}.{name}"
        self.result.setPlainText(f"Running {call}...")
        self.took.clear()

        def done(result):
            self.result.setPlainText("(no result)" if result is None else cli._show(result))
            self.took.setText(f"{call}: {(time.perf_counter() - start) * 1e3:.1f} ms")

        def failed(exc):
            self.result.setPlainText(error_text(exc))
            self.took.setText(f"{call} failed after {(time.perf_counter() - start) * 1e3:.1f} ms")

        window.submit(window.actions, lambda: window.backend.call(address, device, name, *positional,
                                                                  kwargs=keyword), done, failed)


class DevicePage(_Page):
    def __init__(self, window: Manager):
        super().__init__("", back="Devices", on_back=window.go_back, scroll=False)
        self.window, self.server, self.name = window, None, None
        self.connect_now = ui.button("Connect now", "power",
                                     tip="Open the instrument session now, without waiting for a command",
                                     slot=lambda: window.device_action(self.server, self.name, "$open"))
        self.release = ui.button(
            "Release", "release",
            tip="Close the instrument session so that other software (e.g. the vendor's) can use the instrument. "
                "The next command or periodic task opens it again; stop the periodic tasks to keep it closed.",
            slot=lambda: window.device_action(self.server, self.name, "$release"))
        self.live = ui.button("Live view", "live", kind="primary", tip="Live plots of its streams, in a window of "
                              "their own", slot=lambda: window.live_view(self.server, self.name))
        for button in (self.connect_now, self.release, self.live):
            self.header.actions.addWidget(button)

        self.facts = _Facts()
        self.instrument = self.facts.row("Instrument")
        self.activity = self.facts.row("Activity")
        self.requests = self.facts.row("Requests")
        self.via = self.facts.row("Server")
        self.watchers = self.facts.row("Watched by")
        self.preview_box, inside = ui.card(margins=(12, 12, 12, 10), spacing=6)
        self.preview = ui.PreviewView()
        self.preview_caption = ui.label("", "small")
        inside.addWidget(self.preview, 1)
        inside.addWidget(self.preview_caption)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(self.facts, 3)
        top.addWidget(self.preview_box, 2)
        self.body.addLayout(top)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.props = _Properties(self)
        self.streams = _table("Stream", "Data", "Rate", "Last frame", "Watchers", "Description")
        self.tasks = _table("Periodic task", "Every", "Running", "Runs", "Errors", "Last error")
        self.state = _table("Key", "Value")
        self.commands = _Commands(self)
        self._switches: dict[str, ui.Switch] = {}
        for widget, title in ((self.props, "Settings"), (self._pad(self.streams), "Streams"),
                              (self._pad(self.tasks), "Periodic tasks"), (self._pad(self.state), "State"),
                              (self.commands, "Commands")):
            self.tabs.addTab(widget, title)
        self.body.addWidget(self.tabs, 1)

    @staticmethod
    def _pad(table) -> QtWidgets.QWidget:
        host = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(host)
        box.setContentsMargins(0, 12, 0, 0)
        box.addWidget(_boxed(table))
        return host

    def task_switch(self, task: str) -> ui.Switch:
        return self._switches[task]

    def show_device(self, s: Server, name: str) -> None:
        changed = (s, name) != (self.server, self.name)
        self.server, self.name = s, name
        d = s.device(name)
        status, word, sentence = device_state(d)
        h = self.header
        h.title.setText(name)
        h.pill.set(status, word)
        h.pill.show()
        doc = f" · {_esc(d['doc'])}" if d and d.get("doc") else ""
        what = _esc(d["type"]) if d else ("device listed in devices.toml" if s.origin == "remote" else "device")
        h.subtitle.setText(what + doc)
        f = self.facts
        f.show_row(self.instrument, _badge(status, sentence))
        reachable = d is not None
        if d:
            st = d["status"]
            if st["busy"]:
                waiting = f", {st['queued']} more waiting" if st["queued"] else ""
                activity = f"running <code>{_esc(st['busy'])}</code>{waiting}"
            else:
                activity = "idle"
            f.show_row(self.activity, activity)
            f.show_row(self.requests, f"{st['calls']:,} handled · {st['errors']:,} failed")
        else:
            f.show_row(self.activity, None)
            f.show_row(self.requests, None)
        how = route(s.address, s.origin) if s.address else ""
        pid = f" (pid {s.pid})" if s.origin == "local" and s.pid else ""
        f.show_row(self.via, f"{_esc(s.name)}{pid} · {ui.muted(how)}")
        watching = {}
        reported = bool(s.probe) and s.probe["clients"] is not None
        for c in s.probe["clients"] if reported else ():
            streams = [sub.partition("/")[2] for sub in c.get("subscriptions", ()) if sub.startswith(name + "/")]
            if streams:
                watching[c["program"]] = watching.get(c["program"], []) + streams
        nobody = "no other program" if reported else "not reported (the server runs an older insthub)"
        f.show_row(self.watchers, "; ".join(f"{_esc(p)} ({', '.join(map(_esc, v))})" for p, v in watching.items())
                   or (ui.muted(nobody) if reachable else None))
        connected = bool(d and d["status"]["connected"])
        self.connect_now.setEnabled(reachable and not connected)
        self.release.setEnabled(reachable and connected)
        live_ok, why = self.window.can_view(s)
        if live_ok and d is not None and not any(n != "state" for n in d["streams"]):
            live_ok, why = False, "It publishes no streams to plot"
        self.live.setEnabled(reachable and live_ok)
        self.live.setToolTip("Live plots of its streams, in a window of their own" if live_ok else why)

        preview = s.previews.get(name) if self.window.previews_on else None
        self.preview_box.setVisible(preview is not None)
        if preview is not None:
            stale = preview.age() > STALE
            self.preview.set_preview(preview, stale)
            rate = s.rates.get((name, preview.stream))
            when = f"last frame {ago(preview.age())} ago" if stale else _rate(rate)
            kind = d["streams"].get(preview.stream, {}).get("kind") if d else None
            self.preview_caption.setText(" · ".join(x for x in (preview.stream, kind, when) if x))

        now = time.time()
        streams = {n: st for n, st in d["streams"].items() if n != "state"} if d else {}  # state: its own tab
        _fill(self.streams, [[n, st["kind"] or "nothing yet", _rate(s.rates.get((name, n))),
                              f"{ago(now - st['time'])} ago" if st["time"] else "never",
                              st["subscribers"], st["doc"]] for n, st in sorted(streams.items())])
        tasks = sorted((d["tasks"] if d else {}).items())
        _fill(self.tasks, [[n, f"{t['interval']:g} s" if t["interval"] else "continuously", "",
                            f"{t['runs']:,}", t["errors"], t["last_error"] or ""] for n, t in tasks])
        if changed or list(self._switches) != [n for n, _ in tasks]:
            self._switches = {}
            for row, (task, _) in enumerate(tasks):
                switch = self._switches[task] = ui.Switch()
                switch.toggled.connect(lambda on, task=task: self.window.task_action(self.server, self.name, task,
                                                                                     bool(on)))
                cell = QtWidgets.QWidget()
                inner = QtWidgets.QHBoxLayout(cell)
                inner.setContentsMargins(10, 0, 10, 0)
                inner.addWidget(switch)
                inner.addStretch(1)
                self.tasks.setCellWidget(row, 2, cell)
            self.tasks.horizontalHeader().resizeSection(2, 90)
        for task, t in tasks:
            self._switches[task].set_quietly(t["enabled"])
            self._switches[task].setToolTip(f"{task} is {'running' if t['enabled'] else 'stopped'}; click to "
                                            f"{'stop' if t['enabled'] else 'start'} it")
        state = s.probe["states"].get(name, {}) if s.probe else {}
        _fill(self.state, [[k, _short(v)] for k, v in state.items()])
        self.props.load(s, name, d)
        self.commands.load(d, changed)
        counts = (len(d["properties"]) if d else 0, len(streams), len(tasks), len(state), len(d["commands"]) if d else 0)
        for i, (title, count) in enumerate(zip(("Settings", "Streams", "Periodic tasks", "State", "Commands"), counts,
                                               strict=True)):
            self.tabs.setTabText(i, f"{title} ({count})" if count else title)
        if changed and d:  # open on the tab that has something to show
            self.tabs.setCurrentIndex(next((i for i, n in enumerate(counts) if n), 0))


# ---------------------------------------------------------------- AI agents and Settings
def _claude(*args: str) -> subprocess.CompletedProcess:
    """Run Claude Code's command line (`claude mcp ...`)."""
    return subprocess.run(["claude", *args], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)


def _copyable(window: Manager, text: str) -> QtWidgets.QWidget:
    line = QtWidgets.QLineEdit(text)
    line.setProperty("role", "code")
    line.setReadOnly(True)
    line.setCursorPosition(0)
    copy = ui.button("Copy", "copy", tip="Copy to the clipboard", slot=lambda: window.copy(text))
    row = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(line, 1)
    layout.addWidget(copy)
    return row


class AgentsPage(_Page):
    """AI agents on the bus: giving one access through `insthub mcp`, and which are connected now."""

    def __init__(self, window: Manager):
        super().__init__("AI agents")
        self.window = window
        self._asking = False
        self.header.subtitle.setText(
            "An AI agent such as Claude Code can use the instruments through <code>insthub mcp</code>: list them, "
            "read their documentation, call commands, set properties and take live traces. While it does, it is "
            "listed among the programs connected to a server, and the tray says when one starts.")
        command = mcp_command()
        self.add = ["mcp", "add", "--scope", "user", "insthub", "--", *command]

        claude, box = ui.card()
        head = QtWidgets.QHBoxLayout()
        head.addWidget(ui.label("Claude Code", "h2"))
        head.addStretch(1)
        self.setup = ui.button("Set up", "check", kind="primary", tip="Runs: " + shlex.join(["claude", *self.add]),
                               slot=lambda: self._run("Setting up insthub for Claude Code", self.add))
        self.remove = ui.button("Remove", "trash", tip="Runs: claude mcp remove --scope user insthub",
                                slot=lambda: self._run("Removing insthub from Claude Code",
                                                       ["mcp", "remove", "--scope", "user", "insthub"]))
        head.addWidget(self.setup)
        head.addWidget(self.remove)
        box.addLayout(head)
        self.status = ui.label("", wrap=True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        box.addWidget(self.status)
        box.addWidget(ui.label("The command it runs:", "small"))
        box.addWidget(_copyable(window, shlex.join(["claude", *self.add])))

        other, box2 = ui.card()
        box2.addWidget(ui.label("Other agents", "h2"))
        box2.addWidget(ui.label("Claude Desktop and other MCP clients take this in their own settings "
                                "(docs/agents.md explains the rest):", "muted", wrap=True))
        box2.addWidget(_copyable(window, json.dumps({"mcpServers": {"insthub": {"command": command[0],
                                                                                "args": command[1:]}}})))
        now, box3 = ui.card()
        box3.addWidget(ui.label("Connected now", "h2"))
        self.connected = ui.label("", wrap=True)
        self.connected.setTextFormat(Qt.TextFormat.RichText)
        box3.addWidget(self.connected)
        for widget in (claude, other, now):
            self.body.addWidget(widget)
        self.body.addStretch(1)

    def check(self) -> None:
        """Ask Claude Code whether it has insthub (in a worker: it starts `insthub mcp` to check it)."""
        self.show_connected()
        if shutil.which("claude") is None:
            self.status.setText("Not installed here (there is no <code>claude</code> command); other agents take "
                                "the settings below.")
            self.setup.hide()
            self.remove.hide()
            return
        if self._asking:
            return
        self._asking = True
        self.status.setText("asking...")
        self.setup.hide()
        self.remove.hide()
        self.window.submit(self.window.actions, lambda: _claude("mcp", "get", "insthub").returncode == 0,
                           self._answered, self._failed)

    def _answered(self, registered: bool) -> None:
        self._asking = False
        self.status.setText(_badge(GOOD, "set up, for all your projects") if registered else "not set up yet")
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

    def show_connected(self) -> None:
        agents = self.window.agents()
        self.connected.setText("<br>".join(f"{_esc(name)} (pid {pid}), using {', '.join(map(_esc, servers))}"
                                           for pid, (name, servers) in agents.items())
                               or ui.muted("No agent is connected right now."))


def _setting(title: str, detail: str, *controls) -> QtWidgets.QWidget:
    """A row of a settings card: a title, a line of detail under it (`row.hint`), controls on the right."""
    row = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(row)
    layout.setContentsMargins(0, 6, 0, 6)
    layout.setSpacing(10)
    text = QtWidgets.QVBoxLayout()
    text.setSpacing(2)
    text.addWidget(ui.label(title))
    row.hint = ui.label(detail, "small", wrap=True, select=True)
    row.hint.setTextFormat(Qt.TextFormat.RichText)
    row.hint.setVisible(bool(detail))
    text.addWidget(row.hint)
    layout.addLayout(text, 1)
    for control in controls:
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
    return row


class SettingsPage(_Page):
    THEMES = (("system", "Same as the desktop"), ("light", "Light"), ("dark", "Dark"))

    def __init__(self, window: Manager):
        super().__init__("Settings")
        self.window = window
        settings = window.settings
        self.header.subtitle.setText("How this window looks and behaves. Your servers are not affected.")

        look, box = ui.card()
        box.addWidget(ui.label("Appearance", "h2"))
        self.theme = QtWidgets.QComboBox()
        for key, text in self.THEMES:
            self.theme.addItem(text, key)
        self.theme.currentIndexChanged.connect(self._theme_chosen)
        box.addWidget(_setting("Theme", "", self.theme))
        self.previews = ui.Switch(window.previews_on)
        self.previews.toggled.connect(lambda on: (settings.setValue("previews", bool(on)), window.render()))
        box.addWidget(_setting("Live previews on tiles", "The last frame each device published, fetched once a "
                               "second. Fetching it never makes an instrument acquire.", self.previews))

        tray, box2 = ui.card()
        box2.addWidget(ui.label("Notifications and startup", "h2"))
        self.notify = ui.Switch(settings.value("notify", True, type=bool))
        self.notify.toggled.connect(lambda on: settings.setValue("notify", bool(on)))
        box2.addWidget(_setting("Notify me when something breaks", "A device loses its instrument, a server stops "
                                "answering or a service fails; also when an AI agent starts.", self.notify))
        self.login = ui.Switch(launcher.autostart())
        self.login.toggled.connect(lambda on: launcher.set_autostart(bool(on)))
        self.login_row = _setting("Start at login, in the tray", "Runs <code>insthub gui --tray</code> when you log "
                                  "in.", self.login)
        box2.addWidget(self.login_row)
        self.menu_add = ui.button("Add", "plus", tip="Add insthub to the applications menu and the desktop",
                                  slot=self._install)
        self.menu_remove = ui.button("Remove", "trash", tip="Remove the launcher (and Start at login)",
                                     slot=self._uninstall)
        self.menu_row = _setting("In the applications menu", "So that insthub starts from an icon; on GNOME, "
                                 "right-click it in the dock to pin it.", self.menu_add, self.menu_remove)
        box2.addWidget(self.menu_row)
        if not sys.platform.startswith("linux"):
            self.login_row.hide()
            self.menu_row.hide()

        files, box3 = ui.card()
        box3.addWidget(ui.label("Files", "h2"))
        self.paths: dict[str, QtWidgets.QLabel] = {}
        for key, title in (("runtime", "Running servers"), ("devices", "Devices on other computers"),
                           ("services", "Background services"), ("settings", "Settings of this window")):
            open_button = ui.button("Open", "folder" if key in ("runtime", "services") else "file",
                                    slot=lambda key=key: self._open(key))
            extra = [ui.button("Add...", "plus", tip="Add devices of a server on another computer",
                               slot=window.remote_dialog)] if key == "devices" else []
            row = _setting(title, "", *extra, open_button)
            self.paths[key] = row.hint
            box3.addWidget(row)

        about, box4 = ui.card()
        box4.addWidget(ui.label("About", "h2"))
        version = QtCore.qVersion()
        box4.addWidget(ui.label(f"insthub {__version__} · Python {sys.version.split()[0]} "
                                f"(<code>{_esc(sys.executable)}</code>) · {BINDING} on Qt {version}", "small",
                                wrap=True, select=True))
        for widget in (look, tray, files, about):
            self.body.addWidget(widget)
        self.body.addStretch(1)

    def _theme_chosen(self) -> None:
        self.window.settings.setValue("theme", self.theme.currentData())
        self.window.apply_theme()

    def _locations(self) -> dict[str, Path]:
        return {"runtime": registry.runtime_dir(), "devices": registry.devices_file(), "services": service.unit_dir(),
                "settings": Path(self.window.settings.fileName())}

    def _open(self, key: str) -> None:
        path = self._locations()[key]
        if key == "devices" and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Devices on other computers, found by name from this one:\n#\n# [devices]\n"
                            '# awg = "tcp://192.168.1.20:8701"\n')
        if not path.exists():
            self.window.say(f"{path} does not exist yet")
        elif not open_path(path):
            self.window.say(f"cannot open {path}")

    def _install(self) -> None:
        self.window.act("Adding insthub to the applications menu", lambda: launcher.install(desktop=True),
                        "insthub is in the applications menu", after=self.sync)

    def _uninstall(self) -> None:
        self.window.act("Removing the launcher", launcher.uninstall, "launcher removed", after=self.sync)

    def sync(self) -> None:
        settings = self.window.settings
        blocked = self.theme.blockSignals(True)
        self.theme.setCurrentIndex(max(0, self.theme.findData(settings.value("theme", "system"))))
        self.theme.blockSignals(blocked)
        self.previews.set_quietly(self.window.previews_on)
        self.notify.set_quietly(settings.value("notify", True, type=bool))
        self.login.set_quietly(launcher.autostart())
        installed = (launcher.data_dir() / "applications" / f"{launcher.APP_ID}.desktop").is_file()
        self.menu_add.setText("Update" if installed else "Add")
        self.menu_remove.setVisible(installed)
        for key, path in self._locations().items():
            self.paths[key].setText(f"<code>{_esc(path)}</code>")
            self.paths[key].show()


# ---------------------------------------------------------------- the frame of the window
class Sidebar(QtWidgets.QWidget):
    def __init__(self, window: Manager):
        super().__init__()
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFixedWidth(224)
        logo = QtWidgets.QLabel()
        logo.setPixmap(QtGui.QIcon(str(launcher.ICON)).pixmap(28, 28))
        brand = QtWidgets.QHBoxLayout()
        brand.setSpacing(10)
        brand.addWidget(logo)
        brand.addWidget(ui.label("insthub", "brand"))
        brand.addStretch(1)
        self.nav = {name: ui.NavButton(text, icon) for name, text, icon in (
            ("devices", "Devices", "devices"), ("servers", "Servers", "servers"), ("agents", "AI agents", "agents"),
            ("settings", "Settings", "settings"))}
        group = QtWidgets.QButtonGroup(self)
        group.setExclusive(True)
        for name, button in self.nav.items():
            group.addButton(button)
            button.clicked.connect(lambda *_, name=name: window.show_page(name))
        self.summary = ui.label("", "small", wrap=True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(14, 18, 14, 14)
        box.setSpacing(2)
        box.addLayout(brand)
        box.addSpacing(18)
        for name in ("devices", "servers", "agents"):
            box.addWidget(self.nav[name])
        box.addStretch(1)
        box.addWidget(self.summary)
        box.addSpacing(10)
        box.addWidget(self.nav["settings"])

    def select(self, name: str) -> None:
        self.nav[name].setChecked(True)

    def sync(self, servers: list[Server], agents: int, scanned: bool) -> None:
        total = sum(len(s.device_names()) for s in servers)
        broken_devices = sum(1 for s in servers if s.probe for d in s.probe["devices"].values()
                             if device_state(d)[0] == BAD)
        broken_servers = sum(1 for s in servers if server_state(s)[0] == BAD)
        self.nav["devices"].set_count(str(total) if scanned else "", alert=bool(broken_devices))
        self.nav["servers"].set_count(str(len(servers)) if scanned else "", alert=bool(broken_servers))
        self.nav["agents"].set_count(str(agents) if agents else "")
        running = sum(1 for s in servers if s.origin != "service")
        lines = [f"{_count(running, 'server')} · {_count(total, 'device')}"]
        if agents:
            lines.append(f"{_count(agents, 'AI agent')} connected")
        if broken_devices:
            lines.append(_badge(BAD, f"{broken_devices} not connected"))
        if broken_servers:
            lines.append(_badge(BAD, f"{_count(broken_servers, 'server')} in trouble"))
        self.summary.setText("<br>".join(lines))


class LogDialog(QtWidgets.QDialog):
    """The recent output of a server, refreshed while the dialog is open."""

    def __init__(self, window: Manager, title: str, load):
        super().__init__(window)
        self.window, self._load = window, load
        self._loading, self._last = False, None
        self.setWindowTitle(title)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setProperty("role", "code")
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self.follow = QtWidgets.QCheckBox("Follow new output")
        self.follow.setChecked(True)
        close = ui.button("Close", slot=self.hide)
        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self.follow)
        bottom.addStretch(1)
        bottom.addWidget(close)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(16, 16, 16, 14)
        box.addWidget(self.text)
        box.addLayout(bottom)
        self.resize(960, 540)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.reload)
        self._timer.start(2000)

    def reload(self) -> None:
        if self._loading or not self.isVisible():
            return
        self._loading = True
        self.window.submit(self.window.actions, self._load, self._show, self._failed)

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
