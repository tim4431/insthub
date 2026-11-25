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
to expose), `close` (name of the close method), `link_errors` (exception
classes as "module:Class" that mean the connection broke), and for all
devices `reconnect_interval`.

`dump()` writes such a file (the manager window's "New server" does).
"""
from __future__ import annotations

import importlib
import inspect
import json
import math
import re
import sys
import tomllib
from pathlib import Path

from .device import Device, wrap

_RESERVED = ("driver", "path", "lazy", "args", "kwargs", "include", "exclude", "attrs", "close", "link_errors",
             "reconnect_interval")


def load_object(spec: str, path: str | None = None):
    """Import "module:Name" (after adding `path` to sys.path)."""
    if path:
        folder = str(Path(path).expanduser())
        if folder not in sys.path:
            sys.path.insert(0, folder)
    module, sep, name = spec.partition(":")
    if not sep:
        raise ValueError(f"driver {spec!r} must look like 'module:Class'")
    obj = importlib.import_module(module)
    for part in name.split("."):
        obj = getattr(obj, part)
    return obj


def _driver(name: str, spec: dict):
    if "driver" not in spec:
        raise ValueError(f"[devices.{name}] needs a driver = 'module:Class'")
    try:
        return load_object(spec["driver"], spec.get("path"))
    except (ImportError, AttributeError) as e:
        raise ImportError(f"[devices.{name}]: cannot load driver {spec['driver']!r}: {e} (this is {sys.executable}; "
                          "is it the Python environment that has the driver's dependencies?)") from e


def build_device(name: str, spec: dict) -> Device:
    spec = dict(spec)
    cls = _driver(name, spec)
    kwargs = {**{k: v for k, v in spec.items() if k not in _RESERVED}, **spec.get("kwargs", {})}
    link_errors = spec.get("link_errors")
    if link_errors is not None:
        link_errors = tuple(load_object(e) for e in link_errors) + Device.link_errors
    if isinstance(cls, type) and issubclass(cls, Device):
        if "name" in inspect.signature(cls).parameters:
            kwargs.setdefault("name", name)
        dev = cls(*spec.get("args", ()), **kwargs)
        dev.name = name
        if link_errors is not None:
            dev.link_errors = link_errors
    else:
        dev = wrap(cls, name, args=spec.get("args", ()), kwargs=kwargs, include=spec.get("include"),
                   exclude=spec.get("exclude", ()), attrs=spec.get("attrs", ()), close=spec.get("close", "close"),
                   link_errors=link_errors)
    if spec.get("lazy"):
        dev.open_on_start = False
    if "reconnect_interval" in spec:
        dev.reconnect_interval = float(spec["reconnect_interval"])
    return dev


def check(path: str | Path) -> None:
    """Import the driver (and link_errors) of every device in a config file,
    without creating any device.  Raises if one cannot be loaded."""
    with open(Path(path).expanduser(), "rb") as f:
        config = tomllib.load(f)
    for name, spec in config.get("devices", {}).items():
        _driver(name, spec)
        for spec_error in spec.get("link_errors", ()):
            load_object(spec_error)


def load(path: str | Path) -> tuple[dict, list[Device]]:
    """Read a config file.  Returns (keyword arguments for Server, devices)."""
    path = Path(path).expanduser()
    with open(path, "rb") as f:
        config = tomllib.load(f)
    server = dict(config.get("server", {}))
    server.setdefault("name", path.stem)
    unknown = set(server) - {"name", "tcp", "ipc"}
    if unknown:
        raise ValueError(f"unknown keys in [server]: {sorted(unknown)}")
    devices = [build_device(name, spec) for name, spec in config.get("devices", {}).items()]
    return server, devices


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _toml_value(key)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        return ("inf" if value > 0 else "-inf") if math.isinf(value) else repr(value)
    if isinstance(value, str):  # a JSON string is a TOML basic string, but for DEL, which TOML wants escaped
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items()) + " }" \
            if value else "{}"
    raise ValueError(f"{value!r} cannot be written to a config file (TOML has no {type(value).__name__})")


def dump(server: dict, devices: dict[str, dict], comment: str = "") -> str:
    """The text of a config file that `load` reads back as `server` (the [server]
    table) and `devices` (name -> its keys).  Values: strings, numbers, booleans,
    and lists and dicts of them.  `comment` goes on top, as # lines."""
    lines = [f"# {line}".rstrip() for line in comment.splitlines()]
    lines += [""] if lines else []
    lines.append("[server]")
    lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in server.items()]
    for name, spec in devices.items():
        lines += ["", f"[devices.{_toml_key(name)}]"]
        lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in spec.items()]
    return "\n".join(lines) + "\n"
