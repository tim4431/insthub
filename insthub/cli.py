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
    insthub check PATH [--import]                  what is wrong with an extension (its folder or manifest), or
                                                  with the extensions of a server's .toml; --import also imports
                                                  the drivers, in the extension's Python

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
    if args.save:
        path = Path(args.save)
        if isinstance(result, (bytes, bytearray)):
            path.write_bytes(result)
        elif isinstance(result, np.ndarray):
            np.save(path, result)
        elif dataclasses.is_dataclass(result) or isinstance(result, dict):
            fields = result if isinstance(result, dict) else \
                {f.name: getattr(result, f.name) for f in dataclasses.fields(result)}
            np.savez(path, **fields)
        else:
            path.write_text(_show(result) + "\n")
        print(f"saved to {path}")
    elif result is not None:
        print(_show(result))
    print(f"({elapsed * 1e3:.2f} ms)", file=sys.stderr)
    return 0


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
                           ("error", ext.error), ("hidden", ext.hidden_by and f"by {ext.hidden_by}")):
            if text:
                print(f"    {what:8s} {text}")
    return 0


def _cmd_check(args) -> int:
    from . import extensions

    problems = extensions.check(args.path, import_=args.import_)
    print("\n".join(problems) or f"{args.path}: no problems found" +
          ("" if args.import_ else " (--import also imports the drivers and the UIs)"))
    return 1 if problems else 0


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
    s.add_argument("--save", metavar="FILE", help="write the result to a file (bytes raw, arrays as .npy/.npz)")
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
    s = sub.add_parser("check", help="what is wrong with an extension, or with the extensions of a server file")
    s.add_argument("path", help="an extension's folder, its insthub-extension.toml, or a server's .toml")
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
