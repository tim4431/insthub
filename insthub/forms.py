"""The dialogs of `insthub gui` that set things up, so that nothing has to be
typed into a file by hand.  Each shows the file it writes or the command it runs.

    ServerDialog      a server's config.  New: its name, who may reach it, and its devices;
                      for each a driver (one an extension offers, "module:Class", or a .py
                      file to pick a class from) and the arguments of its constructor, which
                      are read from the driver's source without running it.  An extension's
                      device brings its keys, and its Python to run the server (one Python
                      per server: it says which devices another would suit).  Writes
                      NAME.toml (config.dump), then starts
                      it as a background service (at login or not), or once, or not at all.
                      Existing (`path=`): the same form, filled from the file; saving changes
                      only what was changed (tomlfile.edit: comments stay), then restarts it.
    ServiceDialog     keep an existing .toml config or .py server running in the background
    RemoteDialog      a server on another computer: asks it which devices it has, and adds
                      them to devices.toml (registry.edit_static_devices)

The window that opens them (manager.Manager) runs whatever may block on its worker threads.
"""
from __future__ import annotations

import ast
import dataclasses
import difflib
import inspect
import json
import os
import re
import shlex
import sys
import tomllib
from pathlib import Path

from . import cli, config, extensions, registry, service, tomlfile
from . import widgets as ui
from .client import connect
from .inventory import server_python
from .qt import QtCore, QtGui, QtWidgets

Qt = QtCore.Qt

_DRIVER, _OFFER = Qt.ItemDataRole.UserRole, Qt.ItemDataRole.UserRole + 1  #: what an entry of _Offers holds


class _Offers(QtGui.QStandardItemModel):
    """The driver list: the devices that extensions offer (insthub's own simulated instruments
    among them), each shown as its extension's title and its doc.  Choosing one puts its driver
    into the field, because an editable combo box fills its field from an entry's edit role
    (here the driver, _DRIVER) and shows the display role in its list.  _OFFER: (extension, offer)."""

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        return super().data(index, _DRIVER if role == Qt.ItemDataRole.EditRole else role)

    def add(self, ext: extensions.Extension) -> None:
        """The devices an extension offers, unless they are listed already."""
        if any(self.item(row).data(_OFFER)[0].manifest == ext.manifest for row in range(self.rowCount())):
            return
        for offer in ext.devices.values():
            item = QtGui.QStandardItem(f"{ext.title}: {offer.doc or offer.name}")
            item.setData(offer.driver, _DRIVER)
            item.setData((ext, offer), _OFFER)
            item.setToolTip(offer.driver if ext.builtin else f"{offer.driver}, from {ext.folder}")
            self.appendRow(item)

    def offer(self, driver: str) -> extensions.DeviceOffer | None:
        """The device offered with this driver, if exactly one is (a manifest may offer one driver
        twice, as a real instrument and as a simulated one: then only a choice in the list tells)."""
        offers = [offer for row in range(self.rowCount()) if (offer := self.item(row).data(_OFFER)[1]).driver == driver]
        return offers[0] if len(offers) == 1 else None


def servers_folder() -> Path:
    """Where new server configs go unless another folder is chosen."""
    return registry.config_dir() / "servers"


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


def constructor_args(driver: str, folder: str = "", base: str | Path | None = None
                     ) -> list[tuple[str, str | None, bool]] | None:
    """The constructor arguments of a driver ("module:Class"), or None where they cannot
    be known without importing it.  Only insthub's own drivers are imported; any other
    is read from its .py file in `folder` (a relative one counts from `base`, the folder
    the server runs in: the config's)."""
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
    path = driver_file(driver, folder, base)
    if path is None:
        return None
    try:
        return classes_in(path.read_text()).get(name)
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return None


def driver_file(driver: str, folder: str = "", base: str | Path | None = None) -> Path | None:
    """The .py file of a driver ("module:Class") in `folder` (a relative one counts from `base`, as
    in a server file: config.device_folder), if there."""
    module = driver.partition(":")[0]
    where = config.device_folder({"path": folder}, base) if module else None
    return extensions.module_file(module, where) if where is not None else None


