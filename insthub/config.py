"""Build a server from a TOML file (`insthub serve lab.toml`).

    [server]
    name = "lab"                 # how clients find it (default: the file name)
    tcp = 8765                   # optional: also listen on TCP ("0.0.0.0:8765" for other machines)

    [devices.psu]                # the section name is the device name on the bus
    driver = "mydriver:PowerSupply"   # "module:Class".  A Device subclass is used as is;
                                 # any other class is hosted with insthub.wrap().
    path = "~/Code/mydriver"     # optional: where the module lives
    lazy = true                  # optional: open on first use, not at start
    port = "/dev/ttyUSB0"        # every other key is a constructor argument

Further optional keys for wrapped classes: `args` (positional constructor
arguments), `include` / `exclude` (method names), `attrs` (instance attributes
to expose), `close` (name of the close method), `accessors` (get_X/set_X pairs
as properties), `link_errors` (exception classes as "module:Class" that mean
the connection broke).  For all devices: `reconnect_interval`, what the device
is (`extension`, `extension_version`, `simulated`), metadata (`meta = {power =
{unit = "dBm"}}`), what to read periodically (`poll = {temperature = 1.0}`) and
what is dangerous (`dangerous = ["fire"]`, or true).  `_RESERVED` lists these
keys of insthub's own, with the kind of value each takes and what it does.

Each table is checked against its driver (`validate`): each of insthub's keys
for its kind of value, every other key against the constructor's parameters,
with a suggestion for a misspelled one.  `load()` checks every table of the
file before it constructs any device, and reports every problem of the file at
once.  `check()` does the same without running any driver's code, and warns
about what may not work where servers run; `insthub service install` runs it
in the service's Python.  The names in `meta`, `poll` and `dangerous` must be
the device's: both check them for a wrapped driver (making its device runs
none of the driver's code), and load() for a Device subclass once it has
constructed it.
Two folders that each have a module of one name cannot both be used in one
server, and `load_object` says so (for the module a driver names, also inside
a namespace package) instead of silently using the first.

What goes wrong is told apart by what would fix it.  A ConfigError (a
ValueError) is what is wrong with the file, every problem of it at once.  An
ImportError is a driver that cannot be loaded, which another Python
environment may have; when the file has other problems as well, they all come
in one ConfigError, which lists the drivers in `unloadable`.  A DriverError is
a driver's own code that failed (its module, or its constructor): it names the
table, and the driver's exception, its cause, has the traceback.

`dump()` writes such a file (the manager window's "New server" does); tomlfile.py
changes one in place, comments kept (its "Edit config" does).
"""
from __future__ import annotations

import importlib
import importlib.machinery
import inspect
import sys
import tomllib
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from . import tomlfile
from .device import Device, _checked_meta, given_address, wrap
from .errors import UnknownCommand, did_you_mean


class ConfigError(ValueError):
    """What is wrong with a config file (or why it cannot be read), one line per problem: editing
    the file fixes it.  `unloadable`: the ImportErrors of the drivers among them that cannot be
    loaded, which another Python environment, or packages installed into this one, may fix."""

    def __init__(self, message: str, unloadable: tuple = ()):
        super().__init__(message)
        self.unloadable = unloadable


class DriverError(RuntimeError):
    """A driver's own code failed while a config file was loaded: importing its module (for
    another reason than a module it cannot find), or constructing its device.  Not a problem of
    the file: the message names the table, and the driver's exception, its __cause__, has the
    traceback."""


class ConfigWarning(UserWarning):
    """Something in a config file that works, but probably not as meant, or not where servers run."""


# ---------------------------------------------------------------- insthub's own keys
class _Kind(NamedTuple):
    """A kind of value that a key takes."""

    what: str  #: as a message says it: "lazy: should be true or false"
    test: Callable[[object], bool]


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _strings(v, test=lambda s: True) -> bool:
    return isinstance(v, list) and all(isinstance(s, str) and test(s) for s in v)


