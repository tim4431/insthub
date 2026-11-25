# Using insthub

How to put an instrument on the bus and how to use it from other programs.
For what happens underneath, read [how insthub works](architecture.md).

- [Writing a device](#writing-a-device)
- [Putting an existing driver on the bus](#putting-an-existing-driver-on-the-bus)
- [Using a device from another program](#using-a-device-from-another-program)
- [Streams](#streams)
- [Errors, timeouts and restarts](#errors-timeouts-and-restarts)
- [What can be sent](#what-can-be-sent)
- [GUIs](#guis)
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
`tasks`, `connected`, `streams`, `link_errors`, `reconnect_interval`,
`open_on_start`.

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
| `dev.bus.help()` | printable list of commands, properties and streams |
| `dev.bus.describe()` | the same as a dict |
| `dev.bus.status()` | is the instrument connected, what is it doing, last error |
| `dev.bus.state()` | the device's state dict |
| `dev.bus.subscribe(stream, ...)` | see [Streams](#streams) |
| `dev.bus.latest(stream)` | the last published frame, without subscribing |
| `dev.bus.with_timeout(5)` | the same device, but waiting calls give up after 5 s |
| `dev.bus.release()` | close the instrument session, e.g. to use the vendor's software; the next command reopens it |
| `dev.bus.reopen()` | open the session now |
| `dev.bus.task(name, enabled, interval)` | switch a periodic task of the device |

Most of these also work directly, as `dev.subscribe(...)`, `dev.state()` and
so on. If a device has its own command with one of these names, the device's
command wins, and the bus version is still there under `dev.bus`.

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
| `UnknownCommand` | the device has no such command, property or task |
| `ConnectionLost` | the server cannot be reached or went away before replying |

**Timeouts.** A waiting call waits as long as the device takes. For a limit,
use `dev.with_timeout(5)` or `fut.result(timeout=5)`. This stops the waiting,
not the instrument.

**Server restarts.** Calls under way fail with `ConnectionLost`. The next call
reconnects by itself, and open subscriptions resume when the server is back. A
request is never run twice.

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
widgets from the GUI thread. Two helpers in `insthub.qt` take care of both:

```python
from insthub import qt

watcher = qt.watch(scope.subscribe("ch1"), lambda frame: curve.setData(frame.data), fps=30)
qt.deliver(scope.measure.future("VPP"), lambda vpp: label.setText(f"{vpp:.3f} V"))
```

- `qt.watch` calls your function in the GUI thread with the newest frame, at
  most `fps` times a second.
- `qt.deliver` calls your function in the GUI thread with the result of a call
  when it is ready (and an optional error function if it failed).

A GUI can live in the server's program or in a separate one; the code is the
same. In the server's program, use `server.device("scope")` and frames arrive
without being copied. See `examples/04_server_with_gui.py`.

`insthub view DEVICE` is a ready-made viewer built on these helpers: one live
panel per stream (traces, images, strip charts for numbers) and a table of the
device's state.

## Command line

```
insthub list                                   servers and devices on this computer
insthub info DEVICE                            commands, properties, streams, status
insthub call DEVICE COMMAND [ARG ...] [KEY=VALUE ...] [--save FILE]
insthub get DEVICE PROPERTY
insthub set DEVICE PROPERTY VALUE
insthub watch DEVICE [STREAM]                  print frames as they arrive
insthub release DEVICE                         close the instrument session, e.g. for the vendor's software
insthub reopen DEVICE                          open it again now (any command does so too)
insthub view DEVICE                            live plots
insthub serve CONFIG.toml                      host the devices of a config file
insthub service install | list | remove | logs keep servers running in the background
insthub sim [--gui]                            simulated instruments to try things on
insthub bench                                  speed on this computer
```

Values are read as JSON where possible (`1e3`, `true`, `[1, 2]`) and as text
otherwise. Add `-a ADDRESS` to talk to a server by address instead of by name.