# ---------------------------------------------------------------- a new server
@dataclasses.dataclass
class DeviceSpec:
    """One device of a server's config, as shown in the dialog.  `loaded` and `original`
    remember what the file said, so that a field nobody touched is written back exactly
    as it was (its type, its spaces, an empty string)."""

    name: str
    driver: str = ""
    folder: str = ""
    lazy: bool = False
    description: str = ""  #: what this one is, in a few words (shown on its tile)
    args: dict = dataclasses.field(default_factory=dict)  #: constructor argument -> text typed (empty: default)
    texts: set = dataclasses.field(default_factory=set)  #: arguments whose default is text
    strings: set = dataclasses.field(default_factory=set)  #: keys that are text in the file
    extra: str = ""  #: more keys, as TOML lines:  key = value
    loaded: dict = dataclasses.field(default_factory=dict)  #: argument -> the text its field was filled with
    in_kwargs: set = dataclasses.field(default_factory=set)  #: arguments the file gives under kwargs
    known: set | None = None  #: the arguments of its driver as it is now (None: not known)
    original: dict = dataclasses.field(default_factory=dict)  #: the device's table in the file
    was: str | None = None  #: its name in the file (None: a new device)
    dirty: bool = False  #: changed in the dialog
    #: (extension, DeviceOffer) chosen in the driver list, while its driver is the one in the field
    offer: tuple | None = None
    auto_name: str = ""  #: the name the dialog gave it after what was chosen (not one typed)

    def table(self) -> dict:
        """Its [devices.NAME] table.  Raises ValueError for what cannot be written."""
        registry.check_name(self.name, "device name")
        if ":" not in self.driver:
            raise ValueError(f"{self.name}: the driver must look like module:Class")
        old = self.original
        out: dict = {"driver": self.driver.strip()}
        if self.folder.strip():
            out["path"] = old["path"] if old.get("path") == self.folder.strip() else self.folder.strip()
        if self.lazy:
            out["lazy"] = True
        elif old.get("lazy") is False:
            out["lazy"] = False  # written as false in the file: stays so
        if self.description.strip():
            text = old.get("description")
            out["description"] = text if isinstance(text, str) and text.strip() == self.description.strip() else \
                self.description.strip()
        old_kwargs = self._old_kwargs()
        kwargs: dict = {}  # what the dialog shows of them: its fields, and the kwargs.* lines of more keys
        for key, text in self.args.items():
            if self.known is not None and key not in self.known:
                continue  # an argument of another driver chosen before (kept, should that one come back)
            inside, value = self.argument(key, text)
            target = kwargs if inside else out
            if value is not None:
                target[key] = value
            else:
                target.pop(key, None)
        try:
            more = tomllib.loads(self.extra)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{self.name}: more keys: {e} (write them as in the file:  key = value)") from None
        for key, value in more.items():
            if key == "kwargs" and isinstance(value, dict):
                kwargs.update({k: old_kwargs[k] if k in old_kwargs and tomlfile.same(old_kwargs[k], v) else v
                               for k, v in value.items()})
            else:
                out[key] = old[key] if key in old and tomlfile.same(old[key], value) else value
        if kwargs:
            out["kwargs"] = kwargs
        return out

    def _old_kwargs(self) -> dict:
        return self.original["kwargs"] if isinstance(self.original.get("kwargs"), dict) else {}

    def argument(self, key: str, text: str) -> tuple[bool, object]:
        """(whether it goes under kwargs, its value; None: not given) of an argument whose field holds
        `text`.  Raises ValueError for what cannot be written."""
        # where the file gives it, and an argument called like a key of insthub's: under kwargs
        inside = key in config._RESERVED or key in self.in_kwargs
        if key in self.loaded and text == self.loaded[key]:
            return inside, (self._old_kwargs() if inside else self.original).get(key)  # untouched: as it was
        if not text.strip():
            return inside, None  # emptied: the driver's default
        if key in self.texts or key in self.strings:
            return inside, text.strip()
        value = cli._value(text.strip())
        if value is None:
            raise ValueError(f"{self.name}: {key} = null cannot be written (a TOML file has no null); "
                             "leave the field empty to keep the driver's default")
        return inside, value


_SHOWN = ("driver", "path", "lazy", "description")  #: keys of a device table that have their own field


def _gone(old: dict, new: dict, path: tuple = ()) -> list[str]:
    """What is in `old` but not in `new`, as HTML: the tables, and the keys with the table they were in."""
    out = []
    for k, v in old.items():
        if k not in new:
            if isinstance(v, dict):
                out.append(f"<code>{ui.esc(tomlfile.header((*path, k)))}</code>")
            else:
                line = f"{tomlfile.key(k)} = {tomlfile.value(v)}"
                line = line if len(line) <= 60 else line[:57] + "..."
                out.append(f"<code>{ui.esc(line)}</code>" + (f" in <code>{ui.esc(tomlfile.header(path))}</code>"
                                                              if path else ""))
        elif isinstance(v, dict) and isinstance(new[k], dict):
            out += _gone(v, new[k], (*path, k))
    return out


def read_config(text: str, base: str | Path | None = None) -> tuple[dict, list[DeviceSpec]]:
    """The [server] table and the devices of a config, as the dialog shows them: each
    constructor argument the driver declares in a field of its own, the rest as TOML lines
    of `key = value`.  `base`: the config's folder.  Raises ValueError for a file that is
    not an insthub config."""
    doc = tomllib.loads(text)
    server, devices = doc.get("server", {}), doc.get("devices", {})
    if not isinstance(server, dict) or not isinstance(devices, dict) or \
            any(not isinstance(t, dict) for t in devices.values()):
        raise ValueError("this is not an insthub server's config (its [server] and [devices.NAME] are not tables)")
    specs = []
    for name, table in devices.items():
        spec = DeviceSpec(name, str(table.get("driver", "")), str(table.get("path", "")), table.get("lazy") is True,
                          original=table, was=name)
        if isinstance(table.get("description"), str):
            spec.description = table["description"]
        arguments = constructor_args(spec.driver, spec.folder, base)
        known = spec.known = None if arguments is None else {arg for arg, _, _ in arguments}
        known = known or set()
        extra, kwargs = [], table.get("kwargs") if isinstance(table.get("kwargs"), dict) else {}

        def field(spec, key, value, inside=False):
            spec.args[key] = spec.loaded[key] = _text(value)
            if isinstance(value, str):
                spec.strings.add(key)
            if inside:
                spec.in_kwargs.add(key)

        for key, value in table.items():  # insthub's keys (driver, close, ...) never fill an argument's field
            if (key in _SHOWN and (key != "description" or isinstance(value, str))) or \
                    (key == "kwargs" and isinstance(value, dict)):
                continue
            if key in known and _plain(value) and key not in config._RESERVED and key not in kwargs:
                field(spec, key, value)
            else:
                extra.append(f"{tomlfile.key(key)} = {tomlfile.value(value)}")
        for key, value in kwargs.items():  # (what kwargs gives wins over a key of the same name)
            if key in known and _plain(value):
                field(spec, key, value, inside=True)
            else:
                extra.append(f"kwargs.{tomlfile.key(key)} = {tomlfile.value(value)}")
        spec.extra = "\n".join(extra)
        specs.append(spec)
    return server, specs


def _first_change(old: str, new: str) -> int | None:
    """The first line of `new` that differs from `old` (None: none does)."""
    a, b = old.split("\n"), new.split("\n")
    for op, _, _, b0, _ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            return min(b0, len(b) - 1)
    return None


def _placeholder(d: DeviceSpec) -> bool:
    """Whether a new device has no name of its own yet: the one Add gave it (device1), or one the
    dialog gave it after a driver chosen before.  The driver chosen may name it."""
    return d.was is None and (re.fullmatch(r"device\d+", d.name) is not None or d.name == d.auto_name)


