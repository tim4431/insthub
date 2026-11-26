"""Command line.

    insthub list [--json]                          servers and devices on this machine, and on the other
                                                  computers devices.toml names; a server here that runs
                                                  older code than its folders hold says why
    insthub states [DEVICE ...] [--json]           what every device is like now: its state, whether it is
                                                  connected (to save next to a data file); with -a, the
                                                  devices of that server; exit code 1 if a device named,
                                                  or every device, could not be read
    insthub logs SERVER [-n N] [--level L] [-f]    what a server logged (its last records; -f: follow it),
                                                  from any computer; SERVER may also be one of its devices
    insthub info DEVICE                            its commands, properties and streams: units, limits, choices,
                                                  what is dangerous, and whether it is simulated
    insthub call DEVICE COMMAND [ARG ...] [KEY=VALUE ...] [--save FILE]
    insthub get DEVICE PROPERTY
    insthub set DEVICE PROPERTY VALUE
    insthub watch DEVICE [STREAM]                  print frames as they arrive
    insthub release DEVICE                         close the instrument session (to use the vendor's software);
                                                  the next command reopens it.  `insthub reopen DEVICE` does it now
    insthub pause DEVICE                           close it and refuse every command until `insthub resume DEVICE`
    insthub view DEVICE                            live plots of its streams (needs insthub[gui])
    insthub open DEVICE [UI]                       a UI of its extension (a window made for it), run with the
                                                  extension's Python; without UI, the one there is, or a list
    insthub run RECIPE [NAME=VALUE ...] [--dry-run] [--yes] [--timeout S] [--save FILE]
                                                  run a recipe (a function of yours, kept in a .py file) in a
                                                  process of its own, once its devices are checked; RECIPE is
                                                  its name, FILE.function, or path/to/file.py:function.
                                                  --dry-run only checks it; Ctrl-C stops it
    insthub recipes [DEVICE] [--json]              the recipes this computer knows of (those that use DEVICE)
    insthub panel DEVICE MODULE:FUNCTION [--title TITLE]
                                                  a panel, make(device, parent=None) -> QWidget, in a window
    insthub gui [--tray | --install | --uninstall] the manager window: every server and device, how each is
                                                  connected, and controls; it stays in the tray when closed.
                                                  --install adds a launcher icon
    insthub serve CONFIG.toml                      host the devices listed in a config file
    insthub sim [--name NAME] [--tcp PORT] [--gui] simulated scope, synth, camera and spectrum analyzer
    insthub bench                                  latency and throughput on this machine
    insthub mcp [--data DIR] [--timeout S]         the bus for AI agents (MCP on stdin/stdout); see docs/agents.md

    insthub ext list [--json]                      the extensions this computer knows of, and what is wrong with
                                                  them (an extension: a folder with an insthub-extension.toml)
    insthub ext add PATH | ext remove PATH         add an extension's folder to your list, or take it off
    insthub check PATH [--import]                  what is wrong with an extension (its folder or manifest), with
                                                  the extensions of a server's .toml, or with a file of recipes
                                                  (.py, read without running it); --import also imports the
                                                  drivers, in the extension's Python

    insthub service install TARGET [--name NAME] [--python PATH] [--no-login]
                                                  keep a server running in the background (systemd user service);
                                                  TARGET is a .toml config or a .py server script.  The service
                                                  runs this Python (or PATH), which must import its drivers.
                                                  It waits until the server answers, or shows why it did not.
                                                  --no-login: started only by `insthub service start NAME`
    insthub service list | start NAME | stop NAME | restart NAME | remove NAME | logs NAME [-f]
    insthub service enable NAME | disable NAME     start it at every login, or no longer
    insthub service update [NAME ...] [--check] [--json]
                                                  after a git pull or an edited driver: write units of an
                                                  older insthub anew (same file, same Python) and restart
                                                  the services that run older code; never starts one that
                                                  is stopped.  --check only says what it would do, with exit
                                                  code 1 if anything would change

Values are parsed as JSON when possible (1e3, true, [1,2], "text"), else taken
as text.  `-a ADDRESS` talks to a server by address instead of looking the
device up by name.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
import warnings
from pathlib import Path

import numpy as np


def _value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_params(items) -> tuple[list, dict]:
    """Arguments as typed after a command: KEY=VALUE are keyword arguments, the rest
    positional; each value as JSON when possible, else as text."""
    positional, keyword = [], {}
    for item in items:
        key, eq, text = item.partition("=")
        if eq and key.isidentifier():
            keyword[key] = _value(text)
        else:
            positional.append(_value(item))
    return positional, keyword


def split_arguments(text: str) -> list[str]:
    """Arguments typed on one line (in `insthub gui`): split at spaces, but not inside
    quotes or brackets, so that [1, 2] and {"a": 1} stay one argument.  Single quotes
    around a whole argument (or around what follows its `=`) are removed, as a shell
    does: 'two words'.  Raises ValueError for a quote left open."""
    words, word, depth, quote, escaped = [], "", 0, None, False
    for ch in text:
        if quote:
            word += ch
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote, word = ch, word + ch
        elif ch.isspace() and depth == 0:
            if word:
                words.append(word)
            word = ""
        else:
            depth += (ch in "[{(") - (ch in "]})")
            depth = max(depth, 0)
            word += ch
    if quote:
        raise ValueError(f"a {quote} is not closed")
    if word:
        words.append(word)

    def unquote(w: str) -> str:
        return w[1:-1] if len(w) >= 2 and w[0] == w[-1] == "'" else w

    out = []
    for w in words:
        key, eq, value = w.partition("=")
        out.append(f"{key}={unquote(value)}" if eq and key.isidentifier() else unquote(w))
    return out


def _plain(obj):
    """A JSON-friendly rendering of a result: arrays and blobs become summaries."""
    from .device import summarize

    if isinstance(obj, np.ndarray):
        if obj.size <= 8:
            return obj.tolist()
        return f"<array {summarize(obj)} min={obj.min():.6g} max={obj.max():.6g}>" if obj.dtype.kind in "iuf" \
            else f"<array {summarize(obj)}>"
    if isinstance(obj, (bytes, bytearray)):
        return f"<{len(obj)} bytes>"
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_plain(v) for v in obj]
    if isinstance(obj, complex):
        return str(obj)
    return obj


def _show(obj) -> str:
    plain = _plain(obj)
    return plain if isinstance(plain, str) else json.dumps(plain, indent=2, default=str)


def _device(args):
    import insthub

    return insthub.device(args.device, args.address)


def _cmd_list(args) -> int:
    import insthub
    from insthub.device import busy_with

    servers = [insthub.connect(args.address).info()] if args.address else insthub.discover(remote=True)
    unread = [s for s in servers if s.get("error") and not s.get("address")]  # devices.toml cannot be read
    for info in unread:
        print(f"error: {info['error']}", file=sys.stderr)
    listed = [_listing(info, args.address) for info in servers if info not in unread]
    if args.json:
        print(json.dumps(_plain(listed), indent=2, default=str))
        return 0 if listed else 1
    if not listed:
        print("no insthub server is running on this machine" + ("" if unread else ", and devices.toml names "
                                                                "none on another"))
        return 1
    for s in listed:
        if s["server"] is None:  # a server on another computer that does not answer
            print(f"{s['address']}  (on another computer)\n    unreachable: {s['error']}")
            continue
        where = " on another computer" if s["remote"] else ""
        print(f"{s['server']}  (pid {s['pid']}{where})  {'  '.join(s['addresses'])}")
        if "error" in s:
            print(f"    unreachable: {s['error']}")
            continue
        if s["outdated"]:
            print(f"    out of date: {'; '.join(s['outdated'])}; a restart loads the new code")
        for name, d in s["devices"].items():
            st = d["status"]
            state = _status_text(st)
            busy = f", busy: {busy_with(st)}" if busy_with(st) and not st.get("paused") else ""  # (pausing says it)
            print(f"    {name:16s} {d['type']:16s} {state}{busy}; {len(d['commands'])} commands, "
                  f"{len(d['properties'])} properties, streams: {', '.join(d['streams']) or '-'}")
    return 0


def _listing(info: dict, address: str | None) -> dict:
    """What `insthub list` says of one server (`info`: its discover() entry; with -a, the info() of
    the server at `address`): who it is and where; if it answers, why it runs older code
    (extensions.outdated, judged only for a server of this computer, by its registry entry) and its
    devices in brief; else "error" (and "server" None for one on another computer)."""
    import insthub
    from insthub.extensions import outdated

    s = {"server": info.get("server"), "pid": info.get("pid"), "remote": bool(info.get("remote")),
         "address": address or info.get("address"), "addresses": list(info.get("addresses") or ()),
         "version": info.get("version")}
    if info.get("error"):
        return {**s, "error": info["error"]}
    try:
        described = insthub.connect(s["address"]).describe()
    except insthub.BusError as e:
        return {**s, "error": str(e)}
    s["outdated"] = outdated(None if info.get("remote") or address else info, described)  # (with -a: no entry)
    s["devices"] = {name: {"type": d["type"], "status": d["status"], "commands": sorted(d["commands"]),
                           "properties": sorted(d["properties"]), "streams": sorted(d["streams"])}
                    for name, d in sorted(described.items())}
    return s


def _cmd_states(args) -> int:
    import insthub

    found = insthub.states(args.devices or None, address=args.address)
    unread = [name for name, d in found.items() if "error" in d]
    # 1 when a device you named, or every device, could not be read: what was saved is not what was meant
    code = 1 if not found or len(unread) == len(found) or (args.devices and unread) else 0
    if args.json:
        print(json.dumps(_plain(found), indent=2, default=str))
        return code
    for name, d in sorted(found.items()):
        if "error" in d:
            print(f"{name}" + (f"  (on {d['server']})" if d.get("server") else "") + f": {d['error']}")
            continue
        print(f"{name}  ({d['type']} on {d['server']}, {'connected' if d['connected'] else 'not connected'})")
        for key, value in d["state"].items():
            print(f"    {key}: {json.dumps(_plain(value), default=str)}")
    if not found:
        print("no device on the bus (insthub list shows the servers)", file=sys.stderr)
    return code


def _server_address(args) -> str:
    """The address of the server `args.server` names: one on this computer or another (by its
    name, as `insthub list` shows it), or the one that hosts a device of that name; or -a's.
    This computer's servers and devices first: those on other computers may take a second to
    answer."""
    import insthub

    from .errors import BusError, DeviceNotFound

    if args.address:
        return args.address
    if not args.server:
        raise BusError("name a server (insthub list shows them), or give its address before the command: "
                       "insthub -a tcp://HOST:PORT logs")
    here = insthub.discover()
    for info in here:
        if info["server"] == args.server:
            return info["address"]
    if any(args.server in info.get("devices", ()) for info in here):
        return insthub.locate(args.server)  # (which says so if two servers here have one)
    for info in insthub.discover(remote=True):
        if info.get("server") == args.server:
            return info["address"]
    try:
        return insthub.locate(args.server)  # a device elsewhere: from what those servers just said
    except DeviceNotFound:
        raise BusError(f"no server or device named {args.server!r} answers (insthub list shows them)"
                       ) from None


def _cmd_logs(args) -> int:
    import insthub

    from .client import log_text
    from .errors import ConnectionLost
    from .server import LOG_RECORDS

    client = insthub.connect(_server_address(args), timeout=10.0)
    records = client.logs(args.lines, args.level)
    print(log_text(records) or f"({client.hello.get('server')} has logged nothing at {args.level} or above)",
          flush=True)
    last, pid, gone = (records[-1]["seq"] if records else None), client.hello.get("pid"), False
    try:
        while args.follow:
            time.sleep(1.0)
            try:
                records = client.logs(LOG_RECORDS, args.level, last)
            except ConnectionLost:
                if not gone:
                    print(f"--- {client.hello.get('server')} does not answer; waiting for it", flush=True)
                gone = True
                continue
            if client.hello.get("pid") != pid:  # started again: its records are numbered from 1 again
                pid, gone, last = client.hello.get("pid"), False, None
                print(f"--- {client.hello.get('server')} started again", flush=True)
                records = client.logs(LOG_RECORDS, args.level)
            if records:
                print(log_text(records), flush=True)
                last = records[-1]["seq"]
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_info(args) -> int:
    dev = _device(args)
    print(dev.bus.help())
    st = dev.bus.status()
    print(f"\nstatus: {_status_text(st)}, {st['calls']} calls, {st['errors']} errors, "
          f"{st['subscribers']} subscribers")
    return 0


def _cmd_call(args) -> int:
    dev = _device(args)
    positional, keyword = parse_params(args.params)
    start = time.perf_counter()
    result = dev.bus.call(args.command, *positional, **keyword)
    elapsed = time.perf_counter() - start
    code = _print_result(result, args.save)
    print(f"({elapsed * 1e3:.2f} ms)", file=sys.stderr)
    return code


def _print_result(result, save: str | None = None) -> int:
    """Print what a command or a recipe returned (nothing for None), arrays and bytes in brief; or,
    with `save`, write it to that file (see _save).  Returns the exit code: 1 if it could not be
    saved, and then it is printed instead, so that it is not lost."""
    if save:
        try:
            saved = _save(result, Path(save).expanduser())
        except Exception as e:  # noqa: BLE001 - whatever it is, what ran must not be lost
            print(_show(result))
            print(f"error: cannot save it to {save}: {type(e).__name__}: {e} (it is printed above)", file=sys.stderr)
            return 1
        print(f"saved to {saved}")
    elif result is not None:
        print(_show(result))
    return 0


def _save(result, path: Path) -> str:
    """Write a result to a file, and say where: bytes as they are; an array as .npy; a dict or a
    dataclass as .npz, an array per key (as np.savez(**it) would, whatever the keys); a list or a
    tuple that holds arrays as .npz, an array per item (arr_0, arr_1, ..., as np.savez(*it)
    would); anything else as text.  numpy adds .npy or .npz to a name that has not; a value
    that is no plain array (None, a dict) is pickled, so np.load(file, allow_pickle=True) reads it."""
    if isinstance(result, (bytes, bytearray)):
        path.write_bytes(result)
        return str(path)
    if isinstance(result, np.ndarray):
        path = path if path.suffix == ".npy" else path.with_name(path.name + ".npy")
        np.save(path, result)
        return str(path)
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        items = [(f.name, getattr(result, f.name)) for f in dataclasses.fields(result)]
    elif isinstance(result, dict):
        items = [(str(k), v) for k, v in result.items()]
    elif isinstance(result, (list, tuple)) and any(isinstance(v, np.ndarray) for v in result):
        items = [(f"arr_{i}", v) for i, v in enumerate(result)]
    else:
        path.write_text(_show(result) + "\n")
        return str(path)
    names = [name for name, _ in items]
    twice = next((name for name in names if names.count(name) > 1), None)
    if twice is not None:
        raise ValueError(f"two of its keys are both {twice!r} as text")
    path = path if path.suffix == ".npz" else path.with_name(path.name + ".npz")
    _savez(path, items)
    return f"{path} ({', '.join(names)})" if names else str(path)


def _savez(path: Path, items: list) -> None:
    """np.savez, without keyword arguments: no key ("file", "allow_pickle", 1) can clash with its own."""
    import zipfile

    def array(value):
        try:
            return np.asanyarray(value)
        except ValueError:  # arrays of several lengths: kept as they are
            return np.array(value, dtype=object)

    z = zipfile.ZipFile(path, "w", zipfile.ZIP_STORED, allowZip64=True)
    try:
        with z:
            for name, value in items:
                with z.open(f"{name}.npy", "w", force_zip64=True) as f:
                    np.lib.format.write_array(f, array(value), allow_pickle=True)
    except BaseException:
        try:
            path.unlink()  # (not half a file)
        except OSError:
            pass
        raise


def _cmd_get(args) -> int:
    print(_show(_device(args).bus.get(args.property)))
    return 0


def _cmd_set(args) -> int:
    _device(args).bus.set(args.property, _value(args.value))
    return 0


def _cmd_watch(args) -> int:
    from .device import summarize

    dev = _device(args)
    streams = [args.stream] if args.stream else sorted(dev.bus.describe()["streams"])
    if not streams:
        print(f"{args.device} has not published anything yet; name a stream to wait for it", file=sys.stderr)
        return 1

    import threading

    stop = threading.Event()

    def show(frame):
        if stop.is_set():
            return
        age = frame.age * 1e3
        value = frame.data if not isinstance(frame.data, (np.ndarray, bytes, bytearray)) else summarize(frame.data)
        if not isinstance(value, str):
            value = json.dumps(_plain(value), default=str)
        if len(value) > 100:
            value = value[:97] + "..."
        try:
            print(f"{frame.stream:12s} #{frame.seq:<7d} {age:7.1f} ms old  {value}"
                  + (f"  {frame.meta}" if frame.meta else ""), flush=True)
        except BrokenPipeError:  # e.g. `insthub watch dev | head`
            stop.set()

    subs = [dev.bus.subscribe(s, show) for s in streams]
    try:
        while not stop.wait(0.2):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        for sub in subs:
            sub.close()
            print(f"{sub.stream}: {sub.received} frames received, {sub.dropped} skipped", file=sys.stderr)
    if stop.is_set():  # stdout is gone: keep the interpreter from complaining about it at exit
        import os

        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


def _status_text(st: dict) -> str:
    from .device import busy_with

    if st.get("paused"):
        by = f" by {st['paused']['by']}" if st["paused"].get("by") else ""
        if st.get("busy") == "$pause":
            return f"pausing{by}: its session is being closed"
        what = busy_with(st)
        if what or st["connected"]:  # the pause itself closes the session: until then, what runs finishes
            return f"pausing{by}: closed once {what or 'what it is doing'} finishes"
        return f"paused{by} (insthub resume to end it)"
    if st["connected"]:
        return "connected"
    if st["error"] is None:
        return "not opened yet (it opens on first use)"
    if st["error"] == "released":
        return "released (the next command opens it again)"
    return f"NOT CONNECTED ({st['error']})"


def _cmd_release(args) -> int:
    from .client import PROCESS_TOKEN
    from .device import busy_with

    bus = _device(args).bus
    doing = {"pause": "pausing", "resume": "resuming"}.get(args.cmd)
    st = bus.status() if doing else {}
    if args.cmd == "pause" and "paused" not in st:
        print(f"{args.device}: its server runs an older insthub, which cannot pause a device (update insthub "
              "there, then restart it)", file=sys.stderr)
        return 1
    if args.cmd == "resume" and not st.get("paused"):
        print(f"{args.device}: not paused ({_status_text(st)})")
        return 0
    busy = busy_with(st) if st.get("busy") != "$pause" else None
    if args.cmd == "resume" and not busy:  # paused and closed: the wait is for its own reopening
        waits, then = "resumed; opening the instrument again...", "resumed; the instrument is still being opened"
    else:
        what = busy or "what it is doing"
        waits = f"{doing}; waiting for {what} to finish..."
        then = (f"paused (refusing every call already), and closed once {what} finishes" if args.cmd == "pause" else
                f"resumed once {what} finishes")
    request = {"release": ("$release", ()), "reopen": ("$open", ()), "resume": ("$resume", ()),
               "pause": ("$pause", (f"insthub {args.cmd}", PROCESS_TOKEN))}[args.cmd]
    reply = bus._t.request(bus.name, *request, {})
    waiting = False
    try:
        try:  # the pause (or the resume) takes effect once what runs now has finished: say so if it takes long
            status = reply.result(0.5 if doing else bus.timeout)
        except TimeoutError:
            if not doing:
                raise
            waiting = True
            print(f"{args.device}: {waits}", flush=True)
            status = reply.result(bus.timeout)
    except KeyboardInterrupt:
        if not doing:
            raise
        # the request has reached the server: it is carried out all the same
        print(("\n" if waiting else "") + f"{args.device}: stopped waiting; it is {then}", file=sys.stderr)
        return 130
    if status.get("paused"):
        print(f"{args.device}: paused; its commands fail until  insthub resume {args.device}")
    else:
        print(f"{args.device}: " + ("connected" if status["connected"] else
                                    "not connected yet (it opens on first use)" if status["error"] is None else
                                    f"not connected ({status['error']})"))
    return 0


def qt_hint(what: str) -> str:
    """What to run to give the Python of this insthub the Qt that `what` ("insthub gui") needs."""
    import importlib.util
    import shutil

    from .service import tool_environment

    source = Path(__file__).resolve().parent.parent
    kind = tool_environment()
    if kind == "uv tool":  # the user-wide command: reinstall it with the extra
        spec = f'--editable "{source}[gui,mcp]"' if (source / "pyproject.toml").is_file() else \
            '"insthub[gui,mcp] @ git+https://github.com/tim4431/insthub"'
        fix = f"uv tool install --force {spec}"
    elif kind == "pipx":
        fix = "pipx inject insthub pyqtgraph PyQt6"
    elif importlib.util.find_spec("pip") is None and shutil.which("uv"):  # a virtualenv made by uv has no pip
        fix = f"uv pip install --python {sys.executable} pyqtgraph PyQt6"
    else:
        fix = f"{sys.executable} -m pip install pyqtgraph PyQt6"
    return f"{what} needs Qt, which the Python of this insthub ({sys.executable}) does not have. Add it with:\n" \
           f"    {fix}"


def _gui_module(name: str, what: str):
    """Import insthub.NAME (the viewer or the manager window), or say how to get the Qt it needs."""
    import importlib

    from .errors import BusError

    try:
        return importlib.import_module(f"{__package__}.{name}")
    except ImportError as e:
        if "Qt binding" in str(e) or e.name == "pyqtgraph":
            raise BusError(qt_hint(what)) from None
        raise


def _cmd_view(args) -> int:
    return _gui_module("viewer", "insthub view").main(_device(args))


_STARTUP = 3.0  #: seconds `insthub open` waits to see whether the UI started (one that cannot, ends sooner)


def _cmd_open(args) -> int:
    import insthub

    from . import extensions

    address = args.address or insthub.locate(args.device)
    described = insthub.device(args.device, address).bus.describe()
    ext, uis, why = extensions.device_uis(described, address)
    if why is None and not uis:
        why = ("it has no UI of its own" if ext is None else f"its extension, {ext.title}, has no UI for it") + \
            f" (insthub view {args.device} plots its streams)"
    chosen = [ui for ui in uis if args.ui in (None, ui.name)]
    if why is None and not chosen:
        why = f"no UI named {args.ui!r}; it has {', '.join(ui.name for ui in uis)}"
    if why:
        print(f"error: {args.device}: {why}", file=sys.stderr)
        return 1
    if len(chosen) > 1:
        print(f"error: {args.device} has {len(chosen)} UIs: name one, as  insthub open {args.device} NAME", file=sys.stderr)
        for ui in chosen:
            print(f"    {ui.name:16s} {ui.title} ({ui.kind})", file=sys.stderr)
        return 1
    launched = extensions.launch(ext, chosen[0].name, args.device, address)
    failure = launched.failure(wait=_STARTUP)
    if failure:
        print(f"error: {launched.title} ended with an error:\n{failure}\n(its log: {launched.log})", file=sys.stderr)
        return 1
    print(f"opened {launched.title} (pid {launched.pid}; its output goes to {launched.log})")
    return 0


def _cmd_panel(args) -> int:
    from .config import load_object

    try:
        make = load_object(args.panel)  # before insthub.qt: then the panel's own Qt binding is the one used
    except (ImportError, AttributeError, ValueError) as e:
        print(f"error: cannot load the panel {args.panel!r}: {e} (this is {sys.executable}; does it have the "
              "libraries of the extension's UI, such as its gui extra?)", file=sys.stderr)
        return 1
    qt = _gui_module("qt", "insthub panel")
    window = qt.device_window(make, _device(args), args.title)
    window.show()
    return qt.run()


def _cmd_gui(args) -> int:
    from . import launcher

    if args.install:
        for path in launcher.install(desktop=not args.no_desktop):
            print(f"wrote {path}")
        print("insthub is now in the applications menu; to keep it in the dock, right-click its icon there "
              "and pin it")
        return 0
    if args.uninstall:
        removed = launcher.uninstall()
        print("\n".join(f"removed {path}" for path in removed) or "no launcher was installed")
        return 0
    return _gui_module("manager", "insthub gui").main(tray_only=args.tray)


def _cmd_serve(args) -> int:
    from . import Server, config
    from .server import keep_logs

    try:
        kwargs, devices = config.load(args.config)
    except (config.ConfigError, ImportError) as e:  # what is wrong with the file, or with this Python for it
        print(f"error: {e}", file=sys.stderr)  # (a driver's own error, a DriverError, keeps its traceback)
        return 1
    if args.tcp is not None:
        kwargs["tcp"] = args.tcp
    keep_logs(everything=True)  # (once loaded: a problem of the file is printed as it is, not logged)
    server = Server(kwargs.pop("name"), devices, **kwargs)
    try:
        server.start()
    except OSError as e:  # a server of that name runs already, its TCP port is taken: said, without a traceback
        print(f"error: {e}", file=sys.stderr)
        return 1
    server.serve_forever()  # (started: this only waits, until Ctrl-C)
    return 0


def _cmd_sim(args) -> int:
    from . import Server, sim
    from .server import keep_logs

    keep_logs(everything=True)
    viewer = _gui_module("viewer", "insthub sim --gui") if args.gui else None  # before a server is made
    server = Server(args.name, [*sim.devices(), sim.Loopback()], tcp=args.tcp)
    if viewer is None:
        server.serve_forever()
        return 0
    with server:
        return viewer.main(*(server.device(name) for name in ("scope", "camera", "synth")))


def _cmd_bench(args) -> int:
    from . import bench

    return bench.main(args.address, quick=args.quick)


def _cmd_mcp(args) -> int:
    from . import mcp

    return mcp.main(args.data, args.timeout)


def _cmd_service(args) -> int:
    from . import service

    if args.action == "install":
        login = not (args.no_start or args.no_login)
        path = service.install(args.target, args.name, start=not args.no_start, python=args.python, login=login)
        name = path.stem[len(service.PREFIX):]
        print(f"installed {path}")
        if not args.no_start:  # (install waited until its server answered, or said why it did not)
            unit = next((u for u in service.units() if u["name"] == name), {})
            print(f"state: {unit.get('state', 'unknown')}" + (f", its server is up (pid {unit['pid']})"
                                                             if unit.get("pid") else ""))
        print(f"restart after changing its code or config:  insthub service restart {name}")
        if login:
            hint = service.linger_hint()
            if hint:
                print(f"it starts when you log in; to start it at boot instead, run once:  {hint}")
        else:
            print(f"it does not start at login: start it with  insthub service start {name}"
                  f"  (or from now on at every login:  insthub service enable {name})")
        return 0
    if args.action == "remove":
        service.remove(args.name)
        print(f"removed {service.PREFIX}{args.name}")
        return 0
    if args.action == "logs":
        return service.logs(args.name, follow=args.follow, lines=args.lines)
    if args.action in ("start", "stop", "restart"):
        getattr(service, args.action)(args.name)
        state = next((u["state"] for u in service.units() if u["name"] == args.name), "unknown")
        print(f"{service.PREFIX}{args.name}: {state}")
        return 0
    if args.action in ("enable", "disable"):
        getattr(service, args.action)(args.name)
        print(f"{service.PREFIX}{args.name}: " + ("starts at every login" if args.action == "enable" else
                                                  "no longer starts at login (start it by hand)"))
        return 0
    if args.action == "update":
        records = service.update(args.names or None, check=args.check)
        if args.json:
            print(json.dumps([dataclasses.asdict(r) for r in records], indent=2))
        else:
            print("\n".join(r.text() for r in records) or "no insthub services installed")
        if args.check:  # for scripts: 1 when anything would change (or is wrong)
            return 1 if any(r.plan or r.error for r in records) else 0
        return 1 if any(r.error for r in records) else 0
    units = service.units()
    if not units:
        print("no insthub services installed (insthub service install TARGET)")
    old = False
    for unit in units:
        boot = "at login" if unit["enabled"] else "by hand"
        print(f"{unit['name']:16s} {unit['state']:10s} {boot:9s} {unit['command']}")
        told = service.restarts_text(unit)
        found = service.Update(unit["name"], stale=unit.get("stale"), outdated=service.outdated(unit)).found
        for line in ([told] if told else []) + found:
            print(f"{'':16s} {line}")
        old = old or bool(found)
    if old:
        print("insthub service update brings them up to date (--check: what it would do)")
    return 0


def _cmd_ext(args) -> int:
    from . import extensions

    try:
        if args.action == "add":
            ext = extensions.add_folder(args.path)
            print(f"added {ext.folder}" + (f": {ext.id} ({ext.title})" if ext.id else ""))
            if ext.error:
                print(f"warning: it cannot be used yet: {ext.error}", file=sys.stderr)
            return 0
        if args.action == "remove":
            print(f"removed {', '.join(extensions.remove_folder(args.path))}")
            return 0
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    found = extensions.discover()
    if args.json:
        print(json.dumps([{**dataclasses.asdict(ext), "folder": ext.folder} for ext in found], indent=2, default=str))
        return 0
    for ext in found:
        print(f"{ext.id or '?'} {ext.version}".rstrip() + f"  {ext.title}  ({ext.source})")
        uis = [f"{ui.name} ({ui.kind}{', for ' + ', '.join(ui.types) if ui.types else ''})"
               for ui in ext.ui.values()]
        for what, text in (("folder", str(ext.folder)), ("devices", ", ".join(ext.devices)), ("ui", ", ".join(uis)),
                           ("recipes", ", ".join(_recipe_file_text(path, ext.folder) for path in ext.recipes)),
                           ("error", ext.error),
                           ("hidden", ext.hidden_by and f"by {ext.hidden_by}")):
            if text:
                print(f"    {what:8s} {text}")
    return 0


def _recipe_file_text(path: Path, folder: Path) -> str:
    """A file of an extension's recipes, as `insthub ext list` shows it: "recipes/lab.py (setup, scan)"."""
    from . import recipes

    shown = path.relative_to(folder) if path.is_relative_to(folder) else path
    found, problems = recipes.read_file(path)
    if problems:
        return f"{shown} (cannot be read: insthub check {path} says why)"
    return f"{shown} ({', '.join(r.name + (' (cannot be used)' if r.error else '') for r in found) or 'no recipe'})"


