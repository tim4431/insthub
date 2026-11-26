"""The bus for AI agents: an MCP server (`insthub mcp`).

    claude mcp add --scope user insthub -- /path/to/venv/bin/insthub mcp

The agent starts this program and talks to it on stdin/stdout using the Model
Context Protocol. It is an ordinary insthub client: it finds devices the way
`insthub.device()` does, and the servers do not know it exists.

Eight generic tools cover every device, however many there are, because devices
describe themselves:

    list_devices()                       every device on the bus, in brief
    describe(device)                     signatures and docstrings of its commands, properties, streams
    get(device, property)                read a property
    set(device, property, value)         write a property
    call(device, command, args, kwargs)  run a command
    snapshot(device, stream, fresh)      a frame of a stream, published after the call started
    call_dangerous(device, command, ...) run a command marked dangerous, or write such a property
    run_recipe(name, params)             run a recipe: a Python function the user keeps under a name
                                         (recipes.py); describe(device) lists those that use it

`set` and `call` refuse what a device marks dangerous (see _danger).  Only
`call_dangerous` runs it, so a user can let an agent use every other tool
without being asked, and still be asked before anything dangerous.

`run_recipe` runs the user's own code, in a process of its own (recipes.start),
so that a recipe that hangs or crashes never takes this server along; one still
running when its timeout passes, when the agent cancels the request, or when
this server exits, is stopped.  It runs only the recipes describe lists (the
user's folder, extensions): a function of any other file is run only by the
user (`insthub run path/to/file.py:function`).  Before it runs, insthub asks the
devices it can see in the code (recipes.check): a recipe with a missing or
paused device, or a typo in what it uses, is refused, and so is one that uses
what is marked dangerous: only the user runs that (`insthub run`, the window).

Results are JSON. Bulk data goes to files (see render.py). The protocol part is
the stdio subset of MCP that tools need, written out here so that insthub needs
no extra packages.  See docs/agents.md.
"""
from __future__ import annotations