def _offer_lines(ext: extensions.Extension, offer: extensions.DeviceOffer) -> list[str]:
    """The more-keys lines of a device an extension offers: its table (Extension.device_table) but
    the driver and the folder, which have fields of their own."""
    return [f"{tomlfile.key(k)} = {tomlfile.value(v)}" for k, v in ext.device_table(offer).items()
            if k not in ("driver", "path")]


def _same_python(a, b) -> bool:
    """Whether two paths name one Python: compared as written, made absolute, but not resolved (a
    virtualenv's python is a link to the Python it was made from)."""
    return a is not None and b is not None and os.path.abspath(a) == os.path.abspath(b)


def _plain(value) -> bool:
    """Whether a value fits in a field of its own (else it is shown under more keys)."""
    return isinstance(value, (str, int, float, bool))


def _given_in(extra: str, in_kwargs: set = frozenset()) -> set:
    """The constructor arguments that these more-keys lines give (kwargs.x, and x for a key that
    is not insthub's own, nor one that kwargs gives too: kwargs wins)."""
    try:
        more = tomllib.loads(extra)
    except tomllib.TOMLDecodeError:
        return set()
    inner = more.get("kwargs") if isinstance(more.get("kwargs"), dict) else {}
    return {k for k in more if k not in config._RESERVED and k not in in_kwargs} | set(inner)


def _browse_row(edit: QtWidgets.QLineEdit, slot) -> QtWidgets.QWidget:
    row = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(edit, 1)
    browse = ui.button("", "folder", tip="Choose...", slot=slot)
    browse.setFixedWidth(38)
    layout.addWidget(browse)
    row.browse = browse
    return row


def _form() -> QtWidgets.QFormLayout:
    form = QtWidgets.QFormLayout()
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setHorizontalSpacing(16)
    form.setVerticalSpacing(10)
    return form


LOCAL, TCP_HERE, NETWORK = "local", "tcp-here", "network"  #: who may reach a server


def _reach(tcp) -> tuple[str, int | None]:
    """(who may reach it, the port) from a [server] tcp value: none, 8765 or "127.0.0.1:8765"
    (TCP, but this computer only), "0.0.0.0:8765" (other computers too)."""
    if tcp is None:
        return LOCAL, None
    if isinstance(tcp, int) or (isinstance(tcp, str) and tcp.isdigit()):
        return TCP_HERE, int(tcp)
    host, _, port = str(tcp).rpartition(":")
    port = int(port) if port.isdigit() else None
    return (TCP_HERE if host.startswith("127.") or host in ("localhost", "::1", "[::1]") else NETWORK), port


def _tcp(reach: str, port: int):
    """The [server] tcp value for a choice of _reach (None: no TCP)."""
    return None if reach == LOCAL else port if reach == TCP_HERE else f"0.0.0.0:{port}"