def _strict_json(value) -> bool:
    """Whether strict JSON holds `value` as it is (not inf or nan, no key that is not text, ...)."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _as_json(value):
    """`value` as strict JSON can hold it: as it is when it can; else dicts with their keys as text,
    lists for tuples, and the text of anything else (inf, nan, a set, bytes)."""
    if _strict_json(value):
        return value
    if isinstance(value, dict):
        return {str(k): _as_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_json(v) for v in value]
    return repr(value) if isinstance(value, float) else str(value)


def _param_json(param) -> dict:
    """A recipe's parameter in `insthub recipes --json`: its default if the file writes it as a literal
    that JSON can hold (inf, a set or a dict with keys that are not text it cannot), and as the file
    writes it."""
    out = {}
    if not param.required:
        if param.literal and _strict_json(param.default):
            out["default"] = param.default
        out["default_text"] = param.default_text
    return {**out, "required": param.required, "annotation": param.annotation, "doc": param.doc,
            "meta": _as_json(param.meta)}


def _cmd_recipes(args) -> int:
    from . import recipes

    found, problems = recipes.available()
    listed = [r for r in found if args.device in r.devices] if args.device else found
    if args.json:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        print(json.dumps(_as_json([{"name": r.name, "full_name": r.full_name, "title": r.title, "doc": r.doc,
                                    "file": str(r.file), "line": r.line, "origin": r.origin,
                                    "devices": list(r.devices),
                                    "params": {p.name: _param_json(p) for p in r.params.values()},
                                    "dangerous": r.dangerous, "error": r.error} for r in listed]),
                         indent=2, allow_nan=False))
        return 0
    if not listed:
        print((f"no recipe uses {args.device}" if args.device else "no recipes") +
              f": write one in {recipes.folder()} (a function in a .py file; see docs/guide.md#recipes), or run a "
              "command on a device's page in insthub gui, then Save as recipe")
    file = None
    for r in listed:
        if r.file != file:
            file = r.file
            print(f"{r.where()}: {r.file}")
        title = " ".join(filter(None, [r.title if r.title != r.name else "", r.dangerous and "(marked dangerous)"]))
        # (what to type to run it: its name; FILE.function when another has its name; its path when
        # another has its FILE.function too and is the one that name runs)
        print(f"    {recipes.runs_as(r, found):16s} {title}".rstrip())
        devices = ", ".join(r.devices) + (" and one named only when it runs" if r.named_later else "")
        for what, text in (("params", ", ".join(p.text() for p in r.params.values())), ("devices", devices),
                           ("error", r.error)):
            if text:
                print(f"        {what:8s} {text}")
    if problems:
        print("not listed:")
        for problem in problems:
            print(f"    {problem}")
    return 0


_STOP_GRACE = 3.0  #: seconds a recipe that Ctrl-C reached has to end by itself before `insthub run` stops it


def _cmd_run(args) -> int:
    from . import recipes

    recipe = recipes.find(args.recipe)
    positional, values = parse_params(args.values)
    if positional:
        print(f"error: give each value with its parameter's name, as NAME=VALUE (recipe {recipe.full_name} takes "
              f"{', '.join(recipe.params) or 'none'})", file=sys.stderr)
        return 2
    recipes.check_values(recipe, values)
    checked = recipes.check(recipe)
    name = recipe.full_name
    title = f": {recipe.title}" if recipe.title != recipe.name else ""
    simulated = f" (simulated: {', '.join(checked.simulated)})" if checked.simulated else ""
    print(f"{name}{title} ({recipe.where()}){simulated}", flush=True)
    if checked.problems:
        for problem in checked.problems:
            print(f"error: {problem}", file=sys.stderr)
        print("nothing ran", file=sys.stderr)
        return 1
    if args.dry_run:
        _print_check(recipe, values, checked)
        print("(checked; nothing ran)")
        return 0
    if checked.dangers and not args.yes:
        what = "; ".join(checked.dangers)
        if not _can_ask():
            print(f"error: it uses what is marked dangerous ({what}): add --yes to run it", file=sys.stderr)
            return 1
        # (on stderr: stdout is what the recipe prints and returns, perhaps into a file)
        print(f"It uses what is marked dangerous ({what}). Run it? [y/N] ", end="", file=sys.stderr, flush=True)
        try:
            answer = input()
        except EOFError:  # (Ctrl-D: no)
            answer = ""
            print(file=sys.stderr)
        if answer.strip().lower() not in ("y", "yes"):
            print("nothing ran", file=sys.stderr)
            return 1
    run = recipes.start(recipe, values, capture=False)  # (what it prints, and its traceback, go to this terminal)
    why = None
    try:
        try:
            result = run.wait(args.timeout)
        except TimeoutError:
            why = "--timeout"
            print(f"stopping {name}: it is still running after {args.timeout:g} s (--timeout)", file=sys.stderr,
                  flush=True)
            result = _stop(run, 0.0)
        except KeyboardInterrupt:
            why = "Ctrl-C"
            print(f"\nstopping {name} (Ctrl-C)", file=sys.stderr, flush=True)
            result = _stop(run, _STOP_GRACE)
    except recipes.RecipeError as e:
        if why == "Ctrl-C":
            print(f"{name} was stopped before it finished", file=sys.stderr)
            return 130
        print(f"error: {name} was stopped before it finished" if why else f"error: {e}", file=sys.stderr)
        return 1
    code = _print_result(result, args.save)  # (one stopped that returned all the same: what it returned)
    print(f"({run.elapsed:.2f} s" + (f"; it returned this once {why} stopped it)" if why else ")"), file=sys.stderr)
    return code


def _can_ask() -> bool:
    """Whether `insthub run` can ask its question: someone at a terminal reads stderr and types on stdin."""
    return all(stream is not None and stream.isatty() for stream in (sys.stdin, sys.stderr))


def _stop(run, grace: float):
    """End a recipe that `insthub run` started, and return what run.wait() returns: what the recipe
    returned, if it did (one that catches KeyboardInterrupt can); else it raises RecipeError, as the
    recipe was stopped.  The recipe first has `grace` seconds to end by itself:
    Ctrl-C in this terminal reaches it too, as KeyboardInterrupt, and its finally blocks are running,
    which run.stop() would interrupt.  Ctrl-C while this waits stops it at once."""
    try:
        return run.wait(grace)
    except (TimeoutError, KeyboardInterrupt):
        pass
    run.stop()  # (killed if it is still running 3 s later)
    while True:
        try:
            return run.wait()
        except KeyboardInterrupt:
            run.stop()


def _print_check(recipe, values: dict, checked) -> None:
    """What `insthub run --dry-run` shows: where the recipe is, the values it would run with, the
    devices it uses and what of them (with the line), what of it is dangerous, and what check() could
    not see."""
    given = [f"{k}={json.dumps(v, default=str)}" for k, v in values.items()]
    kept = [f"{p.name}={p.default_text}" for p in recipe.params.values() if p.name not in values]
    devices = ", ".join(recipe.devices) or "none that insthub sees in its code"
    rows = [("file", f"{recipe.file}:{recipe.line}"), ("values", ", ".join(given + kept)),
            ("devices", devices + (" (and one named only when it runs)" if recipe.named_later else ""))]
    rows += [("uses" if i == 0 else "", f"{use.text()}  ({use.where()})") for i, use in enumerate(recipe.uses)]
    rows += [("dangerous", danger) for danger in checked.dangers] + [("note", note) for note in checked.notes]
    for what, text in rows:
        if text:
            print(f"  {what:9s} {text}")


def _imports_code(path: Path) -> bool:
    """Whether `insthub check PATH --import` would import code: a server's .toml, or an extension that
    adds devices or UIs (not a file of recipes, nor an extension of recipes only)."""
    from . import extensions

    if path.suffix == ".py":
        return False
    if path.suffix == ".toml" and path.name != extensions.MANIFEST:
        return True
    manifest = extensions.find_manifest(path) if path.is_dir() else path
    if manifest is None:
        return False
    ext = extensions.load(manifest)
    return bool(ext.devices or ext.ui)


def _cmd_check(args) -> int:
    from . import extensions, recipes

    path = Path(args.path).expanduser()
    problems = extensions.check(path, import_=args.import_)
    if problems:
        print("\n".join(problems))
        return 1
    if path.suffix == ".py":
        found = ", ".join(r.name for r in recipes.read_file(path)[0])
        print(f"{args.path}: no problems found" + (f"; its recipes: {found}" if found else ""))
    else:
        hint = not args.import_ and _imports_code(path)
        print(f"{args.path}: no problems found" + (" (--import also imports the drivers and the UIs)" if hint else ""))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="insthub", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-a", "--address", help="server address (tcp://host:port, ipc:///path); default: find by name")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="servers and devices, here and on the computers devices.toml names")
    s.add_argument("--json", action="store_true", help="as JSON, with why each server here is out of date")
    s.set_defaults(fn=_cmd_list)
    s = sub.add_parser("states", help="what every device is like now (to save next to a data file)")
    s.add_argument("devices", nargs="*", metavar="DEVICE",
                   help="only these (default: every device, or with -a every device of that server)")
    s.add_argument("--json", action="store_true", help="as JSON")
    s.set_defaults(fn=_cmd_states)
    s = sub.add_parser("logs", help="what a server logged, from any computer")
    s.add_argument("server", nargs="?", help="a server's name, or one of its devices' (or -a ADDRESS)")
    s.add_argument("-n", "--lines", type=int, default=50, help="how many records (default 50)")
    s.add_argument("--level", default="INFO", help="DEBUG, INFO (default), WARNING, ERROR or CRITICAL")
    s.add_argument("-f", "--follow", action="store_true", help="go on showing what it logs, until Ctrl-C")
    s.set_defaults(fn=_cmd_logs)
    s = sub.add_parser("info", help="commands, properties and streams of a device, with units, limits, "
                                    "choices and what is dangerous")
    s.add_argument("device")
    s.set_defaults(fn=_cmd_info)
    s = sub.add_parser("call", help="call a command: insthub call scope measure VPP channel=2")
    s.add_argument("device")
    s.add_argument("command")
    s.add_argument("params", nargs="*", help="positional values and key=value pairs")
    s.add_argument("--save", metavar="FILE", help="write the result to a file (bytes raw, an array as .npy; a dict, "
                                                  "or a tuple of arrays, as .npz; else as text)")
    s.set_defaults(fn=_cmd_call)
    s = sub.add_parser("get", help="read a property")
    s.add_argument("device")
    s.add_argument("property")
    s.set_defaults(fn=_cmd_get)
    s = sub.add_parser("set", help="write a property")
    s.add_argument("device")
    s.add_argument("property")
    s.add_argument("value")
    s.set_defaults(fn=_cmd_set)
    s = sub.add_parser("watch", help="print the frames of a stream (default: every stream) until Ctrl-C")
    s.add_argument("device")
    s.add_argument("stream", nargs="?")
    s.set_defaults(fn=_cmd_watch)
    s = sub.add_parser("release", help="close the instrument session; the next command reopens it")
    s.add_argument("device")
    s.set_defaults(fn=_cmd_release)
    s = sub.add_parser("reopen", help="open the instrument session now")
    s.add_argument("device")
    s.set_defaults(fn=_cmd_release)
    s = sub.add_parser("pause", help="close the instrument session and refuse every command until resume")
    s.add_argument("device")
    s.set_defaults(fn=_cmd_release)
    s = sub.add_parser("resume", help="end a pause (reconnecting if it was connected before)")
    s.add_argument("device")
    s.set_defaults(fn=_cmd_release)
    s = sub.add_parser("view", help="live plots of a device's streams")
    s.add_argument("device")
    s.set_defaults(fn=_cmd_view)
    s = sub.add_parser("open", help="open a UI of a device's extension (a window made for it), with the "
                                    "extension's Python")
    s.add_argument("device")
    s.add_argument("ui", nargs="?", help="which one, when there are several (without it, they are listed)")
    s.set_defaults(fn=_cmd_open)
    s = sub.add_parser("run", help="run a recipe, a function of yours kept in a .py file, in a process of its own: "
                                   "insthub run square freq=2e3")
    s.add_argument("recipe", help="its name, FILE.function (as insthub recipes lists it), or path/to/file.py:function")
    s.add_argument("values", nargs="*", metavar="NAME=VALUE", help="its parameters; the others keep their defaults")
    s.add_argument("-n", "--dry-run", action="store_true",
                   help="check its devices and show what it uses of them; run nothing")
    s.add_argument("-y", "--yes", action="store_true", help="run it without asking when it uses what is marked "
                                                            "dangerous")
    s.add_argument("--timeout", type=float, metavar="S", help="stop it if it is still running after S seconds")
    s.add_argument("--save", metavar="FILE", help="write what it returns to a file (bytes raw, an array as .npy; a "
                                                  "dict, or a tuple of arrays, as .npz; else as text)")
    s.set_defaults(fn=_cmd_run)
    s = sub.add_parser("recipes", help="the recipes this computer knows of; with DEVICE, those that use it")
    s.add_argument("device", nargs="?")
    s.add_argument("--json", action="store_true", help="as JSON")
    s.set_defaults(fn=_cmd_recipes)
    s = sub.add_parser("panel", help="show a panel for a device in a window of its own (insthub open runs this)")
    s.add_argument("device")
    s.add_argument("panel", metavar="MODULE:FUNCTION", help="the panel: make(device, parent=None) -> QWidget")
    s.add_argument("--title", help="the window's title, before the device's name")
    s.set_defaults(fn=_cmd_panel)
    s = sub.add_parser("gui", help="the manager window: servers, devices and how each is connected")
    s.add_argument("--install", action="store_true", help="add insthub to the applications menu and the desktop")
    s.add_argument("--no-desktop", action="store_true", help="with --install: no icon on the desktop")
    s.add_argument("--uninstall", action="store_true", help="remove what --install added")
    s.add_argument("--tray", action="store_true", help="start in the tray, without opening the window")
    s.set_defaults(fn=_cmd_gui)
    s = sub.add_parser("serve", help="host the devices of a config file")
    s.add_argument("config")
    s.add_argument("--tcp", help="also listen on this port or host:port")
    s.set_defaults(fn=_cmd_serve)
    s = sub.add_parser("sim", help="serve simulated instruments (scope, synth, camera, loopback)")
    s.add_argument("--name", default="sim")
    s.add_argument("--tcp", help="also listen on this port or host:port")
    s.add_argument("--gui", action="store_true", help="open the viewer in the server process")
    s.set_defaults(fn=_cmd_sim)
    s = sub.add_parser("bench", help="measure call latency and data throughput")
    s.add_argument("--quick", action="store_true", help="shorter run")
    s.set_defaults(fn=_cmd_bench)
    s = sub.add_parser("mcp", help="serve the bus to AI agents over MCP (stdin/stdout); see docs/agents.md")
    s.add_argument("--data", metavar="DIR",
                   help="where results too large to return inline are saved (default: $INSTHUB_DATA or ./.insthub)")
    s.add_argument("--timeout", type=float, default=60.0, metavar="S",
                   help="seconds a call may take unless the agent asks for longer (default 60)")
    s.set_defaults(fn=_cmd_mcp)
    s = sub.add_parser("ext", help="extensions: folders that add drivers and UIs for their instruments")
    s.set_defaults(fn=_cmd_ext)
    actions = s.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="the extensions this computer knows of, and what is wrong with them"
                       ).add_argument("--json", action="store_true", help="as JSON")
    actions.add_parser("add", help="add an extension's folder to your list").add_argument("path")
    actions.add_parser("remove", help="take a folder off your list").add_argument("path")
    s = sub.add_parser("check", help="what is wrong with an extension, with the extensions of a server file, or "
                                     "with a file of recipes")
    s.add_argument("path", help="an extension's folder, its insthub-extension.toml, a server's .toml, or a .py "
                                "file of recipes")
    s.add_argument("--import", dest="import_", action="store_true",
                   help="also import the drivers and the UIs' modules, in the extension's Python (for a "
                        "server file: the checks of `insthub service install`, in the Python that serves it)")
    s.set_defaults(fn=_cmd_check)
    s = sub.add_parser("service", help="keep servers running in the background (systemd user services)")
    s.set_defaults(fn=_cmd_service)
    actions = s.add_subparsers(dest="action", required=True)
    a = actions.add_parser("install", help="register a server and start it; again to replace it")
    a.add_argument("target", help="a .toml config (run with `insthub serve`) or a .py server script")
    a.add_argument("--name", help="service name (default: the file name without extension)")
    a.add_argument("--no-start", action="store_true", help="only write the unit file (neither start it nor "
                                                               "start it at login)")
    a.add_argument("--no-login", action="store_true", help="start it now, but not at every login "
                                                           "(later: insthub service start NAME)")
    a.add_argument("--python", metavar="PATH",
                   help="the Python the service runs (default: that of the devices' extensions, else a "
                        "virtualenv next to the file or its drivers, else the one running this command); it "
                        "needs insthub and the drivers' packages")
    actions.add_parser("list", help="installed services and their state, and what is out of date")
    a = actions.add_parser("update", help="bring services up to date after a git pull or an edited driver: "
                                          "write old units anew, restart what runs older code")
    a.add_argument("names", nargs="*", metavar="NAME", help="only these services (default: every one)")
    a.add_argument("--check", action="store_true", help="only say what it would do; exit code 1 if anything "
                                                        "would change")
    a.add_argument("--json", action="store_true", help="as JSON: a record per service")
    for action, text in (("start", "start a service"),
                         ("stop", ("stop a service (until `start`, or the next start of your services if it "
                                   "starts at login)")),
                         ("restart", "restart a service, e.g. after changing its code or config"),
                         ("enable", "start a service at every login from now on"),
                         ("disable", "no longer start a service at login")):
        actions.add_parser(action, help=text).add_argument("name")
    a = actions.add_parser("remove", help="stop a service and delete it")
    a.add_argument("name")
    a = actions.add_parser("logs", help="show a service's output")
    a.add_argument("name")
    a.add_argument("-f", "--follow", action="store_true")
    a.add_argument("-n", "--lines", type=int, default=50)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .errors import BusError

    with warnings.catch_warnings():
        warnings.showwarning = _warning_printer(warnings.showwarning)
        try:
            return args.fn(args)
        except BusError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130


def _warning_printer(default):
    """A warnings.showwarning that prints a config file's warnings (a driver that imports Qt, ...)
    as this command prints errors: one line, without the line of insthub that raised it."""

    def show(message, category, filename, lineno, file=None, line=None):
        from .config import ConfigWarning

        if issubclass(category, ConfigWarning):
            print(f"warning: {message}", file=file or sys.stderr)
        else:
            default(message, category, filename, lineno, file, line)

    return show


if __name__ == "__main__":
    sys.exit(main())