_TEXT = _Kind("text in quotes", lambda v: isinstance(v, str))
_DRIVER = _Kind('text as "module:Class"', lambda v: isinstance(v, str) and ":" in v)
_BOOL = _Kind("true or false", lambda v: isinstance(v, bool))
_SECONDS = _Kind("a number of seconds", lambda v: _number(v) and v >= 0)
_LIST = _Kind("a list", lambda v: isinstance(v, list))
_TABLE = _Kind("a table", lambda v: isinstance(v, dict))
_NAMES = _Kind('a list of names, as ["read", "write"]', _strings)
_CLASSES = _Kind('a list of "module:Class" names', lambda v: _strings(v, lambda s: ":" in s))
_NAMES_OR_ALL = _Kind('true, or a list of names, as ["fire", "power"]', lambda v: v is True or _strings(v))
_SECONDS_EACH = _Kind("a table of seconds, as { temperature = 1.0 }",
                      lambda v: isinstance(v, dict) and all(_number(s) and s >= 0 for s in v.values()))
_TABLES = _Kind('a table of tables, as { power = { unit = "dBm" } }',
                lambda v: isinstance(v, dict) and all(isinstance(t, dict) for t in v.values()))


class _Key(NamedTuple):
    """One of the keys of a [devices.NAME] table that insthub reads itself."""

    kind: _Kind
    wrap: bool = False  #: an option of wrap(): only for a driver class that is not a Device
    #: load(value): what the value names, such as classes.  Run when the table is checked, before
    #: any device is made; a ConfigError it raises is a problem of the table.
    load: Callable | None = None
    apply: Callable | None = None  #: apply(device, loaded value): what it does to the device once made
    #: Its load imports what the value names, so it needs the Python the driver runs in (a
    #: manifest is checked in insthub's own, and imports only with `insthub check --import`).
    imports: bool = False


def _sets(attr: str, convert: Callable | None = None) -> Callable:
    """The `apply` of a key that sets an attribute of the device to its value (converted)."""
    return lambda dev, value: setattr(dev, attr, value if convert is None else convert(value))


def _exceptions(names: list) -> tuple:
    """The classes that `link_errors` names, and those that always mean a broken link.
    Raises ConfigError for a name that is not an exception class that can be loaded."""
    found = []
    for name in names:
        try:
            cls = load_object(name)
        except (ImportError, AttributeError) as e:
            raise ConfigError(f"cannot load {name!r}: {e}") from None
        if not (isinstance(cls, type) and issubclass(cls, BaseException)):
            raise ConfigError(f"{name!r} is not an exception class")
        found.append(cls)
    return (*found, *Device.link_errors)


def _metadata(tables: dict) -> dict:
    """What `meta` says, each table checked as Device.set_meta checks it (which names exist is
    known only once the device is made).  Raises ConfigError."""
    for name, meta in tables.items():
        nested = next((key for key, value in meta.items() if isinstance(value, dict)), None)
        if nested is not None:  # `set_level.dbm = {...}` unquoted: TOML reads a table in a table
            raise ConfigError(f'{name}.{nested}: write a parameter\'s name in quotes, as "{name}.{nested}" = {{ ... }}')
        try:
            _checked_meta(meta, name)
        except TypeError as e:
            raise ConfigError(str(e)) from None
    return tables


def _set_meta(dev: Device, tables: dict) -> None:
    for name, meta in tables.items():
        dev.set_meta(name, **meta)