import base64
import dataclasses
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
from .device import busy_with
from .errors import DevicePaused, RemoteError
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
published after the call started, so the frame reflects settings changed just before. describe also lists the \
"recipes" that use the device: Python functions the user keeps under a name. run_recipe runs one, in a process of \
its own, when the user names it or asks for what it is titled; insthub first checks the devices it can see in the \
recipe's code, and refuses a recipe that uses what is marked dangerous (only the user runs that).
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
    {
        "name": "run_recipe",
        "title": "Run a recipe",
        "description": "Run a recipe: a Python function the user keeps under a name (in their recipes folder, "
                       "an extension or an experiment's folder), in a process of its own. describe(device) lists "
                       "the recipes that use that device, with their parameters. Before it runs, insthub asks "
                       "each device it can see in the recipe's code whether it is there, not paused, and has the "
                       "commands and properties the code uses; what the code does through names known only when "
                       "it runs is not checked. A recipe that cannot run is refused, and so is one that uses "
                       "what is marked dangerous (or is marked so itself): only the user runs that. Only the "
                       "recipes describe lists can be run, not a function of another file. One still running "
                       "when the timeout passes, or when the request is cancelled, is stopped. Returns what the "
                       "function returned, the end of what it printed, its simulated devices and the seconds it "
                       "took; a recipe that fails returns its error and traceback." + _BULK,
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "The recipe's name as describe lists it (FILE.function, or "
                                                      "path/to/FILE.py:function when two recipes have the same "
                                                      "FILE.function), or the function's name alone if no other "
                                                      "recipe has it."},
            "params": {"type": "object", "description": "Its parameters by name; the others keep their defaults. "
                                                        "{\"$file\": path} passes saved data."},
            "timeout": {"type": "number", "description": "Seconds it may run before it is stopped (default: the "
                                                         "server's, 60 s)."},
            "plot": _PLOT},
            "required": ["name"]},
        "annotations": {"readOnlyHint": False},
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
    """The eight tools as plain methods (testable without any MCP machinery).
    Each returns a `Reply`; errors become a Reply with error=True."""

    def __init__(self, renderer: Renderer | None = None, timeout: float = 60.0):
        self.renderer = renderer or Renderer()
        self.timeout = float(timeout)
        self._clients: dict[str, client.Client] = {}
        self._lock = threading.Lock()
        self._runs: dict = {}  #: the recipes run_recipe runs now -> the Event set when their request is cancelled
        self._closed = False
        self._local = threading.local()  # .cancelled: that Event, for the call this thread answers

    def run(self, name: str, arguments: dict, cancelled: threading.Event | None = None) -> Reply:
        """Run a tool.  `cancelled` is set when the agent cancels the request: a recipe that
        run_recipe runs for it is then stopped (see cancel())."""
        fn = getattr(self, name, None) if name in {t["name"] for t in TOOLS} else None
        if fn is None:
            return Reply(f"unknown tool {name!r}", error=True)
        self._local.cancelled = cancelled
        try:
            return fn(**(arguments or {}))
        except Exception as e:  # noqa: BLE001 -- every error goes back to the agent
            return Reply(_error_text(e), error=True)
        finally:
            self._local.cancelled = None

    def cancel(self, cancelled: threading.Event) -> None:
        """Stop the recipe that a cancelled request runs (`cancelled`: the Event given to run(), now set)."""
        with self._lock:
            runs = [run for run, event in self._runs.items() if event is cancelled]
        for run in runs:
            run.stop()

    def close(self) -> None:
        """Stop every recipe still running, and any that a call starts from now on: the server is
        exiting, and a recipe left running would have no one to stop it at its timeout."""
        with self._lock:
            self._closed = True
            runs = list(self._runs)
        for run in runs:
            run.stop()

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
        targets, known = [], set()
        for srv in client.discover():
            addresses = srv.get("addresses") or []
            for scheme in ("inproc://", "ipc://", ""):
                address = next((a for a in addresses if a.startswith(scheme)), None)
                if address:
                    targets.append(address)
                    break
            known.update(addresses)
        targets += sorted(set(registry.static_devices().values()) - known)

        def fetch(address):
            try:
                c = self._client(address)
                return c.info()["server"], c.describe()
            except Exception as e:  # noqa: BLE001
                return e

        with ThreadPoolExecutor(max(1, min(8, len(targets)))) as pool:
            results = list(pool.map(fetch, targets))
        devices, owner, notes = {}, {}, []
        for address, result in zip(targets, results):
            if isinstance(result, Exception):
                notes.append(f"{address}: unreachable ({_error_text(result)})")
                continue
            server, described = result
            for name, d in sorted(described.items()):
                if name in owner:
                    if owner[name] != server:
                        notes.append(f"device name {name!r} is on servers {owner[name]!r} and {server!r}; "
                                     "only the first is listed")
                    continue
                owner[name] = server
                devices[name] = _brief(server, d)
        if not targets:
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
        recipes = _recipes(d["name"])
        if recipes:
            out["recipes"] = recipes
        return self._reply(out, f"{device}-describe", False)

    def run_recipe(self, name: str, params=None, timeout: float | None = None, plot: bool = False) -> Reply:
        from . import recipes

        try:
            recipe = recipes.find(name, any_file=False)  # (a path only for a file of the recipes describe lists)
        except recipes.RecipeError:
            path, sep, _ = name.rpartition(":")
            if sep and path.endswith(".py") and not _listed_file(path):
                raise PermissionError(f"{path} is not a file of the recipes describe lists (the user's recipes "
                                      "folder, extensions), so this tool refuses it, and nothing ran: only the user "
                                      f"runs a function of another file (insthub run {name}).") from None
            raise
        values = _resolve(_as_dict(params))
        recipes.check_values(recipe, values)
        described = {}

        def describe(device: str) -> dict:
            described[device] = self._bus(device).describe()
            return described[device]

        checked = recipes.check(recipe, describe)
        full = recipe.full_name
        if checked.problems:
            problems, paused = _paused_problems(checked.problems, recipe.devices, described)
            text = f"recipe {full} cannot run, so nothing ran: {'; '.join(problems)}"
            if paused:  # what to do about it: not what check() tells people ("resume it first")
                text += "" if text.endswith((".", "?")) else "."
                text += " " + (_PAUSED_BY_AGENT if all(p.get("by") == _AGENT for p in paused) else _PAUSED)
            raise recipes.RecipeError(text)
        if checked.dangers:
            raise PermissionError(f"recipe {full} uses what is marked dangerous ({'; '.join(checked.dangers)}), so "
                                  "this tool refuses it, and nothing ran: only the user runs such a recipe "
                                  f"(insthub run {full}, or from insthub gui).")
        timeout = self.timeout if timeout is None else float(timeout)
        cancelled = getattr(self._local, "cancelled", None) or threading.Event()
        if cancelled.is_set():
            return Reply(f"recipe {full} was cancelled before it started, so nothing ran", error=True)
        run = recipes.start(recipe, values)
        with self._lock:
            self._runs[run] = cancelled
            closed = self._closed
        stopped = None
        try:
            if closed or cancelled.is_set():  # (cancel() or close() came before the run was in the list)
                run.stop()
            try:
                result = run.wait(timeout)
            except TimeoutError:
                run.stop()
                try:
                    result = run.wait(10.0)  # (stop() kills it if it has not ended 3 s later)
                except (recipes.RecipeError, TimeoutError):
                    return Reply(f"TimeoutError: recipe {full} was still running after {timeout:g} s, so it was "
                                 "stopped (give a longer timeout if it needs more time)" + _printed(run.output()),
                                 error=True)
                stopped = f"it was still running after {timeout:g} s, so it was stopped; this is what it returned then"
            except recipes.RecipeError as e:
                return Reply(f"RecipeError: {e}" + _printed(e.output), error=True)
        finally:
            with self._lock:
                self._runs.pop(run, None)

        def wrap(rendered) -> dict:
            out = {"recipe": full, "result": rendered}
            if stopped:
                out["stopped"] = stopped
            if run.output():
                out["output"] = _tail(run.output())
            if checked.simulated:
                out["simulated"] = checked.simulated
            return {**out, "seconds": round(run.elapsed, 3)}

        return self._reply(result, f"{full}-recipe", plot, wrap=wrap)

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


