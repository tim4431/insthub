"""Running a server in the background: `insthub service ...` (Linux with systemd).

A server started in a terminal stops when the terminal closes.  This module
registers it as a systemd *user* service instead: started when you log in (or
at boot, see `linger_hint`), restarted if it crashes, its output kept in the
journal.  Nothing here needs root.

There is one convention for every server, whatever instrument it drives:

    unit        ~/.config/systemd/user/insthub-NAME.service
    command     the Python interpreter that ran `insthub service install`, so the
                server runs in that environment (the virtualenv with the
                driver's dependencies), on a .toml config or a .py script

What stays outside: anything that gives the operating system access to the
hardware (udev rules, kernel modules, groups).  That belongs to the driver.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import registry
from .errors import BusError

PREFIX = "insthub-"


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _quote(arg: str) -> str:
    """One argument of an ExecStart= line (systemd's own quoting, not the shell's)."""
    arg = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{arg}"'


def command_for(target: str | Path, python: str | None = None) -> list[str]:
    """The command that serves `target`: a .toml config or a .py server script."""
    target = Path(target).expanduser().resolve()
    python = python or sys.executable
    if not target.is_file():
        raise BusError(f"{target} does not exist")
    if target.suffix == ".toml":
        return [python, "-m", "insthub", "serve", str(target)]
    if target.suffix == ".py":
        return [python, str(target)]
    raise BusError(f"{target.name}: expected a .toml config (for `insthub serve`) or a .py server script")


def unit_text(name: str, target: str | Path, python: str | None = None) -> str:
    """The systemd unit that keeps the server of `target` running."""
    registry.check_name(name, "service name")
    target = Path(target).expanduser().resolve()
    command = " ".join(_quote(part) for part in command_for(target, python))
    return f"""# Written by `insthub service install`.  Remove it with:  insthub service remove {name}
[Unit]
Description=insthub server {name}

[Service]
ExecStart={command}
WorkingDirectory={str(target.parent).replace("%", "%%")}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=2
TimeoutStopSec=20

[Install]
WantedBy=default.target
"""


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    if shutil.which("systemctl") is None or not os.environ.get("XDG_RUNTIME_DIR"):
        raise BusError("`insthub service` needs systemd user services (Linux, in a login session). "
                       "Elsewhere, start the server from your system's own autostart mechanism.")
    result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise BusError(f"systemctl --user {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def install(target: str | Path, name: str | None = None, start: bool = True, python: str | None = None) -> Path:
    """Register the server of `target` as the service insthub-NAME and start it.
    Installing again under the same name replaces the old definition."""
    target = Path(target).expanduser().resolve()
    name = name or target.stem
    text = unit_text(name, target, python)
    folder = unit_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{PREFIX}{name}.service"
    existed = path.exists()
    path.write_text(text)
    _systemctl("daemon-reload")
    if start:
        _systemctl("enable", path.name)
        _systemctl("restart" if existed else "start", path.name)
    return path


def _unit(name: str) -> str:
    path = unit_dir() / f"{PREFIX}{name}.service"
    if not path.exists():
        raise BusError(f"no service {PREFIX}{name} (see `insthub service list`)")
    return path.name


def remove(name: str) -> None:
    """Stop the service and delete its unit."""
    path = unit_dir() / _unit(name)
    _systemctl("disable", "--now", path.name, check=False)
    path.unlink()
    _systemctl("daemon-reload")
    _systemctl("reset-failed", path.name, check=False)


def start(name: str) -> None:
    _systemctl("start", _unit(name))


def stop(name: str) -> None:
    """Stop until the next login or `start` (it stays installed)."""
    _systemctl("stop", _unit(name))


def restart(name: str) -> None:
    """Restart, e.g. after changing the server's code or config."""
    _systemctl("restart", _unit(name))


def units() -> list[dict]:
    """The installed services: name, state ("active", "failed", "inactive", "activating", ...), sub
    (systemd's detail, e.g. "auto-restart" while it waits to restart a crashed server), enabled,
    pid (of the running server, else None) and command."""
    paths = sorted(unit_dir().glob(f"{PREFIX}*.service"))
    if not paths:
        return []
    out = _systemctl("show", "--property=Id,ActiveState,SubState,UnitFileState,MainPID",
                     *(path.name for path in paths), check=False).stdout
    shown = {}
    for block in out.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in props:
            shown[props["Id"]] = props
    found = []
    for path in paths:
        command = next((line.split("=", 1)[1] for line in path.read_text().splitlines()
                        if line.startswith("ExecStart=")), "")
        props = shown.get(path.name, {})
        pid = int(props.get("MainPID") or 0)
        found.append({"name": path.stem[len(PREFIX):], "state": props.get("ActiveState") or "unknown",
                      "sub": props.get("SubState", ""), "enabled": props.get("UnitFileState") == "enabled",
                      "pid": pid or None, "command": command, "unit": str(path)})
    return found


def journal(name: str, lines: int = 200) -> str:
    """The service's recent output, as text."""
    command = ["journalctl", "--user", "-u", f"{PREFIX}{name}.service", "-n", str(lines), "--no-pager",
               "-o", "short-iso"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BusError(f"cannot read the log of {PREFIX}{name}: {e}") from None
    return result.stdout or result.stderr


def logs(name: str, follow: bool = False, lines: int = 50) -> int:
    """Show the service's output (journalctl)."""
    command = ["journalctl", "--user", "-u", f"{PREFIX}{name}.service", "-n", str(lines), "--no-pager"]
    if follow:
        command.append("-f")
    try:
        return subprocess.call(command)
    except KeyboardInterrupt:
        return 0


def linger_hint() -> str | None:
    """User services start at login and stop at logout unless lingering is on.
    Returns the command that turns it on, or None if it already is."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    try:
        out = subprocess.run(["loginctl", "show-user", user, "-p", "Linger"], capture_output=True, text=True).stdout
    except OSError:
        return None
    return None if "Linger=yes" in out else f"sudo loginctl enable-linger {user}"
