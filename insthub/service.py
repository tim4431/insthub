"""Running a server in the background: `insthub service ...` (Linux with systemd).

A server started in a terminal stops when the terminal closes.  This module
registers it as a systemd *user* service instead: started when you log in (or
at boot, see `linger_hint`) unless it is told not to (`login=False`, `disable`),
restarted if it crashes (but not forever: see unit_text), its output kept in the
journal.  Installing, starting and restarting wait until its server has
announced itself, and say why it did not, with the journal's last lines.
Nothing here needs root.

There is one convention for every server, whatever instrument it drives:

    unit        ~/.config/systemd/user/insthub-NAME.service
    command     the Python interpreter that ran `insthub service install` (or
                --python), so the server runs in that environment (the
                virtualenv with the driver's dependencies), on a .toml config
                or a .py script

Before writing the unit, `install` makes sure that Python can import insthub
and, for a config, every driver it names, and that each device's keys fit its
driver (config.check); what config.check warns about is passed on as a
ConfigWarning.  It refuses to run from the user-wide `insthub` command (a uv
tool or pipx environment), which has no drivers.

What stays outside: anything that gives the operating system access to the
hardware (udev rules, kernel modules, groups).  That belongs to the driver.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

from . import registry
from .config import ConfigWarning
from .errors import BusError

PREFIX = "insthub-"
WAIT = 60.0  #: seconds install, start and restart wait for the server to announce itself


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


def tool_environment(prefix: str | None = None) -> str | None:
    """"uv tool" or "pipx" if this Python is the private environment of a command
    installed that way (the user-wide `insthub` command); else None."""
    root = Path(prefix or sys.prefix)
    if (root / "uv-receipt.toml").exists():
        return "uv tool"
    if (root / "pipx_metadata.json").exists():
        return "pipx"
    return None


def ask_python(python: str, code: str, *args: str, cwd: str | Path, doing: str) -> tuple[dict | None, str]:
    """Run `python -c code REPORT ARGS...` in the folder `cwd`, where `code` writes what it found,
    as JSON, to the file REPORT: not to stdout, which the drivers it imports may write to as well.
    Returns (that report, or None if it wrote none: it is not a Python, or it crashed; what it
    wrote to stderr).  Raises BusError if `python` cannot be run, or takes more than 120 s to do
    what `doing` says ("import the drivers")."""
    with tempfile.TemporaryDirectory(prefix="insthub-check-") as folder:
        report = Path(folder) / "report.json"
        try:
            result = subprocess.run([python, "-c", code, str(report), *args], cwd=cwd, capture_output=True,
                                    text=True, errors="replace", timeout=120, check=False)  # (a vendor
            # library may write bytes of another encoding to stderr: they must not stop the check)
        except OSError as e:
            raise BusError(f"cannot run {python}: {e}") from None
        except subprocess.TimeoutExpired:
            raise BusError(f"{python} took more than 120 s to {doing}") from None
        try:
            return json.loads(report.read_text()), result.stderr
        except (OSError, ValueError):
            return None, result.stderr


#: What check_report runs in the service's Python: `python -c _CHECK REPORT TARGET`.  It writes its
#: report to the file REPORT (see ask_python), as JSON: the problem, if any; what would fix it, by
#: the kind of error ("file": the config file; "environment": another Python, or packages installed
#: into this one; for a driver's own error, neither); and config.check's warnings, not those of the
#: drivers' libraries.  Besides config.check it uses only the standard library, and it falls back
#: to what an older insthub raises (a plain ValueError for a problem of the file), so any insthub
#: there will do.
_CHECK = """\
import json, sys, warnings
report = {"problem": None, "fixes": [], "warnings": []}
with warnings.catch_warnings(record=True) as caught:
    try:
        from insthub import config
        if sys.argv[2].endswith(".toml"):
            config.check(sys.argv[2])
    except Exception as e:
        cfg = sys.modules.get("insthub.config")
        of_file = getattr(cfg, "ConfigError", ValueError)  # an older insthub has none: a ValueError
        # insthub's own errors say what they are; others (from a driver's module, ...) by their kind
        said = isinstance(e, (of_file, ImportError, getattr(cfg, "DriverError", ())))
        report["problem"] = str(e) if said else f"{type(e).__name__}: {e}"
        if isinstance(e, of_file):
            report["fixes"].append("file")
        # drivers that cannot be loaded, alone or among the file's problems; not an ImportError's
        # subclass, such as two folders' modules of one name: no Python fixes that
        if any(type(u) in (ImportError, ModuleNotFoundError) for u in [e, *getattr(e, "unloadable", ())]):
            report["fixes"].append("environment")
    ours = getattr(sys.modules.get("insthub.config"), "ConfigWarning", ())
    report["warnings"] = [str(w.message) for w in caught if issubclass(w.category, ours)]