def _paused_text(device: str, paused: dict) -> str:
    """"synth is paused (since 14:02:11 by Alice)", from the "paused" of the device's status."""
    said = [f"since {time.strftime('%H:%M:%S', time.localtime(paused['since']))}" if paused.get("since") else "",
            f"by {paused['by']}" if paused.get("by") else ""]
    said = " ".join(filter(None, said))
    return f"{device} is paused" + (f" ({said})" if said else "")


def _paused_refusal(device: str, paused: dict) -> DevicePaused:
    """What a snapshot of a paused device says, worded like the device's own refusal of a call."""
    return DevicePaused(f"{_paused_text(device, paused)}: it publishes nothing until it is resumed "
                        f"(insthub resume {device})")


def _paused_problems(problems: list, devices, described: dict) -> tuple[list, list]:
    """check()'s problems, with what it says of a paused device (meant for people: "resume it
    first") replaced by who paused it and since when; and the "paused" of each paused device."""
    problems, paused = list(problems), []
    for name in devices:
        p = (described.get(name) or {}).get("status", {}).get("paused")
        if not p:
            continue
        paused.append(p)
        at = next((i for i, text in enumerate(problems) if text.startswith(f"{name} is paused")), None)
        if at is None:
            problems.insert(0, _paused_text(name, p))
        else:
            problems[at] = _paused_text(name, p)
    return problems, paused


def _listed_file(path: str) -> bool:
    """Whether `path` is a file of the recipes describe lists: one of the user's folder, or of a usable
    extension (as recipes.find(any_file=False) accepts)."""
    from . import extensions, recipes

    try:
        file = Path(path).expanduser().resolve()
        listed = [*recipes.your_files(), *(p for ext in extensions.discover() if ext.usable for p in ext.recipes)]
        return any(p.resolve() == file for p in listed)
    except (OSError, ValueError, RuntimeError):
        return False


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


def _recipes(device: str) -> dict:
    """The recipes that use a device (as recipes.for_device), for agents, by the name run_recipe runs
    each by (_run_name): their titles, parameters, files and devices.  Finding them never makes
    describe fail: none are listed then."""
    from . import recipes

    try:
        found, _ = recipes.available()
        return {_run_name(r, found): _drop_empty({
            "title": r.title, "doc": r.doc, "params": {p.name: _param(p) for p in r.params.values()} or None,
            "file": f"{r.file}:{r.line}", "devices": list(r.devices) or None, "dangerous": r.dangerous or None,
            "error": r.error}) for r in found if device in r.devices}
    except Exception:  # noqa: BLE001 - a damaged file or manifest: describe still answers
        _log.exception("cannot look up the recipes of %s", device)
        return {}


def _run_name(recipe, found: list) -> str:
    """The name that runs a recipe (recipes.find()): its full name (FILE.function), unless another
    recipe has that full name too and find() would run that one (yours, of several) or ask which:
    then its path, "path/to/FILE.py:function"."""
    from . import recipes

    name = recipes.runs_as(recipe, found)
    return recipe.full_name if name in (recipe.name, recipe.full_name) else name


def _param(param) -> dict:
    """A recipe's parameter, for agents: its default (as the file writes it, if not as a literal), or
    that it is required; its annotation, doc and metadata (unit, min, max, step, choices)."""
    if param.required:
        out = {"required": True}
    elif not param.literal:
        out = {"default_text": param.default_text}
    else:
        out = {"default": param.default}  # (None too: it is the default)
    return {**out, **_drop_empty({"annotation": param.annotation, "doc": param.doc, "meta": param.meta or None})}


