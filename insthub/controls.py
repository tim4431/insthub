"""Controls for any device, made from what it says about itself (`describe()`): its settings
(properties), each changed in place with the control its metadata asks for, and its commands, run
with arguments typed as on the command line.  The window's device page (`insthub gui`) is made of
them, and a panel can embed them (docs/guide.md#writing-a-panel).

    Controls    both, in two tabs: what a panel embeds
    Settings    its properties, read when they are shown (and on Reload); a settable one is changed
                from a list, a spin box with its unit, a switch or a text field
    Commands    its commands, and a line to run one: VPP channel=2 gains=[1, 2]

    box.addWidget(Controls(device, exclude=["live", "set_timebase"]))  # all the panel does not do itself

They never block the GUI thread: they reach the device only through its proxy's futures
(`device.bus.future`), started off the GUI thread by qt.start (so not even a server that has gone
away, which the proxy tries to reconnect to, holds it up), and qt.deliver hands back the results,
to none that were deleted meanwhile.  Writing a property or running a command that the device
marks dangerous asks first.  `include` and `exclude` choose which of its names they show, as in
wrap(): a list of names, or one name.  They show the device as it was described when they were
made; `load(device.bus.describe())` shows it anew (after it was paused or resumed, say).  Made for
a proxy, they bring the look of their texts (an error in red, the lines between settings) with
them, for a panel in a program of its own.  A host that reaches devices another way passes no
proxy and replaces `call()` and `confirm()`: the window does, to ask through its worker threads
and with its own dialog (pages.py), and styles them itself.
"""
from __future__ import annotations

import json
import time

from . import cli
from . import widgets as ui
from .client import meta_text
from .qt import QtCore, QtGui, QtWidgets, Signal, deliver, start

Qt = QtCore.Qt
DANGER = "It is marked dangerous: it can harm the instrument, the sample or a person."


def _edit_text(value) -> str:
    """A property's value as it is typed back: text as it is, anything else as JSON."""
    if isinstance(value, str):
        return value
    plain = cli._plain(value)
    return plain if isinstance(plain, str) else json.dumps(plain, default=str)


def _listed(names) -> list[str] | None:
    """`include` or `exclude` as a list: None stays None, and one name is not taken for its letters."""
    if names is None:
        return None
    return [names] if isinstance(names, str) else list(names)


class _Part(QtWidgets.QWidget):
    """What Settings and Commands share: the device, which of its names they show, what it said
    about itself (`described`), and how they reach it (`call`, `confirm`)."""

    def __init__(self, device=None, include=None, exclude=None, parent=None):
        super().__init__(parent)
        self.device = device
        self.include, self.exclude = _listed(include), set(_listed(exclude) or ())
        self.described: dict | None = None  #: describe() of the device shown
        self.paused = False  #: whether it was paused when it was described
        self.shown: list[str] = []  #: the names shown, in their order
        if device is not None:  # (in the window, its stylesheet does it)
            size = self.font().pointSizeF()
            self.setStyleSheet(ui.role_stylesheet(ui.theme("system"), size if size > 0 else 10.0))

    def _which(self, described: dict | None, where) -> object:
        """Which device is shown: `where`, when the host says (the window: its server and the
        device's name), else the proxy's device, else the one `described`.  So a device that could
        not be described just now (described is None) is still the same device."""
        if where is not None:
            return where
        if self.device is not None:
            return self.device.bus.name
        return described and described["name"]

    def _names(self, table: dict) -> list[str]:
        """Which names of a describe() table ("properties" or "commands") to show: those of
        `include`, in its order, or else all of them, sorted; never those of `exclude`."""
        names = [n for n in self.include if n in table] if self.include is not None else sorted(table)
        return [n for n in names if n not in self.exclude]

    def _take(self, described: dict | None) -> bool:
        """Keep what the device says about itself now; returns whether it was paused before.  (A
        proxy's describe() updates one dict in place, so what it said before is not kept in it.)"""
        was, self.described = self.paused, described
        self.paused = bool(described and described["status"].get("paused"))
        return was

    def call(self, method: str, args: tuple, kwargs: dict, done, failed) -> None:
        """Call `method` of the device (one of its commands, or "$get" / "$set"); then `done(result)`
        or `failed(exception)` runs in the GUI thread (not once these controls are deleted)."""
        deliver(start(self.device.bus.future, method, *args, **kwargs), done, failed, owner=self)

    def confirm(self, title: str, text: str, action: str) -> bool:
        """Ask before writing or running what the device marks dangerous."""
        return ui.confirm(self, title, text, action)


