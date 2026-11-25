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


def remove(name: str) -> None:
    """Stop the service and delete its unit."""
    path = unit_dir() / f"{PREFIX}{name}.service"
    if not path.exists():
        raise BusError(f"no service {PREFIX}{name} (see `insthub service list`)")
    _systemctl("disable", "--now", path.name, check=False)
    path.unlink()
    _systemctl("daemon-reload")
    _systemctl("reset-failed", path.name, check=False)


def units() -> list[dict]:
    """The installed services: name, state ("active", "failed", "inactive", ...), enabled, command."""
    found = []
    for path in sorted(unit_dir().glob(f"{PREFIX}*.service")):
        command = next((line.split("=", 1)[1] for line in path.read_text().splitlines()
                        if line.startswith("ExecStart=")), "")
        state = _systemctl("is-active", path.name, check=False).stdout.strip() or "unknown"
        enabled = _systemctl("is-enabled", path.name, check=False).stdout.strip() == "enabled"
        found.append({"name": path.stem[len(PREFIX):], "state": state, "enabled": enabled,
                      "command": command, "unit": str(path)})
    return found


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
