"""The pages of the manager window (`insthub gui`), and what each shows.  A page
is filled in from what the window knows (inventory.Server) and never waits for
a server: anything it asks for goes through the window's workers.

    Sidebar        the way to each page; how many servers and devices, what is broken
    DevicesPage    a tile per device, in a section per server, with a search
    DevicePage     a device: its connection, its properties (to read and change), streams,
                   periodic tasks, state, commands (to run, or save as a recipe) and the
                   recipes that use it; Pause and Resume; an Open button for each UI of
                   its extension.  Its Settings and Commands tabs are the controls a panel
                   can embed too (controls.py)
    ServersPage    a card per server; Update all, when a server can be updated
    ServerPage     a server: its process, addresses, service (and whether it starts at
                   login, and whether its unit is out of date), devices and clients
    RecipesPage    every recipe, to read and run (each in a process of its own); your
                   recipes folder
    AgentsPage     giving AI agents the instruments (insthub mcp); which are connected
    SettingsPage   how the window looks and behaves; the files insthub uses; whether the
                   window itself runs older code
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
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__, cli, controls, extensions, launcher, recipes, registry, service, tomlfile
from . import widgets as ui
from .client import meta_text
from .device import busy_with
from .errors import BusError
from .inventory import (
    ACTION_TIMEOUT,
    BAD,
    BUSY,
    GOOD,
    PAUSED,
    STALE,
    Server,
    ago,
    clock,
    device_state,
    mcp_command,
    paused_by,
    route,
    routes,
    server_file,
    server_state,
)
from .qt import BINDING, QtCore, QtGui, QtWidgets

if TYPE_CHECKING:
    from .manager import Manager

Qt = QtCore.Qt
PAUSE_TIP = ("Close the instrument session and refuse every command, property and periodic task until Resume, "
             "e.g. while you use the instrument by hand or with the vendor's software")
RESUME_TIP = "End the pause: its commands run again, and it reconnects if it was connected before"
RELEASE_TIP = ("Close the instrument session for other software for a moment; the next command, or the next run "
               "of a periodic task, opens it again (Pause keeps it closed)")
SIM_TIP = "Simulated: it talks to a simulator, not to an instrument, so its data is not a measurement."
OUTDATED = "OUT OF DATE"  #: the mark of a server that runs older code than its folders hold (or whose unit is old)


def open_path(path: str | Path) -> bool:
    """Open a file or folder with the desktop's own program for it."""
    return QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))


def _badge(status: str, text: str) -> str:
    """Rich text: a dot in the colour of a status, then the text."""
    return f"<span style='color:{ui.STATUS[status]}'>&#9679;</span>&nbsp;{ui.esc(text)}"


def outdated_text(s: Server, restart: str = "A restart loads the new code.") -> str:
    """Why a server runs older code than its folders hold, in a sentence, then `restart`: what
    loads the new code ("" when it runs what they hold)."""
    return f"Out of date: {'; '.join(s.outdated)}. {restart}".rstrip() if s.outdated else ""


def stale_text(s: Server) -> str:
    """What is old about the unit of a server's service, in a sentence ("" when nothing is)."""
    stale = (s.service or {}).get("stale")
    return f"Its unit is out of date: {stale}." if stale else ""


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