with open(sys.argv[1], "w") as f:
    json.dump(report, f)
"""

#: What to do about a problem that check_python found, by what would fix it.
_FIXES = {
    "file": "Fix {file}, then install again.",
    "environment": "The service runs this Python, so it needs insthub and the drivers' packages. "
                   "Install them there, or pass --python with the Python that has them.",
}


def check_report(target: str | Path, python: str) -> dict:
    """What `python` finds when it is to serve `target`: it imports insthub and, for a .toml
    config, every driver of it (without creating a device), from the folder the service will
    run in, and checks each device's keys against its driver (config.check).  Returns
    {"problem": what is missing or wrong, or None; "fixes": what would fix it (see _FIXES);
    "warnings": config.check's}.  Raises BusError as ask_python does."""
    target = Path(target).expanduser().resolve()
    report, stderr = ask_python(python, _CHECK, str(target), cwd=target.parent,
                                doing=f"import the drivers of {target.name}")
    if report is None:  # it did not get that far: not a Python, or one that crashed
        report = {"problem": (stderr.strip().splitlines() or ["(no output)"])[-1], "fixes": ["environment"],
                  "warnings": []}
    return report


def check_python(target: str | Path, python: str) -> list[str]:
    """Make sure `python` can serve `target` (check_report).  Raises BusError saying
    what is missing or wrong, and what to do about it.  Returns the warnings, such
    as a driver that imports Qt, which a server without a screen may not have."""
    target = Path(target).expanduser().resolve()
    report = check_report(target, python)
    if report["problem"]:
        advice = [_FIXES[fix].format(file=target.name) for fix in report["fixes"]]
        raise BusError("\n".join([f"{python} cannot serve {target.name}: {report['problem']}", *advice]))
    return report["warnings"]


def unit_text(name: str, target: str | Path, python: str | None = None) -> str:
    """The systemd unit that keeps the server of `target` running: restarted 5 s after it crashes,
    but no more than 5 starts in 5 minutes, so that a server that cannot start (a broken config,
    an instrument it cannot do without) ends up "failed" instead of restarting forever."""
    registry.check_name(name, "service name")
    target = Path(target).expanduser().resolve()
    command = " ".join(_quote(part) for part in command_for(target, python))
    return f"""# Written by `insthub service install`.  Remove it with:  insthub service remove {name}
[Unit]
Description=insthub server {name}
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
ExecStart={command}
WorkingDirectory={str(target.parent).replace("%", "%%")}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
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


def install(target: str | Path, name: str | None = None, start: bool = True, python: str | None = None,
            login: bool | None = None, wait: float | None = None) -> Path:
    """Register the server of `target` as the service insthub-NAME and start it (and wait for
    it, as start() does).  Installing again under the same name replaces the old definition.
    `python`: the interpreter the service runs (default: this one).
    `login`: start it at every login (default: as `start`); False: only when started by hand."""
    target = Path(target).expanduser().resolve()
    name = name or target.stem
    if python is None:
        python = sys.executable
    else:
        found = shutil.which(python)
        if found is None:
            raise BusError(f"--python {python}: no such program")
        python = os.path.abspath(found)  # not realpath: a virtualenv is recognized by the path of its python link
    kind = tool_environment(str(Path(python).parent.parent))  # <env>/bin/python, <env>\Scripts\python.exe
    if kind:
        raise BusError(
            f"{python} belongs to the user-wide `insthub` command (a {kind} environment), which has none of your "
            "drivers, so a service run with it could not load them. Use the Python of the server's own "
            f"environment: run its insthub (e.g.  ~/Code/mydriver/.venv/bin/insthub service install {target.name})"
            " or pass --python.")
    text = unit_text(name, target, python)
    for message in check_python(target, python):
        warnings.warn(message, ConfigWarning, stacklevel=2)
    folder = unit_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{PREFIX}{name}.service"
    existed = path.exists()
    path.write_text(text)
    _systemctl("daemon-reload")
    if start if login is None else login:
        _systemctl("enable", path.name)
    elif existed:  # a new definition: the new choice holds
        _systemctl("disable", path.name, check=False)
    if start:
        try:
            _begin("restart" if existed else "start", name, wait)
        except BusError as e:
            raise BusError(f"installed {path}, but {e}") from None
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


def start(name: str, wait: float | None = None) -> int | None:
    """Start it (also one that gave up after too many restarts), and wait up to `wait` seconds
    (default WAIT; 0: not at all) until its server has announced itself (_wait_up).  Returns that
    server's pid."""
    return _begin("start", name, wait)


def stop(name: str) -> None:
    """Stop until the next login or `start` (it stays installed)."""
    _systemctl("stop", _unit(name))


def enable(name: str) -> None:
    """Start it at every login from now on (it is not started now)."""
    _systemctl("enable", _unit(name))


def disable(name: str) -> None:
    """No longer start it at login (a running one keeps running)."""
    _systemctl("disable", _unit(name))


def restart(name: str, wait: float | None = None) -> int | None:
    """Restart, e.g. after changing the server's code or config, and wait as start() does."""
    return _begin("restart", name, wait)


def _begin(what: str, name: str, wait: float | None) -> int | None:
    unit = _unit(name)
    _systemctl("reset-failed", unit, check=False)  # what failed before (or gave up) starts again by hand
    _systemctl(what, unit)
    return _wait_up(name, WAIT if wait is None else wait)


def _wait_up(name: str, wait: float) -> int | None:
    """Wait up to `wait` seconds (0: not at all) until the process of insthub-NAME has announced
    its server (a registry entry with its pid), and return that pid.  Raises BusError, with the last
    lines of its journal, if it ends or waits to be restarted (it crashed) before that, or if it
    takes longer."""
    if not wait:
        return None
    end = time.monotonic() + wait
    while True:
        u = units(name)[0]
        if u["pid"] and any(entry.get("pid") == u["pid"] for entry in registry.servers()):
            return u["pid"]
        if u["state"] in ("failed", "inactive") or u["sub"] == "auto-restart":
            why = "ended" if u["state"] == "inactive" else "stopped with an error"
            break
        if time.monotonic() > end:
            why = f"has announced no insthub server after {wait:g} s"
            break
        time.sleep(0.25)
    try:
        said = journal(name, 20).rstrip() or "(nothing)"
    except BusError as e:
        said = str(e)
    raise BusError(f"{PREFIX}{name} {why}. The end of its log (insthub service logs {name}):\n{said}")


def units(name: str | None = None) -> list[dict]:
    """The installed services (or the one called `name`): name, state ("active", "failed",
    "inactive", "activating", ...), sub (systemd's detail, e.g. "auto-restart" while it waits to
    restart a crashed server), enabled, pid (of the running server, else None), restarts (how often
    systemd restarted it since it was started), result (how it last ended: "success", "exit-code",
    "start-limit-hit" once it gave up, ...) and command."""
    paths = [unit_dir() / _unit(name)] if name else sorted(unit_dir().glob(f"{PREFIX}*.service"))
    if not paths:
        return []
    out = _systemctl("show", "--property=Id,ActiveState,SubState,UnitFileState,MainPID,NRestarts,Result",
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
                      "pid": pid or None, "restarts": int(props.get("NRestarts") or 0),
                      "result": props.get("Result", ""), "command": command, "unit": str(path)})
    return found


def restarts_text(unit: dict) -> str:
    """"restarted 3 times" (since it was started), and "gave up" when systemd did; else ""."""
    n = unit.get("restarts") or 0
    told = [f"restarted {n} time{'s' * (n != 1)}"] if n else []
    if unit.get("result") == "start-limit-hit":
        told.append("gave up: too many restarts")
    return ", ".join(told)


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
