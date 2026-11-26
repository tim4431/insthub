"""The bus for AI agents: an MCP server (`insthub mcp`).

    claude mcp add --scope user insthub -- /path/to/venv/bin/insthub mcp

The agent starts this program and talks to it on stdin/stdout using the Model
Context Protocol. It is an ordinary insthub client: it finds devices the way
`insthub.device()` does, and the servers do not know it exists.

Seven generic tools cover every device, however many there are, because devices
describe themselves:

    list_devices()                       every device on the bus, in brief
    describe(device)                     signatures and docstrings of its commands, properties, streams
    get(device, property)                read a property
    set(device, property, value)         write a property
    call(device, command, args, kwargs)  run a command
    snapshot(device, stream, fresh)      a frame of a stream, published after the call started
    call_dangerous(device, command, ...) run a command marked dangerous, or write such a property

`set` and `call` refuse what a device marks dangerous (see _danger).  Only
`call_dangerous` runs it, so a user can let an agent use every other tool
without being asked, and still be asked before anything dangerous.

Results are JSON. Bulk data goes to files (see render.py). The protocol part is
the stdio subset of MCP that tools need, written out here so that insthub needs
no extra packages.  See docs/agents.md.
"""
from __future__ import annotations

import base64
import dataclasses
import functools
import io
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import __version__, client, registry
from .device import ABOUT, busy_with
from .errors import BusError, DevicePaused, RemoteError
from .render import Renderer

_log = logging.getLogger("insthub.mcp")