class _PropRow(QtWidgets.QWidget):
    """One property: its name, documentation and metadata, its value, and (if settable) a way to change
    it.  The control follows the value and its metadata: a list for one of its choices, a spin box for a
    number with a unit or limits, a switch for true or false, else the text field, which also takes
    what the others cannot show, such as "auto"."""

    def __init__(self, settings: Settings, name: str, info: dict):
        super().__init__()
        self.settings, self.name, self.settable = settings, name, info["settable"]
        self.meta, self.dangerous = info.get("meta") or {}, bool(info.get("dangerous"))
        self.choices = list(self.meta.get("choices") or ())
        self.value, self.loaded, self._held = None, False, False
        title = ui.label(name)
        title.setFont(ui.scaled(title.font(), 1.0, QtGui.QFont.Weight.DemiBold))
        doc = (info.get("doc") or "").split("\n")[0]
        if not self.settable and "read-only" not in doc.lower():
            doc = f"{doc} (read-only)" if doc else "read-only"
        danger = "dangerous to change: you are asked first" if self.dangerous and self.settable else ""
        about = " · ".join(x for x in (doc, meta_text(self.meta), danger) if x)
        self.edit = QtWidgets.QLineEdit()
        self.edit.setReadOnly(not self.settable)
        self.edit.setPlaceholderText("reading..." if self.settable else "")
        self.edit.returnPressed.connect(self.apply)
        self.edit.textEdited.connect(lambda *_: self.set_button.setEnabled(True))
        self.number = ui.NumberBox()
        self.number.set_meta(self.meta)
        self.number.valueChanged.connect(lambda *_: self.set_button.setEnabled(True))
        self.number.submitted.connect(self.apply)
        self.choice = QtWidgets.QComboBox()
        self.choice.addItems([_edit_text(c) for c in self.choices])
        self.choice.activated.connect(lambda index: self.settings.write(self.name, self.choices[index]))
        self.switch = ui.Switch()
        self.switch.toggled.connect(lambda on: self.settings.write(self.name, bool(on)))
        self.editor: QtWidgets.QWidget = self.edit  #: the control shown now
        for control in (self.number, self.choice, self.switch):
            control.hide()
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
        grid.addWidget(ui.label(ui.esc(about), "small", wrap=True), 1, 0)
        editor = QtWidgets.QHBoxLayout()
        editor.setSpacing(8)
        for control in (self.edit, self.number, self.choice):
            editor.addWidget(control, 1)
        editor.addWidget(self.switch)
        editor.addWidget(self.set_button)
        grid.addLayout(editor, 0, 1, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self.message, 2, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 4)

    def _control(self, value) -> QtWidgets.QWidget:
        """The control that shows `value` and changes it (see the class docstring)."""
        if not self.settable:
            return self.edit
        if isinstance(value, bool):
            return self.switch
        plain = isinstance(value, (str, int, float))
        if plain and value in self.choices:
            return self.choice
        if (plain and not isinstance(value, str) and self.meta.keys() & {"unit", "min", "max", "step"}
                and self.number.fits(value)):
            return self.number
        return self.edit

    def show_value(self, value, keep_message: bool = False) -> None:
        self.value, self.loaded = value, True
        self.editor = self._control(value)
        for control in (self.edit, self.number, self.choice, self.switch):
            control.setVisible(control is self.editor)
        self.set_button.setVisible(self.settable and self.editor in (self.edit, self.number))
        if self.editor is self.switch:
            self.switch.set_quietly(value)
        elif self.editor is self.choice:
            self.choice.setCurrentIndex(self.choices.index(value))  # (only a click on the list sends one)
        elif self.editor is self.number:
            blocked = self.number.blockSignals(True)
            self.number.setValue(value)
            self.number.blockSignals(blocked)
        else:
            self.edit.setText(_edit_text(value))
            self.edit.setCursorPosition(0)
        self.set_button.setEnabled(False)
        if not keep_message:
            self.say("")

    def show_error(self, exc) -> None:
        if not self.loaded:
            self.edit.setPlaceholderText("could not be read")
        self.say(ui.error_text(exc), error=True)

    def hold(self, paused: bool) -> None:
        """While the device is paused, a property can be neither read nor set: say so by not offering it."""
        self._held = paused
        self.edit.setReadOnly(paused or not self.settable)
        for control in (self.number, self.choice, self.switch):
            control.setEnabled(not paused)
        if paused:
            self.set_button.setEnabled(False)

    def apply(self) -> None:
        """Send what is typed (Set, or Enter): the spin box's number, or the text field's text."""
        if not self.settable or self._held:  # (Enter in a field held while paused)
            return
        if self.editor is self.number:
            number = self.number.value()
            self.settings.write(self.name, int(number) if isinstance(self.value, int) and number.is_integer()
                                else number)
            return
        text = self.edit.text()
        self.settings.write(self.name, text if isinstance(self.value, str) else cli._value(text.strip()))

    def say(self, text: str, error: bool = False) -> None:
        self.message.setText(ui.esc(text))
        self.message.setProperty("role", "error" if error else "small")
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)
        self.message.setVisible(bool(text))


