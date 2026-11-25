"""The dialogs of `insthub gui` that set things up, so that nothing has to be
typed into a file by hand.  Each shows the file it writes or the command it runs.

    NewServerDialog   a new server: its name, who may reach it, and its devices.  For each a
                      driver ("module:Class", or a .py file to pick a class from) and the
                      arguments of its constructor, which are read from the driver's source
                      without running it.  Writes NAME.toml (config.dump), then starts it as
                      a background service, or once, or not at all.
    ServiceDialog     keep an existing .toml config or .py server running in the background
    RemoteDialog      a server on another computer: asks it which devices it has, and adds
                      them to devices.toml (registry.edit_static_devices)

The window that opens them (manager.Manager) runs whatever may block on its worker threads.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import os
import shlex
import sys
from pathlib import Path

from . import cli, config, registry, service
from . import widgets as ui
from .client import _SocketTransport
from .inventory import find_python
from .qt import QtCore, QtWidgets

Qt = QtCore.Qt

EXAMPLES = {  #: drivers offered in the list, so that a server can be tried without hardware
    "insthub.sim:SimScope": "a simulated two-channel oscilloscope",
    "insthub.sim:SimSynth": "a simulated signal generator",
    "insthub.sim:SimCamera": "a simulated camera",
    "insthub.sim:SimAnalyzer": "a simulated spectrum analyzer",
    "insthub.sim:Loopback": "echoes, sleeps and fails on demand",
}


def servers_folder() -> Path:
    """Where new server configs go unless another folder is chosen."""
    return Path.home() / ".config" / "insthub" / "servers"


# ---------------------------------------------------------------- reading a driver without running it
def _text(value) -> str:
    """A value as it is typed into an argument field: text as it is, anything else as JSON."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)


def _default(node) -> tuple[str | None, bool]:
    """(the default of an argument as text, or None if it has none; whether it is a string)."""
    if node is None:
        return None, False
    try:
        value = ast.literal_eval(node)
    except ValueError:
        return ast.unparse(node), False  # an expression, such as a call: shown, never evaluated
    return _text(value), isinstance(value, str)


def classes_in(source: str) -> dict[str, list[tuple[str, str | None, bool]]]:
    """The public classes of a Python source, each with the arguments of its
    __init__ as (name, default as text or None, whether the default is text).
    Read from the source: nothing in it is run."""
    found = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            args = []
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    a = item.args
                    positional = a.posonlyargs + a.args
                    defaults = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
                    pairs = list(zip(positional, defaults, strict=True))[1:] + list(zip(a.kwonlyargs, a.kw_defaults,
                                                                                         strict=True))
                    args = [(arg.arg, *_default(default)) for arg, default in pairs if arg.arg != "name"]
            found[node.name] = args
    return found