#: Newest first.  A client asking for another version is answered with the newest.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """\
insthub gives access to lab instruments. Each instrument is a device hosted by an insthub server on this \
computer or the lab network. list_devices shows them all. describe(device) gives the signature and docstring \
(units, ranges) of each command, plus its properties and streams, with their units, limits and choices ("meta", \
"params") where the device states them. call, get and set act on the real instrument. A command or property \
marked "dangerous" can harm the instrument, the sample or a person: call and set refuse it; call_dangerous runs \
it, so use it only when the user wants that. A device marked "simulated" talks to a simulator: its data is not \
a measurement. snapshot returns live data a device publishes; with fresh=1 (the default) it waits for a frame \
published after the call started, so the frame reflects settings changed just before.
Results are JSON. Arrays longer than 64 elements, long text and bytes are saved to files and summarized as \
{"array": "float32[1001]", "min": ..., "max": ..., "file": ...}; load them with numpy.load(file). plot=true \
adds a PNG of the arrays. To pass saved data back to a device, use {"$file": path} as an argument value."""

_DEVICE = {"type": "string", "description": "Device name, as listed by list_devices."}
_PLOT = {"type": "boolean", "default": False, "description": "Also return a PNG plot of the arrays in the result."}
_TIMEOUT = {"type": "number", "description": "Seconds to wait for the reply (default: the server's, 60 s)."}
_BULK = (" Arrays longer than 64 elements, long text and bytes come back as a summary with a file path "
         "(load it with numpy.load).")
_CALL = {"type": "object", "properties": {
    "device": _DEVICE, "command": {"type": "string"},
    "args": {"type": "array", "items": {}, "description": "Positional arguments."},
    "kwargs": {"type": "object", "description": "Keyword arguments."},
    "timeout": _TIMEOUT, "plot": _PLOT}, "required": ["device", "command"]}

TOOLS = [
    {
        "name": "list_devices",
        "title": "List instruments",
        "description": "Every device on the insthub bus: its server, type, whether its hardware is connected "
                       "(\"simulated\": true means it is a simulator, not an instrument), its extension, and the "
                       "names of its commands, properties and streams. Start here.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "describe",
        "title": "Describe an instrument",
        "description": "One device in full: the signature and docstring of every command, its properties "
                       "(settable or read-only), its streams with their last frame, periodic tasks, status, "
                       "and its state dict (cached settings). The docstrings document the instrument: units, "
                       "ranges, meaning. Where the device states them, \"meta\" (of a property) and \"params\" "
                       "(of a command's parameters) give unit, min, max, step and choices; \"dangerous\": true "
                       "marks what only call_dangerous runs.",
        "inputSchema": {"type": "object", "properties": {"device": _DEVICE}, "required": ["device"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get",
        "title": "Read a property",
        "description": "Read a property of a device from the instrument." + _BULK,
        "inputSchema": {"type": "object", "properties": {
            "device": _DEVICE, "property": {"type": "string"}, "timeout": _TIMEOUT, "plot": _PLOT},
            "required": ["device", "property"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "set",
        "title": "Write a property",
        "description": "Write a property of a device. The driver checks the value and raises an error if the "
                       "instrument does not accept it. Returns ok. A property marked dangerous is refused: "
                       "see call_dangerous.",
        "inputSchema": {"type": "object", "properties": {
            "device": _DEVICE, "property": {"type": "string"},
            "value": {"description": "Any JSON value, or {\"$file\": path} for saved data."},
            "timeout": _TIMEOUT}, "required": ["device", "property", "value"]},
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "call",
        "title": "Run a command",
        "description": "Run a command of a device and return its result. Arguments are JSON values. An argument "
                       "{\"$file\": path} (with \"key\" for .npz) passes a .npy/.npz array, or the content of "
                       "a .txt/other file. If the timeout passes, the command keeps running on the instrument "
                       "and later calls to that device wait behind it. Every device also answers $release "
                       "(close the instrument session so other software can use it; the next call reopens it), "
                       "$open (reopen now) and $task (args: [task_name, enabled true/false/null, interval in "
                       "s or null]). A paused device (\"paused\" in list_devices) refuses every call: a person "
                       "paused it, usually to use the instrument by hand, so tell the user instead of calling "
                       "$resume, unless they ask you to. A command marked dangerous is refused (and so is $set "
                       "of a property marked dangerous): see call_dangerous." + _BULK,
        "inputSchema": _CALL,
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "snapshot",
        "title": "Get live data",
        "description": "A frame of a stream the device publishes (live trace, image, state, ...): its data, "
                       "meta (e.g. the x axis as t0/dt or x0/dx, and unit), sequence number and age. With "
                       "fresh=N it returns the N-th frame published after the call starts, so the frame "
                       "reflects settings changed before the call (use 2 if a frame could have been mid-"
                       "acquisition at that moment). fresh=0 returns the last frame already published, "
                       "however old (from a paused device, marked \"paused\": true)." + _BULK,
        "inputSchema": {"type": "object", "properties": {
            "device": _DEVICE, "stream": {"type": "string"},
            "fresh": {"type": "integer", "minimum": 0, "default": 1},
            "timeout": {"type": "number", "default": 10, "description": "Seconds to wait for the frame."},
            "plot": _PLOT}, "required": ["device", "stream"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "call_dangerous",
        "title": "Run a dangerous command",
        "description": "Run a command that the device marks dangerous (it can harm the instrument, the sample "
                       "or a person: firing a laser, switching on a high voltage), or write a property marked "
                       "dangerous with command \"$set\" and args [property, value]. Use it only when the user "
                       "asked for exactly this. Otherwise it works like call." + _BULK,
        "inputSchema": _CALL,
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
]


@dataclasses.dataclass
class Reply:
    """The result of one tool call."""

    text: str
    image: bytes | None = None
    error: bool = False

    def to_mcp(self) -> dict:
        content = [{"type": "text", "text": self.text}]
        if self.image:
            content.append({"type": "image", "data": base64.b64encode(self.image).decode(), "mimeType": "image/png"})
        return {"content": content, "isError": self.error}


# ---------------------------------------------------------------- the tools
class Tools:
    """The seven tools as plain methods (testable without any MCP machinery).
    Each returns a `Reply`; errors become a Reply with error=True."""

    def __init__(self, renderer: Renderer | None = None, timeout: float = 60.0):
        self.renderer = renderer or Renderer()
        self.timeout = float(timeout)
        self._clients: dict[str, client.Client] = {}
        self._lock = threading.Lock()

    def run(self, name: str, arguments: dict) -> Reply:
        fn = getattr(self, name, None) if name in {t["name"] for t in TOOLS} else None
        if fn is None:
            return Reply(f"unknown tool {name!r}", error=True)
        try:
            return fn(**(arguments or {}))
        except Exception as e:  # noqa: BLE001 -- every error goes back to the agent
            return Reply(_error_text(e), error=True)

    # -- connections ---------------------------------------------------------
    def _client(self, address: str) -> client.Client:
        c = self._clients.get(address)
        if c is not None and not c.closed:
            return c
        new = client.connect(address)
        with self._lock:
            c = self._clients.get(address)
            if c is None or c.closed:
                self._clients[address] = c = new
                return c
        new.close()
        return c

    def _bus(self, device: str, timeout: float | None = None) -> client.Bus:
        bus = self._client(client.locate(device)).device(device).bus
        return bus.with_timeout(self.timeout if timeout is None else float(timeout)).bus

    def _safe_bus(self, device: str, command: str, args: list, timeout: float | None) -> client.Bus:
        """The device's bus, if running `command` with `args` on it is not dangerous; else
        PermissionError, which says to use call_dangerous.  The device is asked each time, so
        that a server restarted with new marks is never trusted from memory."""
        bus = self._bus(device, timeout)
        what = _danger(bus.describe(), command, args)
        if what:
            how = f'"$set", [{args[0]!r}, value]' if command == "$set" else f"{command!r}, ..."
            raise PermissionError(f"{device}: {what} is marked dangerous, so this tool refuses it. If the user wants "
                                  f"exactly this, use call_dangerous({device!r}, {how}); they may be asked first.")
        return bus

    def _reply(self, value, name: str, plot: bool, axis: dict | None = None, wrap=None) -> Reply:
        r = self.renderer.render(value, name, axis=axis, plot=plot)
        text = _json(r.value if wrap is None else wrap(r.value))
        if r.image_file:
            text += f"\n(plot saved to {r.image_file})"
        elif r.note:
            text += f"\n({r.note})"
        return Reply(text, r.image)

    # -- tools ----------------------------------------------------------------
    def list_devices(self) -> Reply:
        servers = client.discover(remote=True)
        notes = [f"{s['address']}: unreachable ({s['error']})" if s["address"] else s["error"]
                 for s in servers if s.get("error")]  # (no address: devices.toml cannot be read)
        targets = [(s["server"], s["address"]) for s in servers if not s.get("error")]

        def fetch(target):
            try:
                return self._client(target[1]).describe()
            except Exception as e:  # noqa: BLE001
                return e

        with ThreadPoolExecutor(max(1, min(8, len(targets)))) as pool:
            results = list(pool.map(fetch, targets))
        places: dict[str, list] = {}  # device name -> (server, address, its describe()) of each that has one
        for (server, address), described in zip(targets, results):
            if isinstance(described, Exception):
                notes.append(f"{address}: unreachable ({_error_text(described)})")
                continue
            for name, d in described.items():
                places.setdefault(name, []).append((server, address, d))
        # Each is listed where the other tools find it (client._place, as locate does), or not at all
        here = [s for s in servers if not s.get("remote")]
        elsewhere = [s for s in servers if s.get("remote") and not s.get("error")]
        static = functools.cache(registry.static_devices)
        devices = {}
        for name, found in sorted(places.items()):
            try:
                address = client._place(name, here, static, lambda: elsewhere)
            except BusError as e:  # two servers have one: the tools refuse it as well
                notes.append(_error_text(e))
                continue
            chosen = next((f for f in found if f[1] == address), None)
            if chosen is None:
                notes.append(f"device {name!r} is looked for at {address}, so the one on "
                             f"{', '.join(repr(f[0]) for f in found)} is not listed")
                continue
            devices[name] = _brief(chosen[0], chosen[2])
        if not servers:
            notes.append("no insthub server is running on this computer, and none is listed in devices.toml")
        out = {"devices": devices}
        if notes:
            out["notes"] = notes
        return Reply(_json(out))

    def describe(self, device: str) -> Reply:
        bus = self._bus(device)
        d = bus.describe()
        now = time.time()
        out = {"name": d["name"], "type": d["type"], **_about(d)}
        if d.get("doc"):
            out["doc"] = d["doc"]
        out["status"] = {k: v for k, v in d["status"].items() if v is not None}
        out["commands"] = {n: _drop_empty({"signature": c["signature"], "doc": c["doc"], "params": c.get("params"),
                                           "dangerous": c.get("dangerous")})
                           for n, c in sorted(d["commands"].items())}
        out["properties"] = {n: _drop_empty({"doc": p["doc"], "readonly": not p["settable"] or None,
                                             "meta": p.get("meta"), "dangerous": p.get("dangerous")})
                             for n, p in sorted(d["properties"].items())}
        out["streams"] = {n: _drop_empty({"doc": s["doc"], "kind": s["kind"], "frames": s["seq"] or None,
                                          "age_s": round(now - s["time"], 3) if s["time"] else None})
                          for n, s in sorted(d["streams"].items())}
        if d["tasks"]:
            out["tasks"] = {n: _drop_empty({"interval": t["interval"], "enabled": t["enabled"],
                                            "errors": t["errors"] or None, "last_error": t["last_error"]})
                            for n, t in sorted(d["tasks"].items())}
        out["state"] = bus.state()
        return self._reply(out, f"{device}-describe", False)

    def get(self, device: str, property: str, timeout: float | None = None, plot: bool = False) -> Reply:
        return self._reply(self._bus(device, timeout).get(property), f"{device}-{property}", plot)

    def set(self, device: str, property: str, value, timeout: float | None = None) -> Reply:
        self._safe_bus(device, "$set", [property], timeout).set(property, _resolve(value))
        return Reply("ok")

    def call(self, device: str, command: str, args=None, kwargs=None, timeout: float | None = None,
             plot: bool = False) -> Reply:
        args = _as_list(args)
        return self._run(self._safe_bus(device, command, args, timeout), command, args, kwargs, plot)

    def call_dangerous(self, device: str, command: str, args=None, kwargs=None, timeout: float | None = None,
                       plot: bool = False) -> Reply:
        return self._run(self._bus(device, timeout), command, _as_list(args), kwargs, plot)

    def _run(self, bus: client.Bus, command: str, args: list, kwargs, plot: bool) -> Reply:
        if command == "$pause" and not args:  # say who paused it: the refusals then do not blame a person
            args = [_AGENT, client.PROCESS_TOKEN]
        try:
            result = bus.call(command, *_resolve(args), **_resolve(_as_dict(kwargs)))
        except TimeoutError:
            if command != "$pause":
                raise
            # its reply waits for what runs now, but the pause is in effect already: say so, not "no reply"
            st = bus.status()
            result = {**st, "note": "pausing: it refuses every call already; its session is closed once "
                                    f"{busy_with(st) or 'what it is doing'} finishes (list_devices shows it paused)"}
        return self._reply(result, f"{bus.name}-{command}", plot)

    def snapshot(self, device: str, stream: str, fresh: int = 1, timeout: float = 10.0, plot: bool = False) -> Reply:
        bus = self._bus(device)
        described = bus.describe()
        streams = described["streams"]
        if stream not in streams:  # subscribing would create it on the server; say what there is instead
            raise LookupError(f"{device} has no stream {stream!r} (it has: {', '.join(sorted(streams)) or 'none'}; "
                              "a stream that is not declared appears after its first frame)")
        fresh = max(0, int(fresh))
        paused = described["status"].get("paused")
        if paused and (fresh or not streams[stream]["seq"]):  # it publishes nothing: do not wait for a frame
            raise _paused_refusal(device, paused)
        end = time.monotonic() + float(timeout)
        try:
            with bus.subscribe(stream, retained=fresh == 0) as sub:
                frame = sub.get(max(0.0, end - time.monotonic()))
                for _ in range(fresh - 1):
                    frame = sub.get(max(0.0, end - time.monotonic()))
        except TimeoutError:
            now = bus.describe()
            if now["status"].get("paused"):  # paused while waiting
                raise _paused_refusal(device, now["status"]["paused"]) from None
            last = now["streams"].get(stream) or {}
            if not last.get("seq"):
                why = "nothing has been published on it yet"
            else:
                why = f"the last frame, #{last['seq']}, is {time.time() - last['time']:.1f} s old; fresh=0 returns it"
            raise TimeoutError(f"no new frame on {device}.{stream} within {timeout:g} s ({why})") from None
        meta = self.renderer.render(frame.meta, f"{device}-{stream}-meta").value
        head = {"stream": stream, "seq": frame.seq, "age_s": round(frame.age, 3), "meta": meta}
        if paused:  # fresh=0: the last frame from before the pause
            head["paused"] = True
        return self._reply(frame.data, f"{device}-{stream}", plot, axis=frame.meta,
                           wrap=lambda data: {**head, "data": data})


def _paused_refusal(device: str, paused: dict) -> DevicePaused:
    """What a snapshot of a paused device says, worded like the device's own refusal of a call."""
    by = f" by {paused['by']}" if paused.get("by") else ""
    since = time.strftime("%H:%M:%S", time.localtime(paused["since"]))
    return DevicePaused(f"{device} is paused (since {since}{by}): it publishes nothing until it is resumed "
                        f"(insthub resume {device})")