class ServerDialog(QtWidgets.QDialog):
    """The config of a server: a new one (written with config.dump, then started), or an
    existing file (`path`), changed in place with tomlfile so that its comments and whatever
    is not changed stay as they are, then restarted."""

    SERVICE, ONCE, SAVE, RESTART = "service", "once", "save", "restart"

    def __init__(self, window, path: str | Path | None = None, server=None, select: str | None = None,
                 add: bool = False):
        super().__init__(window)
        self.window = window
        adding = add  # (below, `add` is the Add button)
        self.existing = Path(path).expanduser() if path else None
        self.server = server  #: inventory.Server of a running (or installed) server, when editing its file
        self._server_python = server_python(server) if server is not None else None
        self._arg_edits: dict[str, QtWidgets.QLineEdit] = {}
        self._loading = False
        if self.existing is not None:
            self._original = tomlfile.read(self.existing)
            table, self.devices = read_config(self._original, self.existing.parent)
            self._server_table = table
        else:
            self._original, self._server_table = "", {}
            self.devices = [DeviceSpec("scope", "insthub.sim:SimScope", auto_name="scope")]  # (another driver renames it)
        title = (f"Add a device to {self.existing.name}" if add else f"Edit {self.existing.name}") \
            if self.existing else "New server"
        self.setWindowTitle(title)

        self.name = QtWidgets.QLineEdit(str(self._server_table.get("name") or
                                            (self.existing.stem if self.existing else "lab")))
        self.folder = QtWidgets.QLineEdit(str(self.existing.parent if self.existing else servers_folder()))
        self.reach = QtWidgets.QComboBox()
        self.reach.addItem("This computer only", LOCAL)
        self.reach.addItem("This computer, also on a TCP port", TCP_HERE)
        self.reach.addItem("Other computers too, on a TCP port", NETWORK)
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.port.setPrefix("port ")
        reach_now, port = _reach(self._server_table.get("tcp"))
        self.reach.setCurrentIndex(self.reach.findData(reach_now))
        self.port.setValue(port or 8700)
        self._reach_initial = (reach_now, self.port.value())
        reach = QtWidgets.QHBoxLayout()
        reach.setSpacing(8)
        reach.addWidget(self.reach, 1)
        reach.addWidget(self.port)
        server_card, box = ui.card()
        box.addWidget(ui.label("Server", "h2"))
        form = _form()
        form.addRow(ui.label("Name", "label"), self.name)
        folder_row = _browse_row(self.folder, self._browse_folder)
        if self.existing:
            self.folder.setReadOnly(True)
            folder_row.browse.hide()
        form.addRow(ui.label("Saved in", "label"), folder_row)
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
        self.dev_description = QtWidgets.QLineEdit()
        self.dev_description.setPlaceholderText("optional: what this one is, e.g. the one on the optics table")
        self.dev_description.setToolTip("Shown on its tile, to tell it from other instruments of its kind")
        self.driver = QtWidgets.QComboBox()
        self.driver.setEditable(True)
        self.driver.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.offers = _Offers(self.driver)
        self.driver.setModel(self.offers)
        self.offers.add(extensions.load(extensions.OWN, "insthub"))  # at once; the others once they are found
        window.submit(window.actions, extensions.discover, self._found)
        self.driver.lineEdit().setPlaceholderText("module:Class, e.g. mydriver:PowerSupply")
        driver_row = QtWidgets.QHBoxLayout()
        driver_row.setSpacing(8)
        driver_row.addWidget(self.driver, 1)
        driver_row.addWidget(ui.button("From a file...", "file", tip="Pick the .py file of your driver, then its "
                                       "class", slot=self._browse_driver))
        self.dev_folder = QtWidgets.QLineEdit()
        self.dev_folder.setPlaceholderText("where its module is, if it is not an installed package")
        self.open_now = ui.Switch(True)
        self.open_now.setToolTip("On: the instrument is opened when the server starts. Off (lazy): on first use, "
                                 "e.g. for one that is often unplugged or shared with other software")
        self.args_form = _form()
        self.args_note = ui.label("", "small", wrap=True)
        self.extra = QtWidgets.QPlainTextEdit()
        self.extra.setProperty("role", "code")
        self.extra.setPlaceholderText("more keys, one per line, as in the file:  port = \"/dev/ttyUSB0\"")
        self.extra.setFixedHeight(70)
        editor = _form()
        editor.addRow(ui.label("Name", "label"), self.dev_name)
        editor.addRow(ui.label("Description", "label"), self.dev_description)
        editor.addRow(ui.label("Driver", "label"), driver_row)
        self.driver_note = ui.label("", "small", wrap=True)  # when the driver may not import in the server's Python
        self.driver_note.hide()
        editor.addRow("", self.driver_note)
        editor.addRow(ui.label("Folder", "label"), _browse_row(self.dev_folder, self._browse_dev_folder))
        open_row = QtWidgets.QHBoxLayout()
        open_row.addWidget(self.open_now)
        self.open_hint = ui.label("", "small")
        open_row.addWidget(self.open_hint, 1)
        editor.addRow(ui.label("Open at start", "label"), open_row)
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
        devices_card, box2 = ui.card()
        box2.addWidget(ui.label("Devices", "h2"))
        box2.addWidget(ui.label("One per instrument. Each needs a driver: a Python class that talks to it.", "muted",
                                wrap=True))
        split = QtWidgets.QHBoxLayout()
        split.setSpacing(18)
        split.addLayout(side)
        self.device_editor = QtWidgets.QWidget()
        self.device_editor.setLayout(right)
        right.setContentsMargins(0, 0, 0, 0)
        split.addWidget(self.device_editor, 1)
        box2.addLayout(split)

        self.python = QtWidgets.QLineEdit(sys.executable)
        self._python_set = sys.executable  #: what the dialog put there (another text: chosen by hand, see _fit_python)
        self.then = QtWidgets.QComboBox()
        self.login = ui.Switch(True)
        self.login.setToolTip("Start the service at every login (and at boot, with lingering on)")
        running_card, box3 = ui.card()
        box3.addWidget(ui.label("After saving" if self.existing else "Running it", "h2"))
        form3 = _form()
        self.python_row = _browse_row(self.python, self._browse_python)
        if not self.existing:
            form3.addRow(ui.label("Python", "label"), self.python_row)
        form3.addRow(ui.label("Then", "label"), self.then)
        self.login_label = ui.label("Start at login", "label")
        form3.addRow(self.login_label, self.login)
        box3.addLayout(form3)
        self.then_note = ui.label("", "small", wrap=True)
        box3.addWidget(self.then_note)
        self._fill_then()

        area, content = ui.scroll_page(margins=(0, 0, 8, 0), spacing=14)
        for widget in (server_card, devices_card, running_card):
            content.addWidget(widget)
        content.addStretch(1)
        self.file_label = ui.label("", "label")
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setProperty("role", "code")
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.removed = ui.label("", "small", wrap=True, select=True)  # the lines saving removes
        self.removed.setTextFormat(Qt.TextFormat.RichText)
        self.problem = ui.label("", "error", wrap=True)
        file_side = QtWidgets.QVBoxLayout()
        file_side.setSpacing(8)
        file_side.addWidget(ui.label("The file after saving" if self.existing else "The file it writes", "h2"))
        file_side.addWidget(self.file_label)
        if self.existing:
            file_side.addWidget(ui.label("What saving changes is highlighted.", "small"))
        file_side.addWidget(self.preview, 1)
        file_side.addWidget(self.removed)
        file_side.addWidget(self.problem)
        columns = QtWidgets.QHBoxLayout()
        columns.setSpacing(20)
        columns.addWidget(area, 1)
        preview_side = QtWidgets.QWidget()
        preview_side.setLayout(file_side)
        preview_side.setFixedWidth(400)
        columns.addWidget(preview_side)

        self.create = ui.button("Save" if self.existing else "Create", "check", kind="primary", slot=self._create)
        cancel = ui.button("Cancel", slot=self.reject)
        bottom = QtWidgets.QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(cancel)
        bottom.addWidget(self.create)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(14)
        outer.addWidget(ui.label(title, "h1"))
        outer.addWidget(ui.label(
            "Only what you change is written into the file; its comments, and everything else, stay as they are. "
            "The server reads it when it starts." if self.existing else
            "A server holds the connections to its instruments. Choose its devices; the config file is written "
            "for you, and you can edit it later.", "muted", wrap=True))
        outer.addLayout(columns, 1)
        outer.addLayout(bottom)
        self.resize(1200, 780)

        for edit in (self.name, self.folder, self.python):
            edit.textChanged.connect(self._update)
        self.reach.currentIndexChanged.connect(self._reach_changed)
        self.port.valueChanged.connect(self._reach_changed)
        self.then.currentIndexChanged.connect(self._update)
        self.dev_name.textEdited.connect(self._edited)
        self.dev_description.textEdited.connect(self._edited)
        self.driver.currentTextChanged.connect(self._driver_changed)
        self.driver.activated.connect(self._offer_chosen)
        self.dev_folder.textEdited.connect(self._driver_changed)
        self.open_now.toggled.connect(self._edited)
        self.extra.textChanged.connect(self._edited)
        self._refill_list()
        names = [d.name for d in self.devices]
        row = names.index(select) if select in names else 0
        self.list.setCurrentRow(row)
        self._select(row)
        if adding:  # a new device, to fill in
            self._add()

    def _fill_then(self) -> None:
        """What may follow the saving: for a new server, how to start it; for an existing file,
        whether to restart the server that runs it."""
        then, s, window = self.then, self.server, self.window
        then.clear()
        if not self.existing:
            if window._systemd:
                then.addItem("Start it as a background service", self.SERVICE)
            then.addItem("Start it now, until it is stopped", self.ONCE)
            then.addItem("Only save the file", self.SAVE)
            return
        running = s is not None and s.origin == "local"
        if s is not None and s.service:
            then.addItem("Restart the service, to use it" if running else "Start the service, to use it",
                         self.RESTART)
        elif running and window.started_here(s.pid):
            then.addItem("Restart the server, to use it", self.RESTART)
        then.addItem("Only save the file", self.SAVE)

    # -- the list of devices --------------------------------------------------
    def _refill_list(self) -> None:
        row = self.list.currentRow()
        blocked = self.list.blockSignals(True)
        self.list.clear()
        for d in self.devices:
            self.list.addItem(d.name or "(no name)")
        self.list.setCurrentRow(min(max(row, 0), len(self.devices) - 1))
        self.list.blockSignals(blocked)
        self.drop.setEnabled(len(self.devices) > (0 if self.existing else 1))  # a new server needs a device

    def _add(self) -> None:
        taken = {d.name for d in self.devices}
        name = next(f"device{i}" for i in range(1, 1000) if f"device{i}" not in taken)
        self.devices.append(DeviceSpec(name, dirty=True))
        self._refill_list()
        self.list.setCurrentRow(len(self.devices) - 1)
        self._select(len(self.devices) - 1)
        self.dev_name.setFocus()
        self.dev_name.selectAll()

    def _remove(self) -> None:
        if self.current() is not None and len(self.devices) > (0 if self.existing else 1):
            del self.devices[self.list.currentRow()]
            self._refill_list()
            self._fit_python()
            self._select(self.list.currentRow())

    def current(self) -> DeviceSpec | None:
        row = self.list.currentRow()
        return self.devices[row] if 0 <= row < len(self.devices) else None

    def _select(self, row: int) -> None:
        d = self.current()
        self.device_editor.setEnabled(d is not None)
        if d is None:
            self._update()
            return
        self._loading = True
        self.dev_name.setText(d.name)
        self.dev_description.setText(d.description)
        self.driver.setCurrentText(d.driver)
        self.dev_folder.setText(d.folder)
        self.open_now.set_quietly(not d.lazy)
        self.extra.setPlainText(d.extra)
        self._loading = False
        self._show_args(d)
        self._update()

    # -- editing a device -------------------------------------------------------
    def _base(self) -> Path | None:
        """The folder a relative driver folder counts from: the config's."""
        return self.existing.parent if self.existing else None

    def _edited(self, *_) -> None:
        d = self.current()
        if d is None or self._loading:
            return
        d.dirty = True
        d.name = self.dev_name.text().strip()
        d.description = self.dev_description.text()
        d.driver = self.driver.currentText().strip()
        d.folder = self.dev_folder.text().strip()
        d.lazy = not self.open_now.isChecked()
        d.extra = self.extra.toPlainText()
        for key, edit in self._arg_edits.items():
            d.args[key] = edit.text()
        self._mark_given()
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
            if d.offer is not None and d.driver != d.offer[1].driver:  # another driver typed in its place
                self._drop_offer(d)
            self._show_args(d)
            offer = self.offers.offer(d.driver)
            if offer is not None:
                self._name_after(d, offer.name)
            self._fit_python()
            self._edited()  # (again: what is written depends on the arguments of the driver now chosen)

    def _name_after(self, d: DeviceSpec, name: str) -> None:
        """Name a device that has no name of its own yet (_placeholder) after what was chosen for it,
        unless another device has that name."""
        if _placeholder(d) and name not in {o.name for o in self.devices if o is not d}:
            self.dev_name.setText(name)
            d.auto_name = name

    def _found(self, found: list) -> None:
        """The devices of the other extensions, once discover() has found them (on a worker thread)."""
        text, blocked = self.driver.currentText(), self.driver.blockSignals(True)
        for ext in found:
            if ext.usable:
                self.offers.add(ext)
        self.driver.setEditText(text)
        self.driver.blockSignals(blocked)

    def _offer_chosen(self, row: int) -> None:
        """A device an extension offers was chosen from the list: its driver, its folder and the
        keys its manifest gives (more keys, so that they are written as they are), named after it
        if it has no name of its own yet, and run by the extension's Python (_fit_python)."""
        d, chosen = self.current(), self.offers.item(row).data(_OFFER) if row >= 0 else None
        if d is None or chosen is None:
            return
        ext, offer = chosen
        self._loading = True
        self._name_after(d, offer.name)
        self.driver.setEditText(offer.driver)
        self.dev_folder.setText(ext.device_table(offer).get("path", ""))
        self.extra.setPlainText("\n".join(_offer_lines(ext, offer)))
        self._loading = False
        d.offer = chosen
        self._driver_changed()

    def _drop_offer(self, d: DeviceSpec) -> None:
        """Another driver than that of the device chosen from the list: the device is no longer that
        one, so the keys the choice brought go from more keys (those still as they were), above all
        its `extension`, which would announce an extension it is not from.  The folder stays: the
        driver typed may be another class of it."""
        brought = set(_offer_lines(*d.offer))
        d.offer = None
        self._loading = True
        self.extra.setPlainText("\n".join(line for line in self.extra.toPlainText().split("\n") if line not in brought))
        self._loading = False

    def _needed_python(self, d: DeviceSpec) -> str | None:
        """The Python a device's code is to run in, if it says: that of the extension it was chosen
        from (none for insthub's own, which runs in any Python that has insthub), else as for a
        device of a server file (extensions.device_python: its folder's extension, or virtualenv)."""
        if d.offer is not None:
            ext = d.offer[0]
            return None if ext.builtin else ext.python
        return extensions.device_python({"driver": d.driver, "path": d.folder}, self._base())

    def _fit_python(self) -> None:
        """A new server runs in the Python its devices' code needs (_needed_python): that of the
        first device that needs one, else insthub's own.  One chosen by hand stays; _check_python
        says which devices it may not suit."""
        if self.existing is not None or self.python.text() != self._python_set:
            return
        self._python_set = next((p for d in self.devices if (p := self._needed_python(d))), sys.executable)
        self.python.setText(self._python_set)

    def _show_args(self, d: DeviceSpec) -> None:
        while self.args_form.rowCount():
            self.args_form.removeRow(0)
        self._arg_edits = {}
        known = constructor_args(d.driver, d.folder, self._base())
        exact = d.driver.partition(":")[0].split(".")[0] == "insthub"  # imported: [] means it takes none
        if known or (known == [] and exact):  # its own arguments, each in a field (another driver's, typed
            # before, are kept but not written)
            d.texts = {name for name, _, text in known if text}
            d.known = {name for name, _, _ in known}
            fields = [(name, "required" if default is None else default) for name, default, _ in known]
            self.args_note.setText("Leave a field empty to keep the driver's default." if known else
                                   "Its constructor takes no arguments; give anything else under more keys.")
        else:  # not known here (or inherited): what the fields hold stays in sight, and is written as it is
            d.known = None
            fields = [(name, "") for name, text in d.args.items() if text.strip()]
            why = ("Its arguments are not known until its folder is set (or it is an installed package)"
                   if known is None else "Its constructor takes no arguments of its own (or inherits them)")
            self.args_note.setText(
                ("Choose a driver first." if not d.driver else f"{why}; give them under more keys.") +
                (" The fields below hold what was given before, and are written: empty one to leave it out."
                 if fields else ""))
        for name, hint in fields:
            edit = QtWidgets.QLineEdit(d.args.get(name, ""))
            edit.setProperty("hint", hint)
            edit.textEdited.connect(self._edited)
            self._arg_edits[name] = edit
            self.args_form.addRow(ui.label(name, "label"), edit)
        self._mark_given()

    def _check_python(self, d: DeviceSpec | None) -> None:
        """Say so when a device's code needs a Python (_needed_python) other than the one its server
        runs in: the server imports every driver in its one Python, which may not have this one's
        packages.  The server's: for a new one, the one chosen below; else that of its service or
        its command line, if known."""
        ours = (self.python.text().strip() or sys.executable) if self.existing is None else self._server_python
        theirs = self._needed_python(d) if d is not None and ours else None
        note = ""
        if theirs is not None and not _same_python(theirs, ours):
            own = (f"Its extension, {d.offer[0].title}, runs in {theirs}" if d.offer else
                   f"This driver's folder has a Python of its own ({theirs})")
            if self.existing is not None:
                note = (f"{own}, but {self.server.name} runs in {ours}. If the driver's packages are only in its "
                        "own, it will not load here: give it a server of its own (Add → New server...).")
            elif other := next((o.name for o in self.devices
                                if o is not d and _same_python(self._needed_python(o), ours)), None):
                note = (f"{own}, but the server runs in {ours}, which {other} needs: a server runs in one Python, "
                        "so host them in separate servers.")
            else:
                note = (f"{own}, but the server is to run in {ours}. If the driver's packages are only in its own, "
                        "it will not load there.")
        self.driver_note.setText(note)
        self.driver_note.setVisible(bool(note))

    def _mark_given(self) -> None:
        """An argument that more keys gives (a list, say) is set there, not in its field: say so."""
        d = self.current()
        given = _given_in(self.extra.toPlainText(), d.in_kwargs if d is not None else set())
        for name, edit in self._arg_edits.items():
            there = name in given
            edit.setReadOnly(there)
            edit.setPlaceholderText("given under more keys, below" if there else edit.property("hint"))
            edit.setToolTip("Its value is in the more keys below: change it there" if there else "")

    def _browse_driver(self) -> None:
        start = self.dev_folder.text().strip() or str(Path.home())
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "The driver's Python file", start, "Python (*.py)")
        if not path:
            return
        path = Path(path)
        try:
            classes = list(classes_in(path.read_text()))
        except (OSError, SyntaxError, ValueError, UnicodeDecodeError) as e:
            QtWidgets.QMessageBox.warning(self, self.windowTitle(), f"Cannot read {path.name}: {e}")
            return
        if not classes:
            QtWidgets.QMessageBox.warning(self, self.windowTitle(), f"{path.name} defines no class.")
            return
        choice = classes[0]
        if len(classes) > 1:
            choice, ok = QtWidgets.QInputDialog.getItem(self, self.windowTitle(), f"Which class of {path.name} is "
                                                        "the driver?", classes, 0, False)
            if not ok:
                return
        self.dev_folder.setText(str(path.parent))
        self.driver.setCurrentText(f"{path.stem}:{choice}")
        d = self.current()
        if d is not None:
            self._name_after(d, choice.lower())
        self._edited()
        self._show_args(self.current())
        self._fit_python()  # (the virtualenv next to the file, unless a Python was chosen by hand)

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

    def _reach_changed(self, *_) -> None:
        self._update()

    # -- the file ------------------------------------------------------------
    def path(self) -> Path:
        if self.existing is not None:
            return self.existing
        return Path(self.folder.text().strip() or servers_folder()).expanduser() / f"{self.name.text().strip()}.toml"

    def _tcp_wanted(self):
        """The tcp the form asks for: the file's own if "Reached from" is as it was, and with
        only the port changed, the file's address with the new port (one network, say)."""
        now, old = (self.reach.currentData(), self.port.value()), self._server_table.get("tcp")
        if self.existing is not None and old is not None and now[0] == self._reach_initial[0]:
            if now == self._reach_initial:
                return old
            if isinstance(old, str) and ":" in old:
                return f"{old.rpartition(':')[0]}:{now[1]}"
        return _tcp(*now)

    def changes(self) -> tuple[dict, dict, dict]:
        """What saving changes in the file: (first, changes, renames), `first` the devices removed
        whose name another one takes (renamed to it, or added), removed before the rest, and the
        rest as tomlfile.edit takes it.  Only devices changed in the dialog are looked at, and in
        them only what differs."""
        old = tomllib.loads(self._original)
        out, renames, first = {}, {}, {}
        name = self.name.text().strip()
        registry.check_name(name, "server name")
        server = {}
        if name != (self._server_table.get("name") or self.existing.stem):
            server["name"] = name
        tcp = self._tcp_wanted()
        if not tomlfile.same(tcp, self._server_table.get("tcp")):
            server["tcp"] = tcp
        if server:
            out[("server",)] = server
        names = [d.name for d in self.devices]
        if len(set(names)) != len(names):
            raise ValueError("two devices have the same name")
        before = old.get("devices", {})
        kept = {d.was for d in self.devices if d.was is not None}
        taken = {d.name for d in self.devices}
        for device in before:
            if device not in kept:  # removed; first if another device takes its name: it is a new table then
                (first if device in taken else out)[("devices", device)] = None
        sub_tables = set(tomlfile.tables(self._original))
        for d in self.devices:
            if d.was is None:
                out[("devices", d.name)] = d.table()
                continue
            if d.name != d.was:
                renames[("devices", d.was)] = ("devices", d.name)
            if not d.dirty:
                continue
            was, table = before.get(d.was, {}), d.table()
            diff = {k: v for k, v in table.items() if not tomlfile.same(was.get(k), v)}
            diff.update({k: None for k in was if k not in table})
            for key in [k for k in diff if ("devices", d.was, k) in sub_tables]:  # written as [devices.X.key]
                new, old_sub = diff.pop(key), was.get(key) or {}
                if not isinstance(new, dict) or not new:
                    out[("devices", d.name, key)] = None  # gone: the table too
                    if new is not None:
                        diff[key] = new
                    continue
                sub = {k: v for k, v in new.items() if not tomlfile.same(old_sub.get(k), v)}
                sub.update({k: None for k in old_sub if k not in new})
                if sub:
                    out[("devices", d.name, key)] = sub
            if diff:
                out[("devices", d.name)] = diff
        return first, out, renames

    def text(self) -> str:
        """The file as it will be, or ValueError saying what is wrong."""
        if self.existing is not None:
            first, changes, renames = self.changes()
            text = tomlfile.edit(self._original, first) if first else self._original
            return tomlfile.edit(text, changes, renames)
        name = self.name.text().strip()
        registry.check_name(name, "server name")
        server: dict = {"name": name}
        tcp = _tcp(self.reach.currentData(), self.port.value())
        if tcp is not None:
            server["tcp"] = tcp
        names = [d.name for d in self.devices]
        if len(set(names)) != len(names):
            raise ValueError("two devices have the same name")
        return config.dump(server, {d.name: d.table() for d in self.devices}, comment=(
            f"The insthub server \"{name}\", written by insthub gui; edit it freely.\n"
            f"Start it, in this folder, with:  insthub serve {self.path().name}"))

    def _show_changes(self, text: str) -> None:
        """Highlight in the preview the lines saving writes, and list the ones it removes."""
        self.removed.clear()
        if self.existing is None:
            self.preview.setExtraSelections([])
            return
        # (lines as Qt's blocks are: only a line feed ends one, not U+2028 and the like)
        old, new = (t.replace("\r\n", "\n").removesuffix("\n").split("\n") for t in (self._original, text))
        marks = []
        for op, a0, a1, b0, b1 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if op in ("replace", "insert"):
                for line in range(b0, b1):
                    block = self.preview.document().findBlockByNumber(line)
                    sel = QtWidgets.QTextEdit.ExtraSelection()
                    sel.format.setBackground(ui.color(ui.current().accent, 0.18))
                    sel.format.setProperty(QtGui.QTextFormat.Property.FullWidthSelection, True)
                    sel.cursor = QtGui.QTextCursor(block)
                    marks.append(sel)
        self.preview.setExtraSelections(marks)
        gone = self._gone(text)
        if gone:
            shown = "<br>".join(gone[:6])
            more = f"<br>and {len(gone) - 6} more" if len(gone) > 6 else ""
            self.removed.setText(f"Removed:<br>{shown}{more}")

    def _gone(self, text: str) -> list[str]:
        """What saving removes, as HTML: the devices removed, then the keys and tables gone from
        the rest (a device renamed is compared under its new name: a rename removes nothing)."""
        old, new = tomllib.loads(self._original), tomllib.loads(text)
        before = old.get("devices", {})
        names = {d.was: d.name for d in self.devices if d.was is not None}
        gone = [f"<code>{ui.esc(tomlfile.header(('devices', n)))}</code>" for n in before if n not in names]
        if "devices" in old:
            old["devices"] = {names[n]: table for n, table in before.items() if n in names}
            if not old["devices"] and "devices" not in new:
                del old["devices"]  # (no [devices] header of its own: nothing more is gone than its devices)
        return gone + _gone(old, new)

    def _update(self, *_) -> None:
        self.port.setVisible(self.reach.currentData() != LOCAL)
        path = str(self.path())
        self.file_label.setText(self.file_label.fontMetrics().elidedText(path, Qt.TextElideMode.ElideMiddle, 390))
        self.file_label.setToolTip(path)
        d = self.current()
        self.open_hint.setText("" if d is None else "lazy: opened on first use" if d.lazy else "")
        self._check_python(d)  # (here: a change of the Python below counts too)
        then = self.then.currentData()
        service_chosen = then == self.SERVICE
        self.login.setVisible(service_chosen)
        self.login_label.setVisible(service_chosen)
        self.then_note.setText({
            self.SERVICE: "A systemd user service: it is started again if it crashes, and its output is kept. "
                          "The Python must be the one that has the drivers' packages; a virtualenv next to the "
                          "driver or the config is picked by itself.",
            self.ONCE: "It runs in the background until you stop it (on its page), or, with lingering off, "
                       "you log out.",
            self.RESTART: "The server reads its config only when it starts.",
            self.SAVE: "" if self.existing is None else
                       "The server reads its config only when it starts: restart it to use the changes.",
        }.get(then, ""))
        try:
            text = self.text()
            # (compared with what it was given, not toPlainText(): Qt turns a no-break space into a space)
            shown, now = getattr(self, "_shown", ""), text.replace("\r\n", "\n")
            if shown != now:
                bar = self.preview.verticalScrollBar()
                at = bar.value()  # the preview stays where it was scrolled to ...
                self.preview.setPlainText(text)
                self._shown = now
                bar.setValue(at)
                where = _first_change(shown, now) if shown else None
                if where is not None:  # ... but shows the line this edit changed
                    self.preview.setTextCursor(QtGui.QTextCursor(self.preview.document().findBlockByNumber(where)))
                    self.preview.ensureCursorVisible()
            self._show_changes(text)
            self.problem.clear()
            ok = True
            unchanged = self.existing is not None and text == self._original
        except ValueError as e:
            self.problem.setText(str(e))
            ok, unchanged = False, False
        self.create.setEnabled(ok and not unchanged)
        if self.existing is not None:
            self.create.setText("Save and restart" if then == self.RESTART and self.server.origin == "local" else
                                "Save and start" if then == self.RESTART else "Save")
        else:
            self.create.setText("Create" if then == self.SAVE else "Create and start")

    def _create(self) -> None:
        try:
            text = self.text()
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, self.windowTitle(), str(e))
            return
        if self.existing is not None:
            self._save(text)
            return
        path, name, python = self.path(), self.name.text().strip(), self.python.text().strip() or sys.executable
        then = self.then.currentData()
        if then != self.SAVE and not os.access(python, os.X_OK):
            QtWidgets.QMessageBox.warning(self, "New server", f"{python} is not a program.")
            return
        if path.exists() and not self.window.confirm("Replace it?", f"{path} exists already. Replace it?", "Replace"):
            return
        if then != self.SAVE and any(s.name == name and s.origin == "local" for s in self.window.servers.values()):
            if not self.window.confirm("A server of that name runs", f"A server named “{name}” is running already, "
                                       "and a second one cannot take its name. Save the file anyway?", "Save"):
                return
            then = self.SAVE
        try:
            tomlfile.write(path, text)
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.warning(self, "New server", f"Cannot write {path}: {e}")
            return
        self.accept()
        if then == self.SERVICE:
            login = self.login.isChecked()
            self.window.act(f"Installing {service.PREFIX}{name}",
                            lambda: service.install(path, name, python=python, login=login),
                            f"{service.PREFIX}{name} installed and started"
                            + ("" if login else "; it does not start at login"))
        elif then == self.ONCE:
            self.window.start_server([python, "-m", "insthub", "serve", str(path)], name, cwd=path.parent)
        else:
            self.window.say(f"Saved {path}. Start it with: insthub serve {path}", 10000)

    def _save(self, text: str) -> None:
        path = self.existing
        try:
            now = tomlfile.read(path)
        except (OSError, UnicodeDecodeError) as e:
            QtWidgets.QMessageBox.warning(self, self.windowTitle(), f"Cannot read {path}: {e}")
            return
        if now != self._original and not self.window.confirm(  # changed by somebody else since it was read
                "The file changed", f"{path} was changed since this dialog read it. Save what is shown here over "
                "it? Those other changes are then lost; Cancel, and open it again, to see them.", "Save over them"):
            return
        try:
            tomlfile.write(path, text)  # exactly what the preview showed
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.warning(self, self.windowTitle(), f"Cannot write {path}: {e}")
            return
        self.accept()
        if self.then.currentData() == self.RESTART:
            self.window.restart_server(self.server, after=f"saved {path.name}", name=self.name.text().strip() or None)
        else:
            self.window.say(f"Saved {path.name}; restart the server to use it", 8000)