def constructor_args(driver: str, folder: str = "") -> list[tuple[str, str | None, bool]] | None:
    """The constructor arguments of a driver ("module:Class"), or None where they cannot
    be known without importing it.  Only insthub's own drivers are imported; any other
    is read from its .py file in `folder`."""
    module, _, name = driver.partition(":")
    if not module or not name:
        return None
    if module.split(".")[0] == "insthub":
        try:
            cls = config.load_object(driver)
            params = inspect.signature(cls).parameters.values()
        except (ImportError, AttributeError, TypeError, ValueError):
            return None
        return [(p.name, None if p.default is p.empty else _text(p.default), isinstance(p.default, str))
                for p in params if p.name != "name" and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
    if not folder:
        return None
    base = Path(folder).expanduser() / Path(*module.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            try:
                return classes_in(path.read_text()).get(name)
            except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
                return None
    return None


# ---------------------------------------------------------------- a new server
@dataclasses.dataclass
class DeviceSpec:
    """One device of a new server, as typed into the dialog."""

    name: str
    driver: str = ""
    folder: str = ""
    lazy: bool = False
    args: dict = dataclasses.field(default_factory=dict)  #: constructor argument -> text typed (empty: default)
    texts: set = dataclasses.field(default_factory=set)  #: arguments whose value is text whatever it looks like
    extra: str = ""  #: more arguments, a "key = value" per line

    def table(self) -> dict:
        """Its [devices.NAME] table.  Raises ValueError for what cannot be written."""
        registry.check_name(self.name, "device name")
        if ":" not in self.driver:
            raise ValueError(f"{self.name}: the driver must look like module:Class")
        out: dict = {"driver": self.driver.strip()}
        if self.folder.strip():
            out["path"] = self.folder.strip()
        if self.lazy:
            out["lazy"] = True
        for key, text in self.args.items():
            if text.strip():
                out[key] = text.strip() if key in self.texts else cli._value(text.strip())
        for line in self.extra.splitlines():
            if not line.strip() or line.strip().startswith("#"):
                continue
            key, eq, text = line.partition("=")
            if not eq or not key.strip().isidentifier():
                raise ValueError(f"{self.name}: “{line.strip()}” should look like  key = value")
            out[key.strip()] = cli._value(text.strip())
        return out


def _browse_row(edit: QtWidgets.QLineEdit, slot) -> QtWidgets.QWidget:
    row = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(edit, 1)
    browse = ui.button("", "folder", tip="Choose...", slot=slot)
    browse.setFixedWidth(38)
    layout.addWidget(browse)
    return row


def _form() -> QtWidgets.QFormLayout:
    form = QtWidgets.QFormLayout()
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setHorizontalSpacing(16)
    form.setVerticalSpacing(10)
    return form


class NewServerDialog(QtWidgets.QDialog):
    """Write the config of a new server and start it."""

    START_SERVICE, START_ONCE, SAVE_ONLY = "service", "once", "save"

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("New server")
        self.devices: list[DeviceSpec] = [DeviceSpec("scope", "insthub.sim:SimScope")]
        self._arg_edits: dict[str, QtWidgets.QLineEdit] = {}
        self._loading = False

        self.name = QtWidgets.QLineEdit("lab")
        self.folder = QtWidgets.QLineEdit(str(servers_folder()))
        self.reach = QtWidgets.QComboBox()
        self.reach.addItem("This computer only", False)
        self.reach.addItem("Other computers too, on a TCP port", True)
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(8700)
        reach = QtWidgets.QHBoxLayout()
        reach.setSpacing(8)
        reach.addWidget(self.reach, 1)
        reach.addWidget(self.port)
        server, box = ui.card()
        box.addWidget(ui.label("Server", "h2"))
        form = _form()
        form.addRow(ui.label("Name", "label"), self.name)
        form.addRow(ui.label("Saved in", "label"), _browse_row(self.folder, self._browse_folder))
        form.addRow(ui.label("Reached from", "label"), reach)
        box.addLayout(form)

        self.list = QtWidgets.QListWidget()
        self.list.setProperty("card", True)
        self.list.setFixedWidth(150)
        self.list.currentRowChanged.connect(self._select)
        add = ui.button("Add", "plus", tip="Another device", slot=self._add)
        self.drop = ui.button("Remove", "trash", tip="Remove this device", slot=self._remove)
        side = QtWidgets.QVBoxLayout()
        side.setSpacing(8)
        side.addWidget(self.list, 1)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(self.drop)
        side.addLayout(buttons)
        self.dev_name = QtWidgets.QLineEdit()
        self.driver = QtWidgets.QComboBox()
        self.driver.setEditable(True)
        self.driver.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        for driver, what in EXAMPLES.items():
            self.driver.addItem(driver)
            self.driver.setItemData(self.driver.count() - 1, what, Qt.ItemDataRole.ToolTipRole)
        self.driver.lineEdit().setPlaceholderText("module:Class, e.g. mydriver:PowerSupply")
        driver_row = QtWidgets.QHBoxLayout()
        driver_row.setSpacing(8)
        driver_row.addWidget(self.driver, 1)
        driver_row.addWidget(ui.button("From a file...", "file", tip="Pick the .py file of your driver, then its "
                                       "class", slot=self._browse_driver))
        self.dev_folder = QtWidgets.QLineEdit()
        self.dev_folder.setPlaceholderText("where its module is, if it is not an installed package")
        self.open_now = ui.Switch(True)
        self.open_now.setToolTip("Off: the instrument is opened on first use instead")
        self.args_form = _form()
        self.args_note = ui.label("", "small", wrap=True)
        self.extra = QtWidgets.QPlainTextEdit()
        self.extra.setProperty("role", "code")
        self.extra.setPlaceholderText("more arguments, one per line:  port = \"/dev/ttyUSB0\"")
        self.extra.setFixedHeight(64)
        editor = _form()
        editor.addRow(ui.label("Name", "label"), self.dev_name)
        editor.addRow(ui.label("Driver", "label"), driver_row)
        editor.addRow(ui.label("Folder", "label"), _browse_row(self.dev_folder, self._browse_dev_folder))
        editor.addRow(ui.label("Open at start", "label"), self.open_now)
        args = QtWidgets.QVBoxLayout()
        args.setSpacing(6)
        args.addWidget(ui.label("Arguments of its constructor", "label"))
        args.addWidget(self.args_note)
        args.addLayout(self.args_form)
        args.addWidget(self.extra)
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(12)
        right.addLayout(editor)
        right.addLayout(args)
        right.addStretch(1)
        devices, box2 = ui.card()
        box2.addWidget(ui.label("Devices", "h2"))
        box2.addWidget(ui.label("One per instrument. Each needs a driver: a Python class that talks to it.", "muted",
                                wrap=True))
        split = QtWidgets.QHBoxLayout()
        split.setSpacing(18)
        split.addLayout(side)
        split.addLayout(right, 1)
        box2.addLayout(split)

        self.python = QtWidgets.QLineEdit(sys.executable)
        self.then = QtWidgets.QComboBox()
        if window._systemd:
            self.then.addItem("Start it now, and at every login (background service)", self.START_SERVICE)
        self.then.addItem("Start it now, until it is stopped", self.START_ONCE)
        self.then.addItem("Only save the file", self.SAVE_ONLY)
        running, box3 = ui.card()
        box3.addWidget(ui.label("Running it", "h2"))
        form3 = _form()
        form3.addRow(ui.label("Python", "label"), _browse_row(self.python, self._browse_python))
        form3.addRow(ui.label("Then", "label"), self.then)
        box3.addLayout(form3)
        box3.addWidget(ui.label("The Python must be the one that has the drivers' packages; a virtualenv next to "
                                "the driver or the config is picked by itself.", "small", wrap=True))

        area, content = ui.scroll_page(margins=(0, 0, 8, 0), spacing=14)
        for widget in (server, devices, running):
            content.addWidget(widget)
        content.addStretch(1)
        self.file_label = ui.label("", "label", select=True)
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setProperty("role", "code")
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.problem = ui.label("", "error", wrap=True)
        file_side = QtWidgets.QVBoxLayout()
        file_side.setSpacing(8)
        file_side.addWidget(ui.label("The file it writes", "h2"))
        file_side.addWidget(self.file_label)
        file_side.addWidget(self.preview, 1)
        file_side.addWidget(self.problem)
        columns = QtWidgets.QHBoxLayout()
        columns.setSpacing(20)
        columns.addWidget(area, 1)
        preview_side = QtWidgets.QWidget()
        preview_side.setLayout(file_side)
        preview_side.setFixedWidth(380)
        columns.addWidget(preview_side)

        self.create = ui.button("Create", "check", kind="primary", slot=self._create)
        cancel = ui.button("Cancel", slot=self.reject)
        bottom = QtWidgets.QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(cancel)
        bottom.addWidget(self.create)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(14)
        outer.addWidget(ui.label("New server", "h1"))
        outer.addWidget(ui.label("A server holds the connections to its instruments. Choose its devices; the "
                                 "config file is written for you, and you can edit it later.", "muted", wrap=True))
        outer.addLayout(columns, 1)
        outer.addLayout(bottom)
        self.resize(1180, 760)

        for edit in (self.name, self.folder, self.python):
            edit.textChanged.connect(self._update)
        self.reach.currentIndexChanged.connect(self._update)
        self.port.valueChanged.connect(self._update)
        self.then.currentIndexChanged.connect(self._update)
        self.dev_name.textEdited.connect(self._edited)
        self.driver.currentTextChanged.connect(self._driver_changed)
        self.dev_folder.textEdited.connect(self._driver_changed)
        self.open_now.toggled.connect(self._edited)
        self.extra.textChanged.connect(self._edited)
        self._refill_list()
        self._select(0)

    # -- the list of devices --------------------------------------------------
    def _refill_list(self) -> None:
        row = self.list.currentRow()
        blocked = self.list.blockSignals(True)
        self.list.clear()
        for d in self.devices:
            self.list.addItem(d.name or "(no name)")
        self.list.setCurrentRow(min(max(row, 0), len(self.devices) - 1))
        self.list.blockSignals(blocked)
        self.drop.setEnabled(len(self.devices) > 1)

    def _add(self) -> None:
        taken = {d.name for d in self.devices}
        name = next(f"device{i}" for i in range(1, 1000) if f"device{i}" not in taken)
        self.devices.append(DeviceSpec(name))
        self._refill_list()
        self.list.setCurrentRow(len(self.devices) - 1)
        self.dev_name.setFocus()
        self.dev_name.selectAll()

    def _remove(self) -> None:
        if len(self.devices) > 1:
            del self.devices[self.list.currentRow()]
            self._refill_list()
            self._select(self.list.currentRow())

    def current(self) -> DeviceSpec | None:
        row = self.list.currentRow()
        return self.devices[row] if 0 <= row < len(self.devices) else None

    def _select(self, row: int) -> None:
        d = self.current()
        if d is None:
            return
        self._loading = True
        self.dev_name.setText(d.name)
        self.driver.setCurrentText(d.driver)
        self.dev_folder.setText(d.folder)
        self.open_now.set_quietly(not d.lazy)
        self.extra.setPlainText(d.extra)
        self._loading = False
        self._show_args(d)
        self._update()

    # -- editing a device -------------------------------------------------------
    def _edited(self, *_) -> None:
        d = self.current()
        if d is None or self._loading:
            return
        d.name = self.dev_name.text().strip()
        d.driver = self.driver.currentText().strip()
        d.folder = self.dev_folder.text().strip()
        d.lazy = not self.open_now.isChecked()
        d.extra = self.extra.toPlainText()
        for key, edit in self._arg_edits.items():
            d.args[key] = edit.text()
        item = self.list.currentItem()
        if item is not None:
            item.setText(d.name or "(no name)")
        self._update()

    def _driver_changed(self, *_) -> None:
        if self._loading:
            return
        self._edited()
        d = self.current()
        if d is not None:
            self._show_args(d)
            if d.name.startswith("device") and d.driver in EXAMPLES:  # a better name than device1
                name = d.driver.partition(":")[2].removeprefix("Sim").lower()
                if name not in {o.name for o in self.devices}:
                    self.dev_name.setText(name)
                    self._edited()

    def _show_args(self, d: DeviceSpec) -> None:
        while self.args_form.rowCount():
            self.args_form.removeRow(0)
        self._arg_edits = {}
        known = constructor_args(d.driver, d.folder)
        d.texts = {name for name, _, text in known or () if text}
        if known is None:
            self.args_note.setText("Its arguments are not known until its folder is set (or it is an installed "
                                   "package); type them below." if d.driver else "Choose a driver first.")
        elif not known:
            self.args_note.setText("Its constructor takes no arguments (or inherits them); add any below.")
        else:
            self.args_note.setText("Leave a field empty to keep the driver's default.")
        for name, default, _ in known or ():
            edit = QtWidgets.QLineEdit(d.args.get(name, ""))
            edit.setPlaceholderText("required" if default is None else default)
            edit.textEdited.connect(self._edited)
            self._arg_edits[name] = edit
            self.args_form.addRow(ui.label(name, "label"), edit)

    def _browse_driver(self) -> None:
        start = self.dev_folder.text().strip() or str(Path.home())
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "The driver's Python file", start, "Python (*.py)")
        if not path:
            return
        path = Path(path)
        try:
            classes = list(classes_in(path.read_text()))
        except (OSError, SyntaxError, ValueError, UnicodeDecodeError) as e:
            QtWidgets.QMessageBox.warning(self, "New server", f"Cannot read {path.name}: {e}")
            return
        if not classes:
            QtWidgets.QMessageBox.warning(self, "New server", f"{path.name} defines no class.")
            return
        choice = classes[0]
        if len(classes) > 1:
            choice, ok = QtWidgets.QInputDialog.getItem(self, "New server", f"Which class of {path.name} is the "
                                                        "driver?", classes, 0, False)
            if not ok:
                return
        self.dev_folder.setText(str(path.parent))
        self.driver.setCurrentText(f"{path.stem}:{choice}")
        d = self.current()
        if d is not None and d.name.startswith("device"):
            self.dev_name.setText(choice.lower())
        self._edited()
        self._show_args(self.current())
        if self.python.text() == sys.executable:
            self.python.setText(find_python(path))

    def _browse_dev_folder(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Folder of the driver",
                                                          self.dev_folder.text() or str(Path.home()))
        if path:
            self.dev_folder.setText(path)
            self._driver_changed()

    def _browse_folder(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Where to save the config", self.folder.text())
        if path:
            self.folder.setText(path)

    def _browse_python(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Python interpreter", str(Path(self.python.text()).parent))
        if path:
            self.python.setText(path)

    # -- the file ------------------------------------------------------------
    def path(self) -> Path:
        return Path(self.folder.text().strip() or servers_folder()).expanduser() / f"{self.name.text().strip()}.toml"

    def text(self) -> str:
        """The config file, or ValueError saying what is wrong."""
        name = self.name.text().strip()
        registry.check_name(name, "server name")
        server: dict = {"name": name}
        if self.reach.currentData():
            server["tcp"] = f"0.0.0.0:{self.port.value()}"
        names = [d.name for d in self.devices]
        if len(set(names)) != len(names):
            raise ValueError("two devices have the same name")
        devices = {d.name: d.table() for d in self.devices}
        return config.dump(server, devices, comment=(
            f"The insthub server \"{name}\", written by insthub gui; edit it freely.\n"
            f"Start it, in this folder, with:  insthub serve {self.path().name}"))

    def _update(self, *_) -> None:
        self.port.setVisible(bool(self.reach.currentData()))
        self.file_label.setText(str(self.path()))
        try:
            self.preview.setPlainText(self.text())
            self.problem.clear()
            ok = True
        except ValueError as e:
            self.problem.setText(str(e))
            ok = False
        self.create.setEnabled(ok)
        self.create.setText({self.START_SERVICE: "Create and start", self.START_ONCE: "Create and start",
                             self.SAVE_ONLY: "Create"}[self.then.currentData()])

    def _create(self) -> None:
        try:
            text = self.text()
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "New server", str(e))
            return
        path, name, python = self.path(), self.name.text().strip(), self.python.text().strip() or sys.executable
        then = self.then.currentData()
        if then != self.SAVE_ONLY and not os.access(python, os.X_OK):
            QtWidgets.QMessageBox.warning(self, "New server", f"{python} is not a program.")
            return
        if path.exists() and not self.window.confirm("Replace it?", f"{path} exists already. Replace it?", "Replace"):
            return
        if then != self.SAVE_ONLY and any(s.name == name and s.origin == "local" for s in self.window.servers.values()):
            if not self.window.confirm("A server of that name runs", f"A server named “{name}” is running already, "
                                       "and a second one cannot take its name. Save the file anyway?", "Save"):
                return
            then = self.SAVE_ONLY
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "New server", f"Cannot write {path}: {e}")
            return
        self.accept()
        if then == self.START_SERVICE:
            self.window.act(f"Installing {service.PREFIX}{name}", lambda: service.install(path, name, python=python),
                            f"{service.PREFIX}{name} installed and started")
        elif then == self.START_ONCE:
            self.window.start_server([python, "-m", "insthub", "serve", str(path)], name, cwd=path.parent)
        else:
            self.window.say(f"Saved {path}. Start it with: insthub serve {path}", 10000)


# ---------------------------------------------------------------- a background service
class ServiceDialog(QtWidgets.QDialog):
    """Which server to keep running, under what name, with which Python."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Keep a server running")
        self.target, self.name, self.python = QtWidgets.QLineEdit(), QtWidgets.QLineEdit(), QtWidgets.QLineEdit()
        self.target.setPlaceholderText("a .toml config or a .py server script")
        self.target.textChanged.connect(self._guess)
        for edit in (self.name, self.python):
            edit.textChanged.connect(self._show_command)
        form = _form()
        form.addRow(ui.label("Server", "label"), _browse_row(self.target, self._browse_target))
        form.addRow(ui.label("Name", "label"), self.name)
        form.addRow(ui.label("Python", "label"), _browse_row(self.python, self._browse_python))
        self.command = QtWidgets.QLineEdit()
        self.command.setProperty("role", "code")
        self.command.setReadOnly(True)
        self.install = ui.button("Install and start", "check", kind="primary", slot=self._check)
        bottom = QtWidgets.QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(ui.button("Cancel", slot=self.reject))
        bottom.addWidget(self.install)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(22, 20, 22, 18)
        box.setSpacing(12)
        box.addWidget(ui.label("Keep a server running", "h1"))
        box.addWidget(ui.label("It starts now and at every login, and is started again if it crashes (a systemd "
                               "user service). Python must be the interpreter that has the drivers' packages: a "
                               "virtualenv next to the server file is picked by itself.", "muted", wrap=True))
        box.addLayout(form)
        box.addWidget(ui.label("It runs:", "small"))
        box.addWidget(self.command)
        box.addLayout(bottom)
        self.resize(680, 0)

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
        self._show_command()

    def _show_command(self, *_) -> None:
        target, _, python = self.values()
        try:
            self.command.setText(shlex.join(service.command_for(target, python)) if target else "")
        except Exception as e:  # noqa: BLE001 - e.g. no such file: say so where the command would be
            self.command.setText(str(e))

    def values(self) -> tuple[str, str, str]:
        return self.target.text().strip(), self.name.text().strip(), self.python.text().strip() or sys.executable

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
            QtWidgets.QMessageBox.warning(self, "Keep a server running", problem)
            return
        self.accept()

        def install():
            service.install(target, name, python=python)
            return service.linger_hint()

        def done(hint):
            message = f"{service.PREFIX}{name} installed and started"
            return message + (f"; it starts at login (at boot: {hint})" if hint else "")

        self.window.act(f"Installing {service.PREFIX}{name}", install, done)


# ---------------------------------------------------------------- devices on another computer
def ask_server(address: str, timeout: float = 4.0) -> tuple[dict, dict]:
    """(its info, its devices) from the server at a tcp:// address."""
    transport = _SocketTransport(address, connect_timeout=timeout)
    try:
        info = transport.request("", "info", (), {}).result(timeout)
        devices = transport.request("", "devices", (), {}).result(timeout)
    finally:
        transport.close()
    return info, devices


def tcp_address(text: str) -> str:
    """'192.168.1.20:8700' -> 'tcp://192.168.1.20:8700'."""
    text = text.strip()
    address = text if "://" in text else f"tcp://{text}"
    host, _, port = address.partition("://")[2].rpartition(":")
    if not host or not port.isdigit():
        raise ValueError("type the address as host:port, e.g. 192.168.1.20:8700")
    return address


class RemoteDialog(QtWidgets.QDialog):
    """Ask a server on another computer which devices it has; list them in devices.toml."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.address: str | None = None
        self.setWindowTitle("Devices on another computer")
        self.where = QtWidgets.QLineEdit()
        self.where.setPlaceholderText("host:port, e.g. 192.168.1.20:8700")
        self.where.returnPressed.connect(self.ask)
        self.ask_button = ui.button("Ask it", "search", tip="Connect and ask which devices it has", slot=self.ask)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.where, 1)
        row.addWidget(self.ask_button)
        self.status = ui.label("", "muted", wrap=True)
        self.list = QtWidgets.QListWidget()
        self.list.setProperty("card", True)
        self.list.itemChanged.connect(self._count)
        self.list.hide()
        self.add = ui.button("Add", "plus", kind="primary", slot=self._add)
        self.add.setEnabled(False)
        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(ui.label(f"Written to {registry.devices_file()}", "small", select=True), 1)
        bottom.addWidget(ui.button("Cancel", slot=self.reject))
        bottom.addWidget(self.add)
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(22, 20, 22, 18)
        box.setSpacing(12)
        box.addWidget(ui.label("Devices on another computer", "h1"))
        box.addWidget(ui.label("Programs on this computer then find them by name, as if they were here. The "
                               "server there must listen on TCP: tcp = \"0.0.0.0:PORT\" in its config, or "
                               "“Other computers too” when it is made with New server.", "muted", wrap=True))
        box.addLayout(row)
        box.addWidget(self.status)
        box.addWidget(self.list, 1)
        box.addLayout(bottom)
        self.resize(640, 460)

    def ask(self) -> None:
        try:
            address = tcp_address(self.where.text())
        except ValueError as e:
            self.status.setText(str(e))
            return
        self.status.setText(f"Asking {address}...")
        self.ask_button.setEnabled(False)

        def done(result):
            self.ask_button.setEnabled(True)
            info, devices = result
            self.address = address
            self._show(info, devices)

        def failed(exc):
            self.ask_button.setEnabled(True)
            self.status.setText(f"No answer from {address}: {exc}")
            self.list.hide()
            self.add.setEnabled(False)

        self.window.submit(self.window.actions, lambda: ask_server(address), done, failed)

    def _show(self, info: dict, devices: dict) -> None:
        known = registry.static_devices()
        local = {n for s in self.window.servers.values() if s.origin == "local" for n in s.device_names()}
        self.list.clear()
        for name, d in sorted(devices.items()):
            note = []
            if name in local:
                note.append("a device of this name runs here, and is found first")
            if known.get(name) not in (None, self.address):
                note.append(f"replaces {known[name]}")
            item = QtWidgets.QListWidgetItem(f"{name}  ·  {d['type']}" + (f"  ({'; '.join(note)})" if note else ""))
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if known.get(name) != self.address else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.status.setText(f"“{info.get('server', '?')}” (pid {info.get('pid', '?')}) has "
                            f"{len(devices)} device{'s' * (len(devices) != 1)}"
                            + (": tick the ones to use from here." if devices else "."))
        self.list.setVisible(bool(devices))
        self._count()

    def chosen(self) -> list[str]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def _count(self, *_) -> None:
        n = len(self.chosen())
        self.add.setEnabled(bool(n) and self.address is not None)
        self.add.setText(f"Add {n} device{'s' * (n != 1)}" if n else "Add")

    def _add(self) -> None:
        names, address = self.chosen(), self.address
        try:
            path = registry.edit_static_devices({name: address for name in names})
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.warning(self, "Devices on another computer", str(e))
            return
        self.accept()
        self.window.say(f"{', '.join(names)} added to {path}")
        self.window.refresh()