class Settings(_Part):
    """A device's properties, read from the instrument when they are shown (and on Reload), each
    with its value; a settable one is changed in place.  Once a value is set, `changed(name, value)`
    is emitted and the property read again."""

    changed = Signal(str, object)

    def __init__(self, device=None, include=None, exclude=None, parent=None):
        super().__init__(device, include, exclude, parent)
        self.rows: dict[str, _PropRow] = {}
        self._for: tuple | None = None  # what the rows are for: replies for others are dropped
        self.note = ui.label("Read from the instrument when they are shown. Type values as on the command line: "
                             "2500, true, [1, 2] or text.", "small", wrap=True)
        self.reload = ui.button("Reload", "refresh", kind="ghost", tip="Read them again from the instrument",
                                slot=self.read_all)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.note, 1)
        top.addWidget(self.reload)
        self.card, self.list = ui.card(margins=(18, 6, 18, 6), spacing=0)
        self.empty = ui.label("This device has no properties. Its commands are under Commands.", "muted", wrap=True)
        self.paused_note = ui.label("It is paused: its properties are read again when it is resumed.", "muted",
                                    wrap=True)
        self.paused_note.hide()
        area, content = ui.scroll_page(margins=(0, 0, 0, 0), spacing=10)
        content.addLayout(top)
        content.addWidget(self.paused_note)
        content.addWidget(self.card)
        content.addWidget(self.empty)
        content.addStretch(1)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 12, 0, 0)
        box.addWidget(area)
        if device is not None:
            self.load(device.bus.describe(refresh=False))

    def load(self, described: dict | None, where=None) -> None:
        """Show the properties of the device `described` (and read them), unless they are shown
        already; a paused device is not asked, and is asked again once it is resumed.  `where` says
        which device it is, for a Settings that shows several in turn (the window: its server and
        name)."""
        was = self._take(described)
        table = described["properties"] if described else {}
        props = {name: table[name] for name in self._names(table)}
        key = (self._which(described, where), repr(list(props.items())))  # rows anew when one changes
        paused = self.paused
        self.paused_note.setVisible(paused and bool(props))
        self.note.setVisible(not paused and bool(props))
        self.reload.setVisible(not paused and bool(props))
        for row in self.rows.values():
            row.hold(paused)
        if key == self._for:
            if was != paused:
                self._mark_unread()
            if was and not paused:
                self.read_all()
            return
        self._for, self.shown = key, list(props)
        while (item := self.list.takeAt(0)) is not None:  # the rows (and lines between them) of the one before
            if item.widget() is not None:
                item.widget().hide()  # at once: deleteLater waits for the event loop
                item.widget().deleteLater()
        self.rows = {}
        for index, (prop, info) in enumerate(props.items()):
            if index:
                line = QtWidgets.QFrame()
                line.setFixedHeight(1)
                line.setProperty("role", "rule")
                self.list.addWidget(line)
            row = self.rows[prop] = _PropRow(self, prop, info)
            self.list.addWidget(row)
        self._mark_unread()
        for row in self.rows.values():
            row.hold(paused)
        self.card.setVisible(bool(self.rows))
        self.empty.setVisible(described is not None and not self.rows)
        self.read_all()

    def _mark_unread(self) -> None:
        """What the fields not read yet say: they are being read, or they wait for the device's resume."""
        for row in self.rows.values():
            if not row.loaded:
                row.edit.setPlaceholderText("not read while paused" if self.paused else
                                            "reading..." if row.settable else "")

    def read_all(self) -> None:
        if self.paused:
            return
        for prop in self.rows:
            self.read(prop)

    def read(self, prop: str, keep_message: bool = False) -> None:
        """Read a property from the instrument; keep_message: an error said about it stays."""
        token = self._for

        def done(value):
            if self._for == token and prop in self.rows:
                self.rows[prop].show_value(value, keep_message)

        def failed(exc):
            if self._for == token and prop in self.rows:
                self.rows[prop].show_error(exc)

        self.call("$get", (prop,), {}, done, failed)

    def write(self, prop: str, value) -> None:
        """Set a property on the instrument; one marked dangerous only once the user agrees."""
        row, token, device = self.rows[prop], self._for, self.described["name"]
        if row.dangerous and not self.confirm(f"Set {device}.{prop}?", f"{DANGER}\n\nSet {prop} to "
                                              f"{ui.short(value)}?", "Set"):
            if row.loaded:
                row.show_value(row.value, keep_message=True)  # the control shows what the instrument has again
            return
        row.say("setting...")

        def done(_):
            if self._for == token and prop in self.rows:
                self.changed.emit(prop, value)
                self.read(prop)

        def failed(exc):
            if self._for == token and prop in self.rows:
                self.rows[prop].show_error(exc)
                self.read(prop, keep_message=True)  # the value it has kept

        self.call("$set", (prop, value), {}, done, failed)


