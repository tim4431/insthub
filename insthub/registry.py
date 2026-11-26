"""Finding servers by name, without a broker.

Every running server writes `<runtime dir>/<server>.json` (its pid, addresses
and device names; also its Python and the folders of its devices' code, which
only programs on this computer need, so they are never sent over the network)
and listens on `<runtime dir>/<server>.sock`.  A client looks a device up by
scanning those files.  The directory is private to the user (mode 0700), which
is also what protects the Unix sockets.

Devices on other machines are listed by hand in a small TOML file
(`$INSTHUB_DEVICES`, default `~/.config/insthub/devices.toml`, or in
$XDG_CONFIG_HOME if set: see config_dir), and so are
servers there whose devices are found by asking them:

    [devices]
    awg = "tcp://192.168.1.20:8701"

    [servers]
    labpc = "tcp://192.168.1.21:8700"

`remote_addresses()` is the one list of those servers, for everything that
looks beyond this machine.  `edit_static_devices()` adds and removes entries
of [devices], leaving the rest of the file (comments included) as it was; the
manager window uses it.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Iterable
from pathlib import Path

from .errors import BusError

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


def check_name(name: str, what: str = "name") -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"bad {what} {name!r}: use letters, digits, '_', '-', '.' and start with a letter or digit")
    return name


def runtime_dir() -> Path:
    """Directory holding the socket and registry files of this user's servers.  Without
    XDG_RUNTIME_DIR (nor INSTHUB_DIR), a folder of a known name in the temporary folder, which
    every user may write to: insthub then makes sure that it is this user's, and private (see
    _refuse_shared)."""
    base = os.environ.get("INSTHUB_DIR")
    shared = False
    if base:
        path = Path(base)
    elif os.environ.get("XDG_RUNTIME_DIR"):
        path = Path(os.environ["XDG_RUNTIME_DIR"]) / "insthub"
    else:
        uid = os.getuid() if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
        path, shared = Path(tempfile.gettempdir()) / f"insthub-{uid}", True
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shared:
        _refuse_shared(path)
    return path


def _refuse_shared(path: Path) -> None:
    """Raise PermissionError unless `path` is a folder (not a link) of this user that no one else
    may write to.  Another user could make the folder first: then the .json files in it would lead
    this user's programs to sockets of theirs, and to folders of theirs whose code may be run (the
    extensions of the servers listed there)."""
    if not hasattr(os, "getuid"):  # Windows: the temporary folder is the user's own
        return
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise PermissionError(f"insthub will not use {path}: it is not a folder of this user that only this user may "
                              "use (did another user make it?). Remove it, or set XDG_RUNTIME_DIR, or INSTHUB_DIR, to "
                              "a folder of your own")


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


def identity(info: dict) -> tuple:
    """What tells one server's process from every other, on any computer: its name, its pid and
    when it started, as its hello, its registry entry and discover() say them.  Two addresses
    whose servers have the same identity reach the same server (its addresses are no test: a
    server on another computer may announce the very ones of a server here, as tcp://127.0.0.1
    and the same port)."""
    return info.get("server"), info.get("pid"), info.get("started")


def _answers(info: dict) -> bool:
    """Whether the server of a registry entry still answers at one of its
    addresses, and is the very process the entry names (a port can be reused).
    Used where a pid cannot be tested; works on every platform."""
    from .client import connect  # here, not at the top: client imports this module

    for address in info.get("addresses", ()):
        try:
            client = connect(address, connect_timeout=1.0)
        except (BusError, OSError, ValueError, AttributeError):
            continue
        with client:
            return identity(client.hello) == identity(info)
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


def config_dir() -> Path:
    """insthub's folder of your settings: insthub in XDG_CONFIG_HOME, which is ~/.config unless set
    (devices.toml, extensions.toml, the configs of the servers the window makes)."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "insthub"


def devices_file() -> Path:
    """The hand-written table of devices on other machines."""
    path = os.environ.get("INSTHUB_DEVICES")
    return Path(path) if path else config_dir() / "devices.toml"


def _devices_table(name: str) -> dict[str, str]:
    """A table of devices.toml ("devices", "servers"): {name: address}.  Raises BusError, naming
    the file, if it cannot be read or is not TOML."""
    path = devices_file()
    try:
        with open(path, "rb") as f:
            table = tomllib.load(f).get(name, {})
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise BusError(f"cannot read {path}: {e.strerror or e}") from None
    except tomllib.TOMLDecodeError as e:
        raise BusError(f"{path}: {e}") from None
    return {str(k): str(v) for k, v in table.items()} if isinstance(table, dict) else {}


def static_devices() -> dict[str, str]:
    """The hand-written name -> address table for devices on other machines.  Raises BusError,
    naming devices.toml, if it cannot be read."""
    return _devices_table("devices")


def remote_addresses() -> dict[str, list[str]]:
    """The servers on other machines that this one knows of: {address: the names of the devices
    devices.toml lists there}, each address once.  Those of its [servers] table come first, with
    no device of their own unless [devices] lists some there too: their devices are found by asking
    them (and their names in the table are for you: a server says its own).  Raises BusError,
    naming devices.toml, if it cannot be read."""
    found: dict[str, list[str]] = {address: [] for address in _devices_table("servers").values()}
    for name, address in static_devices().items():
        found.setdefault(address, []).append(name)
    return found


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
    from . import (
        tomlfile,  # here, not at the top: only the window edits the file, and every program imports this
    )

    path = devices_file()
    try:
        tomlfile.update(path, {("devices",): {**dict.fromkeys(remove), **add}}, new_file=(
            "# Devices on other computers, found by name from this one.  Written by insthub gui; edit freely.\n"))
    except ValueError:  # also a devices.toml that is not TOML at all
        raise ValueError(f"{path}: its [devices] table is written in a way this cannot edit safely; "
                         "edit it by hand") from None
    return path