def _danger(described: dict, command: str, args: list) -> str | None:
    """What is dangerous about running `command` with `args`, as the device's describe() marks
    it ("its command 'fire'", "writing its property 'voltage'"), or None.  `$set` counts as the
    property it writes.  The other `$` commands manage the session ($open, $release, $task,
    $pause, $resume), not the instrument's settings."""
    if command == "$set":
        prop = described["properties"].get(args[0]) if args and isinstance(args[0], str) else None
        return f"writing its property {args[0]!r}" if prop and prop.get("dangerous") else None
    found = described["commands"].get(command)
    return f"its command {command!r}" if found and found.get("dangerous") else None


def _about(d: dict) -> dict:
    """What a device says it is, for agents: its description and address (which of several of a
    kind it is), its extension, and whether it is a simulator."""
    return {k: d[k] for k in (*ABOUT, "simulated") if d.get(k)}


def _brief(server: str, d: dict) -> dict:
    st = d["status"]
    out = {"server": server, "type": d["type"], **_about(d)}
    if d.get("doc"):
        out["doc"] = d["doc"]
    out["connected"] = st["connected"]
    if st.get("paused"):
        out["paused"] = True
    if st.get("error"):
        out["error"] = st["error"]
    if st.get("busy"):
        out["busy"] = st["busy"]
    out["commands"] = sorted(d["commands"])
    out["properties"] = sorted(d["properties"])
    out["streams"] = sorted(d["streams"])
    return out


