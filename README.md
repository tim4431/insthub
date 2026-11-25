# insthub

insthub connects the programs of an experiment. For each instrument, one small
program (a *server*) holds the connection to the hardware. Experiment scripts,
notebooks and live displays ask that program instead of opening the instrument
themselves, so several of them can use it at once without getting in each
other's way.

```
   your programs                            a server                        instruments

 ┌───────────────────┐                 ┌─────────────────────────┐
 │ experiment script │ ── commands ──▶ │ scope device  (thread) ─┼──▶ oscilloscope
 │ notebook          │ ◀── results ─── │                         │
 │ live plot         │ ◀── streams ─── │ synth device  (thread) ─┼──▶ signal generator
 └───────────────────┘                 └─────────────────────────┘
```

What you get:

- **A remote instrument reads like the local driver.** `scope.measure("VPP")`
  and `synth.frequency = 2500` work from any program. Errors arrive as the
  exceptions the driver raised.
- **No waiting unless you want to.** Every call can also return at once and
  deliver its result later, so one script can work with several instruments at
  the same time.
- **Live data.** A device can publish traces, images and settings as they
  change. A display always gets the newest one; a recorder can get all of them.
- **Speed.** A call between two programs costs about 12 millionths of a second,
  and arrays move at about 10 GB/s because they are sent straight from memory.
- **Your drivers stay as they are.** An existing driver class goes on the bus
  with one line of Python or four lines of config.
- **Nothing else to run.** There is no central service. Each server stands
  alone, and insthub knows nothing about any particular instrument.
- **A window to keep an eye on it all.** `insthub gui` shows every server and
  device, whether each instrument is connected and which programs are using
  it, with buttons to reconnect, release, restart and read logs. Closed, it
  keeps watching from the tray and tells you when something breaks.

It needs Python 3.11 or later, `numpy` and `msgpack`. Live plotting also needs
`pyqtgraph` and a Qt binding.

```bash
pip install "insthub @ git+https://github.com/tim4431/insthub"          # into the environment of each server and client
pip install "insthub[gui] @ git+https://github.com/tim4431/insthub"     # with live plotting
pip install -e /path/to/insthub                                          # or from a local checkout, to work on it
```

## Try it without hardware

```bash
insthub sim                          # terminal 1: simulated scope, generator, camera and spectrum analyzer
```

```bash
insthub list                         # terminal 2: what is running
insthub call scope measure VPP
insthub set synth frequency 2500
insthub watch scope ch1              # the live trace, as text
insthub view scope                   # the live trace, as a plot
insthub gui                          # every server and device, and how each is connected
python examples/03_experiment.py    # a script that uses several at once
```

## Let an AI agent use the instruments

```bash
claude mcp add --scope user insthub -- "$(which insthub)" mcp
```

Claude Code (or any agent that speaks MCP) can then list the instruments,
read their documentation from your docstrings, call commands, set properties
and take live traces, with large arrays handed over as files. See
[using insthub from AI agents](docs/agents.md).

## The smallest example

A server, in one file:

```python
import insthub

class Thermometer(insthub.Device):
    def open(self):                          # connect to the instrument here
        self.offset = 0.0

    @insthub.command
    def read(self) -> float:
        """Temperature in kelvin."""
        return 293.15 + self.offset

    @insthub.periodic(1.0)                    # once a second, by itself
    def _poll(self):
        self.publish("temperature", self.read())

insthub.serve(Thermometer("thermo"))
```

Any other program on the same computer:

```python
import insthub

thermo = insthub.device("thermo")             # found by name
print(thermo.read())                         # ask and wait
future = thermo.read.future()                # ask, and collect the answer later
for frame in thermo.subscribe("temperature"):
    print(frame.data)                        # arrives by itself, once a second
```

## Documentation

| Read this | To learn |
|---|---|
| [How insthub works](docs/architecture.md) | the pieces, what happens during a call and a stream, how programs find each other, what happens when things break, and why it is built this way |
| [Using insthub](docs/guide.md) | writing devices, putting existing drivers on the bus, calls, streams, errors, GUIs, the command line |
| [Running servers](docs/running-servers.md) | starting servers, keeping them running in the background, who sets up what, several computers |
| [AI agents](docs/agents.md) | letting Claude Code or another agent use the instruments: setup, the tools, what comes back |
| [examples/](examples/) | four runnable examples, all on simulated instruments |

## Status

- 200 tests cover calls, streams, device lifecycle, finding servers,
  reconnecting, the command line, services, the GUIs and the agent interface.
  Run them with `python -m pytest tests` (about 17 s).
- Developed and tested on Linux with Python 3.11, 3.12 and 3.14. Written to
  run on Windows, but never run there.
- Not included: passwords or encryption for network connections, clients for
  languages other than Python, and a way to cancel a command that an
  instrument is already carrying out.

## License

MIT, see [LICENSE](LICENSE).

## Layout

```
insthub/
  device.py     Device, @command, @prop, @periodic, wrap; the per-device thread
  server.py     Server; connections; sending without ever waiting for a client
  client.py     connect, device; proxies, futures, subscriptions, reconnecting
  codec.py      what can be sent, and how it is packed
  wire.py       the layout of a message on a connection
  registry.py   finding servers by name
  config.py     servers from a TOML file
  service.py    insthub service: background services
  qt.py         helpers for Qt GUIs             viewer.py   the generic live viewer
  mcp.py        insthub mcp: the bus for AI agents
  render.py     results as JSON plus files and plots, for agents
  manager.py    insthub gui: the manager window and its tray icon
  launcher.py   the launcher icon of insthub gui
  sim.py        simulated instruments           bench.py    insthub bench
  cli.py        the insthub command
docs/           the pages above
examples/       runnable examples
tests/
```