#: insthub's own keys of a [devices.NAME] table; every other key is a constructor argument.  A key
#: added here takes its name away from constructors (only `kwargs` can then reach a parameter of
#: that name; validate() says so).  driver, path, args and kwargs make the device: see _make().
_RESERVED: dict[str, _Key] = {
    "driver": _Key(_DRIVER),
    "path": _Key(_TEXT),
    "args": _Key(_LIST),
    "kwargs": _Key(_TABLE),
    "include": _Key(_NAMES, wrap=True),
    "exclude": _Key(_NAMES, wrap=True),
    "attrs": _Key(_NAMES, wrap=True),
    "close": _Key(_TEXT, wrap=True),
    "accessors": _Key(_BOOL, wrap=True),
    "lazy": _Key(_BOOL, apply=_sets("open_on_start", lambda lazy: not lazy)),
    "reconnect_interval": _Key(_SECONDS, apply=_sets("reconnect_interval", float)),
    "link_errors": _Key(_CLASSES, load=_exceptions, apply=_sets("link_errors"), imports=True),
    "extension": _Key(_TEXT, apply=_sets("extension")),
    "extension_version": _Key(_TEXT, apply=_sets("extension_version")),
    "simulated": _Key(_BOOL, apply=_sets("simulated")),
    "meta": _Key(_TABLES, load=_metadata, apply=_set_meta),
    "poll": _Key(_SECONDS_EACH, apply=_sets("poll", dict)),
    "dangerous": _Key(_NAMES_OR_ALL, apply=_sets("dangerous", lambda names: names if names is True else tuple(names))),
    "description": _Key(_TEXT, apply=lambda dev, text: _describe(dev, text)),
}


def _describe(dev: Device, text: str) -> None:
    """`description`: what this device is, in a few words, for the people who see it."""
    if not isinstance(inspect.getattr_static(dev, "description", None), (str, type(None))):
        raise ConfigError(f"description: {type(dev).__name__} has a command or property of that name")
    dev.description = text.strip() or None

#: The keys of the [server] table.
_SERVER_KEYS = ("name", "tcp", "ipc")

#: Top-level modules of the Qt bindings and of pyqtgraph.  A server often runs as a
#: service, without a screen, so its drivers should not need them (check() warns).
_GUI_MODULES = ("PyQt5", "PyQt6", "PySide2", "PySide6", "pyqtgraph")


def gui_warning(where: str, driver: str, modules: list[str]) -> str:
    """What check() (and `insthub check --import`) say about a driver that imports some of _GUI_MODULES."""
    return (f"{where} driver {driver} imports {', '.join(modules)}; servers often run without a screen: move UI "
            "code to the extension's UI module (see docs/design.md#rules-inside-an-extension)")


def _own_keys(spec: dict) -> list[tuple[str, object, _Key]]:
    """(key, value, what insthub does with it) for each of insthub's keys in a table."""
    return [(key, value, _RESERVED[key]) for key, value in spec.items() if key in _RESERVED]


def _warn(message: str) -> None:
    warnings.warn(message, ConfigWarning, stacklevel=2)


# ---------------------------------------------------------------- loading drivers
class _Clash(ImportError):
    """Two folders have a module of one name, and Python keeps one module per name."""


def load_object(spec: str, path: str | None = None):
    """Import "module:Name", with the folder `path` first on sys.path.  With `path`,
    refuses a module that the folder has but that Python would take from another
    file (see _refuse_clash)."""
    module, sep, name = spec.partition(":")
    if not sep:
        raise ValueError(f"driver {spec!r} must look like 'module:Class'")
    folder = str(Path(path).expanduser()) if path else None
    if folder:
        if folder not in sys.path:
            sys.path.insert(0, folder)
        _refuse_clash(module, folder)  # a module of that name is loaded already
    obj = importlib.import_module(module)
    if folder:
        _refuse_clash(module, folder)  # or it was found first in a folder before this one on sys.path
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


def _refuse_clash(module: str, folder: str) -> None:
    """Raise if `folder` has a file of `module`, or of a package it is in (say a bus_server.py,
    or hp_insthub/device.py), but Python has a module of that name from another file: an
    earlier device's folder, or one before it on sys.path.  The folder's file would silently
    not be used.  A namespace package (a folder without __init__.py) has no file: Python
    merges those of every folder, so it is the modules inside that must not clash."""
    parts, search, root = module.split("."), [folder], Path(folder).resolve()
    for i in range(len(parts)):
        name = ".".join(parts[:i + 1])
        own = importlib.machinery.PathFinder.find_spec(name, search)
        loaded = sys.modules.get(name)
        if own is None or loaded is None:  # not in that folder, or not loaded (nor anything inside)
            return
        there = getattr(loaded, "__file__", None)
        if not own.has_location and there is None:  # both namespace packages: look inside, in that folder
            search = [p for p in own.submodule_search_locations if Path(p).resolve().is_relative_to(root)]
            continue
        mine = own.origin if own.has_location else next(iter(own.submodule_search_locations))
        if there is None or Path(there).resolve() != Path(mine).resolve():
            raise _Clash(f"a module named {name!r} is loaded already, from {there or repr(loaded)}, so {mine} "
                         "cannot be loaded too: give one of them a unique package name (see "
                         "docs/design.md#rules-inside-an-extension), or host them in separate servers")
        return  # the very file: a module, or a package, whose modules are its own


