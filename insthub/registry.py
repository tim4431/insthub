"""Finding servers by name, without a broker.

Every running server writes `<runtime dir>/<server>.json` (its pid, addresses
and device names) and listens on `<runtime dir>/<server>.sock`.  A client looks
a device up by scanning those files.  The directory is private to the user
(mode 0700), which is also what protects the Unix sockets.

Devices on other machines are listed by hand in a small TOML file
(`$INSTHUB_DEVICES`, default `~/.config/insthub/devices.toml`):

    [devices]
    awg = "tcp://192.168.1.20:8701"

`edit_static_devices()` adds and removes entries of it, leaving the rest of the
file (comments included) as it was; the manager window uses it.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from collections.abc import Iterable
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


def check_name(name: str, what: str = "name") -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"bad {what} {name!r}: use letters, digits, '_', '-', '.' and start with a letter or digit")
    return name


def runtime_dir() -> Path:
    """Directory holding the socket and registry files of this user's servers."""
    base = os.environ.get("INSTHUB_DIR")
    if base:
        path = Path(base)
    elif os.environ.get("XDG_RUNTIME_DIR"):
        path = Path(os.environ["XDG_RUNTIME_DIR"]) / "insthub"
    else:
        uid = os.getuid() if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
        path = Path(tempfile.gettempdir()) / f"insthub-{uid}"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def socket_path(server: str) -> Path:
    return runtime_dir() / f"{server}.sock"


def _entry_path(server: str) -> Path:
    return runtime_dir() / f"{server}.json"


def write(server: str, info: dict) -> None:
    path = _entry_path(server)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(info))
    os.replace(tmp, path)


def remove(server: str) -> None:
    for path in (_entry_path(server), socket_path(server)):
        try:
            path.unlink()
        except OSError:
            pass


def _alive(pid: int) -> bool | None:
    """Whether the process exists; None where there is no safe way to ask."""
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _answers(info: dict) -> bool:
    """Whether the server of a registry entry still answers at one of its
    addresses, and is the very process the entry names (a port can be reused).
    Used where a pid cannot be tested; works on every platform."""
    from .client import _SocketTransport  # here, not at the top: client imports this module
    from .errors import BusError

    for address in info.get("addresses", ()):
        try:
            transport = _SocketTransport(address, connect_timeout=1.0)
        except (BusError, OSError, ValueError, AttributeError):
            continue
        try:
            hello = transport.hello
            return hello.get("pid") == info.get("pid") and hello.get("server") == info.get("server")
        finally:
            transport.close()
    return False


def servers() -> list[dict]:
    """Registry entries of the servers running on this machine (stale ones are removed)."""
    found = []
    for path in sorted(runtime_dir().glob("*.json")):
        try:
            info = json.loads(path.read_text())
            pid = int(info["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        alive = _alive(pid)
        if alive is None:
            alive = _answers(info)
        if not alive:
            remove(path.stem)
            continue
        found.append(info)
    return found


def find(device: str) -> list[dict]:
    """Entries of the servers that host a device of that name."""
    return [s for s in servers() if device in s.get("devices", ())]


def devices_file() -> Path:
    """The hand-written table of devices on other machines."""
    path = os.environ.get("INSTHUB_DEVICES")
    return Path(path) if path else Path.home() / ".config" / "insthub" / "devices.toml"


def static_devices() -> dict[str, str]:
    """The hand-written name -> address table for devices on other machines."""
    try:
        with open(devices_file(), "rb") as f:
            table = tomllib.load(f).get("devices", {})
    except FileNotFoundError:
        return {}
    return {str(k): str(v) for k, v in table.items()}


_SECTION = re.compile(r"^\s*\[\s*devices\s*\]\s*(#.*)?$")
_TABLE = re.compile(r"^\s*\[")
_ENTRY = re.compile(r'^\s*(?:"([^"]+)"|([A-Za-z0-9_\-]+))\s*=')
_VALUE = re.compile(r"""^(\s*[^=]+=\s*)("(?:[^"\\]|\\.)*"|'[^']*')(.*)$""")  # what is around the value: kept


def _entry(name: str, address: str, line: str | None = None) -> str:
    """The line of an entry; given the line it had, only its value changes (a comment after it stays)."""
    match = _VALUE.match(line) if line is not None else None
    if match:
        return match.group(1) + json.dumps(address) + match.group(3)
    key = name if re.fullmatch(r"[A-Za-z0-9_\-]+", name) else json.dumps(name)  # a dot would make it a dotted key
    return f"{key} = {json.dumps(address)}"


def edit_static_devices(add: dict[str, str] | None = None, remove: Iterable[str] = ()) -> Path:
    """Add (or change) entries `name = "address"` of devices.toml, and remove others,
    keeping everything else in the file as it is.  Creates the file if needed.
    Raises ValueError, and writes nothing, if the result would not be what was asked."""
    add = dict(add or {})
    remove = set(remove) - set(add)
    for name, address in add.items():
        check_name(name, "device name")
        if "://" not in address:
            raise ValueError(f"{name}: address {address!r} should look like tcp://host:port")
    path = devices_file()
    text = path.read_text() if path.exists() else (
        "# Devices on other computers, found by name from this one.  Written by insthub gui; edit freely.\n")
    old = tomllib.loads(text).get("devices", {})
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if _SECTION.match(line)), None)
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("[devices]")
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if _TABLE.match(lines[i])), len(lines))
    pending, section, last = dict(add), [], 0
    for line in lines[start + 1:end]:
        match = _ENTRY.match(line)
        key = match and (match.group(1) or match.group(2))
        if key in remove:
            continue
        section.append(_entry(key, pending.pop(key), line) if key in pending else line)
        if match:
            last = len(section)
    section[last:last] = [_entry(name, address) for name, address in pending.items()]  # after the last entry
    new = "\n".join(lines[:start + 1] + section + lines[end:]) + "\n"
    expected = {**{k: v for k, v in old.items() if k not in remove}, **add}
    try:
        result = tomllib.loads(new).get("devices", {})
    except tomllib.TOMLDecodeError:
        result = None
    if result != expected:
        raise ValueError(f"{path}: its [devices] table is written in a way this cannot edit safely; edit it by hand")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(new)
    os.replace(tmp, path)
    return path