NewServerDialog = ServerDialog  #: its older name


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
        self.login = ui.Switch(True)
        self.login.setToolTip("Off: it runs until you stop it (or, with lingering off, you log out), and you "
                              "start it again from its page")
        form.addRow(ui.label("Start at login", "label"), self.login)
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
        box.addWidget(ui.label("It starts now and, unless you switch it off below, at every login; it is started "
                               "again if it crashes (a systemd user service). Python must be the interpreter that "
                               "has the drivers' packages: that of their extensions, or a virtualenv next to "
                               "the server file or its drivers, is picked by itself.", "muted", wrap=True))
        box.addLayout(form)
        box.addWidget(ui.label("It runs:", "small"))
        box.addWidget(self.command)
        box.addLayout(bottom)
        self.setMinimumWidth(680)
        self.adjustSize()  # tall enough for its text: the fields are not squashed

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
            self.python.setText(extensions.python_for(path))
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

        login = self.login.isChecked()

        def install():
            service.install(target, name, python=python, login=login)
            return service.linger_hint() if login else None

        def done(hint):
            message = f"{service.PREFIX}{name} installed and started"
            if not login:
                return message + "; it does not start at login"
            return message + (f"; it starts at login (at boot: {hint})" if hint else "")

        self.window.act(f"Installing {service.PREFIX}{name}", install, done)


# ---------------------------------------------------------------- devices on another computer
def ask_server(address: str, timeout: float = 4.0) -> tuple[dict, dict]:
    """(its info, its devices) from the server at a tcp:// address."""
    with connect(address, timeout=timeout, connect_timeout=timeout) as client:
        return client.info(), client.describe()


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
        keep = self.list.sizePolicy()
        keep.setRetainSizeWhenHidden(True)  # its room stays where it will be, not between the other rows
        self.list.setSizePolicy(keep)
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
            hidden = name in local or known.get(name) == self.address  # found here first, or listed already
            item.setCheckState(Qt.CheckState.Unchecked if hidden else Qt.CheckState.Checked)
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