def _driver(name: str, spec: dict):
    """Load what `driver` names; the table has a driver and path of their kind (validate)."""
    where = f"[devices.{name}]: cannot load driver {spec['driver']!r}"
    try:
        return load_object(spec["driver"], spec.get("path"))
    except _Clash as e:  # still a _Clash: no other Python would help (see service.check_python)
        raise _Clash(f"{where}: {e}") from None
    except (ImportError, AttributeError) as e:
        raise ImportError(f"{where}: {e} (this is {sys.executable}; is it the Python environment that has the "
                          "driver's dependencies?)") from e
    except Exception as e:  # its module's own error, such as a SyntaxError: not one of the file
        raise DriverError(f"{where}: {type(e).__name__}: {e}") from e


# ---------------------------------------------------------------- checking a table
def _is_device(cls) -> bool:
    return isinstance(cls, type) and issubclass(cls, Device)


def _signature(cls) -> inspect.Signature | None:
    """The constructor's signature; None for an object insthub does not construct (an
    instance), or a class whose signature cannot be read (some written in C)."""
    if not isinstance(cls, type):
        return None
    try:
        return inspect.signature(cls)
    except (TypeError, ValueError):
        return None


def _plain(sig: inspect.Signature) -> str:
    """A signature as a message shows it: "(port, baud=9600)", without annotations."""
    params = [p.replace(annotation=p.empty) for p in sig.parameters.values()]
    return str(sig.replace(parameters=params, return_annotation=sig.empty))


def _shown(value) -> str:
    """A value as the file has it: true, "text", [1, 2]."""
    try:
        return tomlfile.value(value)
    except ValueError:
        return repr(value)


def _arguments(name: str, spec: dict, cls) -> tuple[list, dict]:
    """The constructor's arguments from a table: `args`, then every key that is not
    insthub's own, and the `kwargs` table (for a parameter named like one of insthub's
    keys).  A Device subclass whose constructor takes `name` gets the table's name."""
    kwargs = {**{k: v for k, v in spec.items() if k not in _RESERVED}, **spec.get("kwargs", {})}
    sig = _signature(cls)
    if _is_device(cls) and sig is not None and "name" in sig.parameters:
        kwargs.setdefault("name", name)
    return list(spec.get("args", ())), kwargs


def validate(name: str, spec: dict, advise: bool = False) -> tuple[object, list[str]]:
    """Load the driver of the table [devices.NAME] and check the table against it.  Returns
    the driver (None if the table names none that can be looked for) and what is wrong
    with the table, one message per problem, none if it is fine.  Raises ImportError if the
    driver cannot be loaded, and DriverError if its module fails otherwise.

    Each of insthub's own keys must have its kind of value, and what it names must load
    (link_errors).  Every other key must be a parameter of the constructor (a misspelled
    one gets a suggestion), and the constructor must get every argument it requires.  One
    of insthub's keys that the constructor also takes must have its parameter's value
    under `kwargs`, since only that says which of the two the key was meant for.

    Warns (ConfigWarning) about a key that does nothing (an option of wrapped drivers
    given to a Device subclass) or likely not what was meant (a key that a constructor
    taking **kwargs gets, but that is close to one of insthub's own, as `lazzy`).  With
    `advise` (check() does), also about what works, but
    may not where servers run or not as meant: a driver that imports Qt, and an optional
    constructor parameter named like one of insthub's keys that the table does not give,
    which only `kwargs` can reach."""
    where = f"[devices.{name}]"
    wrong = _wrong_kinds(name, spec)
    if "driver" not in spec or wrong.keys() & {"driver", "path"}:  # nothing to look for
        missing = [] if "driver" in spec else [f"{where} needs a driver = 'module:Class'"]
        return None, missing + list(wrong.values())
    absent = [m for m in _GUI_MODULES if m not in sys.modules]
    cls = _driver(name, spec)
    gui = [m for m in absent if m in sys.modules]
    if advise and gui:
        _warn(gui_warning(where, spec["driver"], gui))
    problems = [*wrong.values(), *_key_problems(where, spec, cls, wrong)]
    if not wrong.keys() & {"args", "kwargs"}:  # else the arguments cannot be told, or checked
        problems += _argument_problems(where, name, spec, cls, advise)
    return cls, problems


