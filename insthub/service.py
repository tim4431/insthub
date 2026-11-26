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
    command     a Python interpreter on a .toml config or a .py script: the one
                the server file needs (extensions.python_for: its devices'
                extensions', else a virtualenv next to it or its drivers, else
                the one that ran `insthub service install`), or --python, so
                the server runs in the environment with the drivers' packages

Before writing the unit, `install` makes sure that Python can import insthub
and, for a config, every driver it names, and that each device's keys fit its
driver (config.check); what config.check warns about is passed on as a
ConfigWarning.  It refuses to run from the user-wide `insthub` command (a uv
tool or pipx environment), which has no drivers.

After a `git pull` or an edited driver, `update` brings services up to date, for
`insthub service update` and the window alike: a unit written by an older insthub
(units()' "stale") is written anew with the same file and Python, and a running
service whose unit changed or whose server runs older code (outdated) restarts.

What stays outside: anything that gives the operating system access to the
hardware (udev rules, kernel modules, groups).  That belongs to the driver.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
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
MARK = "# Written by `insthub service install`"  #: how the first line of every unit insthub writes begins
UNIT_FORMAT = 2  #: the format of the units this insthub writes (unit_text), said in their first line

#: What an older insthub wrote where this one writes something else, for _judge: {(section, key): the
#: values older unit_texts gave it, None for "not written at all"}.  Units of format 1 (which did not say
#: their format yet) restarted a crashed server 2 s later, and forever.  When unit_text changes,
#: UNIT_FORMAT gets one more and what it wrote before goes here (a setting it no longer writes too,
#: with its values), so that a unit counts as stale only for what an older insthub really wrote.
_OLDER = {("[Service]", "RestartSec"): {"2"},
          ("[Unit]", "StartLimitIntervalSec"): {None}, ("[Unit]", "StartLimitBurst"): {None}}


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


def served(argv: list[str]) -> str | None:
    """What a server's command line runs (command_for's `target`, read back): the config after
    `insthub serve` (or `python -m insthub serve`), else the script a Python runs ("python
    my_server.py settings.toml": the .py)."""
    args = argv[1:]
    if "serve" in args:
        before = [os.path.basename(a) for a in [argv[0], *args[:args.index("serve")]]]
        if "insthub" in before or "insthub.exe" in before:  # the command, `python -m insthub` or its script
            return next((a for a in args[args.index("serve") + 1:] if a.endswith(".toml")), None)
    if argv and os.path.basename(argv[0]).startswith("python"):
        while args and args[0].startswith("-") and args[0] not in ("-m", "-c"):
            args = args[2:] if args[0] in ("-X", "-W", "--check-hash-based-pycs") else args[1:]  # its options
        if args and args[0].endswith(".py"):
            return args[0]
    return None


def command_of(unit: dict) -> list[str]:
    """The command line of an installed service (units()' "command": its ExecStart=, as command_for
    made it); [] if it cannot be read.  What _quote doubled for systemd ("$$", "%%") is single again."""
    try:
        return [arg.replace("$$", "$").replace("%%", "%") for arg in shlex.split(unit.get("command") or "")]
    except ValueError:
        return []


def target(unit: dict) -> Path | None:
    """The .toml config or .py script that an installed service serves (units()' entry), if it is
    there."""
    arg = served(command_of(unit))
    path = Path(arg) if arg else None
    return path if path is not None and path.is_file() else None


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
    return f"""{MARK} (unit format {UNIT_FORMAT}).  Remove it with:  insthub service remove {name}
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


def _settings(text: str) -> dict | None:
    """What a unit file sets, as {(its section, key): value}; None if it sets one key twice."""
    found, section = {}, ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            section = line
            continue
        key, _, value = line.partition("=")
        if (section, key.strip()) in found:
            return None
        found[section, key.strip()] = value.strip()
    return found


def _differences(have: dict, want: dict, keys: list, now: str) -> str:
    """How the settings `have` differ from `want` (as _settings gives them) in `keys`, for people:
    "RestartSec=2, now 5; without StartLimitIntervalSec=300, StartLimitBurst=5" (`now`: the words
    before want's value)."""
    changed = [f"{key}={have[section, key]}, " + (f"{now} {want[section, key]}" if (section, key) in want
                                                  else "which this insthub does not write")
               for section, key in keys if (section, key) in have]
    missing = [f"{key}={want[section, key]}" for section, key in keys if (section, key) not in have]
    return "; ".join([*changed, *([f"without {', '.join(missing)}"] if missing else [])])


def _judge(unit: dict, text: str) -> dict:
    """Whether an installed service's unit (`text`, its file) is one an older insthub wrote:
    {"stale": what is old about it, for people ("RestartSec=2, now 5; without
    StartLimitIntervalSec=300, StartLimitBurst=5"), else None; "unchecked": why it was not judged
    at all, else None}.  It is compared with the unit this insthub writes (unit_text) for the same
    name, file and Python, as the unit itself says them.  Only a unit insthub wrote is judged: it
    has insthub's first line (MARK), of a format no newer than this insthub's (UNIT_FORMAT), its file
    is still there, its command is the one insthub makes of that file and Python, and each of its
    settings is today's or what an older insthub wrote instead (_OLDER).  Any other unit was written
    by hand, changed by hand (a value that no insthub wrote, a setting insthub does not write) or
    written by a newer insthub, and is left as it is, so that update never undoes what somebody
    chose, nor what a newer insthub wrote."""
    def unchecked(why: str) -> dict:
        return {"stale": None, "unchecked": why}

    if not text.startswith(MARK):
        return unchecked("insthub did not write it")
    number = re.match(rf"{re.escape(MARK)} \(unit format (\d+)\)", text)
    if number and int(number[1]) > UNIT_FORMAT:
        return unchecked(f"a newer insthub wrote it (unit format {number[1]}; this one writes {UNIT_FORMAT})")
    command, file = command_of(unit), target(unit)
    if file is None:
        arg = served(command)
        return unchecked(f"the file it serves, {arg}, is gone" if arg else "its command is not one insthub writes")
    try:
        if command_for(file, command[0]) != command:
            return unchecked("its command is not the one insthub writes for its file and Python")
        have, want = _settings(text), _settings(unit_text(unit["name"], file, command[0]))
    except (BusError, ValueError):  # (a name insthub would not give a service)
        return unchecked("its name is not one insthub gives a service")
    if have is None:
        return unchecked("it was changed by hand (it sets one key twice)")
    keys = list(dict.fromkeys([*want, *have]))
    hand = [key for key in keys if have.get(key) != want.get(key) and have.get(key) not in _OLDER.get(key, ())]
    if hand:
        return unchecked(f"it was changed by hand ({_differences(have, want, hand, 'insthub writes')})")
    old = [key for key in keys if have.get(key) != want.get(key)]
    return {"stale": _differences(have, want, old, "now") or None, "unchecked": None}


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
    `python`: the interpreter the service runs (default: the one the file needs, as `insthub
    check --import` checks it: extensions.python_for, which is this one unless the file's
    devices' extensions or a virtualenv next to it or its drivers say another).
    `login`: start it at every login (default: as `start`); False: only when started by hand."""
    from .extensions import python_for  # (here: the extensions are only looked for when installing)

    target = Path(target).expanduser().resolve()
    name = name or target.stem
    if python is None:
        python = python_for(target)
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
    """systemctl `what` ("start", "restart", or "try-restart": a restart only if it runs), then _wait_up."""
    unit = _unit(name)
    _systemctl("reset-failed", unit, check=False)  # what failed before (or gave up) starts again by hand, and
    # its counts of restarts and starts begin anew (reset-failed starts nothing)
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
    "start-limit-hit" once it gave up, ...), command, unit (the path of its file), stale (what is
    old about its unit, when an older insthub wrote it, else None) and unchecked (why its unit was
    not judged: not written by insthub, changed by hand, ...; else None): see _judge."""
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
        text = path.read_text()
        command = next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("ExecStart=")), "")
        props = shown.get(path.name, {})
        pid = int(props.get("MainPID") or 0)
        unit = {"name": path.stem[len(PREFIX):], "state": props.get("ActiveState") or "unknown",
                "sub": props.get("SubState", ""), "enabled": props.get("UnitFileState") == "enabled",
                "pid": pid or None, "restarts": int(props.get("NRestarts") or 0),
                "result": props.get("Result", ""), "command": command, "unit": str(path)}
        found.append({**unit, **_judge(unit, text)})
    return found


def restarts_text(unit: dict) -> str:
    """"restarted 3 times" (since it was started), and "gave up" when systemd did; else ""."""
    n = unit.get("restarts") or 0
    told = [f"restarted {n} time{'s' * (n != 1)}"] if n else []
    if unit.get("result") == "start-limit-hit":
        told.append("gave up: too many restarts")
    return ", ".join(told)


# ---------------------------------------------------------------- bringing services up to date
def outdated(unit: dict) -> list[str]:
    """Why the server of a running service (units()' entry) runs older code than its folders hold:
    extensions.outdated, the rule `insthub list` and the window follow, applied to the registry entry
    of the service's process (its MainPID) and to what its devices say of themselves.  None when it
    does not run, has announced no server, or when that cannot be told; a server that does not
    answer is judged by its files alone."""
    from . import extensions  # (here: extensions imports this module)
    from .client import connect, discover

    entry = next((e for e in discover() if e.get("pid") == unit.get("pid")), None) if unit.get("pid") else None
    if entry is None:
        return []
    try:
        with connect(entry["address"], timeout=5.0, connect_timeout=2.0) as c:
            devices = c.describe()
    except (BusError, OSError):
        devices = None
    return extensions.outdated(entry, devices)


def plan(unit: dict, reasons: list[str] = ()) -> list[str]:
    """What update() does for a service (units()' entry), in order: "rewrite" its unit when it is stale,
    then "restart" it when it runs and its unit was rewritten or its server runs older code
    (`reasons`: why, as outdated() says).  A service that does not run is never started.  [] for one up
    to date.  The one rule for this: the window asks it too, to offer Update and to say what will
    restart."""
    steps = ["rewrite"] if unit.get("stale") else []
    if unit.get("state") == "active" and (steps or reasons):
        steps.append("restart")
    return steps


_STEPS = {"rewrite": ("write its unit anew", "wrote its unit anew"), "restart": ("restart it", "restarted it")}


def plan_text(steps: list[str], done: bool = False) -> str:
    """Steps of plan() for people: "write its unit anew and restart it" (`done`: "wrote ... restarted it")."""
    return " and ".join(_STEPS[step][done] for step in steps)


@dataclasses.dataclass
class Update:
    """What update() found for one service, and what it did or, with `check`, would do: the record
    that `insthub service update` prints (text(); with --json, as it is) and the window shows."""

    name: str
    state: str = "unknown"  #: what systemd said of it (units()' "state")
    stale: str | None = None  #: what is old about its unit (units()' "stale")
    unchecked: str | None = None  #: why its unit was not judged, and so is left as it is (units()' "unchecked")
    outdated: list[str] = dataclasses.field(default_factory=list)  #: why its server runs older code (outdated())
    plan: list[str] = dataclasses.field(default_factory=list)  #: what it does, in order (plan()); [] if up to date
    done: list[str] = dataclasses.field(default_factory=list)  #: the steps of the plan that were done
    pid: int | None = None  #: its server's process after the restart
    error: str | None = None  #: why (the rest of) the plan was not done, or could not be
    check: bool = False  #: only looked: nothing was changed

    @property
    def found(self) -> list[str]:
        """What is old about it, a sentence each."""
        return [*([f"its unit is out of date ({self.stale})"] if self.stale else []),
                *([f"its server runs older code ({'; '.join(self.outdated)})"] if self.outdated else [])]

    @property
    def outcome(self) -> str:
        """What was done, or would be, and what went wrong; "" when there was nothing to do."""
        if self.check and self.plan:
            told = [f"would {plan_text(self.plan)}" + (f", but cannot: {self.error}" if self.error else "")]
        else:
            told = [plan_text(self.done, done=True) + (f" (pid {self.pid})" if self.pid else "")] if self.done else []
            told += [f"error: {self.error}"] if self.error else []
        if self.plan == ["rewrite"] and not self.error:
            told.append("it does not run, so it is not started; its next start uses the new unit")
        return "; ".join(told)

    def text(self) -> str:
        """For people: "NAME: what was found" ("up to date"), then the outcome on a line of its own;
        and, for a unit that was not judged, why (never "up to date": nobody looked)."""
        told = [self.outcome] if self.outcome else []
        if self.unchecked:
            told.append(f"its unit was not checked: {self.unchecked}, so it is left as it is")
        found = "; ".join(self.found)
        if not found:
            return f"{self.name}: {'; '.join(told) or 'up to date'}"
        return "\n    ".join([f"{self.name}: {found}", *told])


def update(names: list[str] | None = None, *, check: bool = False, wait: float | None = None) -> list[Update]:
    """Bring services up to date after their code or insthub changed (a `git pull`, an edited
    driver): for each service called in `names` (default: every insthub service), what plan()
    says.  A stale unit is written anew with the same file and the same Python, never another one,
    then systemd reloads it; it is written in place, so whether it starts at login stays as it was.
    A running service whose unit was written anew, or whose server runs older code (outdated), is
    restarted, waiting up to `wait` seconds as restart() does.  A service that does not run is never
    started; one up to date is left alone.  `check`: only find out, change nothing.
    Each service is looked at again just before its turn, since an earlier one's restart takes a
    while: one stopped meanwhile is not started, and its restart is systemd's try-restart, which
    never starts a service that has stopped.
    One service failing (its Python gone, a restart that does not come up) never stops the others:
    the result is a record (Update) for each, in order.  Raises BusError only without systemd."""
    known = [u["name"] for u in units()]
    records = []
    for name in dict.fromkeys(known if names is None else names):
        try:
            if name not in known:
                raise BusError(f"no service {PREFIX}{name} (see `insthub service list`)")
            unit = units(name)[0]  # (as it is now)
        except BusError as e:  # (also one removed meanwhile)
            records.append(Update(name, error=str(e), check=check))
            continue
        records.append(_update(unit, check, wait))
    return records


def _update(unit: dict, check: bool, wait: float | None) -> Update:
    record = Update(unit["name"], unit["state"], unit.get("stale"), unit.get("unchecked"), check=check)
    try:
        record.outdated = outdated(unit)
        record.plan = plan(unit, record.outdated)
        record.error = _cannot_start(unit) if record.plan else None
        if check or record.error:
            return record
        for step in record.plan:
            if step == "rewrite":
                path = Path(unit["unit"])  # (in place: the link that starts it at login stays)
                path.write_text(unit_text(unit["name"], target(unit), command_of(unit)[0]))
                _systemctl("daemon-reload")
            else:
                record.pid = _begin("try-restart", unit["name"], wait)
            record.done.append(step)
    except Exception as e:  # noqa: BLE001  (said in its record: one service never stops the others)
        record.error = str(e) or type(e).__name__
    return record


def _cannot_start(unit: dict) -> str | None:
    """Why a service could not start again, so that update leaves it as it is: the Python its unit
    runs is gone (a virtualenv deleted or made anew elsewhere), or the file it serves is; None when
    neither is known to be gone.  It never picks another Python by itself.  The command it advises
    keeps the service as it is: not started if it does not run, and starting at login or not."""
    command, name = command_of(unit), unit["name"]
    file = served(command)
    if command and shutil.which(command[0]) is None:
        gone, again = f"its Python, {command[0]}, is gone", f"{shlex.quote(file or 'FILE')} --name {name} --python PATH"
    elif file is not None and os.path.isabs(file) and not os.path.isfile(file):
        gone, again = f"its file, {file}, is gone", f"FILE --name {name} --python {shlex.quote(command[0])}"
    else:
        return None
    if unit.get("state") != "active":  # (--no-start: not at login either; enable gives that back)
        again += " --no-start" + (f", then insthub service enable {name}" if unit.get("enabled") else "")
    elif not unit.get("enabled"):
        again += " --no-login"
    return f"{gone}, so it could not start again; install it again (insthub service install {again})"


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
