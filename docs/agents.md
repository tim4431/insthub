# Using insthub from AI agents

An AI agent such as Claude Code can use the instruments on the bus directly.
It can, for example, switch on a tone on the signal generator, take a trace on
the spectrum analyzer that is cabled to it, look at the result and change a
setting, the way you would while debugging.

`insthub mcp` makes this possible. It is a small program that the agent starts
and talks to through the [Model Context Protocol](https://modelcontextprotocol.io)
(MCP), the standard way for agents to use outside tools. For insthub it is
just another client, like an experiment script: the servers and drivers do not
change and do not know it exists.

```
   agent (Claude Code, ...)
        │  MCP, on stdin/stdout
   insthub mcp        six tools, results made cheap to read
        │  ordinary insthub calls
   servers            unchanged ──▶ instruments
```

It needs nothing beyond insthub. Plots additionally need matplotlib
(`pip install "insthub[mcp] @ ..."` or just `pip install matplotlib`).

## Setting it up

Install insthub as a command of your own, then register it with the agent
once. For Claude Code:

```bash
uv tool install --editable "/path/to/insthub[gui,mcp]"  # gui: the manager window too (needs Qt)
claude mcp add --scope user insthub -- insthub mcp
```

- **The command of your own.** `uv tool install` gives insthub a private
  environment and puts the `insthub` command in `~/.local/bin`, so the agent
  can start it by name from any folder. `--editable` makes it run the code of
  your checkout, the same as your servers. `[mcp]` adds matplotlib for plots.
  `insthub mcp` needs no driver, because it gets everything from the servers.
  See [the insthub command for yourself](running-servers.md#the-insthub-command-for-yourself).
- **If the agent cannot find it.** The agent starts `insthub` with the
  `PATH` of the environment it was itself started from. A desktop launcher or
  an ssh session may not include `~/.local/bin`, and then `claude mcp list`
  reports a failure. Register the full path instead:
  `claude mcp add --scope user insthub -- ~/.local/bin/insthub mcp`.
- **Without the command of your own,** register the full path of the
  `insthub` in any virtual environment that has insthub
  (`/path/to/venv/bin/insthub mcp`). An activated environment does not help,
  because the agent does not start it from your shell.
- **Same user account.** Run the agent under the same account as the
  servers, because the folder through which servers are found belongs to
  that account.
- **Scope.** `--scope user` makes the tools available in every project on
  this computer. To share the setup with everyone who works in one repository
  instead, commit a `.mcp.json` file at its top:

  ```json
  {"mcpServers": {"insthub": {"command": "insthub", "args": ["mcp"]}}}
  ```

  Everyone who uses it then needs the `insthub` command of their own.

- **Check it.** `claude mcp list` shows whether it connects, and `/mcp`
  inside a session shows its tools.

Other MCP clients (Claude Desktop, other agents) take the same command and
arguments in their own configuration.

The manager window does the same with a click: in `insthub gui`, the **AI
agents** page shows the command and the JSON for this installation, with the
full path filled in, and sets it up for Claude Code (or removes it again).

Options of `insthub mcp`:

| Option | Default | Meaning |
|---|---|---|
| `--data DIR` | `$INSTHUB_DATA`, else `.insthub/` in the folder the agent was started in | where results too large to return inline are saved |
| `--timeout S` | 60 | how long a call may take before the agent is told it is still running; the agent can ask for longer per call |

## Seeing what an agent does

An agent's `insthub mcp` is an ordinary client, so `insthub gui` shows it like
any other program, named after the agent that started it: "AI agent (Claude
Code)" among the programs connected to a server, and among the watchers of the
streams it takes snapshots of. The sidebar counts the agents connected, the
tray menu lists them, and the tray announces each new one ("An AI agent is
using the instruments"). This is how to notice that an agent is acting on
real hardware while you are doing something else.

## How the agent finds the instruments

There are two steps, and you only set up the first.

1. **The agent finds insthub** because it is registered, as above. From then
   on the tools are in every session, together with a short paragraph that
   tells the agent what insthub is and where to start. You do not need to
   explain it in your prompt.
2. **insthub finds the instruments** the same way every client does: the
   servers running on this computer, plus the devices on other computers
   listed in `devices.toml` (see [running servers](running-servers.md#reaching-a-server-from-another-computer)).
   A server you start later is visible at the agent's next call.

The agent's knowledge of each instrument is whatever `describe` returns: the
signatures and docstrings of your commands and properties, the descriptions
of your streams, and the state dict. Your docstrings are the agent's manual;
see [writing devices that agents use well](#writing-devices-that-agents-use-well).

## The tools

There are six, whatever the number of instruments, because every device
describes itself. There is no separate tool for each command.

| Tool | Does | Like |
|---|---|---|
| `list_devices()` | every device: server, type, connected or not, names of its commands, properties and streams | `insthub list` |
| `describe(device)` | signature and docstring of each command, properties (settable or read-only), streams with their last frame, periodic tasks, status, state | `insthub info` |
| `get(device, property)` | read a property | `dev.power` |
| `set(device, property, value)` | write a property | `dev.power = -10` |
| `call(device, command, args, kwargs)` | run a command; also the bus commands `$release`, `$open` and `$task` | `dev.measure("VPP", channel=2)` |
| `snapshot(device, stream, fresh)` | one frame of a stream, published after the call started | `dev.subscribe("trace").get()` |

`get`, `call` and `snapshot` take `plot=true` to add a picture of the result,
and `get`, `set` and `call` take a `timeout` in seconds.

Errors come back as the driver raised them, with the last lines of the
server's traceback, so the agent can see both what went wrong and where in
your driver:

```
ValueError: frequency 30000.0 MHz outside 10.0-24000.0 MHz
--- remote traceback (last lines) ---
  File ".../synth.py", line 146, in frequency
    raise ValueError(f"frequency {mhz} MHz outside {lo}-{hi} MHz")
```

## What comes back

Results are JSON. Bulk data is the exception: an agent reads every number
it is given, so an array with more than 64 elements, a long text or a byte
string is saved to a file and replaced by a summary. A 1001-point spectrum
comes back as:

```json
{"stream": "trace", "seq": 412, "age_s": 0.03,
 "meta": {"x0": 995000000.0, "dx": 10000.0, "xunit": "Hz", "xlabel": "frequency", "unit": "dBm"},
 "data": {"array": "float32[1001]", "min": -92.98, "max": -20.0,
          "file": "/home/me/lab/.insthub/20261124-190552.067-sa-trace.npy"}}
```

The summary costs the same whether the trace has a thousand points or ten
million. When the agent needs the numbers, to find a peak, fit a line or
compare two traces, it loads the file with `numpy.load` in its own Python.

| Value | Comes back as |
|---|---|
| numbers, text, `None`, `True` | themselves |
| lists, tuples, dicts, dataclasses | JSON lists and objects, converted element by element |
| arrays of up to 64 elements, and short lists of numbers | lists |
| larger arrays, and long lists of numbers | `{"array": "dtype[shape]", "min", "max", "file"}`, saved as `.npy` |
| bytes | `"b'OK\n'"` when short, else `{"bytes": n, "file"}` saved as `.bin` |
| text longer than 4000 characters | `{"text": "str[n]", "start", "file"}` saved as `.txt` |
| complex numbers | `"(1+2j)"` |

With `plot=true` the arrays in the result are also drawn as one small picture
(640×360 pixels per panel), which the agent sees. Traces share one plot, and images
(2-D arrays) get panels of their own. The x axis comes from the same
conventions `insthub view` uses: `t0`/`dt`, or `x0`/`dx` with
`xlabel`/`xunit`, and `unit` for the values. They are taken from a stream
frame's meta, or from the fields next to the array in a returned dataclass or
dict. The picture is also saved next to the data, so you can look at what the
agent looked at.

Files are never deleted by insthub. Clear the data folder when you like, and
add `.insthub/` to the `.gitignore` of projects where the agent runs.

## Live data: `snapshot` and `fresh`

When a device publishes a stream, such as a live trace or camera frames,
`snapshot` returns one frame of it. The point to get right is *which* frame.
After the agent changes a setting, the newest frame may have been taken
before the change. So by default (`fresh=1`) `snapshot` waits for the first
frame published after the call started.

| `fresh` | Returns |
|---|---|
| 0 | the last frame already published, however old (its `age_s` says how old); waits only if there was none |
| 1 (default) | the first frame published after the call started |
| N | the N-th. Use 2 when an acquisition may already have been under way at the moment of the change |

`snapshot` always subscribes, rather than reading the last frame the server
remembers. Many devices acquire only while somebody is subscribed, because a
well-written periodic task skips the readout while nobody watches. Their
remembered frame may date from when the last viewer closed.

If no frame arrives within `timeout` (10 s), the agent is told what it would
need to know: whether the stream has ever published, and how old its last
frame is. A stream name that does not exist is refused with the list of
streams the device has.

## Passing data to a device

To send an array to a device, for example a waveform for an arbitrary
waveform generator, the agent saves it to a file and passes
`{"$file": "/path/to/wave.npy"}` in place of the value, in `args`, `kwargs`
or `set`. The device receives the array.

| File | Device receives |
|---|---|
| `.npy` | the array |
| `.npz` | the array named by `"key"`: `{"$file": "x.npz", "key": "volts"}` |
| `.txt` | the text |
| anything else | the bytes |

The files a previous result pointed to work too, so a trace taken from one
instrument can be handed to another without passing through the agent's
context.

## Writing devices that agents use well

Everything the agent knows about an instrument comes from the device itself.
The same things make a device pleasant to use from a notebook:

- **Docstrings with units and ranges.** `"""Output frequency in MHz (10 to
  24000)."""` tells the agent everything it needs. `"""Set the frequency."""`
  leaves it guessing between Hz and MHz.
- **Type hints.** They appear in the signatures the agent reads:
  `(hz: float, noise: float | None = None) -> None`.
- **Check values in the driver** and raise a `ValueError` that says what is
  allowed. The agent reads the message and corrects itself, and the check
  protects the instrument from every client, not only from agents.
- **Declare your streams**, with a description
  (`streams = {"trace": "live spectrum (dBm); meta: x0, dx (Hz)"}`), so the
  agent can see a stream that only publishes while somebody is subscribed.
- **Give traces an axis.** Publish with `t0`/`dt` or `x0`/`dx` (plus
  `xunit`, `xlabel` and `unit`) in the meta, and use the same field names in a
  returned dataclass. Then the agent knows the frequency of each point, and
  its plots have the right axis.
- **Keep state up to date** with `set_state`. `describe` includes it, so the
  agent sees the current settings without a query per property.

## Trying it without hardware

`insthub sim` includes a spectrum analyzer (`sa`) that is cabled to the
simulated synth (`synth`) through a 20 dB attenuator. With the simulation
running and `insthub mcp` registered, ask the agent for example:

> Put a −5 dBm tone at 1002.5 MHz on the synth and check on the spectrum
> analyzer that it arrives at the right frequency and level.

It will find both devices, switch on the synth's output, set frequency and
power, take a fresh trace and report a peak at 1002.5 MHz and −25 dBm.

## Without MCP

An agent that can run shell commands can also use the
[command line](guide.md#command-line). A line in the project's `CLAUDE.md`
(or the equivalent file for your agent) is enough:

```
Lab instruments are on insthub: `insthub list`, `insthub info DEVICE`,
`insthub call DEVICE COMMAND ARGS`, `insthub get/set DEVICE PROPERTY [VALUE]`.
Save arrays with `insthub call ... --save FILE`.
```

This works today and needs no setup. MCP adds structured results, pictures,
`snapshot` with `fresh`, passing files as arguments, and agents that have no
shell.

## Instruments on another computer

There are two ways:

- **Run `insthub mcp` on the agent's computer** and list the remote devices
  in its `devices.toml`. Saved files are then on the agent's computer, where
  it can load them.
- **Start it on the lab computer through ssh:**
  `claude mcp add insthub -- ssh labpc .local/bin/insthub mcp`. Use the path,
  because a command run by ssh usually does not get `~/.local/bin` on its
  `PATH`. This opens no network port. Saved files are then on the lab computer, and the
  agent sees the summaries and pictures but cannot load the files itself.

## What it does not do

- **It does not restrict the agent.** The agent can do whatever an
  experiment script could: every command, every property, `$release`. Limits
  that protect an instrument, such as a maximum power, belong in its driver,
  where they apply to every client. Claude Code asks before each tool call
  unless you allow the tools, and you can allow the read-only ones
  (`list_devices`, `describe`, `get`, `snapshot`) and keep being asked for
  `set` and `call`.
- **It cannot stop a command once it has started.** If a call outlasts its
  timeout, the agent is told the command may still be running, and later
  calls to that device wait behind it (see
  [when things go wrong](architecture.md#when-things-go-wrong)).
- **It does not clean up its files.**

## Why it is built this way

**Why six generic tools and not one per command?** An agent reads the
description of every tool it has at the start of every session. One tool per
command would be hundreds of descriptions for a modest lab, most of them
irrelevant to the task at hand. Generic tools plus `describe` cost the same
for one instrument as for fifty. The agent reads in detail only the devices
it uses, and a new device needs no new tool.

**Why files instead of numbers?** Everything an agent is shown takes up room
in its context and time to read. A summary and a path are enough to decide
what to do next, and the agent's own Python is a better place than its
context for the numbers.

**Why a separate program, not part of each server?** One program sees every
server, including servers on other computers, and the agent has one thing to
register. Servers stay as fast and as simple as they were, and nothing about
agents ends up in your drivers.

**Why no MCP library?** The part of MCP that tools need is a few messages of
JSON on stdin and stdout, about a hundred lines in `insthub/mcp.py`. The
official Python library would add a dozen packages to every environment that
has insthub, including the environments of instrument servers.
