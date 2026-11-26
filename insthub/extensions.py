"""Extensions: what a folder adds to insthub for one instrument, as the file
`insthub-extension.toml` in it, its *manifest*, says (see docs/design.md#extensions).

    [extension]
    id = "hp8562a"                     # what its devices announce as their `extension`
    title = "HP 8562A spectrum analyzer"
    version = "0.3"
    api = 1                            # the version of this file's format
    # python = ".venv/bin/python"      # default: a virtualenv in this folder or the one above

    [devices.analyzer]                 # a ready-made [devices.x] table for a server file
    driver = "hp8562a_insthub.device:SpectrumAnalyzer"
    doc = "HP 8562A over GPIB"
    pad = 18                           # every other key goes into the server file as it is

    [ui.analyzer]                      # an app (`run`), or a panel (`panel = "module:make"`)
    title = "Spectrum analyzer"
    run = ["-m", "hp8562a_insthub.ui", "--address", "{address}", "--name", "{device}"]
    # types = ["HP8562A"]              # only for devices of these types (default: all of its devices)

A manifest is data: reading one runs none of the extension's code, so listing
extensions never loads a vendor library.  `import insthub` never imports this
module; the command line and the window do.

    load(manifest)                  an Extension; never raises: what makes it unusable is in .error
    find_manifest(folder)           the nearest manifest in a folder or above it
    discover()                      the extensions this computer knows of; of one id, the first usable
    for_device(described, folders)  the extension of a device, from its describe()
    device_uis(described, address)  the UIs a device can open here, or why it has none
    launch(ext, ui, device, address)  start one, in a program of its own, with the extension's Python
    add_folder(), remove_folder()   your list of extension folders (extensions.toml)
    check(path, import_=False)      what is wrong with an extension, or with a server file's
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import re
import sys
import tomllib
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from . import config, registry, tomlfile
from ._version import __version__
from .config import ConfigWarning, _Kind
from .errors import BusError, did_you_mean

if TYPE_CHECKING:
    from .inventory import Launched

API = 1  #: the newest manifest format this insthub reads
MANIFEST = "insthub-extension.toml"
OWN = Path(__file__).resolve().with_name(MANIFEST)  #: insthub's own manifest: its simulated instruments
#: Folders to look in (os.pathsep between them) instead of your list and this computer's
#: services and servers; "none": insthub's own extension only.  For trying an extension, and tests.
ENV = "INSTHUB_EXTENSIONS"


@dataclasses.dataclass(frozen=True)
class DeviceOffer:
    """A [devices.NAME] table of a manifest: a device that the New server dialog offers."""

    name: str
    driver: str  #: "module:Class", as in a server file
    doc: str = ""
    keys: dict = dataclasses.field(default_factory=dict)  #: every other key, for the server file as it is


@dataclasses.dataclass(frozen=True)
class UI:
    """A [ui.NAME] table: an app, started as the extension's Python with the arguments `run`, or
    a panel, the function `panel` ("module:make") that makes a widget for a device.  It is for
    every device of the extension, or only for those whose type (describe()'s) is in `types`."""

    name: str
    title: str
    run: tuple | None = None
    panel: str | None = None
    types: tuple = ()

    def fits(self, described: dict) -> bool:
        """Whether it is for that device (its describe())."""
        return not self.types or described.get("type") in self.types


@dataclasses.dataclass
class Extension:
    """An extension as its manifest says, and where it was found (`source`): "insthub" (insthub's
    own), "added" (your list), "service:NAME" (the folder of a device of an installed service),
    "server:NAME" (of a server running here) or "device" (found from a device's own folders).
    One that cannot be used says why in `error`; one whose id an earlier one has names that
    one's manifest in `hidden_by`.  Its attributes `devices` and `ui` hold what it adds, by name."""

    manifest: Path  #: the file it is read from (for a place without one: where it was looked for)
    source: str = ""
    id: str = ""
    title: str = ""
    version: str = ""
    api: int = API
    python: str | None = None  #: the Python its code runs in
    devices: dict[str, DeviceOffer] = dataclasses.field(default_factory=dict)
    ui: dict[str, UI] = dataclasses.field(default_factory=dict)
    error: str | None = None
    hidden_by: str | None = None

    @property
    def folder(self) -> Path:
        return self.manifest.parent

    @property
    def builtin(self) -> bool:
        """Whether it is insthub's own, whose code comes with insthub and so runs wherever insthub does
        (that of this insthub, or of another copy of insthub: see _own)."""
        return _own(self.manifest)

    @property
    def root(self) -> Path:
        """The folder its module names are found in: its own; for insthub's own, the one insthub is in."""
        return self.folder.parent if self.builtin else self.folder

    @property
    def path(self) -> Path | None:
        """The folder that goes first on the module search path of its code (a server file's
        `path`, PYTHONPATH): its own; None for insthub's own, which is installed wherever insthub is."""
        return None if self.builtin else self.folder

    @property
    def usable(self) -> bool:
        return self.error is None and self.hidden_by is None

    def device_table(self, offer: DeviceOffer) -> dict:
        """The [devices.x] table that a server file gets for one of its devices: the driver, the
        folder its module is found in (`path`, if it needs one), the extension's id, and the
        offer's other keys as they are."""
        path = {} if self.path is None else {"path": str(self.path)}
        return {"driver": offer.driver, **path, "extension": self.id, **offer.keys}


# ---------------------------------------------------------------- reading a manifest
_WHOLE = _Kind("a whole number", lambda v: isinstance(v, int) and not isinstance(v, bool))
_ARGS = _Kind('a list of arguments, as ["-m", "mypackage.ui"]',
              lambda v: isinstance(v, list) and all(isinstance(a, str) for a in v))
_FUNCTION = _Kind('text as "module:function"', config._DRIVER.test)

#: The keys of [extension] and of a [ui.NAME] that insthub reads, with the kind of value each
#: takes.  Others are left alone, since a newer insthub may read them (check() warns about them).
_EXTENSION = {"id": config._TEXT, "title": config._TEXT, "version": config._TEXT, "api": _WHOLE,
              "python": config._TEXT}
_UI = {"title": config._TEXT, "run": _ARGS, "panel": _FUNCTION, "types": config._NAMES}
#: Keys of a server file's device table that insthub writes there, not a manifest: why.
_WRITTEN = {"path": "insthub writes the manifest's folder",
            "extension": "insthub writes the manifest's id",
            "extension_version": "it would stay in the server file when the code changes: let the class say it"}
_LATER = ("match", "docs", "implements")  #: kinds of table reserved for later: nothing reads them yet


def _fields(table: dict, kinds: dict, where: str, problems: list) -> dict:
    """The keys of `kinds` that `table` has, each with a value of its kind; a problem for each other."""
    found = {}
    for key, kind in kinds.items():
        if key in table and kind.test(table[key]):
            found[key] = table[key]
        elif key in table:
            problems.append(f"{where} {key}: should be {kind.what}, not {config._shown(table[key])}")
    return found


def _unread(table: dict, kinds: dict, where: str) -> list[str]:
    """A note for each key of a table that this insthub does not read."""
    return [f"{where} {key}: insthub {__version__} does not read it" + did_you_mean(key, kinds)
            for key in table if key not in kinds]


def _offer(name: str, table: dict, problems: list, notes: list) -> DeviceOffer | None:
    """A [devices.NAME] table.  Its keys besides driver and doc are the device's: insthub's own
    among them are checked as in a server file, as far as that needs no import (what link_errors
    names is loaded by check(import_=True)), and the others go to the constructor."""
    where = f"[devices.{name}]"
    wrong = config._wrong_kinds(name, table)
    problems += [*wrong.values(), *config._loaded_problems(where, table, wrong, imports=False)]
    problems += [f"{where} {key}: not for a manifest to say: {why}" for key, why in _WRITTEN.items() if key in table]
    doc = _fields(table, {"doc": config._TEXT}, where, problems).get("doc", "")
    if "driver" not in table:
        problems.append(f'{where} needs a driver = "module:Class"')
    if "driver" not in table or "driver" in wrong:  # (a driver of the wrong kind is among the problems)
        return None
    return DeviceOffer(name, table["driver"], doc, {k: v for k, v in table.items() if k not in ("driver", "doc")})


def _ui(name: str, table: dict, problems: list, notes: list) -> UI:
    """A [ui.NAME] table: an app or a panel."""
    where = f"[ui.{name}]"
    found = _fields(table, _UI, where, problems)
    notes += _unread(table, _UI, where)
    if ("run" in table) == ("panel" in table):
        problems.append(f'{where} needs one of run = [...] (an app) and panel = "module:make" (a panel)')
    run = found.get("run")
    return UI(name, found.get("title", name), None if run is None else tuple(run), found.get("panel"),
              tuple(found.get("types", ())))


#: What a manifest adds to insthub, by kind: each a table of tables [KIND.NAME], whose tables its
#: function reads (name, table, problems, notes) -> the item, or None, into the Extension attribute
#: of that name.  A new kind is one more entry here, and its attribute.
_ADDS = {"devices": _offer, "ui": _ui}


def _name(value, what: str, problems: list) -> str:
    """A name a manifest gives (its id, a device's or a UI's), or "" and a problem."""
    try:
        return registry.check_name(value, what)
    except ValueError as e:
        problems.append(str(e))
        return ""


def _read(manifest: str | Path, source: str = "") -> tuple[Extension, list[str], list[str]]:
    """(the extension of a manifest, the problems that make it unusable, notes on what this insthub
    does not read)."""
    manifest = Path(manifest).expanduser().resolve()
    try:
        with open(manifest, "rb") as f:
            doc = tomllib.load(f)
    except (OSError, ValueError) as e:  # (a TOMLDecodeError and a UnicodeDecodeError are ValueErrors)
        problem = f"cannot read it: {e.strerror or e}" if isinstance(e, OSError) else f"it is not valid TOML: {e}"
        return Extension(manifest, source, title=manifest.parent.name, error=problem), [problem], []
    problems: list[str] = []
    notes = [f"[{key}]: insthub {__version__} does not read it" +
             ("; nothing reads it yet" if key in _LATER else did_you_mean(key, ["extension", *_ADDS]))
             for key in doc if key != "extension" and key not in _ADDS]
    head = doc.get("extension")
    if not isinstance(head, dict):
        problems.append('it has no [extension] table, as [extension] id = "hp8562a"')
        head = {}
    about = _fields(head, _EXTENSION, "[extension]", problems)
    notes += _unread(head, _EXTENSION, "[extension]")
    if "id" not in head:
        problems.append('[extension] needs an id, as id = "hp8562a": the name its devices announce')
    ext_id = _name(about["id"], "[extension] id", problems) if "id" in about else ""
    api = about.get("api", 1)
    if api > API:
        problems.append(f"it is written for a newer insthub (api = {api}; insthub {__version__} reads up to "
                        f"api = {API}): update insthub")
    elif api < 1:
        problems.append(f"[extension] api: should be 1 or more, not {api}")
    if _own(manifest):  # its code comes with (that copy of) insthub, whose version it has
        python = sys.executable
        version = about.get("version") or _assigned(manifest.with_name("_version.py"), "__version__") or ""
    else:
        python, version = _python(about.get("python"), manifest.parent, problems), about.get("version", "")
    adds = {}
    for kind, read in _ADDS.items():
        tables = doc.get(kind, {})
        if not isinstance(tables, dict) or not all(isinstance(t, dict) for t in tables.values()):
            problems.append(f"[{kind}] should hold tables, as [{kind}.NAME]")
            tables = {}
        adds[kind] = {}
        for name, table in tables.items():
            item = read(name, table, problems, notes) if _name(name, f"[{kind}.NAME] name", problems) else None
            if item is not None:
                adds[kind][name] = item
    ext = Extension(manifest, source, ext_id, about.get("title", ext_id), version, api, python, **adds,
                    error="; ".join(problems) or None)
    return ext, problems, notes


def load(manifest: str | Path, source: str = "") -> Extension:
    """The extension of a manifest file.  Never raises: one that cannot be used (it is not valid
    TOML, its api is too new, its id is not a valid name, an entry lacks what it needs, it has no
    Python) says why in its `error`.  Keys that this insthub does not read are left alone, so
    that a manifest written for a newer insthub still works (check() warns about them)."""
    return _read(manifest, source)[0]


def _python(declared: str | None, folder: Path, problems: list) -> str | None:
    """The Python an extension's code runs in: the one its manifest names (relative to its
    folder), else a virtualenv's in its folder or the one above; None and a problem if there is none."""
    if declared:
        python = folder / Path(declared).expanduser()
        if python.is_file():
            return str(python)
        problems.append(f"[extension] python: {python} does not exist")
        return None
    python = venv_python(folder)
    if python is None:
        problems.append(f"it has no Python: make a virtualenv (.venv) in {folder} or the folder above, or name "
                        'one in [extension] as python = "..."')
    return python


def venv_python(folder: str | Path) -> str | None:
    """The Python of a virtualenv (.venv, venv or env) in `folder` or the folder above, if there is one."""
    folder = Path(folder)
    inside = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    for base in (folder, folder.parent):
        for venv in (".venv", "venv", "env"):
            python = base.joinpath(venv, *inside)
            if python.is_file():
                return str(python)
    return None


def find_manifest(folder: str | Path) -> Path | None:
    """The manifest in `folder`, or in the nearest folder above it that has one."""
    start = Path(folder).expanduser().resolve()
    return next((base / MANIFEST for base in (start, *start.parents) if (base / MANIFEST).is_file()), None)


def _own(manifest: Path) -> bool:
    """Whether a manifest is insthub's own: OWN, or that of another copy of insthub, found by where
    it is (in a package `insthub`, next to its _version.py).  A lab has such copies: its projects
    install the checkout beside them (`-e ../insthub`), while the window runs from an environment
    of its own.  Their module names are found in the folder above, as this insthub's are."""
    folder = manifest.parent
    return manifest == OWN or (folder.name == "insthub" and (folder / "__init__.py").is_file()
                               and (folder / "_version.py").is_file())


def module_file(module: str, folder: str | Path) -> Path | None:
    """The source file of a module ("package.module") in a folder, found without importing anything."""
    stem = Path(folder).expanduser().joinpath(*module.split("."))
    return next((p for p in (stem.with_suffix(".py"), stem / "__init__.py") if p.is_file()), None)


# ---------------------------------------------------------------- finding extensions
def user_file() -> Path:
    """Your list of extension folders: `paths = ["/home/me/Code/hp8562a", ...]`."""
    return Path.home() / ".config" / "insthub" / "extensions.toml"


def listed_folders() -> list[str]:
    """The folders of your list.  Raises ValueError (naming the file) if it cannot be read, is not
    valid TOML, or its `paths` is not a list of folders."""
    path = user_file()
    if not path.exists():
        return []
    paths = config._read(path).get("paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError(f'{path}: paths should be a list of folders, as paths = ["/home/me/Code/hp8562a"]')
    return paths


def _places() -> list[tuple[str, Path, str | None]]:
    """(source, folder, why it cannot be used) for each place discover() looks in after insthub's
    own extension, in order.  Your list that cannot be read is one place: the file, and why."""
    chosen = os.environ.get(ENV)
    if chosen is not None:
        return [("added", Path(f).expanduser(), None) for f in chosen.split(os.pathsep) if f.strip() not in ("", "none")]
    try:
        listed = [("added", Path(f).expanduser(), None) for f in listed_folders()]
    except ValueError as e:
        listed = [("added", user_file(), str(e))]
    return [*listed, *_service_folders(), *_server_folders()]


def _service_folders() -> list[tuple[str, Path, None]]:
    """The folders of the code of the installed services' servers (see _code_folders)."""
    from . import inventory, service  # (the window and the command line have them loaded anyway)

    try:
        units = service.units()
    except BusError:  # no systemd user services here
        return []
    found = []
    for unit in units:
        source = f"service:{unit['name']}"
        path = inventory.server_file(inventory.Server(source, unit["name"], "service", service=unit))
        found += [(source, folder, None) for folder in (_code_folders(path) if path else ())]
    return found


def _server_folders() -> list[tuple[str, Path, None]]:
    """The folders of the devices' code of the servers running on this computer (_entry_folders)."""
    return [(f"server:{entry.get('server')}", Path(folder), None)
            for entry in registry.servers() for listed in _entry_folders(entry).values() for folder in listed]


def _entry_folders(entry: dict) -> dict[str, list[str]]:
    """The folders of each device's code that a server's registry entry lists (see Server._entry).
    What is not a list of absolute paths there (a file of another insthub version, or a damaged
    one) is left out."""
    folders = entry.get("folders")
    return {name: [folder for folder in listed if isinstance(folder, str) and os.path.isabs(folder)]
            for name, listed in (folders.items() if isinstance(folders, dict) else ()) if isinstance(listed, list)}


def device_folders(device: str, address: str) -> list[str] | None:
    """The folders of a device's code, from the registry file of its server if that runs on this
    computer (as one of its addresses says); None for a device on another computer."""
    return next((_entry_folders(entry).get(device, []) for entry in registry.find(device)
                 if address in entry.get("addresses", ())), None)


def _code_folders(path: Path) -> list[Path]:
    """The folders a server's code comes from: a .py server's own; for a .toml config, those of its
    devices (_device_folders; none if it cannot be read)."""
    if path.suffix != ".toml":
        return [path.parent]
    try:
        devices = config._read(path).get("devices", {})
    except ValueError:  # (a ConfigError) the service says what is wrong where it runs
        return []
    return list(dict.fromkeys(_device_folders(devices, path.resolve().parent).values()))


def _device_folders(devices, base: Path) -> dict[str, Path]:
    """The folder of each device's code in a server file's [devices] (`devices`; `base`: the file's
    folder, where the server runs): its `path`, relative to `base`; else `base` itself, if its
    driver's module is there, which is where the server finds it then.  A device whose module
    comes from elsewhere (an installed package, such as insthub's simulated instruments) has none."""
    found = {}
    for name, table in (devices.items() if isinstance(devices, dict) else ()):
        path, driver = (table.get("path"), table.get("driver")) if isinstance(table, dict) else (None, None)
        module = driver.partition(":")[0] if isinstance(driver, str) else ""
        if isinstance(path, str) and path.strip():
            found[name] = (base / Path(path).expanduser()).resolve()
        elif module and module_file(module, base) is not None:
            found[name] = base
    return found


def discover() -> list[Extension]:
    """Every extension this computer knows of, each manifest once, in this order: insthub's own;
    the folders of your list (extensions.toml; or of INSTHUB_EXTENSIONS, see ENV, which then
    replaces the rest); the folders of the installed services' code; the folders of the devices
    of the servers running here.  Of the manifests with one id, the first that can be used wins,
    and the others get `hidden_by`; one that cannot be used hides nothing.  Another copy of
    insthub's own manifest (see _own) is insthub's own, listed first.  Never raises: a folder of
    your list without a manifest, or a list that cannot be read, is listed with its error; any
    other folder without a manifest is no extension's."""
    found, seen = [load(OWN, "insthub")], {OWN}
    for source, place, error in _places():
        manifest = None if error else find_manifest(place)
        if manifest is not None and _own(manifest):
            manifest = OWN  # (the simulated instruments of the insthub a server runs: insthub's own)
        if manifest is not None and manifest not in seen:
            seen.add(manifest)
            found.append(load(manifest, source))
        elif manifest is None and source == "added":  # you asked for it: say why it is not an extension
            title = place.name  # (the folder's, or that of your list, which cannot be read)
            if error is None:
                error = f"no {MANIFEST} in {place} or above" if place.is_dir() else f"no folder {place}"
                place = place / MANIFEST
            found.append(Extension(place, source, title=title, error=error))
    first: dict[str, Extension] = {}
    for ext in found:
        if ext.id and ext.error is None and first.setdefault(ext.id, ext) is not ext:
            ext.hidden_by = str(first[ext.id].manifest)
    return found


def for_device(described: dict, folders: list[str] | None = None) -> Extension | None:
    """The extension of a device, from its describe(): the manifest found from the device's own
    folders (`folders`, from the registry file of its server on this computer) whose id is the
    one the device announces, so that the very copy its server runs is the one used; else the
    first discovered extension of that id that can be used (if none can, the first, whose error
    says why).  A device of insthub's own extension gets this insthub's, whichever copy of
    insthub its server runs (see _own): its code comes with insthub, and runs where insthub
    does.  None if the device names no extension, or none here has its id.  Only the id is
    compared: a name that came over the network runs nothing."""
    wanted = described.get("extension")
    if not isinstance(wanted, str) or not wanted:
        return None
    for folder in folders or ():
        manifest = find_manifest(folder)
        if manifest is None:
            continue
        ext = load(OWN, "insthub") if _own(manifest) else load(manifest, "device")
        if ext.id == wanted:
            return ext
    same = [ext for ext in discover() if ext.id == wanted]
    return next((ext for ext in same if ext.usable), same[0] if same else None)


def device_uis(described: dict, address: str) -> tuple[Extension | None, list[UI], str | None]:
    """What a device (its describe(); its server's `address`) can open on this computer: (its
    extension, as for_device() finds it from the device's own folders when its server runs here;
    the UIs of it that are for this device; why there are none when that is not up to the
    extension: it is not on this computer, or cannot be used)."""
    wanted = described.get("extension")
    ext = for_device(described, device_folders(described.get("name", ""), address))
    if ext is None:
        why = f"extension {wanted} is not installed on this computer: insthub ext add FOLDER" if wanted else None
        return None, [], why
    if ext.error:
        return ext, [], f"extension {wanted} cannot be used: {ext.error}"
    return ext, [ui for ui in ext.ui.values() if ui.fits(described)], None


def add_folder(path: str | Path) -> Extension:
    """Add an extension's folder (the folder of the manifest found from `path`) to your list,
    extensions.toml, keeping the rest of the file as it is.  Returns the extension (it may have
    an error; it is listed all the same).  Raises ValueError if there is no manifest, or if the
    file cannot be read or changed safely."""
    manifest = find_manifest(path)
    if manifest is None:
        raise ValueError(f"no {MANIFEST} in {Path(path).expanduser().resolve()} or above: is it an insthub "
                         "extension's folder?")
    paths = listed_folders()
    if not any(Path(p).expanduser().resolve() == manifest.parent for p in paths):
        _write_list([*paths, str(manifest.parent)])
    return load(manifest, "added")


def remove_folder(path: str | Path) -> list[str]:
    """Take a folder off your list: as it is listed, or the folder of the manifest found from it
    (from a folder inside it, or the manifest itself), as add_folder() finds it.  Returns what was
    taken off, as it was listed.  Raises ValueError if neither is listed."""
    folder = Path(path).expanduser().resolve()
    manifest = find_manifest(folder) if folder.exists() else None
    gone = {folder, manifest.parent if manifest else folder}
    paths = listed_folders()
    removed = [p for p in paths if Path(p).expanduser().resolve() in gone]
    if not removed:
        raise ValueError(f"{folder} is not in {user_file()}")
    _write_list([p for p in paths if p not in removed])
    return removed


def _write_list(paths: list[str]) -> None:
    try:
        tomlfile.update(user_file(), {(): {"paths": paths}}, new_file=(
            "# Folders of insthub extensions (docs/design.md#extensions).  Written by `insthub ext add` and "
            "insthub gui; edit freely.\n"))
    except ValueError as e:
        raise ValueError(f"{user_file()}: {e}") from None


# ---------------------------------------------------------------- starting a UI
def launch(ext: Extension, ui_name: str, device: str, address: str) -> Launched:
    """Start a UI of an extension for a device, in a program of its own run with the extension's
    Python (docs/design.md#two-halves-each-where-its-libraries-are): an app as `<python> <run...>`,
    with {device} and {address} in its arguments filled in; a panel as `<python> -m insthub -a
    ADDRESS panel DEVICE MODULE:FUNCTION`, which puts it in a window.  It runs in the extension's
    folder, which is also first on its PYTHONPATH, so a folder works without being installed, and
    its output goes to a log file (inventory.ui_log).  Raises ValueError for an extension that
    cannot be used, and for a device name or address that must not fill a placeholder (the name
    came over the network)."""
    from . import inventory, wire

    if ext.python is None or ext.error:
        raise ValueError(f"extension {ext.id or ext.folder} cannot be used: {ext.error}")
    ui = ext.ui[ui_name]
    values = {"device": registry.check_name(device, "device name"), "address": address}
    if wire.parse_address(address)[0] == "inproc":
        raise ValueError(f"{address}: only the program that hosts it can reach it")
    if ui.run:  # (a placeholder it does not know stays as it is)
        args = [re.sub(r"\{(\w+)\}", lambda m: values.get(m[1], m[0]), arg) for arg in ui.run]
    else:
        args = ["-m", "insthub", "-a", address, "panel", device, ui.panel, f"--title={ui.title}"]
    env = dict(os.environ)
    if ext.path is not None:
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ext.path), env.get("PYTHONPATH")) if p)
    return inventory.spawn([ext.python, *args], inventory.ui_log(device, ui.name), f"{ui.title} for {device}",
                           cwd=ext.root, env=env)