def _wrong_kinds(name: str, spec: dict) -> dict[str, str]:
    """validate()'s first look, which needs no driver: key -> what is wrong, for each of insthub's
    own keys whose value is not of its kind."""
    return {key: f"[devices.{name}] {key}: should be {rule.kind.what}, not {_shown(value)}"
            for key, value, rule in _own_keys(spec) if not rule.kind.test(value)}


def _key_problems(where: str, spec: dict, cls, wrong: dict) -> list[str]:
    """validate()'s half about insthub's own keys that have their kind of value: what they
    name must load (_loaded_problems).  Warns about an option of wrapped drivers given to a
    Device subclass."""
    for key, _, rule in _own_keys(spec):
        if rule.wrap and key not in wrong and _is_device(cls):
            _warn(f"{where} {key}: does nothing for {spec['driver']}, a Device subclass (it is an option for "
                  "drivers that insthub wraps)")
    return _loaded_problems(where, spec, wrong)


def _loaded_problems(where: str, spec: dict, wrong: dict, imports: bool = True) -> list[str]:
    """What is wrong with what insthub's own keys say, each key that has its kind of value (not
    in `wrong`, see _wrong_kinds) checked by its `load` step: the words of `meta`, the classes of
    `link_errors`.  With `imports` false, only the steps that import nothing (as an extension's
    manifest is checked, in insthub's Python rather than the extension's)."""
    problems = []
    for key, value, rule in _own_keys(spec):
        if key in wrong or rule.load is None or (rule.imports and not imports):
            continue
        try:
            rule.load(value)
        except ConfigError as e:
            problems.append(f"{where} {key}: {e}")
    return problems


def _only_kwargs(param: str) -> str:
    """Why a constructor's parameter named like one of insthub's keys gets nothing from that key."""
    return (f"{param} is one of insthub's own keys and never reaches it; "
            f"to pass it, write kwargs = {{ {param} = ... }}")