class Commands(_Part):
    """A device's commands, and a line to run one: arguments as on the command line."""

    def __init__(self, device=None, include=None, exclude=None, parent=None):
        super().__init__(device, include, exclude, parent)
        self._for: object = None  # which device's commands are shown: results for another are dropped
        self._picked: str | None = None  # the command chosen, chosen again when it is back in the table
        self.table = ui.table("Command", "Arguments", "Description")
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
        self.line = line = QtWidgets.QHBoxLayout()  # (the window adds its Save as recipe here)
        line.setSpacing(8)
        line.addWidget(self.args, 1)
        line.addWidget(self.run_button)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 12, 0, 0)
        box.setSpacing(8)
        box.addWidget(ui.boxed(self.table), 1)
        box.addWidget(self.chosen)
        box.addLayout(line)
        box.addWidget(self.result)
        box.addWidget(self.took)
        if device is not None:
            self.load(device.bus.describe(refresh=False))

    def load(self, described: dict | None, where=None) -> None:
        """Show the commands of the device `described`; another device than the one shown (see
        Settings.load for `where`) starts with none chosen.  The same device keeps the command
        chosen, the arguments typed and the result of one that runs, also when it could not be
        described for a moment (described is None)."""
        self._take(described)
        commands = described["commands"] if described else {}
        self.shown = self._names(commands)
        ui.fill(self.table, [[n, commands[n]["signature"], ("Dangerous · " if commands[n].get("dangerous") else "")
                              + (commands[n]["doc"] or "").split("\n")[0]] for n in self.shown])
        key = self._which(described, where)
        if key != self._for:
            self._for, self._picked = key, None
            self.table.clearSelection()
            self.args.clear()
            self.result.clear()
            self.took.clear()
        elif self._picked in self.shown and self.current() != self._picked:  # (gone from the table a moment)
            blocked = self.table.blockSignals(True)
            self.table.selectRow(self.shown.index(self._picked))
            self.table.blockSignals(blocked)
        self._chosen()

    def current(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        return self.shown[rows[0].row()] if rows and rows[0].row() < len(self.shown) else None

    def choose(self, name: str) -> None:
        self.table.selectRow(self.shown.index(name))

    def _chosen(self) -> None:
        name, d = self.current(), self.described
        if name is not None:
            self._picked = name
        if self.paused:
            self.chosen.setText("It is paused: no command runs until it is resumed.")
            self.run_button.setEnabled(False)
            return
        if name is None or d is None or name not in d["commands"]:
            self.chosen.setText("Choose a command above, then Run.")
            self.run_button.setEnabled(False)
            return
        c = d["commands"][name]
        hints = [f"{ui.esc(param)}: {ui.esc(meta_text(meta))}" for param, meta in (c.get("params") or {}).items()]
        if c.get("dangerous"):
            hints.append("dangerous: you are asked before it runs")
        self.chosen.setText(f"<b>{ui.esc(name)}</b>{ui.esc(c['signature'])}"
                            + (f" &nbsp;{ui.muted(ui.esc(c['doc'].splitlines()[0]))}" if c["doc"] else "")
                            + (f"<br>{ui.muted(' · '.join(hints))}" if hints else ""))
        self.run_button.setEnabled(True)

    def run(self) -> None:
        name = self.current()
        if name is None or not self.run_button.isEnabled():  # (Enter in the arguments while paused)
            return
        try:
            positional, keyword = cli.parse_params(cli.split_arguments(self.args.text()))
        except ValueError as e:
            self.result.setPlainText(f"Cannot read the arguments: {e}")
            return
        token, call = self._for, f"{self.described['name']}.{name}"
        if self.described["commands"][name].get("dangerous") and not self.confirm(
                f"Run {call}?", f"{DANGER}\n\nRun {name} {self.args.text()}".rstrip() + "?", "Run"):
            return
        start = time.perf_counter()
        self.result.setPlainText(f"Running {call}...")
        self.took.clear()

        def done(result):
            if self._for == token:
                self.result.setPlainText("(no result)" if result is None else cli._show(result))
                self.took.setText(f"{call}: {(time.perf_counter() - start) * 1e3:.1f} ms")

        def failed(exc):
            if self._for == token:
                self.result.setPlainText(ui.error_text(exc))
                self.took.setText(f"{call} failed after {(time.perf_counter() - start) * 1e3:.1f} ms")

        self.call(name, tuple(positional), keyword, done, failed)


class Controls(QtWidgets.QTabWidget):
    """A device's settings and commands, in two tabs ("Settings (3)", "Commands (5)"); a tab with
    nothing to show is hidden.  `device` is a proxy; `include` and `exclude` choose the names, in
    both tabs.  What a panel embeds for all it does not show in a way of its own."""

    def __init__(self, device, include=None, exclude=None, parent=None):
        super().__init__(parent)
        self.setDocumentMode(True)
        self.settings = Settings(device, include, exclude)
        self.commands = Commands(device, include, exclude)
        for part in (self.settings, self.commands):
            self.addTab(part, "")
        self._titles()

    def load(self, described: dict) -> None:
        """Show the device as it describes itself now (`device.bus.describe()`)."""
        for part in (self.settings, self.commands):
            part.load(described)
        self._titles()

    def _titles(self) -> None:
        for part, title in ((self.settings, "Settings"), (self.commands, "Commands")):
            index, count = self.indexOf(part), len(part.shown)
            self.setTabText(index, f"{title} ({count})" if count else title)
            self.setTabVisible(index, bool(count))