# ---------------------------------------------------------------- checking
def _warn(message: str) -> None:
    warnings.warn(message, ConfigWarning, stacklevel=2)


def check(path: str | Path, import_: bool = False) -> list[str]:
    """What is wrong with an extension (its folder, or its manifest), or with the extensions of the
    devices of a server file (a .toml): one message per problem, none if all is well.  It reads,
    and runs nothing.  With `import_`, it also imports the drivers (and the exception classes their
    link_errors name) in a program of their own, run with the extension's Python, and reports those
    that fail; for a server file, in the Python its
    extensions run in, and with the check `insthub service install` makes.  Warns (ConfigWarning)
    about what works, but probably not as meant: keys this insthub does not read, a device class
    whose `extension_version` (read from its source) is not its manifest's `version`, a driver that
    imports Qt, a server file whose devices' extensions run in different Pythons."""
    path = Path(path).expanduser()
    if path.is_dir():
        manifest = find_manifest(path)
        if manifest is None:
            return [f"no {MANIFEST} in {path.resolve()} or above"]
    elif not path.is_file():
        return [f"{path}: no such file or folder"]
    elif path.name == MANIFEST:
        manifest = path
    elif path.suffix == ".toml":
        return _check_server(path, import_)
    else:
        return [f"{path}: expected an extension's folder, its {MANIFEST}, or a server's .toml"]
    ext, problems = _check_manifest(manifest)
    if import_ and ext.python and ext.devices:
        problems += _import(ext, list(ext.devices.values()))
    return problems


