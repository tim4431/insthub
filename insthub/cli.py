"""Command line.

    insthub list                                   servers and devices on this machine
    insthub info DEVICE                            its commands, properties and streams
    insthub call DEVICE COMMAND [ARG ...] [KEY=VALUE ...] [--save FILE]
    insthub get DEVICE PROPERTY
    insthub set DEVICE PROPERTY VALUE
    insthub watch DEVICE [STREAM]                  print frames as they arrive
    insthub release DEVICE                         close the instrument session (to use the vendor's software);
                                                  the next command reopens it.  `insthub reopen DEVICE` does it now
    insthub view DEVICE                            live plots of its streams (needs insthub[gui])
    insthub gui [--tray | --install | --uninstall] the manager window: every server and device, how each is
                                                  connected, and controls; it stays in the tray when closed.
                                                  --install adds a launcher icon
    insthub serve CONFIG.toml                      host the devices listed in a config file
    insthub sim [--name NAME] [--tcp PORT] [--gui] simulated scope, synth and camera
    insthub bench                                  latency and throughput on this machine

    insthub service install TARGET [--name NAME]   keep a server running in the background (systemd user service);
                                                  TARGET is a .toml config or a .py server script
    insthub service list | start NAME | stop NAME | restart NAME | remove NAME | logs NAME [-f]

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
from pathlib import Path

import numpy as np


def _value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


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

    servers = [insthub.connect(args.address).info()] if args.address else insthub.discover()
    if not servers:
        print("no insthub server is running on this machine")
        return 1
    for info in servers:
        print(f"{info['server']}  (pid {info['pid']})  {'  '.join(info['addresses'])}")
        try:
            address = next((a for a in info["addresses"] if not a.startswith("inproc")), info["addresses"][0])
            described = insthub.connect(args.address or address).describe()
        except insthub.BusError as e:
            print(f"    unreachable: {e}")
            continue
        for name, d in sorted(described.items()):
            st = d["status"]
            state = "connected" if st["connected"] else f"NOT CONNECTED ({st['error']})"
            busy = f", busy: {st['busy']}" if st["busy"] else ""
            print(f"    {name:16s} {d['type']:16s} {state}{busy}; {len(d['commands'])} commands, "
                  f"{len(d['properties'])} properties, streams: {', '.join(sorted(d['streams'])) or '-'}")
    return 0


def _cmd_info(args) -> int:
    dev = _device(args)
    print(dev.bus.help())
    st = dev.bus.status()
    print(f"\nstatus: {'connected' if st['connected'] else 'NOT CONNECTED'}"
          + (f", last error: {st['error']}" if st["error"] else "")
          + f", {st['calls']} calls, {st['errors']} errors, {st['subscribers']} subscribers")
    return 0


def _cmd_call(args) -> int:
    dev = _device(args)
    positional, keyword = [], {}
    for item in args.params:
        key, eq, text = item.partition("=")
        if eq and key.isidentifier():
            keyword[key] = _value(text)
        else:
            positional.append(_value(item))
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


def _cmd_release(args) -> int:
    bus = _device(args).bus
    status = bus.release() if args.cmd == "release" else bus.reopen()
    print(f"{args.device}: " + ("connected" if status["connected"] else f"not connected ({status['error']})"))
    return 0


def _cmd_view(args) -> int:
    from . import viewer

    return viewer.main(_device(args))


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
    from . import manager

    return manager.main(tray_only=args.tray)


def _cmd_serve(args) -> int:
    from . import Server, config

    kwargs, devices = config.load(args.config)
    if args.tcp is not None:
        kwargs["tcp"] = args.tcp
    Server(kwargs.pop("name"), devices, **kwargs).serve_forever()
    return 0


def _cmd_sim(args) -> int:
    from . import Server, sim

    server = Server(args.name, [*sim.devices(), sim.Loopback()], tcp=args.tcp)
    if not args.gui:
        server.serve_forever()
        return 0
    from . import viewer

    with server:
        return viewer.main(*(server.device(name) for name in ("scope", "camera", "synth")))


def _cmd_bench(args) -> int:
    from . import bench

    return bench.main(args.address, quick=args.quick)


def _cmd_service(args) -> int:
    from . import service

    if args.action == "install":
        path = service.install(args.target, args.name, start=not args.no_start)
        name = path.stem[len(service.PREFIX):]
        print(f"installed {path}")
        if not args.no_start:
            time.sleep(1.0)  # long enough for a bad config to have failed
            state = next((u["state"] for u in service.units() if u["name"] == name), "unknown")
            print(f"state: {state}" + ("" if state == "active" else f"   (see: insthub service logs {name})"))
        print(f"restart after changing its code or config:  systemctl --user restart {path.stem}")
        hint = service.linger_hint()
        if hint:
            print(f"it starts when you log in; to start it at boot instead, run once:  {hint}")
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
    units = service.units()
    if not units:
        print("no insthub services installed (insthub service install TARGET)")
    for unit in units:
        boot = "enabled" if unit["enabled"] else "not enabled"
        print(f"{unit['name']:16s} {unit['state']:10s} {boot:12s} {unit['command']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="insthub", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-a", "--address", help="server address (tcp://host:port, ipc:///path); default: find by name")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="servers and devices").set_defaults(fn=_cmd_list)
    s = sub.add_parser("info", help="commands, properties and streams of a device")
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
    s = sub.add_parser("view", help="live plots of a device's streams")
    s.add_argument("device")
    s.set_defaults(fn=_cmd_view)
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
    s = sub.add_parser("service", help="keep servers running in the background (systemd user services)")
    s.set_defaults(fn=_cmd_service)
    actions = s.add_subparsers(dest="action", required=True)
    a = actions.add_parser("install", help="register a server and start it; again to replace it")
    a.add_argument("target", help="a .toml config (run with `insthub serve`) or a .py server script")
    a.add_argument("--name", help="service name (default: the file name without extension)")
    a.add_argument("--no-start", action="store_true", help="only write the unit file")
    actions.add_parser("list", help="installed services and their state")
    for action, text in (("start", "start a service"), ("stop", "stop a service until the next login or start"),
                         ("restart", "restart a service, e.g. after changing its code or config")):
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

    try:
        return args.fn(args)
    except BusError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