def _drop_empty(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != ""}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


_PAUSED = ("A person paused it, usually to use the instrument by hand: tell the user, and do not resume it "
           "unless they ask you to.")


_AGENT = "an AI agent (insthub mcp)"  #: how a pause made through `call(device, "$pause")` is labelled
_PAUSED_BY_AGENT = ("An AI agent paused it, perhaps you at the user's request: resume it ($resume) only if this "
                    "conversation paused it and is done with what the pause was for; else tell the user.")


def _error_text(e: BaseException) -> str:
    if isinstance(e, DevicePaused):  # not the hint for people ("insthub resume ..."): what an agent should do
        text = str(e).rpartition(" (insthub resume")[0] or str(e)
        return f"DevicePaused: {text}. {_PAUSED_BY_AGENT if f' by {_AGENT}' in text else _PAUSED}"
    if isinstance(e, RemoteError):
        text = f"{e.remote_type}: {e}"
        tb = e.remote_traceback.strip().splitlines()
        if tb:
            text += "\n--- remote traceback (last lines) ---\n" + "\n".join(tb[-8:])
        return text
    return f"{type(e).__name__}: {e}"


def _as_list(args) -> list:
    if args is None:
        return []
    if isinstance(args, str):  # a model sometimes sends the list as JSON text
        try:
            parsed = json.loads(args)
        except ValueError:
            return [args]
        return parsed if isinstance(parsed, list) else [parsed]
    return list(args) if isinstance(args, (list, tuple)) else [args]


