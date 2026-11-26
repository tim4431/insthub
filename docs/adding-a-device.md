# Adding an instrument

How to put an instrument on the bus, from a driver that works on its own to a
server that runs in the background and that every program finds by name.
Each step works from the command line and from the window (`insthub gui`).

- [The short version](#the-short-version)
- [1. Choose how](#1-choose-how)
- [2. Write the config file](#2-write-the-config-file)
- [3. Try it](#3-try-it)
- [4. Open it at start, or on first use](#4-open-it-at-start-or-on-first-use)
- [5. Keep it running](#5-keep-it-running)
- [6. Change it later](#6-change-it-later)
- [7. Day to day: release, pause, stop](#7-day-to-day-release-pause-stop)
- [8. Share it as an extension](#8-share-it-as-an-extension)
- [When something is wrong](#when-something-is-wrong)

## The short version

```bash
cd ~/Code/mydriver                                  # the project that has the driver, and its .venv
.venv/bin/pip install -e ~/Code/insthub             # once: insthub into the driver's environment
$EDITOR lab.toml                                    # a few lines, see step 2
.venv/bin/insthub serve lab.toml                    # try it; another terminal: insthub info psu
.venv/bin/insthub service install lab.toml          # then keep it running in the background
```

The second line is needed once per driver project: the server runs in the
driver's environment, so insthub must be installed there too
([why](running-servers.md#using-insthub-from-an-instruments-project)). Two
variants of it:

- a `.venv` made by `uv venv` has no `pip`; use
  `uv pip install --python .venv/bin/python -e ~/Code/insthub`. But in a
  project that uv manages (one with a `pyproject.toml` that you `uv sync`),
  `uv sync` removes what `uv pip` installed, and the server then fails with
  "No module named insthub" (and `.venv/bin/insthub` is gone too). Add it as a dependency there instead, from the
  driver's folder: `uv add --editable ../insthub`. The path is relative so
  that the `pyproject.toml` and `uv.lock` it writes work on other accounts
  too, and the project's `requires-python` must not allow a Python older than
  3.11, which insthub needs;
- without a checkout of insthub at `~/Code/insthub`, install it from its
  repository: `.venv/bin/pip install "insthub @ git+https://github.com/tim4431/insthub"`
  (in a uv project: `uv add "insthub @ git+https://github.com/tim4431/insthub"`).

Once insthub is in the driver's `.venv` (the second line), **Add → New
server...** in the window does the rest: you pick the driver (or its `.py`
file), fill in its arguments, and it writes the file and starts the server. Its **Python** field must be the driver's `.venv`: picking the
driver's `.py` file sets it, if the `.venv` is in the driver's folder or the
one above.

## 1. Choose how

| You have | Use |
|---|---|
| A driver class that works on its own | **a config file**, no code. Step 2. |
| You want live data (streams), a state to show, or polling in the background | **a `Device` subclass**: see [writing a device](guide.md#writing-a-device). It goes into a config file the same way. |
| An existing driver, plus a stream or a command of your own | **a subclass of `insthub.Wrapped`**: see [putting an existing driver on the bus](guide.md#putting-an-existing-driver-on-the-bus). In a config file it is a `Device` subclass: its keys go to the subclass's own constructor, and `include`, `exclude`, `attrs` and `close` do not apply. |
| Code that must run around the server | **a short script** ending in `insthub.serve(...)`: see [running servers](running-servers.md#starting-a-server) |

Most instruments need only the first. The driver stays exactly as it is.

## 2. Write the config file

Say your driver is this class, in `~/Code/mydriver/mydriver.py`:

```python
class PowerSupply:
    def __init__(self, port, baud=9600, timeout=1.0): ...
    def voltage(self) -> float: ...
    def set_voltage(self, volts: float): ...
    def close(self): ...
```

The config file that puts it on the bus, `lab.toml`:

```toml
[server]
name = "lab"                       # how the server is called (default: the file's name)

[devices.psu]                      # the device is called "psu"
driver = "mydriver:PowerSupply"    # module:Class
path = "~/Code/mydriver"           # where the module is (not needed for an installed package)
port = "/dev/ttyUSB0"              # every other key goes to the constructor:
baud = 19200                       #   PowerSupply(port="/dev/ttyUSB0", baud=19200)
```

That is all. Every public method of the class becomes a command (but its
`close`, which insthub calls itself) and every property a property, so any
program can now do:

```python
import insthub

psu = insthub.device("psu")
psu.set_voltage(5.0)
print(psu.voltage())
```

The driver object is made on the device's own thread when the instrument is
opened, and made again after the connection breaks, so reconnecting needs no
code either.

### TOML in two minutes

| Written | Means in Python |
|---|---|
| `port = "/dev/ttyUSB0"` | text, always in quotes |
| `path = 'C:\drivers\psu'` | text in single quotes: backslashes stay as they are (Windows paths) |
| `baud = 19200`, `gain = 0.5`, `span = 1e9` | numbers |
| `lazy = true` | `True` (and `false`) |
| `channels = [1, 2]` | a list |
| `opts = { mode = "fast", avg = 4 }` | a dict |
| `# ...` | a comment, anywhere |

TOML has no `None`. To keep a constructor's default, leave the key out.

### Every key

`[server]`, all optional:

| Key | Meaning |
|---|---|
| `name` | how clients and the window call the server; default: the file's name |
| `tcp` | also listen on TCP: `8765` for this computer only, `"0.0.0.0:8765"` for other computers too ([no password](running-servers.md#reaching-a-server-from-another-computer)) |
| `ipc` | `false`: no Unix socket (only TCP, or only this process) |

`[devices.NAME]`, one table per device. `NAME` is how every program finds it,
so it must be unique among the servers of this computer. Names (of devices and
of servers) use letters, digits, `_`, `-` and `.`, and start with a letter or
a digit. A name with a `.` is quoted in its header, `[devices."psu.2"]`:
TOML reads `[devices.psu.2]` as a table `2` inside a device `psu`.

| Key | Meaning |
|---|---|
| `driver` | **required.** `"module:Class"`. A `Device` subclass is used as it is; any other class is wrapped. `"package.module:Class"` works too. |
| `path` | the folder of the driver's module, put in front of Python's search path. A relative one counts from the config file's folder, wherever the server runs |
| `lazy` | `true`: open the instrument on first use instead of when the server starts ([step 4](#4-open-it-at-start-or-on-first-use)) |
| `description` | what this one is, in a few words, as `"the one on the optics table"`: its tile shows it, where it would show the driver's docstring (the same for every instrument of a kind). Its tile also shows where it is, taken from the constructor's arguments: `host` (with `port`), `resource`, `port`, `serial`, or GPIB's `board` and `pad` |
| `reconnect_interval` | seconds between two attempts to open it (default 1) |
| `link_errors` | exception classes that mean the connection broke, as `["pyvisa.errors:VisaIOError"]`; then the session is closed and opened again. `OSError` and `EOFError` always count. |
| `args` | positional constructor arguments: `args = ["/dev/ttyUSB0"]` |
| `kwargs` | a table of constructor arguments, for one whose name is one of these keys (see below) |
| `include`, `exclude` | plain driver classes only (the ones insthub wraps for you): the methods to expose, or the ones not to |
| `attrs` | plain driver classes only: plain attributes to expose as properties, `["serial_number"]` |
| `close` | plain driver classes only: the method that closes the driver (default `"close"`) |
| `accessors` | plain driver classes only: `true` makes a property `X` of each `get_X()` that needs no argument and has a `set_X(value)` beside it (a getter alone stays a command). `X` and `set_X` write one setting, so `dangerous` on either one marks both. |
| `meta` | units, limits and choices, by property or `"command.parameter"`: `meta = { power = { unit = "dBm", max = 13 }, "set_level.dbm" = { min = -40 } }` (a parameter's name in quotes: unquoted, TOML reads a table in a table). The words are `unit`, `min`, `max`, `step` and `choices`. The window and agents show them; they are not enforced. |
| `poll` | values to read periodically and publish on a stream of the same name: `poll = { temperature = 1.0 }` reads the property (or calls the command, if it needs no argument) `temperature` every second. It replaces what the class polls. |
| `dangerous` | what is dangerous to use: names of commands, and of properties that can be written (to write them), as `["fire", "voltage"]`, or `true` for every command and every property that can be written. Agents need a tool of its own for them, and the window asks first. It adds to what the device's class marks dangerous; it cannot unmark anything. |
| `extension`, `extension_version` | what the device is: the extension it belongs to (see [design](design.md#how-a-device-finds-its-extension)) |
| `simulated` | `true`: it talks to a simulator; the window, `insthub info` and agents mark it SIM |
| anything else | a keyword argument of the constructor |

If the constructor has an argument called like one of these keys, say its own
`path`, give it under `kwargs`, since the key itself never reaches it:

```toml
[devices.psu.kwargs]
path = "/dev/serial/by-id/usb-FTDI_FT232R-if00-port0"
```

When the table has the key itself (`path = ...`, `simulated = true`) and the
constructor takes an argument of that name, insthub cannot tell which of the
two you meant, so it stops with an error until `kwargs` says what the
constructor gets. So a file written for an older insthub, in which such a key
still went to the constructor, never silently starts a driver differently. When
the table does not have the key, an argument that is required is an error that
says why; `insthub service install` warns about an optional one.

A `Device` subclass whose constructor takes `name` gets the table's name.

insthub checks every table of the file against its driver when it loads the
file, before it constructs or opens any driver. A key that is not an argument of
the constructor, a required argument that is missing, one of the keys above
with the wrong kind of value, or a `link_errors` class that cannot be loaded
stops the server. So does a driver that cannot be loaded. The error lists
every problem of the file at once, each with the key and, for a misspelled
one, the name you probably meant:

```
error: [devices.psu] prot: not a parameter of mydriver:PowerSupply(port, baud=9600); did you mean 'port'?
```

Two cases accept no such check. A constructor that takes `**kwargs` accepts
any key. insthub only warns about one that looks like a misspelled key of its
own, such as `lazzy`, because then its own setting is not applied; write the
key under `kwargs` if the driver really takes it. A `driver` that names an object rather than a class (an instance
made in the module, or a function) is hosted as it is, so it takes no
constructor keys, `args` or `kwargs` at all, and insthub says so if the table
has any.

The names in `meta`, `poll` and `dangerous` must be the device's own commands
and properties. For a plain driver class, insthub learns them from the class
without running any of the driver's code, so they are checked with the rest of
the file, by `insthub service install` too. A `Device` subclass knows them only
once it is constructed, and its constructor may do anything, so its names are
checked when `insthub serve` makes it at start, every device's at once:

```
error: [devices.psu] psu: poll names 'temprature', which is neither a property nor a command; did you mean 'temperature'?
```

### More examples

**An instrument on the network, with pyvisa:**

```toml
[devices.scope]
driver = "scopes:Rigol"
path = "~/Code/scopes"
resource = "TCPIP0::192.168.1.30::INSTR"
link_errors = ["pyvisa.errors:VisaIOError"]     # a VISA timeout: reconnect on the next call
```

**A GPIB instrument** (linux-gpib), opened only when a script needs it:

```toml
[devices.hp8562a]
driver = "bus_server:SpectrumAnalyzer"
path = "~/Code/hp_8562a_gpib"
board = 0                       # the GPIB card
pad = 18                        # the instrument's address
lazy = true                     # addressing it locks its front panel: not before it is used
```

The `gpib` module comes with linux-gpib, installed for the whole system, so the
project's virtualenv must see the system's packages
(`python -m venv --system-site-packages .venv`).

**Several devices in one server:**

```toml
[server]
name = "rf"

[devices.synth]
driver = "rf_drivers:Synth"
path = "~/Code/rf_drivers"
address = "192.168.1.40"

[devices.sa]
driver = "rf_drivers:Analyzer"
path = "~/Code/rf_drivers"
address = "192.168.1.41"
```

Put devices together when they share a Python environment and you do not mind
restarting them together; [how many servers](running-servers.md#how-many-servers)
says when to split them.

**A driver whose settings are `get_`/`set_` methods,** described for the
window and for agents, with its raw I/O kept away from them:

```toml
[devices.awg]
driver = "m8195a:M8195A"
path = "~/Code/m8195a_ripa"
address = "TCPIP0::192.168.1.50::hislip0::INSTR"
accessors = true                                    # get_amplitude()/set_amplitude(v) become a property amplitude
exclude = ["write", "query"]                        # raw SCPI stays off the bus
meta = { amplitude = { unit = "V", min = 0.075, max = 1.0 } }
dangerous = ["set_output"]                          # and the property output: agents need call_dangerous, the window asks
poll = { temperature = 5.0 }                        # a live value on the tile
```

**To try it without hardware:** `driver = "insthub.sim:SimScope"` (also
`SimSynth`, `SimCamera`, `SimAnalyzer`, `Loopback`). They describe themselves
with units, limits and choices, and say they are simulated.

## 3. Try it

Run the server with **the Python that has the driver's packages**, usually the
`.venv` of the driver's project. The `insthub` command you installed for
yourself has insthub but none of your drivers, so it cannot load them:

```bash
~/Code/mydriver/.venv/bin/insthub serve lab.toml       # or activate the venv, then: insthub serve lab.toml
```

In another terminal, or in the window:

```bash
insthub list                         # the server and its devices, and whether each is connected
insthub info psu                     # its commands, properties and streams, with their docstrings
insthub call psu set_voltage 5.0
insthub call psu voltage             # a method is a command; `insthub get` is for properties
```

The window shows it as a tile on **Devices**. Click the tile to read and set
its properties and run its commands. Ctrl-C in the server's terminal stops it
and closes the instrument properly. If something does not work, see
[when something is wrong](#when-something-is-wrong).

## 4. Open it at start, or on first use

By default a device opens its instrument when the server starts. With
`lazy = true` it waits for the first command.

**Open at start** (the default) when:

- you want to know at once that the instrument is there: the window shows
  "connected" or "not connected" from the start, and a wrong address shows up
  now rather than in the middle of an experiment;
- opening is slow (an initialisation, a calibration, a camera's SDK): it is
  paid when the server starts, not by your script's first call;
- the driver puts the instrument into a safe state when it opens it.

**Open on first use** (`lazy = true`) when:

- the instrument is often unplugged or switched off;
- other software uses it too, and the server should not hold it before a
  script needs it;
- connecting has side effects: many GPIB instruments lock their front panel
  ("remote") as soon as they are addressed.

A device whose periodic task runs from the start opens the instrument for that
task anyway, so `lazy` makes no difference there. In the window, it is the
device's **Open at start** switch in **New server...**, or on the device's
page under **⋯ → Edit in lab.toml...** (the name of its config file; then
restart the server).

## 5. Keep it running

A server started in a terminal stops with the terminal. To keep one running in
the background, install it as a service:

```bash
~/Code/mydriver/.venv/bin/insthub service install lab.toml               # now, and at every login
~/Code/mydriver/.venv/bin/insthub service install lab.toml --no-login    # now, but not at login
```

The service runs the Python that installed it. Before installing, it checks
that this Python can load every driver of the file, and that every key fits
its driver. A warning, such as a driver that imports Qt (a service has no
screen), is printed, and the install goes on. It then starts the service and
waits until the server answers; if it does not, the install says so and
prints the end of the service's log. A service is restarted if it crashes
(5 s later, and at most 5 starts within 5 minutes), and its output is kept
(`insthub service logs lab`; `insthub logs lab` shows its last lines from any
computer).

Start at login, or only when you start it: an instrument that is always there
is best started at login. One you plug in now and then is better installed with
`--no-login` and started when you need it, with **Start** on its page in the
window or `insthub service start lab`. To change your mind later:
`insthub service enable lab` (start at login) or `insthub service disable lab`
(only when started), or the **Starts at login** switch on the server's page.

In the window: **Add → Keep a server running...**, which picks the `.venv`
next to the file by itself and has a **Start at login** switch. Or
**Add → New server...**, which writes the file and installs it in one go. More
in [running servers](running-servers.md#keeping-a-server-running).

## 6. Change it later

The server reads its config file only when it starts. To change a setting:

- **Edit the file**, in any editor, then restart the server:
  `insthub service restart lab`, or Ctrl-C and `insthub serve lab.toml` again.
- **Or in the window:** on the server's page, **⋯ → Edit lab.toml...**, or
  on a device's page, **⋯ → Edit in lab.toml...** (each named after the
  server's config file; the same items are in the menu of its card and of
  its tiles). It is the form of New
  server, filled in from the file. Only what you change is written into it;
  its comments and everything else stay as they are. **Save and restart**
  restarts the server, if it is a service or was started from this window
  (while the window stays open), so that it uses the change; the window then
  shows its page again once it runs.
- **Another instrument in the same server:** on the server's page, the
  **Add a device** tile after its devices (or **⋯ → Add a device...**, or
  **Add → Add a device to a server...**). It opens the same form with a new
  device in it. The device runs in that server's Python, so its driver must
  load there; the form says so when the driver's folder has a `.venv` of its
  own. A driver of another project, with its own packages, gets a server of
  its own: **Add → New server...**.

What you change in the window while the server runs is not written into the
file. A property you set goes to the instrument, which may keep it after the
server restarts (unless the driver resets it when it opens). Release and Pause
last only as long as the server runs.

## 7. Day to day: release, pause, stop

| You want | Window | Command line | From a script |
|---|---|---|---|
| Let other software (the vendor's) use the instrument for a moment | **Release** | `insthub release psu` | `psu.bus.release()` |
| ... and keep everything else off it until you are done | **Pause** | `insthub pause psu` | `psu.bus.pause()` |
| End the pause | **Resume** | `insthub resume psu` | `psu.bus.resume()` |
| Open it now | **Connect now** | `insthub reopen psu` | `psu.bus.reopen()` |
| Stop the server (and with it its devices) | **Stop** on the server's page | `insthub service stop lab` | |

**Release** closes the session; the next command from any program, or the
next run of a periodic task, opens it again. On a device with a periodic task
(one that acquires by itself), use Pause instead. **Pause** closes the session
too, and then refuses every command, property and periodic task until
**Resume**: a program that calls it gets `DevicePaused` (a kind of
`DeviceNotConnected`) at once, and the window shows the device as paused,
since when, and which program paused it. A command or periodic task that is
running when you pause finishes first (with the calls it makes to its own
device on the way, a sweep, say), and no other starts after it; meanwhile the
window says "pausing", and `insthub pause` waits for it (Ctrl-C stops the
waiting, not the pause). Resume opens the instrument again if it was open when
it was paused; a lazy device not used yet stays closed. The window asks before it
resumes a pause another program made, and before it pauses a device that a
program is watching.

For a plain driver class, Release and Pause close the driver object, and the
next open makes a new one: what was kept only in that Python object starts
again, while the instrument keeps its own settings. A pause lasts until it is
resumed or the server stops: a service restart, a crash, **Save and restart**
after editing the config, or Ctrl-C all end it.

## 8. Share it as an extension

To offer the instrument to others, or to the New server dialog on your other
computers, give its project folder a *manifest*, `insthub-extension.toml`.
[`examples/extension/`](../examples/extension) in insthub's repository is a
complete extension to start from: a simulated laser with a manifest, a
device, a panel, an app and a server file, and a README on making it your
own. The smallest manifest:

```toml
[extension]
id = "mydriver"                      # the name its devices announce
title = "My power supply"
version = "0.1"
api = 1

[devices.psu]
driver = "mydriver:PowerSupply"
doc = "the bench supply, over USB serial"
baud = 9600                          # every other key goes into the server file as it is
```

Then check it and add it:

```bash
insthub check ~/Code/mydriver --import    # imports the driver too, in the folder's .venv
insthub ext add ~/Code/mydriver           # New server now offers "My power supply: the bench supply..."
```

A server file made from it gets `path` (the folder) and `extension =
"mydriver"`, so the device announces its extension. A `Device` or `Wrapped`
subclass can say it itself, with `extension = "mydriver"` on the class.

If the instrument has a window of its own, the manifest names it too, as a
[panel](guide.md#writing-a-panel) (one function that makes a widget, here in
`mydriver_ui.py`) or as an app (a whole program):

```toml
[ui.panel]
title = "Supply panel"
panel = "mydriver_ui:make"           # or an app: run = ["-m", "mydriver_app", "--address", "{address}", "--name", "{device}"]
```

Then `insthub open psu` opens it, and so does **Open Supply panel** on the
device's page in the window, with the folder's Python. Its Qt belongs in that
environment (in a packaged extension, in its `gui` extra), never in the
driver. The panel need not draw every setting itself:
`insthub.controls.Controls(device, exclude=[...])` shows the rest, as the
device's page does. More in [extensions](guide.md#extensions) and in the
[design](design.md#extensions).

## When something is wrong

| You see | Why | What to do |
|---|---|---|
| `cannot load driver 'mydriver:PowerSupply': No module named 'mydriver' (this is .../python; is it the Python environment that has the driver's dependencies?)` | the server runs a Python that does not have the driver, or `path` is wrong | run the `insthub` of the driver's `.venv` ([step 3](#3-try-it)); check `path` |
| `No module named 'serial'` (or another package the driver imports) | the same: that Python lacks the driver's own packages | install them into that environment, or use the one that has them |
| The tile says **not connected: PermissionError: [Errno 13] Permission denied: '/dev/ttyUSB0'** (with pyserial: **SerialException: [Errno 13] could not open port /dev/ttyUSB0**) | your account may not open the port | `sudo usermod -aG dialout $USER`, or a udev rule. The new group reaches only programs started after a new login, and on a desktop everything (terminals, `insthub gui`, the servers it starts, services) descends from your user's service manager, which keeps running across a logout while another session of yours is open or lingering is on: then only a reboot gives it to them. To try it before that: stop the running server (`insthub service stop lab`, or **Stop** on its page), log in again where the new group applies (`ssh localhost`, or a text console), and there run `~/Code/mydriver/.venv/bin/insthub serve ~/Code/mydriver/lab.toml`. This belongs to the driver's project ([who sets up what](running-servers.md#who-sets-up-what)) |
| `[devices.psu] bauds: not a parameter of mydriver:PowerSupply(port, baud=9600); did you mean 'baud'?` (or `missing a required argument: 'port'`) | a key of the file is misspelled, or a constructor argument is missing: every key insthub does not read itself goes to the constructor | fix the file and start the server again |
| `[devices.psu] lazy: should be true or false, not "yes"` | one of insthub's own keys has the wrong kind of value | write it as [every key](#every-key) says |
| `a module named 'bus_server' is loaded already, from .../hp_8562a_gpib/bus_server.py, so .../windfreak_sythHDprov2/bus_server.py cannot be loaded too` | two devices of one server come from folders that each have a module of that name, and Python keeps one module per name | host them in separate servers, or move one into a package with a name of its own ([rules inside an extension](design.md#rules-inside-an-extension)) |
| `[devices.psu] simulated: one of insthub's own keys, but mydriver:PowerSupply(port, simulated=False) has a parameter 'simulated' too, which the key never reaches: say what the driver gets with kwargs = { simulated = ... }` | the table has one of insthub's keys, and the constructor has an argument of that name: which did you mean? | add `kwargs = { simulated = ... }` with what the constructor should get; keep the key if you also mean insthub's ([every key](#every-key)) |
| `[devices.psu] mydriver:PowerSupply(path, baud=9600): missing a required argument: 'path'; path is one of insthub's own keys and never reaches it` (from `insthub service install`, for an optional one: `warning: [devices.psu] mydriver:PowerSupply(port, log=None, lazy=False) has a parameter 'lazy', but lazy is one of insthub's own keys and never reaches it`) | the constructor has an argument named like one of insthub's keys | give it under `kwargs` ([every key](#every-key)) |
| `[devices.psu] link_errors: cannot load 'pyvisa.errors:VisaIOErorr': module 'pyvisa.errors' has no attribute 'VisaIOErorr'` | a class in `link_errors` is misspelled, or is not in the module the name says | write it as `"module:Class"`, as `"pyvisa.errors:VisaIOError"` |
| `warning: [devices.psu] driver mydriver:PowerSupply imports PyQt6; servers often run without a screen` | the driver's module imports Qt, which a service without a screen may lack | keep window code out of the driver's module |
| `insthub service install`: `installed .../insthub-lab.service, but insthub-lab stopped with an error. The end of its log (insthub service logs lab):` and the lines below it | the service started, and its server failed; the lines under the message say why | fix what they name, then `insthub service restart lab` |
| `insthub service list` says `failed` and, under it, `restarted 4 times, gave up: too many restarts` | the server crashed again each time systemd restarted it, so after 5 starts within 5 minutes systemd stopped trying | `insthub service logs lab` says why; once it is fixed, `insthub service start lab` (or **Start** in the window) starts it again |
| `insthub logs lab`: `lab runs an older insthub, which keeps no log for others to read` | that server's insthub is older than `insthub logs` | update insthub in its environment and restart it; until then read its output where it runs (its terminal, or `insthub service logs lab` on that computer) |
| `insthub check`: `it has no Python: make a virtualenv (.venv) in ~/Code/mydriver or the folder above` | an extension's code runs in its own Python, and there is none in its folder | `uv venv` (or `python -m venv .venv`) there and install the driver's packages, or name one in `[extension]` as `python = "..."` |
| `insthub check`: `[devices.psu] path: not for a manifest to say: insthub writes the manifest's folder` (or `extension`, `extension_version`) | the manifest gives a key that the server file gets from insthub | remove it from the manifest |
| `insthub ext list`: `hidden by .../insthub-extension.toml` | two folders have an extension with one `id` (say, a copy) and the first one found that can be used is used | remove the other from your list, or give it another `id` |
| `insthub open psu`: `extension mydriver is not installed on this computer: insthub ext add FOLDER` (the device's page says the same) | the device announces an extension whose manifest this computer does not know: it runs on another computer, or its folder has no manifest | clone the project here and `insthub ext add` its folder |
| `insthub open psu`: `its extension, My power supply, has no UI for it` | the manifest has no `[ui.NAME]`, or its `types` leave this device's type out | add one, or the type `insthub info psu` shows |
| `cannot load the panel 'mydriver_ui:make': insthub.qt needs a Qt binding: pip install 'insthub[gui]' ...` (or `No module named 'PyQt6'`, or `'pyqtgraph'`), in the window's message or `insthub open`'s output | the extension's Python lacks the libraries of its UI | install them there: `uv sync --extra gui` for a packaged extension, else `uv pip install --python .venv/bin/python PyQt6 pyqtgraph` |
| a UI's log ends with `invalid choice: 'panel'` | the extension's Python has an insthub older than `insthub panel` | update insthub in that environment |
| a panel's log ends with `No module named 'insthub.controls'` (or `module 'insthub.qt' has no attribute 'start'`, or `'coalesce'`, or `'follow'`) | the extension's Python has an insthub older than the helpers the panel uses | update insthub in that environment |
| `insthub check` or `insthub ext list`: `every user may change ~/Code/mydriver (chmod o-w ~/Code/mydriver): insthub runs nothing it names` (or `the users of group lab may change ...`, `... belongs to another user`) | insthub starts an extension's UIs only when no one but you (or root) may change its manifest and its folder | `chmod go-w` the folder and its `insthub-extension.toml`; for another user's folder, use a copy of your own |
| `insthub check` or `insthub ext list`: `its Python, /tmp/.venv/bin/python: every user may change /tmp; insthub runs no Python that another user may change` (or `... belongs to another user`) | the extension's Python is a virtualenv that someone else may change, or may have put there: one in the folder above the extension's, such as `/tmp`, or one whose folders are writable by others | make the virtualenv in the extension's own folder (`uv venv`), or `chmod go-w` its folders |
| `insthub check FOLDER --import`: `[devices.psu] mydriver_insthub.device:Psu says extension = "mydrvier", but the manifest's id is "mydriver"` (the device's page: `it announces extension mydrvier, but the manifest of its folder ... has id mydriver`) | the class's `extension` and the manifest's `id` differ, so its devices find no UI | make them the same (import the id from one place, as the reference extension does) |
| `insthub check FOLDER --import`: `[ui.panel] cannot load the panel mydriver_ui:make with ...` (or `[ui.app] cannot find the module mydriver_app with ...`) | a `[ui.NAME]` names a module or function that its Python cannot find: a typo, or one left behind by a rename | fix the name in the manifest |
| `[devices.psu] psu: dangerous names 'outptu', which is none of its commands and properties; did you mean 'output'?` (or the same from `poll` or `meta`) | a name in `dangerous`, `poll` or `meta` is misspelled, or names a method that `include`/`exclude` leave out | write the command's or property's name as `insthub info psu` lists it |
| `[devices.psu] psu: dangerous names 'current', a property that cannot be written` | only writing a property can be dangerous, and this one is read-only | leave it out, or name the command that changes it |
| `[devices.psu] meta: power: 'units' is not one of unit, min, max, step, choices; did you mean 'unit'?` | a word of the metadata is misspelled | use `unit`, `min`, `max`, `step` or `choices` |
| `[devices.psu] meta: set_level.dbm: write a parameter's name in quotes, as "set_level.dbm" = { ... }` | a parameter's name was written without quotes, which TOML reads as a table inside a table | quote it: `"set_level.dbm" = { min = -40 }` |
| `lab.toml: Invalid value (at line 2, column 10)` | the file is not valid TOML there (here a key without a value) | fix that line |
| a traceback that ends in `DriverError: [devices.psu]: mydriver:PowerSupply(...) failed: ...` (or `cannot load driver 'mydriver:PowerSupply': SyntaxError: ...`) | the driver's own code failed while the server made the device, or imported its module: not a problem of the file | the traceback above it shows where, in the driver |
| **not connected:** and an error from the driver | the instrument is off, unplugged or at another address | fix it, then **Connect now** (or just use it) |
| `a server named 'lab' is already running` | two servers with one name | stop the other one, or rename one (`name` in `[server]`) |
| `insthub.device("psu")` says the name is ambiguous (and `insthub states` gives `psu` an error) | two servers each have a device `psu` | device names must be unique on a computer: rename one table |
| A method of the driver is not a command | its name starts with `_`, it is the driver's `close` method, or `include`/`exclude` leave it out | rename it, or adjust `include` / `exclude` |
| It works with `insthub serve` but not as a service | the service runs another Python, or in another folder (the file's) | `insthub service logs lab`; install it with the `insthub` of the right `.venv`. Give a driver that is not installed in that Python its `path` (without one, a service finds its module only in the file's folder) |
| A script gets `DevicePaused` | somebody paused the device | Resume it (window, `insthub resume psu`), or ask who paused it: the window says |