def _check_manifest(manifest: Path) -> tuple[Extension, list[str]]:
    """A manifest's extension and its problems, each naming the manifest.  Warns about what it says
    that this insthub does not read, and about a device class whose extension_version is not the
    manifest's version."""
    ext, problems, notes = _read(manifest)
    for note in notes:
        _warn(f"{ext.manifest}: {note}")
    for offer in ext.devices.values():
        module, _, cls = offer.driver.partition(":")
        file = module_file(module, ext.root)
        said = _assigned(file, "extension_version", cls) if file else None
        if isinstance(said, str) and said != ext.version:
            _warn(f"{ext.manifest}: [devices.{offer.name}] {offer.driver} says extension_version = "
                  f"{config._shown(said)}, but the manifest's version is {config._shown(ext.version)}: is one of "
                  "them out of date? (see docs/design.md#versions)")
    return ext, [f"{ext.manifest}: {p}" for p in problems]


def _assigned(path: Path, attr: str, cls: str | None = None):
    """The constant that a module assigns to `attr` (or its class `cls`, in its body), read from its
    source file without running it; None if it assigns none there (or not a constant)."""
    try:
        tree = ast.parse(path.read_text())
    except (OSError, SyntaxError, ValueError):  # (a UnicodeDecodeError is a ValueError)
        return None
    body = tree.body if cls is None else \
        next((node.body for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls), [])
    for item in body:
        targets = item.targets if isinstance(item, ast.Assign) else \
            [item.target] if isinstance(item, ast.AnnAssign) and item.value is not None else []
        if any(isinstance(t, ast.Name) and t.id == attr for t in targets):
            try:
                return ast.literal_eval(item.value)
            except ValueError:  # an expression: nothing to compare without running it
                return None
    return None