def _as_dict(kwargs) -> dict:
    if kwargs is None:
        return {}
    if isinstance(kwargs, str):
        kwargs = json.loads(kwargs)
    if not isinstance(kwargs, dict):
        raise TypeError(f"kwargs must be an object, got {type(kwargs).__name__}")
    return kwargs


def _resolve(value):
    """Replace {"$file": path[, "key": name]} by the content of that file."""
    if isinstance(value, dict):
        if "$file" in value and set(value) <= {"$file", "key"}:
            return _load(value["$file"], value.get("key"))
        return {k: _resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    return value


def _load(path: str, key: str | None):
    p = Path(path).expanduser()
    if p.suffix == ".npy":
        return np.load(p, allow_pickle=False)
    if p.suffix == ".npz":
        with np.load(p, allow_pickle=False) as z:
            if key is None:
                if len(z.files) != 1:
                    raise ValueError(f"{p} holds {z.files}: add \"key\" to say which")
                key = z.files[0]
            return z[key]
    if p.suffix == ".txt":
        return p.read_text(encoding="utf-8")
    return p.read_bytes()


# ---------------------------------------------------------------- the protocol
class StdioServer:
    """JSON-RPC 2.0, one message per line.  Tool calls run on threads of their
    own, so a slow instrument does not hold up calls to other devices."""

    def __init__(self, tools: Tools, reader, writer):
        self.tools, self._reader, self._writer = tools, reader, writer
        self._write_lock = threading.Lock()

    def run(self) -> None:
        for line in self._reader:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if not isinstance(msg, dict):
                self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
                continue
            self._handle(msg)

    def _handle(self, msg: dict) -> None:
        method, mid = msg.get("method"), msg.get("id")
        if method is None or "id" not in msg:  # a notification (initialized, cancelled, ...) or a stray reply
            return
        params = msg.get("params") or {}
        if method == "tools/call":
            threading.Thread(target=self._call, args=(mid, params), daemon=True,
                             name=f"insthub:mcp:{params.get('name')}").start()
        elif method == "initialize":
            asked = params.get("protocolVersion")
            self._result(mid, {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "insthub", "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        elif method == "tools/list":
            self._result(mid, {"tools": TOOLS})
        elif method == "ping":
            self._result(mid, {})
        else:
            self._send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method!r}"}})

    def _call(self, mid, params: dict) -> None:
        name = params.get("name", "")
        start = time.perf_counter()
        reply = self.tools.run(name, params.get("arguments") or {})
        _log.debug("%s %s: %.1f ms%s", name, params.get("arguments"), (time.perf_counter() - start) * 1e3,
                   " (error)" if reply.error else "")
        self._result(mid, reply.to_mcp())

    def _result(self, mid, result: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": mid, "result": result})

    def _send(self, msg: dict) -> None:
        data = json.dumps(msg, ensure_ascii=False, default=str) + "\n"
        with self._write_lock:
            self._writer.write(data)
            self._writer.flush()


def main(data: str | None = None, timeout: float = 60.0) -> int:
    """Serve on stdin/stdout until the agent closes stdin."""
    # stdout carries the protocol and nothing else: keep the real one for it, and send
    # whatever else would be printed (by a library, a print() left in a driver) to stderr.
    writer = io.TextIOWrapper(os.fdopen(os.dup(sys.stdout.fileno()), "wb"), encoding="utf-8", newline="\n")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    reader = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    tools = Tools(Renderer(data), timeout)
    _log.info("insthub MCP server ready; files go to %s", tools.renderer.folder)
    try:
        StdioServer(tools, reader, writer).run()
    except KeyboardInterrupt:
        pass
    return 0
