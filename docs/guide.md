# Using insthub

How to put an instrument on the bus and how to use it from other programs.
For what happens underneath, read [how insthub works](architecture.md). For a
step-by-step walk through adding an instrument, with every key of a config
file, read [adding an instrument](adding-a-device.md).

- [Writing a device](#writing-a-device)
- [Putting an existing driver on the bus](#putting-an-existing-driver-on-the-bus)
- [Using a device from another program](#using-a-device-from-another-program)
- [Streams](#streams)
- [Errors, timeouts and restarts](#errors-timeouts-and-restarts)
- [What can be sent](#what-can-be-sent)
- [GUIs](#guis)
- [Extensions](#extensions)
- [Command line](#command-line)

## Writing a device

A device is a class that holds the session with one instrument.

```python
import socket
import insthub

class LockIn(insthub.Device):
    streams = {"xy": "demodulated X and Y"}          # optional: lets viewers find the stream

    def open(self):                                   # connect to the instrument
        self.sock = socket.create_connection(("192.168.1.30", 5025), timeout=2)

    def close(self):
        self.sock.close()

    @insthub.command
    def set_time_constant(self, seconds: float) -> float:
        """Filter time constant. Returns the value the instrument accepted."""
        ...

    @insthub.prop
    def frequency(self) -> float:
        """Reference frequency in Hz."""
        ...

    @frequency.setter
    def frequency(self, hz): ...

    @insthub.periodic(0.1)                             # runs every 0.1 s, between requests
    def _poll(self):
        if self.subscribers("xy"):                    # skip the readout nobody watches
            self.publish("xy", self.read_xy(), unit="V")

if __name__ == "__main__":
    insthub.serve(LockIn("lockin"))                    # Ctrl-C to stop
```

The three markers:

| Marker | Makes the method | Seen by clients as |
|---|---|---|
| `@insthub.command` | callable from other programs | `lockin.set_time_constant(0.3)` |
| `@insthub.prop` | a readable (and, with a setter, writable) value | `lockin.frequency`, `lockin.frequency = 1e3` |
| `@insthub.periodic(seconds)` | run by itself, repeatedly | not visible; it usually publishes |

The docstring and signature of a command are what `insthub info lockin` and
`lockin.bus.help()` show, so they are worth writing.

### Saying more about values, and what is dangerous

A device can say more than its signatures do: units, limits, what to choose
from, and what is dangerous. Clients read it from `describe()`. The manager
window picks a control for each setting from it, `insthub info` prints it, and
AI agents see units and limits instead of guessing them.

```python
from typing import Literal

class Laser(insthub.Device):
    extension = "demo-laser"                          # which extension it belongs to (see design.md)
    poll = {"temperature": 1.0}                       # read every 1.0 s, published on the stream "temperature"

    @insthub.prop(unit="mW", min=0, max=100, step=0.5, dangerous=True)
    def power(self) -> float: ...
    @power.setter
    def power(self, mw): ...

    @insthub.prop
    def mode(self) -> Literal["CW", "pulsed"]: ...    # its choices come from the annotation

    @insthub.prop
    def temperature(self) -> float: ...

    @insthub.command(dangerous=True, params={"ms": {"unit": "ms", "min": 0}})
    def fire(self, ms: float = 10.0): ...
```

| What | How | What it does |
|---|---|---|
| **metadata** | `@insthub.prop(unit=, min=, max=, step=, choices=[...])`; for a command's parameters, `@insthub.command(params={"ms": {...}})` | the window shows a list for choices and a spin box with the unit for a number; `insthub info` and agents show them. It is advice: insthub never checks a value against it, so an instrument that also takes `"auto"` still gets it. |
| **choices from annotations** | a `Literal[...]` or `Enum` annotation on a property's getter (its return) or on a parameter | the same as `choices=` (what you write explicitly wins). Not a `Flag`, whose members combine, and not a bare `enum.Enum`, which has no members. |
| **limits that depend on the model** | `self.set_meta("power", max=50)`, or `"fire.ms"` for a parameter, for example in `open()` once the instrument has said what it is | clients see it at their next `describe()`; a value of `None` removes a key |
| **dangerous** | `dangerous=True` on a command (running it) or a property (writing it, so only one with a setter); or on the class, `dangerous = ("fire",)`, or `True` for every command and every property that can be written. A config file's `dangerous` (or `wrap()`'s) adds to the class's marks, never removes one. | AI agents' `call` and `set` refuse it, and a separate tool runs it (see [agents](agents.md)); the window asks before running or writing it |
| **what the device is** | `extension = "..."`, `extension_version = "..."`, `simulated = True` | carried in `describe()`. A simulated device is marked SIM in the window, in `insthub info` and for agents, so that its data is never taken for a measurement. |
| **polling** | `poll = {"temperature": 1.0}`: a property, or a command that needs no argument | a periodic task named `poll:temperature` reads it every 1.0 s and publishes the value on the stream `temperature`, so it gets a tile preview and `insthub view` without a periodic method of your own. A config file's `poll` replaces the class's, intervals included. |

### What the base class does for you

**One thread.** `open`, `close`, commands, properties and periodic tasks all
run on the device's own thread, one at a time. You never need a lock, and two
callers can never interleave on the instrument.

**Connecting and reconnecting.** `open()` is called when the server starts. If
a command later fails with a *link error*, the session is closed with `close()`
and the next command calls `open()` again. Link errors are the exception
classes in `link_errors`: by default `OSError` (which covers socket errors,
timeouts and serial-port errors) and `EOFError`. Add your library's own:

```python
class Scope(insthub.Device):
    link_errors = (pyvisa.errors.VisaIOError, OSError, EOFError)
    reconnect_interval = 1.0        # at least this long between two attempts to open
```

If `open()` itself fails, commands fail at once with `DeviceNotConnected`
rather than queueing up, and the device tries again when the interval has
passed. A missing instrument never stops the server.

**State.** `self.set_state(frequency=1e3, mode="AC")` keeps a small dict of
settings worth showing and publishes it on the stream `"state"` whenever a
value changes. Clients read it with `dev.state()`.

**Periodic tasks at run time.** `self.tasks["_poll"].start(0.05)` and
`.stop()` switch a task on and off or change its interval, for example from a
`live(on)` command. `@periodic(0)` means "again as soon as it returns".

**Publishing.** `self.publish(stream, data, **meta)`. The keyword arguments
become the frame's `meta` dict. After publishing an array, do not modify it;
pass `copy=True` if you reuse an acquisition buffer.

**Answering while busy.** `@insthub.command(concurrent=True)` marks a method
that only returns values the device already holds. It is answered immediately,
even during a long sweep. It must not use the instrument.

**Names the base class uses.** Do not reuse these for something else: `name`,
`log`, `open`, `close`, `publish`, `subscribers`, `state`, `set_state`,
`set_meta`, `tasks`, `connected`, `streams`, `link_errors`,
`reconnect_interval`, `open_on_start`, `extension`, `extension_version`,
`simulated`, `poll`, `dangerous`. (A command or property named `poll`,
`simulated`, `extension` or `dangerous` from before still works; the name then
just declares nothing.)

A device class also works without any server: `LockIn().open()` and plain
method calls behave as ordinary Python.

## Putting an existing driver on the bus

If you already have a driver class, you do not need to rewrite it.

```python
from mydriver import PowerSupply
insthub.serve(insthub.wrap(PowerSupply, "psu", kwargs={"port": "/dev/ttyUSB0"}))
```

Every public method becomes a command and every public property becomes a
property. The driver object is created on the device thread when the session
opens and created again after a link error, so reconnecting comes for free.

| Argument of `wrap` | Purpose |
|---|---|
| `args`, `kwargs` | arguments for the driver's constructor |
| `include`, `exclude` | expose only these methods, or all but these |
| `attrs` | plain instance attributes to expose as properties |
| `close` | name of the driver's closing method (default `"close"`) |
| `link_errors` | exception classes that mean the connection broke |
| `lazy=True` | open on first use instead of at server start |
| `accessors=True` | also make a property `X` of each `get_X()` that needs no argument and has a `set_X(value)` beside it; both stay commands. A getter alone is left alone, so a read-and-clear `get_error()` is never read by the window. `X` and `set_X` write one setting, so marking either one dangerous marks both. |
| `meta` | metadata, by property or `"command.parameter"`: `meta={"power": {"unit": "dBm"}, "set_level.dbm": {"min": -40}}` |
| `dangerous` | names of commands and properties that are dangerous, or `True` for every command and every property that can be written; added to what a `Wrapped` subclass marks |
| `poll` | values to read periodically, `{"temperature": 1.0}` |
| `extension`, `extension_version`, `simulated` | what the device is (by default `simulated` is what the driver class says, if it has a `simulated` attribute) |

To add streams or extra commands to such a driver, subclass `insthub.Wrapped`.
The driver object is `self.target`:

```python
class Supply(insthub.Wrapped):
    def __init__(self):
        super().__init__(PowerSupply, "psu", exclude=["write", "query"])

    @insthub.periodic(2.0)
    def _poll(self):
        self.set_state(temperature=self.target.temperature())
```

A subclass says what `wrap()`'s other options say in its own way:
`extension`, `extension_version` and `dangerous` on the class, `accessors`
and `exclude` to `super().__init__()`, and `self.poll`, `self.simulated` and
`self.set_meta(...)` in its constructor, where limits can depend on its
arguments. The reference extension's device,
`examples/extension/demolaser_insthub/device.py`, does all of this.

Or use no code at all, with a config file and `insthub serve lab.toml`:

```toml
[server]
name = "lab"                      # default: the file name
# tcp = "0.0.0.0:8765"            # also reachable from other computers (no authentication)

[devices.psu]                     # the section name is the device name
driver = "mydriver:PowerSupply"   # module:Class; a Device subclass is used as is, anything else is wrapped
path = "~/Code/mydriver"          # optional: where the module lives
lazy = true                       # optional: open on first use
port = "/dev/ttyUSB0"             # every other key is a constructor argument
```

Every key, more examples (VISA, GPIB, several devices) and what to do when it
does not work: [adding an instrument](adding-a-device.md).

## Using a device from another program

```python
import insthub

scope = insthub.device("scope")              # found by name on this computer
awg = insthub.device("awg", "tcp://192.168.1.20:8701")       # or by address

scope.set_timebase(1e-3)                    # waits until the instrument has done it
wf = scope.get_waveform(1)                  # arrays and dataclasses come back as such
synth = insthub.device("synth")
synth.frequency = 2500.0                    # a property
print(scope.bus.help())                     # commands with signatures and docstrings
```

### Not waiting

Every command can be called in three ways:

```python
vpp = scope.measure("VPP")                  # wait for the result
fut = scope.measure.future("VPP")           # returns at once; fut.result() waits later
scope.run.notify()                          # send it and do not ask for a result
```

Futures are how one script uses several instruments at the same time. Requests
to one device run in the order you made them; requests to different devices
run side by side.

```python
image, wf = insthub.gather(camera.grab.future(), scope.get_waveform.future(1), timeout=10)
```

With asyncio, `await scope.measure.aio("VPP")` and `await fut` both work.

### Things on `dev.bus`

Everything that is about the bus rather than the instrument:

| | |
|---|---|
| `dev.bus.help()` | printable list of commands, properties and streams, with units, limits, choices and what is dangerous |
| `dev.bus.describe()` | the same as a dict |
| `dev.bus.status()` | is the instrument connected, what is it doing, last error |
| `dev.bus.state()` | the device's state dict |
| `dev.bus.subscribe(stream, ...)` | see [Streams](#streams) |
| `dev.bus.latest(stream)` | the last published frame, without subscribing |
| `dev.bus.with_timeout(5)` | the same device, but waiting calls give up after 5 s |
| `dev.bus.release()` | close the instrument session, e.g. to use the vendor's software; the next command, or the next run of a periodic task, reopens it (`pause()` keeps it closed) |
| `dev.bus.reopen()` | open the session now |
| `dev.bus.pause()` | refuse its commands and properties (they raise `DevicePaused` at once) and run none of its periodic tasks until `dev.bus.resume()`; returns once the command running now has finished and the session is closed |
| `dev.bus.resume()` | end a pause; the session opens again if it was open before |
| `dev.bus.task(name, enabled, interval)` | switch a periodic task of the device |

Most of these also work directly, as `dev.subscribe(...)`, `dev.state()` and
so on. If a device has its own command with one of these names, the device's
command wins, and the bus version is still there under `dev.bus`.

### Every device at once

To keep with a measurement what every instrument was set to, save
`insthub.states()` next to the data file:

```python
import json

with open("run42-instruments.json", "w") as f:
    json.dump(insthub.states(), f, indent=2, default=str)
```

It asks every server at once: those on this computer, and those on other
computers that `devices.toml` names. Each device gets its server, its type,
its extension, whether it is connected, its state dict and the time it was
read. A device whose server does not answer within 2 s (`timeout=`) gets
`{"server": ..., "error": ...}` instead, so one stuck server does not stop
the rest. Each name is read where `insthub.device(name)` would find it: a
device of this computer before one elsewhere, and a name that two servers
of this computer have, or two elsewhere, gets `{"error": ...}` naming them,
since `insthub.device` refuses to guess too. `insthub.states(["scope", "synth"])` asks only those, and
`insthub.states(address="tcp://labpc:8700")` only the devices of that
server.

`insthub states --json` prints the same from the command line (`insthub -a
ADDRESS states` for one server). It exits with 1 when a device you named, or
every device, could not be read, so a script can tell that what it saved is
not what it meant to save.

## Streams

```python
sub = scope.subscribe("ch1")                # holds the newest frame only
frame = sub.get(timeout=1)                  # wait for the next one
frame = sub.poll()                          # take it if there is one, else None
for frame in sub: ...                       # also `async for`
sub.close()                                 # or use `with scope.subscribe("ch1") as sub:`
```

A frame has `.data`, `.meta` (a dict), `.seq` (running number), `.time`
(when it was published) and `.age` (seconds since then).

| Call | Behaviour |
|---|---|
| `subscribe("ch1")` | newest frame only; a slow reader skips frames. For displays. |
| `subscribe("ch1", queue=1000)` | the newest 1000 in order; nothing is lost while you keep up. For recording. |
| `subscribe("ch1", callback)` | `callback(frame)` is called for each frame on a thread of its own. |
| `subscribe("ch1", retained=False)` | do not start with the frame that was published before you subscribed. |

`sub.received` and `sub.dropped` count what arrived and what was skipped.

A subscription with a large queue to a client that stops reading keeps up to
that many frames in the server's memory, so choose the size with the frame size
in mind.

## Errors, timeouts and restarts

**Errors.** An exception raised by a device arrives as an exception of the same
builtin kind, so `except ValueError:` works for a remote device exactly as for
a local driver. It is also an `insthub.RemoteError`, which carries
`remote_type` and `remote_traceback`; the remote traceback is printed with it.

insthub's own errors:

| Exception | Meaning |
|---|---|
| `DeviceNotFound` | no device of that name |
| `DeviceNotConnected` | the server is up but could not open the instrument |
| `DevicePaused` | somebody paused the device (**Pause**, `insthub pause`, `dev.bus.pause()`); a kind of `DeviceNotConnected`, so catch it first if your script retries on `DeviceNotConnected` |
| `UnknownCommand` | the device has no such command, property, parameter or task |
| `ConnectionLost` | the server cannot be reached or went away before replying |

**Timeouts.** A waiting call waits as long as the device takes. For a limit,
use `dev.with_timeout(5)` or `fut.result(timeout=5)`. This stops the waiting,
not the instrument. Connecting gives up after 5 s;
`insthub.connect(address, connect_timeout=1)` waits less for a server that
may be off.

**Server restarts.** Calls under way fail with `ConnectionLost`. The next call
reconnects by itself, and open subscriptions resume when the server is back. A
request is never run twice.

**Servers of another insthub version.** A server says which insthub version it
runs and what it supports. Before you rely on something new, ask:

```python
client = insthub.connect("tcp://192.168.1.20:8701")
client.info().get("version")                # "0.1.0"; None from a server too old to say
if client.supports("pause"):                # False for a server older than that feature
    client.psu.bus.pause()
```

**A server's log.** Every server keeps the last 1000 lines it logged, and
`client.logs()` reads them, from any computer:

```python
for record in client.logs(50, level="WARNING"):     # the last 50 warnings and errors, oldest first
    print(record["time"], record["logger"], record["message"], record["exc"] or "")
print(insthub.client.log_text(client.logs()))       # as text, as the server printed it
```

`client.logs(since=n)` gives only the lines after the one numbered `n` (each
has its `"seq"`), which is how `insthub logs SERVER -f` follows a log. A
program that hosts a server keeps insthub's own lines, at the level its
logging lets through (warnings and errors unless it sets up logging).
Keeping them changes nothing about where they are printed: a program that
leaves logging alone still sees its warnings on stderr.
`insthub serve`, `insthub sim` and `insthub.serve()` keep everything the
program logs, from INFO up, and also log exceptions that nothing caught,
in any thread (`insthub.server.keep_logs(everything=True)` does that for
a program of your own). A server of an older insthub keeps none, and
`logs()` raises a `BusError` that says so.

## What can be sent

Arguments, results and published data may be:

- `None`, bool, int, float, complex, str, bytes
- list, tuple, dict, set (tuples stay tuples)
- numpy arrays of any non-object dtype, including structured ones; numpy
  scalars arrive as Python numbers
- enums (as their value) and paths (as str)
- dataclass instances

A dataclass arrives as the same class if its module has been imported on the
receiving side, and otherwise as a `Record`, a dict whose keys are also
attributes. Other classes can be registered with `insthub.register_type`.
Anything else is refused on the sending side with a `TypeError`.

Within one program nothing is converted at all: the object itself is handed
over.

## GUIs

A GUI must not freeze while an instrument works, and may only touch its
widgets from the GUI thread. Five helpers in `insthub.qt` take care of both:

```python
from insthub import qt

watcher = qt.watch(scope.subscribe("ch1"), lambda frame: curve.setData(frame.data), fps=30)
qt.deliver(qt.start(scope.measure.future, "VPP"), lambda vpp: label.setText(f"{vpp:.3f} V"))
timebase = qt.coalesce(scope.set_timebase.future)
slider.valueChanged.connect(lambda us: timebase(seconds_per_div=us * 1e-6))
```

- `qt.watch` calls your function in the GUI thread with the newest frame, at
  most `fps` times a second.
- `qt.start(scope.measure.future, "VPP")` starts a call and returns its future
  at once. `scope.measure.future("VPP")` alone also returns at once while the
  server is there, but if the server has gone away, it first tries to
  reconnect, which can take seconds (a computer that is switched off). `qt.start`
  does that on a thread of its own, keeping the order of the calls, so the
  window never waits. When the server cannot be reached, the future fails with
  `ConnectionLost`. The function you give it runs on that thread: give it the
  device's `.future` (or `device.bus.future`), not code that touches widgets.
- `qt.deliver` calls your function in the GUI thread with the result of a call
  when it is ready (and an optional error function if it failed). With
  `owner=widget`, it calls nothing once that widget is deleted, so an answer
  that comes late never reaches a widget that is gone.
- `qt.coalesce` is for a setting that changes faster than the instrument takes
  it, such as a slider being dragged. It keeps one call on its way at a time,
  started as `qt.start` starts one. Values sent meanwhile wait, and only the
  newest of each name goes out next, so the instrument never works through a
  backlog of old values. Its optional functions: `done(result)` for the last
  call (one with nothing waiting behind it), `error(exception)` for each call
  that fails, and `merge(waiting, new)` for names that go together (a new
  center makes a waiting start and stop wrong). `timebase.busy` says whether a
  call is on its way.
- `qt.follow` is `qt.coalesce` for a control that also shows the device's own
  value, such as the timebase the scope reports in its state.

A control that shows the device's own value must not show the old value over
a new one on its way: until the instrument has taken it, it may still report
the old one. Nor may it show the value that was typed: an instrument that
rounds what it is sent (to 1-2-5 steps, say) or refuses it has another.
`qt.follow` takes care of that. Give it the call and the function that shows a
value, and feed it what the device reports:

```python
def show(seconds):
    box.blockSignals(True)                # (showing a value is not sending it)
    box.setValue(seconds)
    box.blockSignals(False)

timebase = qt.follow(scope.set_timebase.future, show, error=lambda exc: label.setText(f"failed: {exc}"))
box.valueChanged.connect(lambda seconds: timebase(seconds_per_div=seconds))
watcher = qt.watch(scope.subscribe("state"), lambda frame: timebase.report(frame.data["timebase"]))
```

It shows what the device reports while no call is on its way, and keeps what
it reports meanwhile. When the last call ends, it shows what the device took
(the call's result, if it returns one, as `set_timebase` does; else what it
reported last), or, if the call failed, what it reported last. A value that
the device only reads now and then (with `poll`) may be read just before it
takes the new value, and that old report then comes in with the answer. Pass
the subscription as `reports=`, and that report is dropped instead of being
shown a moment after the answer. `insthub/sim_ui.py` follows its timebase this
way, and the demo laser's panel,
`examples/extension/demolaser_insthub/ui/panel.py`, its power, with
`reports=`.

A GUI can live in the server's program or in a separate one; the code is the
same. In the server's program, use `server.device("scope")` and frames arrive
without being copied. See `examples/04_server_with_gui.py`.

`insthub view DEVICE` is a ready-made viewer built on these helpers: one live
panel per stream (traces, images, strip charts for numbers) and a table of the
device's state.

### Writing a panel

A *panel* is the window of one device, written once and shown wherever it is
needed. It is a function that makes a widget:

```python
def make(device, parent=None) -> QWidget:
    ...
```

| The panel... | How |
|---|---|
| gets its device | as `device`, an ordinary proxy: the same thing `insthub.device("scope")` returns |
| never freezes the window | streams through `qt.watch`, calls started with `qt.start(device.command.future, ...)`, results through `qt.deliver(..., owner=the panel)`, so a panel closed inside a larger app gets no late answers |
| sends a setting as fast as the instrument takes it | `qt.coalesce` for a slider, a spin box's arrows or a wheel: one call at a time, with the newest values; `qt.follow` when the control also shows the device's value |
| shows the rest without code of its own | `controls.Controls(device, exclude=[...])` (see below) |
| reaches the instrument only through the bus | commands, properties and streams, so a script or an agent can do all it does |
| stops what it watches when it closes | in its widget's `closeEvent`: `watcher.stop()` for each watcher |
| never disagrees with the device | it shows what the device reports, also after a call it rounded or refused (`qt.follow`, above); the limits of a command's parameters (`min`, `max`, `unit`) come from `device.bus.describe(refresh=False)`, which the proxy already has, so reading them makes no call |

A panel often shows a few things in a way of its own (a trace with markers,
an image) and needs plain controls for the rest. `insthub.controls` has them,
made from what the device says about itself. They are the **Settings** and
**Commands** tabs of the window's device page:

```python
from insthub.controls import Controls

layout.addWidget(Controls(device, exclude=["live", "set_timebase"]))  # what the panel shows itself
```

| Name | What it is |
|---|---|
| `Controls(device, include=None, exclude=None)` | both, in two tabs; a tab with nothing to show is hidden |
| `Settings(device, include, exclude)` | the properties alone: read when shown and on **Reload**, each changed with a list, a spin box with its unit, a switch or a text field |
| `Commands(device, include, exclude)` | the commands alone, and a line to run one with arguments as on the command line |

`include` keeps only the names it lists, in its order; `exclude` leaves names
out (each takes a list of names, or one name). Writing a property or running a
command that the device marks dangerous asks first. The controls start their
calls with `qt.start`, so they never freeze the panel, also when the server has
gone away, and they say an error in red under the setting or command it is
about, in your program too. The controls show the device as it was when they
were made; call `load(device.bus.describe())` on them to show it as it is now,
for example after it was paused. `Settings` emits `changed(name, value)` once a
value is set.

`insthub/sim_ui.py` is a complete example: insthub's own panel for its
simulated scope (two traces, the timebase from 1 ns to 10 s per division as
the device takes it, sent through `qt.follow` and shown as the scope took
it, live on or off, a measurement, and the scope's other commands as
`Controls`). The reference extension's panel,
`examples/extension/demolaser_insthub/ui/panel.py`, is a smaller one: a
laser's power as a slider, its temperature, and `Controls` for the rest. To
try a panel, run it in a window of its own:

```bash
insthub [-a ADDRESS] panel scope insthub.sim_ui:scope [--title TITLE]
```

or from Python:

```python
import insthub
from insthub import qt, sim_ui

window = qt.device_window(sim_ui.scope, insthub.device("scope"), "Scope panel")
window.show()
qt.run()
```

`insthub panel` imports the panel's module before `insthub.qt`, so the Qt
binding the panel imports (PyQt6, PySide6, ...) is the one used. Closing the
window closes the panel's widget. An extension's manifest names its panels
(see [extensions](#extensions)), and insthub then starts them with
`insthub panel`, in the extension's Python.

An *app* is a whole program instead, for a device whose UI is a full
application. It takes the device's address and name (`--address ADDRESS
--name NAME`, as its manifest says), and when it is given an address it only
attaches to that device, so it never starts a second server for the same
instrument. Without an address, it may host the device itself when no server
runs, importing the device's code only then.
`examples/extension/demolaser_insthub/ui/__main__.py` does both.

### The manager window

`insthub gui` opens a window with four pages, in its sidebar: **Devices**,
**Servers**, **AI agents** and **Settings**. The sidebar also counts the
devices and servers, and marks the ones that are broken.

**Devices** shows every device as a tile, in a section per server: the
servers on this computer, the installed services that are not running, and
the servers of other computers listed in `devices.toml`. A tile says whether
its instrument is connected (connected, busy, not opened yet, released, paused
or pausing, or not connected and why), and under its name its class and where
it is (its address, from the arguments it was made with, such as
`10.108.57.16` or `GPIB0::18`). It shows a small live preview of what the device publishes: a trace, an
image or a number. A device that publishes nothing shows the figures of its
state instead, and one with neither its `description` from the config (else
its driver's docstring). The preview is the stream's last frame, fetched once a second,
so it never makes an instrument acquire: a scope that only reads out while
somebody watches stays idle until something does. The search box filters by
device, class, address, description or server.

Click a tile to see whether its instrument is connected and since when, what
it is doing, and which programs are watching it. Its tabs show:

- **Settings**: its properties, read from the instrument when the page opens
  (not while it is paused), with their units and limits.
  A settable one is changed in place, with the control its metadata asks for:
  a list for one of its choices, a spin box with its unit for a number, a
  switch for one that is on or off, and otherwise a text field, which also
  takes values like `auto`.
- **Streams**: how fast each one arrives.
- **Periodic tasks**: a switch each.
- **State**: what the device published as its state.
- **Commands**: run one with arguments typed as on the command line
  (`VPP channel=2`, `[1, 2]`), and see what it returned. The units, limits and
  choices of its parameters are shown under it.

Writing a property or running a command that the device marks dangerous asks
you first. A simulated device has a SIM mark on its tile and on its page, and
the page shows the extension the device belongs to. The Settings and Commands
tabs are the same controls that a [panel](#writing-a-panel) can show.

Its buttons open the instrument session now (**Connect now**), close it for
other software (**Release**), or open its live plots (**Live view**).
**Pause** closes the session and keeps everything off the instrument (every
program's commands fail with `DevicePaused`) until **Resume**; the page says
who paused it and since when. **⋯ → Edit in lab.toml...** (named after the
server's config file) changes its driver, arguments or **Open at start** there.
A device whose [extension](#extensions) has a UI for it gets a button to open
it, such as **Open Scope panel** for the scope of `insthub sim`, or one
**Open** button with a menu of them when it has several; a right-click on its
tile offers them too. Each opens in a program of its own, run with the
extension's Python. If the extension is not on this computer, its page says so:
"extension hp8562a is not installed on this computer: `insthub ext add
FOLDER`". The live view and these windows write their output to a log file; if
one ends with an error, the window shows its last lines.

**Servers** shows a card per server. Open one to see its process, its
addresses, its service, its config file, its devices and the programs
connected to it. A server that runs older code than its folders hold (you
edited a driver or updated insthub after it started) is marked **OUT OF
DATE** on its card and above its devices. Its page says what changed, with a
**Restart** link when the window can restart it (a service, or a server the
window started), and its devices' pages say it too (see [after changing
code](running-servers.md#after-changing-code)). Such a server, where the
window can restart it, and a service whose unit an older insthub wrote (its
page says what is old about it) get **Update**, beside **Restart** and in
their card's menu, and the Servers page then has **Update all**, which asks
once and says what restarts (see [updating](running-servers.md#updating)).
The window also says when it runs older insthub code itself, in its sidebar
and on its Settings page. A service can be started, stopped, restarted and
removed there, and **Starts at login** switched on or off; its page says how
often systemd restarted it after a crash. A server started by hand can be
stopped. **Logs** shows what a server logged: a service's journal, the
output of a server the window started, or, for any other server (one
started in a terminal, one on another computer), the last lines it keeps
itself. **⋯ → Edit lab.toml...** changes its config
file in a form: only what you change is written, the file's comments stay,
and **Save and restart** makes the server use it. The **Add a device** tile
after its devices opens the same form with a new device in it (for a server
that runs from a `.toml` file).

**Add** (on the Devices and Servers pages) sets things up without editing
files by hand:

- **New server...** builds the config of a new server. You choose its devices:
  a driver for each (one from the list, `module:Class`, or a `.py` file to pick
  a class from) and the arguments of its constructor. Those arguments are read
  from the driver's source without running it, with their defaults shown. The
  list offers the devices of your [extensions](#extensions), insthub's own
  simulated instruments among them: choosing one fills in its driver, its
  folder and the keys its manifest gives, and the extension's Python to run
  the server. It writes `NAME.toml`
  (in `~/.config/insthub/servers` unless you choose another folder) and starts
  the server as a background service, or once, or not at all.
- **Add a device to a server...** adds an instrument to a server that runs
  from a `.toml` file (it asks which, if there are several), then restarts it.
- **Keep a server running...** installs an existing `.toml` or `.py` server as
  a service.
- **Devices on another computer...** asks a server there which devices it has
  and lists them in `devices.toml`.
- **Simulated instruments** starts `insthub sim`.

**AI agents** lets an AI agent such as Claude Code use the instruments (see
[agents](agents.md)); a connected agent is listed as "AI agent (Claude Code)"
and counted in the sidebar. **Settings** chooses a light or dark look (or the
desktop's), turns the previews off, and shows the files insthub uses and your
[extensions](#extensions): where each was found, and what is wrong with one
that cannot be used. **Add folder...** and **Remove** change your list of them.

The window is an ordinary client: it needs only Qt, never waits for a server,
and can be closed and reopened at any time without affecting anything.

Closing the window does not stop it: it goes on watching from an icon in the
tray (the top bar). Its lower-right node is a status light: green while all is
well, red while something is broken. Its menu
lists the servers, and its **Quit** closes the window for good. Your servers
are separate programs and keep running either way. When a device loses its
instrument, a server stops answering or a service fails, a notification says
so; click it to see what broke. A notification also says when an AI agent
starts using the instruments. The tray menu can turn notifications off, and
**Start at login** starts the window in the tray each time you log in
(`insthub gui --tray`). Starting `insthub gui` while it already runs brings up
the window that is there. Desktops without a tray (GNOME without its
AppIndicator extension) close the window as usual.

To start it from an icon instead of a terminal, run once:

```bash
insthub gui --install          # applications menu and desktop; --no-desktop for the menu only
```

The launcher runs the window with the Python that installed it. On GNOME,
right-click its icon in the dock and pin it to keep it there.
`insthub gui --uninstall` removes it again.

## Extensions

An *extension* is a folder that adds an instrument to insthub: its driver,
its device, and its own windows if it has some. One small file in it,
`insthub-extension.toml` (its *manifest*), says what it adds. insthub reads
the manifest as data and never runs it, so listing extensions loads no vendor
library. [Design](design.md#extensions) explains the model; this is how to
use it. [`examples/extension/`](../examples/extension) is a complete one to
copy: a simulated laser, with a panel and an app, which runs without hardware.

| To | Do |
|---|---|
| see the extensions this computer knows, and what is wrong with them | `insthub ext list`, or **Settings** in the window |
| add one you cloned | `insthub ext add ~/Code/hp8562a`, or **Settings → Add folder...** |
| take it off your list | `insthub ext remove ~/Code/hp8562a`, or **Remove** next to it |
| check one you are writing | `insthub check ~/Code/hp8562a`; with `--import`, its drivers (and the classes their `link_errors` name) are imported too, in its Python |
| check the extensions of a server file | `insthub check lab.toml`; with `--import`, also what `insthub service install` checks |
| make a server with its devices | **Add → New server...**: they are in the driver list |
| open a device's UI | `insthub open scope` (with a name when there are several: `insthub open analyzer quick`), or **Open ...** on the device's page |

insthub looks for extensions in this order: its own (the simulated
instruments of `insthub sim`), the folders of your list
(`~/.config/insthub/extensions.toml`), the folders the installed services'
code comes from, and the folders of the devices of the servers running on this
computer. Of two extensions with one `id` the first that can be used wins,
and `insthub ext list` says which one it hides. One that cannot be used (its
manifest is not valid TOML, its `api` is too new, it has no Python) is listed
with the reason, and hides nothing. To try an extension without adding it,
`INSTHUB_EXTENSIONS=~/Code/hp8562a insthub gui` looks only there, besides
insthub's own extension (folders separated by `:`, or `;` on Windows).

A manifest:

```toml
[extension]
id = "hp8562a"                       # what its devices announce, as extension = "hp8562a"
title = "HP 8562A spectrum analyzer"
version = "0.3"                      # warned about when its class says another extension_version
api = 1                              # the version of this file's format
# python = ".venv/bin/python"        # default: a .venv in this folder or the one above

[devices.analyzer]                   # a device New server offers
driver = "hp8562a_insthub.device:SpectrumAnalyzer"
doc = "HP 8562A over GPIB"           # shown in the list
pad = 18                             # every other key goes into the server file as it is

[ui.analyzer]                        # an app (run) or a panel (panel = "module:make")
title = "Spectrum analyzer"
run = ["-m", "hp8562a_insthub.ui", "--address", "{address}", "--name", "{device}"]
# types = ["HP8562A", "SimHP8562A"]  # only for devices of these types (default: every device of it)
```

New server writes a device's table with two keys added: `path`, the
extension's folder, and `extension`, its id. It runs the server with the
extension's Python, unless you chose one yourself. A server runs in one
Python, so if the extensions of two of its devices run in different ones,
the dialog says so: give them a server each. If you type another driver in
place of the one chosen, the keys the choice brought go again. A manifest
never says `path`, `extension` or `extension_version`, and `insthub check`
reports them.

A `[ui.NAME]` table is a window for the extension's devices: an app, started
as `<the extension's Python> <run...>` with `{device}` and `{address}`
filled in, or a [panel](#writing-a-panel), started as `<the extension's
Python> -m insthub -a ADDRESS panel DEVICE MODULE:FUNCTION`. Either runs in
the extension's folder, which is also first on its `PYTHONPATH`, so a plain
folder works without being installed. A UI is offered for every device that
announces the extension's `id`, or with `types` only for the devices whose
type (as `insthub info` shows it: the class name) is listed. For a device on
this computer, the extension is found from the device's own folder, so
**Open** starts the copy of the extension that its server runs; for one on
another computer, from your extensions. insthub runs nothing of an
extension whose manifest, folder or Python another user may change (see
[the design](design.md#the-manifest)). Its output goes to
`ui/DEVICE/NAME.log` in insthub's runtime folder. `insthub open` waits 3
seconds and, if it ended with an error by then, prints the log's last lines.

## Command line

```
insthub list [--json]                          servers and devices on this computer, and on the computers
                                               devices.toml names; which servers here run older code
insthub states [DEVICE ...] [--json]           what every device is like now (to save next to a data file);
                                               with -a, one server's; exit code 1 if what you asked for is missing
insthub logs SERVER [-n N] [--level L] [-f]    what a server logged, from any computer; -f follows it
insthub info DEVICE                            commands, properties, streams, status; units, limits, choices,
                                               what is dangerous, and whether it is simulated
insthub call DEVICE COMMAND [ARG ...] [KEY=VALUE ...] [--save FILE]
insthub get DEVICE PROPERTY
insthub set DEVICE PROPERTY VALUE
insthub watch DEVICE [STREAM]                  print frames as they arrive
insthub release DEVICE                         close the instrument session, e.g. for the vendor's software
insthub reopen DEVICE                          open it again now (any command does so too)
insthub pause DEVICE                           close it and refuse every command until resumed
insthub resume DEVICE                          end the pause
insthub view DEVICE                            live plots
insthub open DEVICE [UI]                       a window its extension provides for it (an app or a panel)
insthub panel DEVICE MODULE:FUNCTION           a panel in a window of its own, to try one
insthub gui [--tray | --install]               the manager window (stays in the tray); --install adds an icon
insthub serve CONFIG.toml                      host the devices of a config file
insthub ext list [--json]                      the extensions this computer knows of, and what is wrong with them
insthub ext add PATH | ext remove PATH         add an extension's folder to your list, or take it off
insthub check PATH [--import]                  what is wrong with an extension, or with a server file's extensions;
                                               --import also imports the drivers and the UIs, in its Python
insthub service install | list | start | stop | restart | enable | disable | remove | logs
                                               keep servers running in the background
insthub service update [NAME ...] [--check]    after a git pull or an edited driver: write old units anew,
                                               restart the services that run older code; --check only says
                                               what it would do (exit code 1 if anything would change)
insthub sim [--gui]                            simulated instruments to try things on
insthub bench                                  speed on this computer
insthub mcp                                    the bus for AI agents, see agents.md
```

Values are read as JSON where possible (`1e3`, `true`, `[1, 2]`) and as text
otherwise. Add `-a ADDRESS` before the command (`insthub -a tcp://labpc:8700 info
scope`) to talk to a server by address instead of by name.