def _check_server(path: Path, import_: bool) -> list[str]:
    """check() of a server file: the manifests found from its devices' folders (_device_folders, as
    discover() finds them), and whether each device's `extension` is its manifest's id; with
    `import_`, also what `insthub service install` checks, in the Python of the devices' extensions."""
    try:
        devices = config._read(path).get("devices", {})
    except ValueError as e:
        return [str(e)]
    problems, checked = [], {}
    for name, folder in _device_folders(devices, path.resolve().parent).items():
        manifest = find_manifest(folder)
        if manifest is None:
            continue
        if manifest not in checked:
            checked[manifest], found = _check_manifest(manifest)
            problems += found
        ext, said = checked[manifest], devices[name].get("extension")
        if said is not None and ext.id and said != ext.id:
            problems.append(f"{path}: [devices.{name}] extension = {config._shown(said)}, but the manifest of its "
                            f"folder, {manifest}, has id = {config._shown(ext.id)}")
    pythons = sorted({ext.python for ext in checked.values() if ext.python and not ext.builtin})
    if len(pythons) > 1:
        _warn(f"{path}: its devices' extensions run in different Pythons ({', '.join(pythons)}), but a server runs "
              "in one: host them in separate servers")
    if import_:
        from . import service

        python = pythons[0] if len(pythons) == 1 else venv_python(path.resolve().parent) or sys.executable
        report = service.check_report(path, python)
        for message in report["warnings"]:
            _warn(message)
        problems += [f"{path}: {line}" for line in (report["problem"] or "").splitlines()]
    return problems