_OUTPUT_TAIL = 4000  #: characters of what a recipe printed that run_recipe returns (the last ones)


def _tail(output: str) -> str:
    return output if len(output) <= _OUTPUT_TAIL else "..." + output[-_OUTPUT_TAIL:]


def _printed(output: str) -> str:
    """The end of what a recipe printed, after the error of run_recipe ("" if it printed nothing)."""
    return f"\n--- what it printed (the end) ---\n{_tail(output).rstrip()}" if output.strip() else ""


def _about(d: dict) -> dict:
    """What a device says it is, for agents: its description and address (which of several of a
    kind it is), its extension, and whether it is a simulator."""
    return {k: d[k] for k in ("description", "address", "extension", "extension_version", "simulated") if d.get(k)}


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
    own, so a slow instrument does not hold up calls to other devices.  A call
    the agent cancels (notifications/cancelled) gets no reply, and the recipe
    it runs is stopped."""

    def __init__(self, tools: Tools, reader, writer):
        self.tools, self._reader, self._writer = tools, reader, writer
        self._write_lock = threading.Lock()
        self._calls: dict[str, threading.Event] = {}  #: the tool calls being answered, by id -> set once cancelled
        self._calls_lock = threading.Lock()

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
        if method == "notifications/cancelled":
            params = msg.get("params")
            if isinstance(params, dict):
                self._cancel(params.get("requestId"))
            return
        if method is None or "id" not in msg:  # another notification (initialized, ...) or a stray reply
            return
        params = msg.get("params") or {}
        if method == "tools/call":
            cancelled = threading.Event()
            with self._calls_lock:
                self._calls[_request_key(mid)] = cancelled
            threading.Thread(target=self._call, args=(mid, params, cancelled), daemon=True,
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

    def _call(self, mid, params: dict, cancelled: threading.Event) -> None:
        name = params.get("name", "")
        start = time.perf_counter()
        try:
            reply = self.tools.run(name, params.get("arguments") or {}, cancelled)
        finally:
            with self._calls_lock:
                self._calls.pop(_request_key(mid), None)
        _log.debug("%s %s: %.1f ms%s", name, params.get("arguments"), (time.perf_counter() - start) * 1e3,
                   " (cancelled)" if cancelled.is_set() else " (error)" if reply.error else "")
        if not cancelled.is_set():  # (the agent waits no more for a call it cancelled: it gets no reply)
            self._result(mid, reply.to_mcp())

    def _cancel(self, request) -> None:
        """notifications/cancelled: the agent no longer wants the reply to `request`; stop what it runs."""
        with self._calls_lock:
            cancelled = self._calls.get(_request_key(request))
        if cancelled is not None:
            cancelled.set()
            self.tools.cancel(cancelled)

    def _result(self, mid, result: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": mid, "result": result})

    def _send(self, msg: dict) -> None:
        data = json.dumps(msg, ensure_ascii=False, default=str) + "\n"
        with self._write_lock:
            try:
                self._writer.write(data)
                self._writer.flush()
            except (OSError, ValueError):  # the agent has gone (a recipe stopped as this server exits replies late)
                _log.debug("cannot reply to %s: the agent has closed stdout", msg.get("id"))


def _request_key(mid) -> str:
    """A request's id as a dict key (JSON-RPC: a string or a number)."""
    return json.dumps(mid, sort_keys=True, default=str)


def _exit(signum, frame):
    raise SystemExit(0)


def main(data: str | None = None, timeout: float = 60.0) -> int:
    """Serve on stdin/stdout until the agent closes stdin (or ends this program with SIGTERM);
    then stop the recipes still running."""
    import signal

    # stdout carries the protocol and nothing else: keep the real one for it, and send
    # whatever else would be printed (by a library, a print() left in a driver) to stderr.
    writer = io.TextIOWrapper(os.fdopen(os.dup(sys.stdout.fileno()), "wb"), encoding="utf-8", newline="\n")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    reader = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    tools = Tools(Renderer(data), timeout)
    _log.info("insthub MCP server ready; files go to %s", tools.renderer.folder)
    try:
        previous = signal.signal(signal.SIGTERM, _exit)  # (so that the recipes are stopped below)
    except ValueError:  # not the main thread
        previous = None
    try:
        StdioServer(tools, reader, writer).run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        tools.close()  # (each recipe has 3 s to end by itself: this program waits for that before it exits)
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    return 0