def _argument_problems(where: str, name: str, spec: dict, cls, advise: bool) -> list[str]:
    """validate()'s half about the constructor's arguments."""
    if not isinstance(cls, type):  # an object (or a function), hosted as it is: nothing is constructed
        keys = [*(k for k in spec if k not in _RESERVED), *(k for k in ("args", "kwargs") if spec.get(k))]
        what = f"{spec['driver']} is an object, not a class, so it takes no arguments"
        return [f"{where} {key}: {what}" for key in keys]
    sig = _signature(cls)
    if sig is None:  # it cannot be read, so it cannot be checked
        return []
    problems = []
    args, kwargs = _arguments(name, spec, cls)
    constructor, passed = f"{spec['driver']}{_plain(sig)}", spec.get("kwargs", {})
    params = [p for p in sig.parameters.values() if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
    names = [p.name for p in params]
    if any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()):  # **kwargs takes any key, but
        for key in [k for k in spec if k not in _RESERVED and k not in names]:  # a misspelled one of insthub's?
            if close := did_you_mean(key, _RESERVED):
                _warn(f"{where} {key}: goes to {constructor}, which takes any keyword{close} (to give it to "
                      f"the driver without this warning, write kwargs = {{ {key} = ... }})")
    else:
        for key in [k for k in kwargs if k not in names]:
            del kwargs[key]
            problems.append(f"{where} {'kwargs.' if key in passed else ''}{key}: not a parameter of {constructor}"
                            + did_you_mean(key, names if key in passed else [*names, *_RESERVED]))
    positional = [p.name for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    reached = {*kwargs, *positional[:len(args)]}
    unreached = [p for p in params if p.name in _RESERVED and p.name not in reached]
    # The table gives the key, and nothing says whether it was meant for insthub or for the driver (an
    # older file may have passed `simulated` to it): a problem, so that a driver never silently stops
    # getting a value.  Not `driver`, which every table has, so it says nothing of what was meant.
    ambiguous = [p for p in unreached if p.name in spec and p.name != "driver"]
    problems += [f"{where} {p.name}: one of insthub's own keys, but {constructor} has a parameter {p.name!r} too, "
                 f"which the key never reaches: say what the driver gets with kwargs = {{ {p.name} = ... }}"
                 for p in ambiguous]
    unsaid = [p for p in unreached if p not in ambiguous]
    try:
        sig.bind(*args, **kwargs)
    except TypeError as e:  # a required parameter that no key can reach is the likely cause: say why
        problems.append(f"{where} {constructor}: {e}"
                        + "".join(f"; {_only_kwargs(p.name)}" for p in unsaid if p.default is p.empty))
    if advise:  # an optional one the table does not name: likely fine; said once, when the file is checked
        for p in unsaid:
            if p.default is not p.empty:
                _warn(f"{where} {constructor} has a parameter {p.name!r}, but {_only_kwargs(p.name)}")
    return problems


# ---------------------------------------------------------------- building
def _make(name: str, spec: dict, cls) -> Device:
    """The device of the table [devices.NAME], which validate() found fine for its driver `cls`.
    Raises ConfigError for a name in meta, poll or dangerous that the device does not have, and
    DriverError if a Device subclass's constructor fails.  Only a Device subclass is constructed
    here: wrap() leaves the driver to the session, which makes it when it opens, so making a
    wrapped driver's device runs none of the driver's code."""
    args, kwargs = _arguments(name, spec, cls)
    if _is_device(cls):
        try:
            dev = cls(*args, **kwargs)
        except Exception as e:  # the driver's own code, such as a vendor library it imports: not the file's
            raise DriverError(f"[devices.{name}]: {spec['driver']}(...) failed: {type(e).__name__}: {e}") from e
        dev.name = name
        given_address(dev, cls, args, kwargs)  # (unless it says its address itself)
    else:
        options = {key: value for key, value, rule in _own_keys(spec) if rule.wrap}
        dev = wrap(cls, name, args=args, kwargs=kwargs, **options)
    try:
        for _, value, rule in _own_keys(spec):
            if rule.apply is not None:  # loading again is a look-up in sys.modules now
                rule.apply(dev, value if rule.load is None else rule.load(value))
        dev._rt.build()  # the names that meta, poll and dangerous use must be the device's: said now
    except (UnknownCommand, TypeError) as e:
        raise ConfigError(f"[devices.{name}] {e}") from None
    return dev


def _tables(devices: dict, advise: bool = False, problems: list[str] | None = None
            ) -> list[tuple[str, dict, object, Device | None]]:
    """(name, table, driver, device) for each [devices.NAME] of `devices`, every table checked
    (validate) before any driver runs.  A wrapped driver's device is made now, which runs none
    of its code (see _make), so that the names in its meta, poll and dangerous are checked too;
    a Device subclass's is left to the caller, since its constructor may do anything (device:
    None).  Raises (see _raise) with every problem of the tables, after `problems`: those of
    the rest of the file."""
    problems, unloadable, tables = list(problems or ()), [], []
    for name, spec in devices.items():
        try:
            cls, found = validate(name, spec, advise)
        except ImportError as e:  # said with what else is wrong with this table and the next ones
            cls, found = None, [*_wrong_kinds(name, spec).values(), str(e)]
            unloadable.append(e)
        made = None
        if not found and not _is_device(cls):
            try:
                made = _make(name, spec, cls)
            except ConfigError as e:
                found = [str(e)]
        tables.append((name, spec, cls, made))
        problems += found
    _raise(problems, unloadable)
    return tables


def _raise(problems: list[str], unloadable: list[ImportError]) -> None:
    """Raise what is wrong, if anything.  When each problem is a driver that cannot be loaded
    (`unloadable`, each one's message one of `problems`), which another Python may have, an
    ImportError: as it was raised for a single one; a _Clash if each is one, since no Python
    fixes those.  Else one ConfigError with every problem, those drivers among them."""
    if not problems:
        return
    if len(problems) > len(unloadable):
        raise ConfigError("\n".join(problems), tuple(unloadable))
    if len(unloadable) == 1:
        raise unloadable[0]
    raise (_Clash if all(isinstance(e, _Clash) for e in unloadable) else ImportError)("\n".join(problems))


def build_device(name: str, spec: dict) -> Device:
    """The device of the table [devices.NAME].  Raises ImportError if its driver cannot be
    loaded, ConfigError listing every problem of the table (see validate), or naming what
    meta, poll or dangerous names that the device does not have, and DriverError if the
    constructor of a Device subclass fails."""
    [(_, _, cls, made)] = _tables({name: spec})
    return made if made is not None else _make(name, spec, cls)


def _read(path: Path) -> dict:
    """The file as TOML; a ConfigError, naming it, if it cannot be read or is not TOML."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror or e}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None


def _checked(path: str | Path, advise: bool = False) -> tuple[dict, list[tuple[str, dict, object, Device | None]]]:
    """A config file's [server] table, with its name, and (name, table, driver, device) for
    each [devices.NAME] (see _tables).  Raises as _tables does, with every problem of the
    file."""
    path = Path(path).expanduser()
    config = _read(path)
    server = {"name": path.stem, **config.get("server", {})}
    problems = [f"[server] {key}: not one of {', '.join(_SERVER_KEYS)}" + did_you_mean(key, _SERVER_KEYS)
                for key in server if key not in _SERVER_KEYS]
    return server, _tables(config.get("devices", {}), advise, problems)


def check(path: str | Path) -> None:
    """Check a config file as load() does, without running any driver's code but that of its
    module: load the driver of every device and check its table (validate), and for a
    wrapped driver also the names in meta, poll and dangerous.  Raises ImportError for
    drivers that cannot be loaded, and else one ConfigError with every problem of the file
    (those drivers among them); DriverError if a driver's module fails.  Warns
    (ConfigWarning) about what works, but may not where servers run, or not as meant: a
    driver that imports Qt, an optional constructor parameter that no key reaches.
    `insthub service install` runs it in the service's Python."""
    _checked(path, advise=True)


def load(path: str | Path) -> tuple[dict, list[Device]]:
    """Read a config file.  Returns (keyword arguments for Server, devices).  Raises as
    check() does, before any Device subclass is constructed; then one ConfigError with each
    name in the meta, poll or dangerous of a Device subclass that the device does not have,
    which only the device can tell, or a DriverError for a constructor that fails."""
    server, tables = _checked(path)
    devices, problems = [], []
    for name, spec, cls, made in tables:
        try:
            devices.append(made if made is not None else _make(name, spec, cls))
        except ConfigError as e:
            problems.append(str(e))
    _raise(problems, [])
    return server, devices


def dump(server: dict, devices: dict[str, dict], comment: str = "") -> str:
    """The text of a config file that `load` reads back as `server` (the [server]
    table) and `devices` (name -> its keys).  Values: strings, numbers, booleans,
    and lists and dicts of them.  `comment` goes on top, as # lines."""
    lines = [f"# {line}".rstrip() for line in comment.splitlines()]
    lines += [""] if lines else []
    lines.append("[server]")
    lines += [f"{tomlfile.key(k)} = {tomlfile.value(v)}" for k, v in server.items()]
    for name, spec in devices.items():
        lines += ["", tomlfile.header(("devices", name))]
        lines += [f"{tomlfile.key(k)} = {tomlfile.value(v)}" for k, v in spec.items()]
    return "\n".join(lines) + "\n"