#: What check(import_=True) runs in an extension's Python, as `python -c _IMPORT REPORT FOLDER NAMES
#: GUI`: it loads each name ("module:Name") of NAMES, with the extension's folder first on sys.path,
#: and writes as JSON to the file REPORT, for each, the error if any, whether it is an exception
#: class, and which of the modules GUI (Qt bindings, pyqtgraph) loading it brought in.  It needs
#: only the standard library, so it works with any insthub there, or none.
_IMPORT = """\
import importlib, json, sys
folder, names, gui, report = sys.argv[2], json.loads(sys.argv[3]), json.loads(sys.argv[4]), []
if folder:
    sys.path.insert(0, folder)
for name in names:
    before, obj = set(sys.modules), None
    module, _, attr = name.partition(":")
    try:
        obj = importlib.import_module(module)
        for part in attr.split("."):
            obj = getattr(obj, part)
        error = None
    except BaseException as e:
        error = f"{type(e).__name__}: {e}"
    report.append({"error": error, "exception": isinstance(obj, type) and issubclass(obj, BaseException),
                   "gui": [m for m in gui if m in sys.modules and m not in before]})
with open(sys.argv[1], "w") as f:
    json.dump(report, f)
"""


def _import(ext: Extension, offers: list[DeviceOffer]) -> list[str]:
    """Load what the offers of an extension name, in a program of their own run with its Python
    (_IMPORT): each driver, and the exception classes of its link_errors, as a server does.  A
    problem for each that fails (as config.check says it), a warning for each driver that brings
    in Qt."""
    from . import service

    names = []  # (offer, key, "module:Name")
    for offer in offers:
        errors = offer.keys.get("link_errors")
        names += [(offer, "driver", offer.driver),
                  *((offer, "link_errors", name) for name in (errors if config._CLASSES.test(errors) else ()))]
    folder = str(ext.path or "")
    report, stderr = service.ask_python(ext.python, _IMPORT, folder, json.dumps([name for *_, name in names]),
                                        json.dumps(config._GUI_MODULES), cwd=ext.root, doing="import the drivers")
    if report is None:  # it did not get that far: not a Python, or one that crashed
        return [f"{ext.manifest}: {ext.python} cannot import the drivers: "
                + (stderr.strip().splitlines() or ["it stopped without saying why"])[-1]]
    problems = []
    for (offer, key, name), got in zip(names, report, strict=True):
        where = f"{ext.manifest}: [devices.{offer.name}]"
        if key == "driver":
            if got["error"]:
                problems.append(f"{where} cannot import {name} with {ext.python}: {got['error']}")
            if got["gui"]:
                _warn(config.gui_warning(where, name, got["gui"]))
        elif got["error"]:
            problems.append(f"{where} link_errors: cannot load {name!r} with {ext.python}: {got['error']}")
        elif not got["exception"]:
            problems.append(f"{where} link_errors: {name!r} is not an exception class")
    return problems