def device_view(s: Server, name: str, previews: bool = True, server_shown: bool = True) -> ui.TileView:
    """What a device's tile shows: its kind and where it is (its address; its server, unless
    `server_shown` is False, on the server's own page), a preview of its stream if there is one
    (and `previews`), else the figures of its state, else what it is; why, if it is broken."""
    d = s.device(name)
    status, word, sentence = device_state(d, s.origin == "local")
    kind = d["type"] if d else ""
    where = (kind, d.get("address") if d else None, s.name if server_shown else None)
    about = (d.get("description") or d.get("doc") or "") if d else ""
    view = ui.TileView(title=name, subtitle=" · ".join(x for x in where if x), icon=_device_icon(name, kind),
                       status=status, word=word, tip=f"{name}: {sentence}" + (f"\n{about}" if about else ""))
    if d is not None and d.get("simulated"):
        view.badge, view.tip = "SIM", f"{view.tip}\n{SIM_TIP}"
    if d is None:
        if s.error:
            view.message = f"Its server does not answer: {s.error}"
        elif s.probe is None:
            view.word, view.message = "checking", "Asking its server..."
        return view
    preview = s.previews.get(name) if previews else None
    paused = d["status"].get("paused")
    if paused and word == "paused":
        view.icon = "pause"
        view.message = (f"Paused since {clock(paused['since'])} by {paused_by(paused, s.origin == 'local')}. "
                        "Nothing runs on it until it is resumed.")
    elif status == BAD or word == "pausing":
        view.message = sentence
    elif preview is not None:
        view.preview, view.stale = preview, preview.age() > STALE
    else:
        view.figures = _figures(s.probe["states"].get(name) or {})
        if not view.figures:  # what this one is (its description), else what its kind is (its docstring)
            view.message = d.get("description") or d["doc"] or (
                f"{d['type']}: {_count(len(d['commands']), 'command')}, "
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
    stale = (s.service or {}).get("stale")
    if s.outdated or stale:
        view.badge = OUTDATED
        view.tip = "\n".join(x for x in (sentence, outdated_text(s), stale_text(s)) if x)
    if s.probe:
        devices = list(s.probe["devices"].values())
        connected = sum(1 for d in devices if d["status"]["connected"])
        figures = [("Connected", f"{connected} of {len(devices)}")]
        paused = sum(1 for d in devices if d["status"].get("paused"))
        if paused:
            figures.append(("Paused", str(paused)))
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
        parts.append("service, starts at login" if s.service["enabled"] else "service, not at login")
    view.caption = " · ".join(p for p in parts if p)
    return view


# ---------------------------------------------------------------- building blocks of the pages
def _fit(table: QtWidgets.QTableWidget) -> None:
    """Make a table exactly as tall as its rows, for one inside a scrolling page."""
    height = table.horizontalHeader().sizeHint().height() + table.verticalHeader().length() + 4
    table.setFixedHeight(height)


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

    def control_row(self, name: str, control: QtWidgets.QWidget, hint: str = "") -> QtWidgets.QWidget:
        """A row with a control (a switch) and a line of explanation beside it."""
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(control)
        row.hint = ui.label(hint, "small", wrap=True)
        layout.addWidget(row.hint, 1)
        self.form.addRow(ui.label(name, "label"), row)
        return row

    def show_widget(self, field: QtWidgets.QWidget, visible: bool) -> None:
        field.setVisible(visible)
        self.form.labelForField(field).setVisible(visible)


class _Header(QtWidgets.QWidget):
    """The top of a page: the way back, its title and status, a line below, buttons on the right:
    beside the title, or in a row of their own below it when there is no room for both (a narrow
    window, a long name), so that no button is squeezed until its text is cut off."""

    def __init__(self, title: str = "", back: str | None = None, on_back=None):
        super().__init__()
        self.back = ui.button(back, "back", kind="link", slot=on_back) if back else None
        self.title = ui.label(title, "h1")
        self.badge = ui.Tag("SIM")  #: shown for a simulated device
        self.badge.setToolTip(SIM_TIP)
        self.badge.hide()
        self.pill = ui.Pill()
        self.pill.hide()
        self.subtitle = ui.label("", "muted", wrap=True, select=True)
        self.subtitle.setTextFormat(Qt.TextFormat.RichText)
        self.actions = QtWidgets.QHBoxLayout()
        self.actions.setSpacing(8)
        self._top = top = QtWidgets.QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(self.title)
        top.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addStretch(1)
        top.addLayout(self.actions)
        self._below = QtWidgets.QHBoxLayout()  # the row of the buttons when they do not fit beside the title
        self._below.addStretch(1)
        self.actions_below = False
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        if self.back is not None:
            box.addWidget(self.back, 0, Qt.AlignmentFlag.AlignLeft)
        box.addLayout(top)
        box.addLayout(self._below)
        box.addWidget(self.subtitle)

    def event(self, event) -> bool:
        if event.type() in (QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.LayoutRequest):  # (a button came or
            self._fit_actions()                                                               # went, a new title)
        return super().event(event)

    def _fit_actions(self) -> None:
        """The buttons beside the title if there is room for both at their full size, else below it."""
        beside = [w for w in (self.title, self.badge, self.pill) if not w.isHidden()]
        need = sum(w.sizeHint().width() + self._top.spacing() for w in beside) + self.actions.sizeHint().width()
        below = need > self.width()
        if below != self.actions_below:
            self.actions_below = below
            (self._top if below else self._below).removeItem(self.actions)
            (self._below if below else self._top).addLayout(self.actions)


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


def _sync_tiles(window: Manager, grid: ui.GridFlow, tiles: dict, s: Server, names: list[str], after=()) -> None:
    """Keep one tile per device in `tiles` (name -> Tile), in the order of `names` (then `after`)."""
    for name in [n for n in tiles if n not in names]:
        tile = tiles.pop(name)
        grid.removeWidget(tile)
        tile.deleteLater()
    for name in names:
        if name not in tiles:
            tile = tiles[name] = ui.Tile(parent=grid.parentWidget())
            tile.clicked.connect(lambda key=s.key, name=name: window.open_device(key, name))
            tile.menu_requested.connect(lambda pos, key=s.key, name=name: window.device_menu(key, name, pos))
    grid.set_order([*(tiles[n] for n in names), *after])


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
        self.outdated = ui.Tag(OUTDATED)  #: shown while the server runs older code than its folders hold
        self.outdated.hide()
        self.detail = ui.label("", "muted")
        self.start = ui.button("Start", "start", tip="Start the service",
                               slot=lambda: window.service_action(window.servers[key], "start"))
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(self.name)
        head.addWidget(self.pill)
        head.addWidget(self.outdated)
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
        self.outdated.setVisible(bool(s.outdated))
        self.outdated.setToolTip(outdated_text(s))
        self.detail.setText(_where(s))
        names = s.device_names()
        _sync_tiles(self.window, self.grid, self.tiles, s, names)
        shown = 0
        for name in names:
            tile = self.tiles[name]
            tile.set_view(device_view(s, name, previews))
            d = s.device(name)
            found = f"{name} {d['type']} {d.get('address') or ''} {d.get('description') or ''}" if d else name
            match = not query or query in f"{found} {s.name}".lower()
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
        self.nothing.setText(f"No device matches “{ui.esc(query)}”." if query else "")
        self.nothing.setVisible(bool(query) and not shown)
        total = sum(len(s.device_names()) for s in servers)
        broken = sum(1 for s in servers if s.probe for d in s.probe["devices"].values() if device_state(d)[0] == BAD)
        running = sum(1 for s in servers if s.origin != "service")
        text = f"{_count(total, 'device')} on {_count(running, 'server')}" if scanned else "Looking for servers..."
        paused = sum(1 for s in servers if s.probe for d in s.probe["devices"].values() if d["status"].get("paused"))
        if paused:
            text += f" · {paused} paused"
        if total and any(s.probe for s in servers):
            text += f" · <span style='color:{ui.STATUS[BAD]}'>{broken} not connected</span>" if broken \
                else " · all connected"
        self.header.subtitle.setText(text)


class ServersPage(_Page):
    """Every server: a card each."""

    def __init__(self, window: Manager):
        super().__init__("Servers")
        self.window = window
        self.update_all = ui.button("Update all", "up", tip="Load new code where a server runs older code, and "
                                    "write service units of an older insthub anew (it asks first, and says what "
                                    "restarts)", slot=lambda: window.update_servers(window._ordered()))
        self.header.actions.addWidget(self.update_all)
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
        self.update_all.setVisible(any(self.window.update_plan(s) for s in servers))
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
        self.header.subtitle.setText(" · ".join(parts) + (f" · {ui.esc(problem)}" if problem else ""))


class ServerPage(_Page):
    def __init__(self, window: Manager):
        super().__init__("", back="Servers", on_back=lambda: window.show_page("servers"))
        self.window, self.server = window, None
        self.start = ui.button("Start", "start", kind="primary", tip="Start the service",
                               slot=lambda: window.service_action(self.server, "start"))
        self.restart = ui.button("Restart", "refresh", tip="Stop and start again, e.g. after changing its code or "
                                 "config", slot=self._restart)
        self.update_button = ui.button("Update", "up", slot=lambda: window.update_servers([self.server]))
        self.stop = ui.button("Stop", "stop", tip="Stop the server; its devices close their instruments",
                              slot=self._stop)
        self.logs = ui.button("Logs", "logs", tip="What the server printed", slot=lambda: window.show_logs(self.server))
        self.more = ui.button("", "more", kind="ghost", tip="More")
        self.more.setFixedWidth(38)
        menu = QtWidgets.QMenu(self.more)
        self.add_device = menu.addAction(ui.icon("plus"), "Add a device...")
        self.add_device.setToolTip("Another instrument in this server: written into its config, then it restarts")
        self.add_device.triggered.connect(lambda *_: window.edit_config(self.server.key, add=True))
        self.edit_file = menu.addAction(ui.icon("edit"), "Edit its config...")
        self.edit_file.setToolTip("Change its devices and their settings here; the file's comments are kept")
        self.edit_file.triggered.connect(lambda *_: window.edit_config(self.server.key))
        self.open_file = menu.addAction(ui.icon("file"), "Open its config file")
        self.open_file.setToolTip("In your text editor")
        self.open_file.triggered.connect(lambda *_: open_path(server_file(self.server)))
        self.remove = menu.addAction(ui.icon("trash"), "Remove the service...")
        self.remove.setToolTip("Stop it and no longer start it at login")
        self.remove.triggered.connect(lambda *_: window.service_action(self.server, "remove"))
        self.forget = menu.addAction(ui.icon("trash"), "Remove from devices.toml...")
        self.forget.triggered.connect(lambda *_: window.forget_remote(self.server))
        self.more.setMenu(menu)
        for button in (self.start, self.restart, self.update_button, self.stop, self.logs, self.more):
            self.header.actions.addWidget(button)

        self.facts = _Facts()
        self.state = self.facts.row("State")
        self.code = self.facts.row("Code")  #: shown while it runs older code than its folders hold
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                          | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.code.linkActivated.connect(lambda link: window.update_servers([self.server]) if link == "update"
                                        else self._restart())
        self.process = self.facts.row("Process")
        self.reach = self.facts.row("Reached at")
        self.latency = self.facts.row("Answers in")
        self.service = self.facts.row("Service")
        self.unit = self.facts.row("Unit")  #: shown while its service's unit is out of date, or was not checked
        self.unit.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                          | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.unit.linkActivated.connect(lambda *_: window.update_servers([self.server]))
        self.login = ui.Switch()
        self.login.toggled.connect(lambda on: window.set_login(self.server, bool(on)))
        self.login_row = self.facts.control_row("Starts at login", self.login)
        self.command = self.facts.row("Command")
        self.file = self.facts.row("Config")
        self.file.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                          | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.file.linkActivated.connect(lambda *_: window.edit_config(self.server.key))
        self.devices = self.facts.row("Devices")
        self.body.addWidget(self.facts)
        self.devices_title = ui.label("Devices", "h2")
        self.grid_host = QtWidgets.QWidget()
        self.grid = ui.GridFlow(self.grid_host)
        self.tiles: dict[str, ui.Tile] = {}
        self.add_tile = ui.AddTile("Add a device", parent=self.grid_host)  # (last in the grid: see show_server)
        self.add_tile.clicked.connect(lambda: window.edit_config(self.server.key, add=True))
        self.clients_title = ui.label("Connected programs", "h2")
        self.clients = ui.table("Program", "Process", "Connected since", "Watching")
        self.clients_box = ui.boxed(self.clients)
        self.no_clients = ui.label("No other program is connected to it.", "muted")
        for widget in (self.devices_title, self.grid_host, self.clients_title, self.clients_box, self.no_clients):
            self.body.addWidget(widget)
        self.body.addStretch(1)

    def _stop(self) -> None:
        if self.server.service:
            self.window.service_action(self.server, "stop")
        else:
            self.window.stop_server(self.server)

    def _restart(self) -> None:
        self.window.restart_server(self.server, ask=True)  # (a service too: one restart at a time)

    def _restartable(self, s: Server) -> bool:
        """Whether saving its config can restart it (a service, or a server this window started)."""
        return bool(s.service) or (s.origin == "local" and self.window.started_here(s.pid))

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
        f.show_row(self.state, ui.esc(sentence))
        info = s.probe["info"] if s.probe else {}
        if s.origin == "local" and s.pid:
            started = f" · started {clock(s.started)}, {ago(time.time() - s.started)} ago" if s.started else ""
            f.show_row(self.process, f"pid {s.pid}{started}")
        elif info.get("pid"):
            f.show_row(self.process, f"pid {info['pid']} on that computer")
        else:
            f.show_row(self.process, None)
        f.show_row(self.reach, "<br>".join(
            f"<code>{ui.esc(a)}</code> &nbsp;{ui.muted(route(a, s.origin))}" for a in s.addresses))
        f.show_row(self.latency, f"{s.probe['latency'] * 1e3:.2f} ms" if s.probe else None)
        u = s.service
        if u:
            detail = f" ({u['sub']})" if u.get("sub") not in (None, "", "running", "dead", u["state"]) else ""
            told = service.restarts_text(u)
            f.show_row(self.service, f"{service.PREFIX}{ui.esc(u['name'])} · {ui.esc(u['state'])}{detail}"
                       + (f" · {told}" if told else ""))
            f.show_row(self.command, f"<code>{ui.esc(u['command'])}</code>")
            pending = self.window.login_pending.get(u["name"])
            on = bool(u["enabled"]) if pending is None else pending
            self.login.set_quietly(on)
            self.login.setEnabled(pending is None)
            self.login_row.hint.setTextFormat(Qt.TextFormat.RichText)
            self.login_row.hint.setText(
                "On: it is started when you log in." if on else
                f"Off: it runs only when you start it (Start, above, or "
                f"<code>insthub service start {ui.esc(u['name'])}</code>).")
        elif s.origin == "local":
            f.show_row(self.service, ui.muted(
                "none: started from this window, so it runs until it is stopped (or, with lingering off, you "
                "log out)" if self.window.started_here(s.pid) else
                "none: started by hand, so it stops when its terminal closes"))
            f.show_row(self.command, None)
        else:
            f.show_row(self.service, None)
            f.show_row(self.command, None)
        f.show_widget(self.login_row, bool(u))
        config = server_file(s)
        editable = config is not None and config.suffix == ".toml"
        f.show_row(self.file, (f"<code>{ui.esc(config)}</code>" + (" &nbsp;<a href='edit'>Edit...</a>" if editable else ""))
                   if config else None)
        if s.probe:
            devices = s.probe["devices"].values()
            connected = sum(1 for d in devices if d["status"]["connected"])
            paused = sum(1 for d in devices if d["status"].get("paused"))
            f.show_row(self.devices, f"{len(devices)} · {connected} connected" + (f" · {paused} paused" if paused else ""))
        elif s.declared:
            f.show_row(self.devices, ", ".join(ui.esc(n) for n in s.declared))
        else:  # a service that does not run: what its config holds
            devices = {}
            if editable:
                try:
                    devices = tomllib.loads(tomlfile.read(config)).get("devices", {})
                except (OSError, ValueError, UnicodeDecodeError):
                    pass
            names = list(devices) if isinstance(devices, dict) else []  # (`devices = 3`: the service says why)
            f.show_row(self.devices, (", ".join(ui.esc(n) for n in names) + " " + ui.muted("(from its config)"))
                       if names else None)

        running = s.origin == "local"
        mine = s.pid == os.getpid()
        restartable = running and self._restartable(s)
        self.start.setVisible(bool(u) and not running)
        self.stop.setVisible((bool(u) and running) or (running and not u and bool(s.pid) and not mine))
        self.restart.setVisible(restartable)
        steps = self.window.update_plan(s)
        self.update_button.setVisible(bool(steps))
        self.update_button.setToolTip(f"{service.plan_text(steps).capitalize()} (it asks first)" if steps else "")
        if stale_text(s):
            f.show_row(self.unit, _badge(BUSY, stale_text(s)) + " <a href='update'>Update</a> writes it anew, with the "
                       "same file and Python" + (", and restarts it." if "restart" in steps else
                                                 "; it does not run, so it is not started."))
        else:  # (not judged: Update leaves it as it is)
            unchecked = (u or {}).get("unchecked")
            f.show_row(self.unit, ui.muted(ui.esc(f"Not checked: {unchecked}, so Update leaves it as it is."))
                       if unchecked else None)
        if not s.outdated:
            f.show_row(self.code, None)
        elif "rewrite" in steps:  # (one Update does both, with one restart)
            f.show_row(self.code, _badge(BUSY, outdated_text(s, "")) + " <a href='update'>Update</a> loads the new "
                       "code and writes its unit anew.")
        elif restartable:  # (the link restarts it, as Restart above does)
            f.show_row(self.code, _badge(BUSY, outdated_text(s, "")) + " <a href='restart'>Restart</a> loads the new code.")
        else:
            f.show_row(self.code, _badge(BUSY, outdated_text(s, "Restart it where you started it to load the new code.")))
        self.logs.setVisible(self.window.log_source(s) is not None)
        self.open_file.setVisible(config is not None)
        self.edit_file.setVisible(editable)
        self.add_device.setVisible(editable)
        if editable:
            self.edit_file.setText(f"Edit {config.name}...")
            self.add_tile.set_detail(f"to {config.name}, then {'restart it' if self._restartable(s) else 'save'}")
            self.add_tile.setToolTip(f"Another instrument in this server: written into {config}"
                                     + ("; the server is then restarted to use it" if self._restartable(s) else
                                        "; restart the server where you started it to use it"))
        self.remove.setVisible(bool(u))
        self.forget.setVisible(s.origin == "remote" and bool(s.declared))  # (not one only [servers] names)
        self.more.setVisible(config is not None or bool(u) or s.origin == "remote")

        names = s.device_names()
        _sync_tiles(self.window, self.grid, self.tiles, s, names, after=[self.add_tile])
        for name in names:
            self.tiles[name].set_view(device_view(s, name, self.window.previews_on, server_shown=False))
            self.tiles[name].show()
        self.add_tile.setVisible(editable)
        self.devices_title.setVisible(bool(names) or editable)
        self.grid_host.setVisible(bool(names) or editable)
        clients = (s.probe["clients"] if s.probe else None) or []
        ui.fill(self.clients, [[c["program"], f"pid {c['pid']}" if c.get("pid") else (c.get("peer") or ""),
                                clock(c["since"]) if c.get("since") else "",
                                ", ".join(c.get("subscriptions", ())) or "-"] for c in clients])
        _fit(self.clients)
        self.clients_title.setText(f"Connected programs ({len(clients)})")
        reported = s.probe is not None and s.probe["clients"] is not None
        self.clients_title.setVisible(reported)
        self.clients_box.setVisible(reported and bool(clients))
        self.no_clients.setVisible(reported and not clients)


# ---------------------------------------------------------------- a device
class _InWindow:
    """How the device page's settings and commands (controls.py) reach the device it shows: through
    the window's workers, so that it never waits for a server and nothing arrives after it closed
    (reading a property gives up after 10 s), and with the window's own dialog to confirm."""

    def __init__(self, page: DevicePage):
        super().__init__()
        self.page = page

    def call(self, method: str, args: tuple, kwargs: dict, done, failed) -> None:
        s, device, window = self.page.server, self.page.name, self.page.window
        if s is None or not s.address:
            return
        address, timeout = s.address, 10.0 if method == "$get" else ACTION_TIMEOUT
        window.submit(window.actions, lambda: window.backend.call(address, device, method, *args, kwargs=kwargs,
                                                                  timeout=timeout), done, failed)

    def confirm(self, title: str, text: str, action: str) -> bool:
        return self.page.window.confirm(title, text, action)


class _Settings(_InWindow, controls.Settings):
    """The device page's Settings tab."""


class _Commands(_InWindow, controls.Commands):
    """The device page's Commands tab, with Save as recipe (a panel's Commands has none)."""

    def __init__(self, page: DevicePage):
        super().__init__(page)
        self.save_button = ui.button("Save as recipe...", "file", slot=self.save_recipe,
                                     tip="Keep this command, with these arguments, as a recipe: a Python "
                                         "function in your recipes folder, which runs with one click from the "
                                         "Recipes tab, or with  insthub run NAME")
        self.line.addWidget(self.save_button)
        self._chosen()

    def _chosen(self) -> None:
        super()._chosen()
        if (button := getattr(self, "save_button", None)) is not None:  # (not yet while controls.Commands is made)
            name = self.current()
            button.setEnabled(name is not None and not name.startswith("$"))

    def save_recipe(self) -> None:
        """Save the chosen command, with the arguments typed, as a recipe: a function in your recipes
        folder's file for this device (recipes.save), replacing one of that name once you agree.  A
        name that another of your files has for a recipe is asked about first: `insthub run NAME`
        would then have to say which."""
        name, device = self.current(), self.page.name
        if name is None or device is None:
            return
        try:
            positional, keyword = cli.parse_params(cli.split_arguments(self.args.text()))
        except ValueError as e:
            self.result.setPlainText(f"Cannot read the arguments: {e}")
            return
        window = self.page.window
        path = recipes.save_path(device)
        call = f"{device}.{name} {self.args.text().strip()}".rstrip()
        recipe = name if name.isidentifier() and not name.startswith("_") else ""
        while True:
            recipe, ok = QtWidgets.QInputDialog.getText(
                window, "Save as recipe", f"Save  {call}  as a recipe: a Python function in\n{path}\n\n"
                "Run it later from the Recipes tab, or with  insthub run NAME.  Its name:", text=recipe)
            recipe = recipe.strip()
            if not ok or not recipe:
                return
            mine = recipes.read_file(path)[0] if path.is_file() else []
            old = next((r for r in reversed(mine) if r.name == recipe), None)  # (the one save() replaces: the last)
            others = [r for file in recipes.your_files() if file != path
                      for r in recipes.read_file(file)[0] if r.name == recipe]
            if old is not None or not others or window.confirm(  # (with `old`, there are two of that name already)
                    f"Another recipe called {recipe}?",
                    f"Another of your recipes is called {recipe} "
                    f"({', '.join(f'{r.file.name}, line {r.line}' for r in others)}). With this one too, "
                    f"insthub run {recipe} and agents must say which one, as {others[0].full_name} or "
                    f"{path.stem}.{recipe}.\n\nSave it as {recipe} all the same? Cancel to choose another name.",
                    f"Save as {recipe}"):
                break
        if old is not None and not window.confirm(
                f"Replace {recipe}?", f"{path.name} has a recipe named {recipe} already (line {old.line}: "
                f"{old.title}).\n\nReplace it with this one? The rest of the file stays as it is.", "Replace"):
            return
        try:
            saved = recipes.save(recipe, device, name, positional, keyword)
        except (ValueError, OSError) as e:
            QtWidgets.QMessageBox.warning(window, "Save as recipe", f"Cannot save the recipe.\n\n{e}")
            return
        window.recipes_changed()
        window.say(f"Saved the recipe {recipe} in {saved}", 6000)


# ---------------------------------------------------------------- recipes
CODE_LINES = 14  #: lines of a recipe's code shown at most, before it scrolls
RESULT_SHOWN = 200_000  #: characters of what a recipe returned shown at most, as many as of what it printed


def _result_text(result) -> str:
    """What a recipe returned, as the output box shows it: "Result: 5", or "Result:" and its JSON
    lines; the first RESULT_SHOWN characters of a long one, and how much is left out.  (It can take
    a while: on a worker.)"""
    if result is None:
        return "(no result)"
    shown = cli._show(result)
    if len(shown) > RESULT_SHOWN:
        cut = shown.rfind("\n", 0, RESULT_SHOWN) + 1 or RESULT_SHOWN  # (at the end of a line, if it has lines)
        shown = f"{shown[:cut].rstrip()}\n... and {len(shown) - cut:,} more characters, not shown"
    return f"Result:\n{shown}" if "\n" in shown else f"Result: {shown}"


def _cut(old: str, new: str, tries: int = 64) -> int | None:
    """How many characters to cut from the start of `old` for `new` to go on from what is left: 0
    if `new` starts with `old`; None if it does not go on from it.  (A Run keeps only the last
    characters a recipe printed, and drops whole lines from the start.)"""
    if new.startswith(old):
        return 0
    end = new.find("\n") + 1
    if not end:
        return None
    first = new[:end]  # its first line: a whole line of `old`
    at = old.find(first, 1)
    for _ in range(tries):  # (a line printed many times is found at many places)
        if at < 0:
            return None
        if old[at - 1] == "\n" and new.startswith(old[at:]):
            return at
        at = old.find(first, at + 1)
    return None


def _units(text: str) -> int:
    """The length of `text` as Qt counts it (UTF-16: a character beyond U+FFFF is two)."""
    return len(text) if text.isascii() else len(text.encode("utf-16-le")) // 2


def _uses(r: recipes.Recipe) -> str:
    """What a recipe uses of each of its devices, briefly, as rich text:
    "Uses <b>synth</b>: frequency = ..., rf() · <b>sa</b>: center"."""
    parts = []
    for device in r.devices:
        used = dict.fromkeys(u.text()[len(device) + 1:] for u in r.uses if u.device == device)  # (sa.ch1: a dot)
        parts.append(f"<b>{ui.esc(device)}</b>" + (f": {ui.esc(', '.join(used))}" if used else ""))
    if r.named_later:
        parts.append("a device named only when it runs, which is not checked first")
    if not parts:
        return 'Uses no device that insthub can see (it looks for <code>insthub.device("NAME")</code> in the code)'
    return "Uses " + " · ".join(parts)


def _code_box(placeholder: str = "", wrap: bool = False) -> QtWidgets.QPlainTextEdit:
    """A read-only box of monospace text (code, or what a program printed)."""
    box = QtWidgets.QPlainTextEdit()
    box.setProperty("role", "code")
    box.setReadOnly(True)
    box.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
    if not wrap:
        box.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
    box.setCornerWidget(QtWidgets.QWidget())  # (where the two scroll bars meet: the box's own background)
    box.setUndoRedoEnabled(False)  # (nothing to undo; and no copy kept of what is cut from it)
    box.setPlaceholderText(placeholder)
    box.setMinimumHeight(72)
    return box


def _lines_high(box: QtWidgets.QPlainTextEdit, lines: int) -> int:
    """The height of a box that shows that many lines."""
    return box.fontMetrics().lineSpacing() * lines + 22


class _Recipes(QtWidgets.QWidget):
    """Recipes to read and run (see recipes.py): a table of them; for the one chosen, what it is,
    what it uses, its code and its parameters.  Run asks its devices first (recipes.check, on a
    worker) and runs nothing if one is missing or lacks what it uses; it asks you before what is
    marked dangerous.  The recipe then runs in a process of its own (recipes.start): the window
    never runs its code.  What it prints comes into the output box while it runs, then what it
    returned or why it failed; Stop ends it.  One runs at a time here.  The device page shows
    the recipes that use its device; the Recipes page (`every`), all of them.  `empty` is what
    is shown instead while there is none."""

    def __init__(self, window: Manager, empty: QtWidgets.QWidget, every: bool = False):
        super().__init__()
        self.window, self.empty, self.every = window, empty, every
        self._recipes: list = []
        self._chosen_key: tuple | None = None  # (file, name) of the recipe chosen
        self._code_for: tuple | None = None  # (recipe, its file's mtime) whose code is shown
        self._run: recipes.Run | None = None  # under way (or ended, until what it returned is read)
        self._checking = False  # asking its devices
        self._stopping = False
        self._notes = ""  # what the check said besides problems, for the status line
        self._printed = ""  # what the output box holds
        self.warning = ui.label("", wrap=True, select=True)  # the files that cannot be read
        self.warning.setTextFormat(Qt.TextFormat.RichText)
        self.warning.hide()
        self.empty.hide()
        headers = ["Recipe", "Parameters", "Devices", "From", "Description"]
        self.table = ui.table(*(headers if every else [h for h in headers if h != "Devices"]))
        self.table.itemSelectionChanged.connect(self._chosen)
        self.table.itemDoubleClicked.connect(lambda *_: self.params.setFocus())
        self._rows_shown = 8 if every else 3  # the table is as tall as this many rows at most, then scrolls
        self.boxed = ui.boxed(self.table)

        self.title = ui.label("", wrap=True, select=True)
        self.title.setTextFormat(Qt.TextFormat.RichText)
        self.about = ui.label("", "small", wrap=True, select=True)  # what it uses; its parameters
        self.about.setTextFormat(Qt.TextFormat.RichText)
        self.params = QtWidgets.QLineEdit()
        self.params.returnPressed.connect(self.run)
        self.run_button = ui.button("Run", "start", kind="primary", slot=self.run,
                                    tip="Ask its devices whether they have what it uses, then run it in a process "
                                        "of its own")
        self.stop_button = ui.button("Stop", "stop", slot=self.stop,
                                     tip="End it as Ctrl-C would (its finally blocks run); for good 3 s later")
        self.open_button = ui.button("Open file", "file", slot=self._open_file, tip="Its file, in your text editor")
        line = QtWidgets.QHBoxLayout()
        line.setSpacing(8)
        line.addWidget(self.params, 1)
        for button in (self.run_button, self.stop_button, self.open_button):
            line.addWidget(button)
        self.code_caption = ui.label("Code", "small")
        self.code = _code_box("The code of the recipe you choose appears here.")
        self.output = _code_box("What it prints, then what it returns, appears here.", wrap=True)
        for widget in (self.code, self.output):  # side by side, as tall as each other: as the room there is
            widget.setMaximumHeight(_lines_high(widget, CODE_LINES))
            widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Ignored)
        self.status = ui.label("", "small", wrap=True, select=True)
        output_caption = ui.label("Output", "small")
        for caption in (self.code_caption, output_caption):  # (on top of its box, however tall the page)
            caption.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Fixed)
        boxes = QtWidgets.QGridLayout()
        boxes.setHorizontalSpacing(12)
        boxes.setVerticalSpacing(4)
        boxes.addWidget(self.code_caption, 0, 0)
        boxes.addWidget(output_caption, 0, 1)
        boxes.addWidget(self.code, 1, 0)
        boxes.addWidget(self.output, 1, 1)
        boxes.setColumnStretch(0, 1)
        boxes.setColumnStretch(1, 1)
        boxes.setRowStretch(1, 1)
        self.detail = QtWidgets.QWidget()
        inside = QtWidgets.QVBoxLayout(self.detail)
        inside.setContentsMargins(0, 0, 0, 0)
        inside.setSpacing(8)
        inside.addWidget(self.title)
        inside.addWidget(self.about)
        inside.addLayout(line)
        inside.addLayout(boxes, 1)
        inside.addWidget(self.status)
        inside.addStretch()  # (what is left once the boxes are as tall as they get)

        area, content = ui.scroll_page(margins=(0, 0, 0, 0), spacing=8)  # (it scrolls in a window too small for it)
        for widget in (self.warning, self.empty, self.boxed):
            content.addWidget(widget)
        content.addWidget(self.detail, 1)
        content.addStretch()  # (below what says that there is none)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0 if every else 12, 0, 0)
        box.addWidget(area)
        self.table.horizontalScrollBar().rangeChanged.connect(lambda *_: self._fit_table())
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._tick)
        self._show_list()
        self._chosen()

    @staticmethod
    def _key(r) -> tuple | None:
        return (str(r.file), r.name) if r is not None else None

    def busy(self) -> bool:
        """Whether a recipe is being checked or runs."""
        return self._checking or self._run is not None

    def load(self, found: tuple | None, fresh: bool = False) -> int:
        """Show (recipes, the files that cannot be read) as recipes.available() or for_device() found
        them (None: not known yet).  `fresh`: for another device than before, so the choice and what
        was typed go, and what was shown unless a recipe runs.  Returns how many recipes there are."""
        listed, problems = found if found is not None else ([], [])
        if fresh or listed != self._recipes:
            keep = None if fresh else self._key(self.current())
            self._recipes = list(listed)
            self._show_list()
            keys = [self._key(r) for r in self._recipes]
            blocked = self.table.blockSignals(True)
            self.table.clearSelection()
            if keep in keys:
                self.table.selectRow(keys.index(keep))
            self.table.blockSignals(blocked)
            if fresh and not self.busy():
                self._say("")
                self.status.clear()
            self._chosen()
        else:
            self._refresh_code()
        self.warning.setText("<br>".join(_badge(BAD, p) for p in problems))
        self.warning.setVisible(bool(problems))
        empty = found is not None and not listed
        self.empty.setVisible(empty)
        self.boxed.setVisible(not empty)
        self.detail.setVisible(not empty or self.busy() or bool(self._printed))  # (a run's end stays in sight)
        return len(listed)

    def _show_list(self) -> None:
        rows = []
        for r in self._recipes:
            params = ", ".join(p.text() for p in r.params.values())
            devices = ", ".join(r.devices) + (" (and one named when it runs)" if r.named_later else "")
            row = [r.name, params, devices, r.file.name if r.file else "",
                   f"Cannot be used: {r.error}" if r.error else r.title]
            rows.append(row if self.every else [x for i, x in enumerate(row) if i != 2])
        ui.fill(self.table, rows)
        last = self.table.columnCount() - 1
        for row, r in enumerate(self._recipes):
            self.table.item(row, 0).setToolTip(r.full_name)
            self.table.item(row, last - 1).setToolTip(f"{r.file}\n{r.where()}")
            item = self.table.item(row, last)
            item.setToolTip(r.error or r.doc or r.title)
            item.setData(Qt.ItemDataRole.ForegroundRole, ui.color(ui.STATUS[BAD]) if r.error else None)
        self._fit_table()

    def _fit_table(self) -> None:
        """Make the table as tall as its rows (as _rows_shown of them at most, then it scrolls), and its
        scroll bar when it is too narrow for its columns."""
        bar, rows = self.table.horizontalScrollBar(), min(max(len(self._recipes), 1), self._rows_shown)
        height = (self.table.horizontalHeader().sizeHint().height() + 4
                  + rows * self.table.verticalHeader().defaultSectionSize()
                  + (bar.sizeHint().height() if bar.maximum() > 0 else 0))
        if height != self.table.height():
            self.table.setFixedHeight(height)

    def current(self) -> recipes.Recipe | None:
        rows = self.table.selectionModel().selectedRows()
        return self._recipes[rows[0].row()] if rows and rows[0].row() < len(self._recipes) else None

    def choose(self, name: str) -> None:
        """Choose a recipe by its name (or full name), as a click on its row does."""
        self.table.selectRow(next(i for i, r in enumerate(self._recipes) if name in (r.name, r.full_name)))

    def _chosen(self) -> None:
        r = self.current()
        if self._key(r) != self._chosen_key:  # another recipe: what was typed was for the one before
            self._chosen_key = self._key(r)
            self.params.clear()
        if r is None:
            self.title.setText(ui.muted("Choose a recipe above, then Run.") if self._recipes else "")
            self.about.hide()
            self.params.setPlaceholderText("")
            self.params.setEnabled(False)
        else:
            doc = r.doc.split("\n\n")[0].replace("\n", " ")
            self.title.setText(f"<b>{ui.esc(r.title)}</b>" + (f" &nbsp;{ui.muted(ui.esc(doc))}" if doc else ""))
            hints = [f"{ui.esc(p.name)}: {ui.esc(' · '.join(x for x in (p.doc, meta_text(p.meta)) if x))}"
                     for p in r.params.values() if p.doc or p.meta]
            if r.takes_more:
                hints.append("it also takes other values by name")
            self.about.setText(_uses(r) + (f"<br>{' · '.join(hints)}" if hints else "")
                               + (f"<br><span style='color:{ui.STATUS[BAD]}'>Cannot be used: {ui.esc(r.error)}</span>"
                                  if r.error else ""))
            self.about.show()
            typed = " ".join([f"{p.name}=..." for p in r.params.values() if p.required]
                             + [f"{p.name}={p.default_text}" for p in r.params.values() if not p.required])
            self.params.setPlaceholderText(f"{typed}   (NAME=VALUE; the others keep their defaults)" if typed else
                                           "NAME=VALUE: it takes values by name" if r.takes_more else
                                           "it has no parameters")
            self.params.setEnabled((bool(r.params) or r.takes_more) and r.error is None)
        self._refresh_code(force=True)
        self._buttons()

    def _refresh_code(self, force: bool = False) -> None:
        """Show the chosen recipe's code as its file is now (read again when the file changes)."""
        r = self.current()
        try:
            mtime = r.file.stat().st_mtime_ns if r is not None else None
        except OSError:
            mtime = None
        if not force and self._code_for == (self._key(r), mtime):
            return
        self._code_for = self._key(r), mtime
        text = r.source() if r is not None else ""
        if text != self.code.toPlainText():
            self.code.setPlainText(text)
        self.code_caption.setText(f"Code · {r.file.name}, line {r.line} · {r.where()}" if r is not None else "Code")
        self.code_caption.setToolTip(str(r.file) if r is not None else "")

    def _buttons(self) -> None:
        r = self.current()
        can_run = r is not None and r.error is None and not self.busy()
        if can_run != self.run_button.isEnabled():
            self.run_button.setEnabled(can_run)  # (the icon in the ink of the text: white only on blue)
            self.run_button.setIcon(ui.icon("start", ui.current().on_accent if can_run else None))
        run = self._run
        self.stop_button.setEnabled(run is not None and not run.done() and not run.stopping)  # (see stop)
        self.open_button.setEnabled(r is not None and r.file is not None)

    def _open_file(self) -> None:
        r = self.current()
        if r is not None and not open_path(r.file):
            self.window.say(f"Cannot open {r.file}")

    def _say(self, text: str) -> None:
        """Show `text` in the output box.  If it goes on from what is there (a Run keeps only the last
        characters a recipe printed, so lines may have gone from the start: see _cut), only what
        changed is changed, so the line you read stays in its place and what you selected stays
        selected.  The box follows the end if it was at the end."""
        old, box = self._printed, self.output
        if text == old:
            return
        bar = box.verticalScrollBar()
        value, follow = bar.value(), bar.value() >= bar.maximum() - 4
        cut = _cut(old, text) if old else None
        if cut is None:
            box.setPlainText(text)
            if not follow:
                bar.setValue(min(value, bar.maximum()))
        else:
            top = box.cursorForPosition(QtCore.QPoint(0, 0)).block()  # the first line in sight
            top_at, down = top.position(), value - top.firstLineNumber()  # (and how far into it, if it wraps)
            cursor = QtGui.QTextCursor(box.document())
            if cut:
                gone = _units(old[:cut])
                cursor.setPosition(gone, QtGui.QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            cursor.insertText(text[len(old) - cut:])
            if cut and not follow:
                top = box.document().findBlock(top_at - gone) if top_at >= gone else None
                bar.setValue(top.firstLineNumber() + down if top is not None else 0)
        self._printed = text
        if follow:
            bar.setValue(bar.maximum())

    def _status(self, text: str, error: bool = False) -> None:
        self.status.setText(ui.esc(text) + (f" · {ui.esc(self._notes)}" if self._notes and not error else ""))
        self.status.setProperty("role", "error" if error else "small")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    # -- running ---------------------------------------------------------------
    def run(self) -> None:
        """Check the chosen recipe with the values typed, then run it (see the class docstring)."""
        r = self.current()
        if r is None or r.error or self.busy():
            return
        try:
            positional, values = cli.parse_params(cli.split_arguments(self.params.text()))
            if positional:
                raise ValueError(f"give each as NAME=VALUE: {cli._show(positional[0])} has no name")
            recipes.check_values(r, values)
        except (ValueError, recipes.RecipeError) as e:
            self._notes = ""
            self._say(f"Nothing ran: {e}" if isinstance(e, recipes.RecipeError) else
                      f"Cannot read the parameters: {e}")
            self._status(f"{r.name}: not run", error=True)
            return
        self._checking, self._notes = True, ""
        self._say("")
        self._status(f"{r.name}: asking {', '.join(r.devices)} first..." if r.devices else f"{r.name}: checking...")
        self._buttons()

        def check():  # the recipe as its file is now: it may have changed since it was listed
            found, problems = recipes.read_file(r.file, r.origin, r.python)
            fresh = next((x for x in found if x.name == r.name), None)
            if fresh is None:
                raise recipes.RecipeError(problems[0] if problems else f"{r.file} has no recipe {r.name} any more")
            return fresh, recipes.check(fresh)

        def refused(exc):
            self._checking = False
            self._say(f"Nothing ran: {exc if isinstance(exc, recipes.RecipeError) else ui.error_text(exc)}")
            self._status(f"{r.name}: not run", error=True)
            self._buttons()

        self.window.submit(self.window.actions, check, lambda found: self._checked(*found, values), refused)

    def _checked(self, r: recipes.Recipe, found: recipes.Checked, values: dict) -> None:
        self._checking = False
        self._notes = " · ".join([*(["simulated: " + ", ".join(found.simulated)] if found.simulated else []),
                                  *found.notes])
        if found.problems:
            self._say("Nothing ran. insthub asked its devices first:\n"
                      + "\n".join(f"  {p}" for p in found.problems))
            self._status(f"{r.name}: not run", error=True)
            self._buttons()
            return
        if found.dangers and not self.window.confirm(
                f"Run {r.name}?", f"{r.full_name} uses what is marked dangerous: it can harm the instrument, the "
                "sample or a person.\n\n" + "\n".join(f"• {d}" for d in found.dangers) + "\n\nRun it?", "Run"):
            self._say("Not run.")
            self._status(f"{r.name}: not run")
            self._buttons()
            return
        try:
            self._run = self.window.start_recipe(r, values)
        except (recipes.RecipeError, OSError) as e:
            self._say(f"Nothing ran: {e}")
            self._status(f"{r.name}: not run", error=True)
            self._buttons()
            return
        self._stopping = False
        self._timer.start()
        self._tick()

    def _tick(self) -> None:
        run = self._run
        if run is None:
            self._timer.stop()
            return
        self._say(run.output())
        if not run.done():
            self._status(f"{run.recipe.name}: {'stopping' if self._stopping else 'running'} · {run.elapsed:.1f} s")
            self._buttons()
            return
        self._timer.stop()
        self._buttons()
        self.window.submit(self.window.actions, lambda: _result_text(run.wait(0)),  # (read from a file, written out)
                           lambda said: self._ended(run, said), lambda exc: self._ended(run, "", exc))

    def _ended(self, run: recipes.Run, said: str, exc: BaseException | None = None) -> None:
        """`run` has ended: `said` is what it returned (_result_text), or `exc` why it failed."""
        if run is not self._run:
            return
        self._run = None
        name, took = run.recipe.name, f"{run.elapsed:.2f} s"
        printed = (getattr(exc, "output", "") or run.output()).rstrip("\n")
        if exc is not None:
            said = str(exc) if isinstance(exc, recipes.RecipeError) else ui.error_text(exc)
            self._status(f"{name} was stopped after {took}" if self._stopping else f"{name} failed after {took}",
                         error=not self._stopping)
        else:
            self._status(f"{name}: done in {took}")
        self._say(f"{printed}\n\n{said}" if printed else said)
        self._buttons()

    def stop(self) -> None:
        """End the recipe that runs (see recipes.Run.stop); returns at once.  Once: Stop is not offered
        again while it stops, since a second interrupt would cut its finally blocks short (it is ended
        for good 3 s later all the same)."""
        run = self._run
        if run is not None and not run.done() and not run.stopping:
            self._stopping = True
            run.stop()
            self._tick()


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
        self.pause = ui.button("Pause", "pause", slot=self._pause_or_resume)
        self.live = ui.button("Live view", "live", tip="Live plots of its streams, in a window of their own",
                              slot=lambda: window.live_view(self.server, self.name))
        self.more = ui.button("", "more", kind="ghost", tip="More")
        self.more.setFixedWidth(38)
        menu = QtWidgets.QMenu(self.more)
        self.edit_file = menu.addAction(ui.icon("edit"), "Edit in its config...")
        self.edit_file.setToolTip("Its driver, its arguments, whether it opens at start; the file's comments are kept")
        self.edit_file.triggered.connect(lambda *_: window.edit_config(self.server.key, self.name))
        menu.addAction(ui.icon("copy"), "Copy its name").triggered.connect(lambda *_: window.copy(self.name))
        self.more.setMenu(menu)
        self.ui_buttons = QtWidgets.QHBoxLayout()  # Open <title>, or Open with a menu of its UIs (see _show_uis)
        self.ui_buttons.setSpacing(8)
        self._uis_shown: tuple | None = None
        for button in (self.connect_now, self.release, self.pause, self.live):
            self.header.actions.addWidget(button)
        self.header.actions.addLayout(self.ui_buttons)
        self.header.actions.addWidget(self.more)

        self.facts = _Facts()
        self.instrument = self.facts.row("Instrument")
        self.instrument.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                                | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.instrument.linkActivated.connect(lambda *_: window.edit_config(self.server.key, self.name))
        self.activity = self.facts.row("Activity")
        self.requests = self.facts.row("Requests")
        self.via = self.facts.row("Server")
        self.extension = self.facts.row("Extension")
        self.watchers = self.facts.row("Watched by")
        self.preview_box, inside = ui.card(margins=(12, 12, 12, 10), spacing=6)
        self.preview = ui.PreviewView()
        self.preview.setToolTip("Right-click to choose the stream it shows")
        self.preview_box.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.preview_box.customContextMenuRequested.connect(
            lambda pos: window.preview_menu(self.server, self.name, self.preview_box.mapToGlobal(pos)))
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
        self.props = _Settings(self)
        self.props.changed.connect(lambda prop, value: window.say(f"{self.name}.{prop} = {ui.short(value)}"))
        self.streams = ui.table("Stream", "Data", "Rate", "Last frame", "Watchers", "Description")
        self.tasks = ui.table("Periodic task", "Every", "Running", "Runs", "Errors", "Last error")
        self.state = ui.table("Key", "Value")
        self.commands = _Commands(self)
        self.no_recipes = ui.label("", "muted", wrap=True, select=True)  # (see show_device)
        self.no_recipes.setTextFormat(Qt.TextFormat.RichText)
        self.recipes = _Recipes(window, self.no_recipes)
        self._switches: dict[str, ui.Switch] = {}
        for widget, title in ((self.props, "Settings"), (self._pad(self.streams), "Streams"),
                              (self._pad(self.tasks), "Periodic tasks"), (self._pad(self.state), "State"),
                              (self.commands, "Commands"), (self.recipes, "Recipes")):
            self.tabs.addTab(widget, title)
        self.body.addWidget(self.tabs, 1)

    @staticmethod
    def _pad(table) -> QtWidgets.QWidget:
        host = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(host)
        box.setContentsMargins(0, 12, 0, 0)
        box.addWidget(ui.boxed(table))
        return host

    def task_switch(self, task: str) -> ui.Switch:
        return self._switches[task]

    def _pause_or_resume(self) -> None:
        d = self.server.device(self.name) if self.server else None
        paused = bool(d and d["status"].get("paused"))
        self.window.device_action(self.server, self.name, "$resume" if paused else "$pause")

    def _show_uis(self, s: Server, name: str) -> str | None:
        """An Open button for each UI of the device's extension (found on a worker: none until then);
        returns why it has none, when that is not up to its extension (it is not installed here)."""
        ext, uis, why = self.window.device_uis(s, name) or (None, [], None)
        if (s.key, name, ext, uis) != self._uis_shown:
            self._uis_shown = (s.key, name, ext, uis)
            while self.ui_buttons.count():
                button = self.ui_buttons.takeAt(0).widget()
                button.hide()  # (at once: deleteLater waits for the event loop)
                button.deleteLater()
            if len(uis) == 1:
                self.ui_buttons.addWidget(ui.button(f"Open {uis[0].title}", "window",
                                                    slot=lambda: self.window.open_ui(self.server, self.name, ext,
                                                                                     uis[0])))
            elif uis:  # one button, its menu listing them: a button for each would crowd the header
                button = ui.button("Open", "window")
                menu = QtWidgets.QMenu(button)
                menu.setToolTipsVisible(True)
                for u in uis:
                    action = menu.addAction(ui.icon("window"), u.title)
                    action.setToolTip(f"Its {u.kind}, in a window of its own")
                    action.triggered.connect(lambda *_, u=u: self.window.open_ui(self.server, self.name, ext, u))
                button.setMenu(menu)
                self.ui_buttons.addWidget(button)
        can, why_not = self.window.can_open(s)
        for i in range(self.ui_buttons.count()):
            button = self.ui_buttons.itemAt(i).widget()
            button.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)  # (never cut)
            button.setEnabled(can)
            what = f"{uis[0].kind}" if len(uis) == 1 else "UIs (" + ", ".join(u.title for u in uis) + ")"
            button.setToolTip(why_not or f"{ext.title}'s {what} for it, in a window of its own (run with "
                                         f"{ext.python})")
        return why

    def show_device(self, s: Server, name: str) -> None:
        changed = (s, name) != (self.server, self.name)
        self.server, self.name = s, name
        d = s.device(name)
        status, word, sentence = device_state(d, s.origin == "local")
        h = self.header
        h.title.setText(name)
        h.pill.set(status, word)
        h.pill.show()
        what = ui.esc(d["type"]) if d else ("device listed in devices.toml" if s.origin == "remote" else "device")
        about = [x for x in ((d.get("address"), d.get("description") or d.get("doc")) if d else ()) if x]
        h.subtitle.setText(" · ".join([what, *(ui.esc(x) for x in about)]))
        f = self.facts
        config = server_file(s)
        editable = config is not None and config.suffix == ".toml"
        fix = f" &nbsp;<a href='edit'>Edit in {ui.esc(config.name)}...</a>" if editable and status == BAD else ""
        f.show_row(self.instrument, _badge(status, sentence) + fix)
        reachable = d is not None
        if d:
            st = d["status"]
            if st["busy"]:
                waiting = f", {st['queued']} more waiting" if st["queued"] else ""
                running = ui.esc(busy_with(st)) if st["busy"].startswith("$") else f"<code>{ui.esc(st['busy'])}</code>"
                activity = f"busy with {running}{waiting}" if st["busy"].startswith("$") else f"running {running}{waiting}"
            elif st.get("opening"):
                activity = "opening the instrument session"
            else:
                activity = "idle"
            f.show_row(self.activity, activity)
            f.show_row(self.requests, f"{st['calls']:,} handled · {st['errors']:,} failed")
        else:
            f.show_row(self.activity, None)
            f.show_row(self.requests, None)
        how = route(s.address, s.origin) if s.address else ""
        pid = f" (pid {s.pid})" if s.origin == "local" and s.pid else ""
        f.show_row(self.via, f"{ui.esc(s.name)}{pid} · {ui.muted(how)}" + (
            f"<br>{_badge(BUSY, outdated_text(s, f'A restart of {s.name} loads the new code.'))}"
            if s.outdated else ""))
        why = self._show_uis(s, name)
        f.show_row(self.extension, ui.esc(f"{d['extension']} {d.get('extension_version') or ''}".rstrip())
                   + (f"<br>{ui.muted(ui.esc(why))}" if why else "") if d and d.get("extension") else None)
        h.badge.setVisible(bool(d and d.get("simulated")))
        watching = {}
        reported = bool(s.probe) and s.probe["clients"] is not None
        for c in s.probe["clients"] if reported else ():
            streams = [sub.partition("/")[2] for sub in c.get("subscriptions", ()) if sub.startswith(name + "/")]
            if streams:
                watching[c["program"]] = watching.get(c["program"], []) + streams
        nobody = "no other program" if reported else "not reported (the server runs an older insthub)"
        f.show_row(self.watchers, "; ".join(f"{ui.esc(p)} ({', '.join(map(ui.esc, v))})" for p, v in watching.items())
                   or (ui.muted(nobody) if reachable else None))
        connected = bool(d and d["status"]["connected"])
        paused = bool(d and d["status"].get("paused"))
        self.connect_now.setEnabled(reachable and not connected and not paused)
        self.release.setEnabled(reachable and connected)
        self.release.setToolTip(RELEASE_TIP)
        can_pause, why_not = self.window.can_pause(d)
        self.pause.setEnabled(paused or can_pause)
        self.pause.setText("Resume" if paused else "Pause")
        self.pause.setToolTip(RESUME_TIP if paused else why_not or PAUSE_TIP)
        live_ok, why = self.window.can_view(s, d)
        self.live.setEnabled(reachable and live_ok)
        self.live.setToolTip("Live plots of its streams, in a window of their own" if live_ok else why)
        if self.pause.property("kind") != ("primary" if paused else None):  # a paused device stands out: Resume
            self.pause.setProperty("kind", "primary" if paused else None)
            self.pause.style().unpolish(self.pause)
            self.pause.style().polish(self.pause)
        self.pause.setIcon(ui.icon("start" if paused else "pause", "on_accent" if paused else None))
        self.edit_file.setVisible(editable)
        if editable:
            self.edit_file.setText(f"Edit in {config.name}...")

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
        ui.fill(self.streams, [[n, st["kind"] or "nothing yet", _rate(s.rates.get((name, n))),
                                f"{ago(now - st['time'])} ago" if st["time"] else "never",
                                st["subscribers"], st["doc"]] for n, st in sorted(streams.items())])
        tasks = sorted((d["tasks"] if d else {}).items())
        ui.fill(self.tasks, [[n, f"{t['interval']:g} s" if t["interval"] else "continuously", "",
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
            self._switches[task].setToolTip(
                f"{task} is {'on' if t['enabled'] else 'off'}; it does not run while the device is paused" if paused
                else f"{task} is {'running' if t['enabled'] else 'stopped'}; click to "
                     f"{'stop' if t['enabled'] else 'start'} it")
        state = s.probe["states"].get(name, {}) if s.probe else {}
        ui.fill(self.state, [[k, ui.short(v)] for k, v in state.items()])
        self.props.load(d, (s.key, name))  # (the same device also when a probe failed: d is None)
        self.commands.load(d, (s.key, name))
        if changed:
            self.no_recipes.setText(
                f"No recipe uses {ui.esc(name)} yet. A recipe is a Python function you keep in a .py file in "
                f"<code>{ui.esc(recipes.folder())}</code>, to run by name; it is listed here when its code calls "
                f"<code>insthub.device(\"{ui.esc(name)}\")</code>. Or run a command in the Commands tab, then "
                "<b>Save as recipe...</b>")
        used_by = self.recipes.load(self.window.device_recipes(name), fresh=changed)
        counts = (len(d["properties"]) if d else 0, len(streams), len(tasks), len(state), len(d["commands"]) if d else 0,
                  used_by)
        for i, (title, count) in enumerate(zip(("Settings", "Streams", "Periodic tasks", "State", "Commands", "Recipes"),
                                               counts, strict=True)):
            self.tabs.setTabText(i, f"{title} ({count})" if count else title)
        if changed and d:  # open on the tab that has something to show
            self.tabs.setCurrentIndex(next((i for i, n in enumerate(counts) if n), 0))


# ---------------------------------------------------------------- Recipes
_EXAMPLE = '''\
# redpitaya.py, in your recipes folder
import insthub


def square(freq: float = 1e3, volt: float = 0.5):
    """Square wave out of OUT1."""
    rp = insthub.device("redpitaya")
    rp.sour_set(1, func="SQUARE", volt=volt, freq=freq)
    rp.tx_txt("OUTPUT1:STATE ON")
'''


class _NoRecipes(QtWidgets.QFrame):
    """The Recipes page while there is no recipe: what one is, and where to write one."""

    def __init__(self, page: RecipesPage):
        super().__init__()
        self.setProperty("card", True)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(28, 26, 28, 26)
        box.setSpacing(10)
        box.addWidget(ui.label("No recipes yet", "h2"))
        text = ui.label(
            "A recipe is a Python function that you keep in a file, to run it by name: from here, from the page "
            "of each device it uses, with <code>insthub run NAME</code>, or from a script. Each public function "
            f"of a .py file in your recipes folder, <code>{ui.esc(recipes.folder())}</code>, is one. Or run a "
            "command on a device's page, then <b>Save as recipe...</b>", "muted", wrap=True, select=True)
        text.setTextFormat(Qt.TextFormat.RichText)
        box.addWidget(text)
        example = _code_box()
        example.setPlainText(_EXAMPLE.rstrip("\n"))
        example.setFixedHeight(_lines_high(example, len(_EXAMPLE.splitlines())))
        box.addWidget(example)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(ui.button("Open the recipes folder", "folder", kind="primary", slot=page.open_folder,
                                    tip="Write a .py file in it with your text editor (it is made if missing)"))
        buttons.addStretch(1)
        box.addSpacing(4)
        box.addLayout(buttons)
        box.addStretch(1)  # (any height to spare goes below, not between the lines)


class RecipesPage(_Page):
    """Every recipe this computer knows of (recipes.available()), to read and run; your recipes folder."""

    def __init__(self, window: Manager):
        super().__init__("Recipes", scroll=False)
        self.window = window
        self.folder_button = ui.button("Open folder", "folder", slot=self.open_folder,
                                       tip=f"Your recipes folder, {recipes.folder()}: each .py file in it holds "
                                           "recipes")
        self.header.actions.addWidget(self.folder_button)
        self.view = _Recipes(window, _NoRecipes(self), every=True)
        self.body.addWidget(self.view, 1)

    def sync(self, found: tuple | None) -> None:
        """Show every recipe, as recipes.available() found them (None: not known yet)."""
        count = self.view.load(found)
        files = len({r.file for r in found[0]}) if found else 0
        self.header.subtitle.setText(
            "Reading the recipes..." if found is None else
            "Snippets you keep as Python functions, to run by name" if not count else
            f"{_count(count, 'recipe')} in {_count(files, 'file')} · each runs in a process of its own · yours are "
            f"the .py files in <code>{ui.esc(recipes.folder())}</code>")

    def open_folder(self) -> None:
        """Open your recipes folder (made first, if it is missing)."""
        folder = recipes.folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.window.say(f"Cannot make {folder}: {e}", 8000)
            return
        if not open_path(folder):
            self.window.say(f"Cannot open {folder}")


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
            "listed among the programs connected to a server, and the tray says when one starts. A device you "
            "pause stays paused for agents too: they see it, and are told not to resume it.")
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
        self.status.setText(f"cannot ask it: {ui.esc(exc)}")

    def _run(self, doing: str, args: list[str]) -> None:
        def run():
            result = _claude(*args)
            if result.returncode != 0:
                raise BusError((result.stderr or result.stdout).strip() or f"exit status {result.returncode}")

        self.window.act(doing, run, f"{doing}: done", after=self.check)

    def show_connected(self) -> None:
        agents = self.window.agents()
        self.connected.setText("<br>".join(f"{ui.esc(name)} (pid {pid}), using {', '.join(map(ui.esc, servers))}"
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

        self.outdated_card, box0 = ui.card()  #: first on the page, shown while this window runs older code
        self.outdated = ui.label("", wrap=True, select=True)
        self.outdated.setTextFormat(Qt.TextFormat.RichText)
        box0.addWidget(self.outdated)
        self.outdated_card.hide()
        self.outdated.hide()

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
        self.login_row = _setting("Start this window at login, in the tray", "Runs <code>insthub gui --tray</code> "
                                  "when you log in. Each server has its own Starts at login switch, on its page.",
                                  self.login)
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

        exts, box5 = ui.card()
        box5.addWidget(ui.label("Extensions", "h2"))
        box5.addWidget(ui.label("Folders that add drivers, and windows, for their instruments; each has an "
                                "<code>insthub-extension.toml</code>. New server offers their devices.", "small",
                                wrap=True))
        self.ext_rows = QtWidgets.QVBoxLayout()  # one per extension (see _show_extensions)
        self.ext_rows.setSpacing(0)
        box5.addLayout(self.ext_rows)
        box5.addWidget(ui.button("Add folder...", "plus", tip="Add an extension's folder to your list (the same as "
                                 "insthub ext add FOLDER)", slot=self._add_extension), 0, Qt.AlignmentFlag.AlignLeft)

        about, box4 = ui.card()
        box4.addWidget(ui.label("About", "h2"))
        version = QtCore.qVersion()
        box4.addWidget(ui.label(f"insthub {__version__} · Python {sys.version.split()[0]} "
                                f"(<code>{ui.esc(sys.executable)}</code>) · {BINDING} on Qt {version}", "small",
                                wrap=True, select=True))
        for widget in (self.outdated_card, look, tray, files, exts, about):
            self.body.addWidget(widget)
        self.body.addStretch(1)

    def show_outdated(self, reasons: list[str]) -> None:
        """Say that this window runs older insthub code than the disk holds, and why (extensions.outdated_here),
        or nothing when it does not."""
        quit_how = "Quit in its tray menu" if self.window.tray is not None else "close it"
        self.outdated.setText(_badge(BUSY, f"This window runs older insthub code: {'; '.join(reasons)}.") +
                              f" To load the new code, quit it ({quit_how}) and start it again; your servers "
                              "keep running." if reasons else "")
        self.outdated.setVisible(bool(reasons))
        self.outdated_card.setVisible(bool(reasons))

    def show_notice(self) -> None:
        """Show this page at its top, where it says that this window runs older code (the sidebar's link)."""
        self.window.show_page("settings")
        self.scroll.verticalScrollBar().setValue(0)

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

    def _find_extensions(self) -> None:
        self.window.submit(self.window.actions, extensions.discover, self._show_extensions)  # (it asks systemd)

    def _show_extensions(self, found: list) -> None:
        """A row per extension: what it is, where it was found, and what is wrong with it."""
        while self.ext_rows.count():
            row = self.ext_rows.takeAt(0).widget()
            row.hide()  # (at once: deleteLater waits for the event loop)
            row.deleteLater()
        for ext in found:
            kind, _, name = ext.source.partition(":")
            where = {"insthub": "comes with insthub", "added": "in your list", "service": f"used by the service {name}",
                     "server": f"used by the server {name}, running here"}.get(kind, ext.source)
            detail = " · ".join(ui.esc(x) for x in (ext.id, ext.version, where) if x) + \
                f"<br><code>{ui.esc(ext.folder)}</code>"
            if ext.error:
                detail += "<br>" + _badge(BAD, ext.error)
            elif ext.hidden_by:
                detail += f"<br>Not used: the one in <code>{ui.esc(Path(ext.hidden_by).parent)}</code> has its id"
            remove = [ui.button("Remove", "trash", tip="Take it off your list (the folder stays as it is)",
                                slot=lambda folder=ext.folder: self._remove_extension(folder))] \
                if ext.source == "added" else []
            self.ext_rows.addWidget(_setting(ext.title or ext.folder.name, detail, *remove))

    def _add_extension(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "An extension's folder", str(Path.home()))
        if not path:
            return
        try:
            ext = extensions.add_folder(path)
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Add an extension", str(e))
            return
        self.window.say(f"Added {ext.title or ext.folder}" + (f"; it cannot be used yet: {ext.error}" if ext.error
                                                              else ""), 8000)
        self._find_extensions()

    def _remove_extension(self, folder: Path) -> None:
        try:
            removed = extensions.remove_folder(folder)
        except ValueError as e:
            self.window.say(str(e), 8000)
            return
        self.window.say(f"Took {', '.join(removed)} off your extensions")
        self._find_extensions()

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
            self.paths[key].setText(f"<code>{ui.esc(path)}</code>")
            self.paths[key].show()
        self._find_extensions()


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
            ("devices", "Devices", "devices"), ("servers", "Servers", "servers"), ("recipes", "Recipes", "logs"),
            ("agents", "AI agents", "agents"), ("settings", "Settings", "settings"))}
        group = QtWidgets.QButtonGroup(self)
        group.setExclusive(True)
        for name, button in self.nav.items():
            group.addButton(button)
            button.clicked.connect(lambda *_, name=name: window.show_page(name))
        self.summary = ui.label("", "small", wrap=True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.summary.linkActivated.connect(lambda *_: window.settings_page.show_notice())  # (this window is out of date)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(14, 18, 14, 14)
        box.setSpacing(2)
        box.addLayout(brand)
        box.addSpacing(18)
        for name in ("devices", "servers", "recipes", "agents"):
            box.addWidget(self.nav[name])
        box.addStretch(1)
        box.addWidget(self.summary)
        box.addSpacing(10)
        box.addWidget(self.nav["settings"])

    def select(self, name: str) -> None:
        self.nav[name].setChecked(True)

    def sync(self, servers: list[Server], agents: int, scanned: bool, outdated: list[str] = ()) -> None:
        """Show the counts and what is wrong; `outdated`: why this window runs older code, if it does."""
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
        paused = sum(1 for s in servers if s.probe for d in s.probe["devices"].values() if d["status"].get("paused"))
        if paused:
            lines.append(_badge(PAUSED, f"{paused} paused"))
        if broken_devices:
            lines.append(_badge(BAD, f"{broken_devices} not connected"))
        if broken_servers:
            lines.append(_badge(BAD, f"{_count(broken_servers, 'server')} in trouble"))
        if outdated:
            lines.append(f"<span style='color:{ui.STATUS[BUSY]}'>&#9679;</span>&nbsp;"
                         "<a href='settings'>This window runs older code</a>")
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
