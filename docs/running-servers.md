# Running servers

How to start servers, keep them running, and decide what belongs where. For
what a server is, read [how insthub works](architecture.md).

## Starting a server

A server is an ordinary Python program. There are two ways to write one.

**From a config file**, when the devices are existing classes:

```bash
insthub serve lab.toml
```

**From a script**, when you want code around it:

```python
# scope_server.py
import insthub
from mydrivers import Scope

insthub.serve(Scope("scope"), name="scopes")
```

```bash
python scope_server.py
```

Either way it runs until Ctrl-C, and then closes every instrument properly.
While it runs, `insthub list` in another terminal shows it.

## Keeping a server running

A server started in a terminal stops when the terminal closes. To keep one
running in the background, register it as a service:

```bash
insthub service install lab.toml           # or a .py server script
```

This writes a small *systemd user service*, starts it, waits until its server
answers (up to a minute), and from then on:

- it starts by itself when you log in (unless installed with `--no-login`),
- it is restarted 5 seconds after it crashes, but not forever: after 5 starts
  within 5 minutes systemd gives up and marks it *failed*, so a server that
  cannot start (a broken config, an instrument it cannot do without) does not
  restart all day. `insthub service list` and its page in the window say how
  often it was restarted,
- its output is kept and can be read with `insthub service logs lab`.

If the server stops, or does not answer within a minute, `install` (and
`start` and `restart`) say so and print the last 20 lines of its log, which
usually name the problem.

An instrument that is not always connected may be better off with
`--no-login`: installed, with its logs and its buttons in the window, but
started only when you start it. `insthub service enable lab` and
`insthub service disable lab` change that later, and so does the **Starts at
login** switch on the server's page in the window.

| Command | Effect |
|---|---|
| `insthub service install TARGET [--name NAME] [--python PATH]` | register and start, now and at every login; run it again to replace the definition |
| `... install TARGET --no-login` | register and start now, but not at login |
| `insthub service list` | what is installed, whether it is running, whether it starts at login ("at login") or only when started ("by hand"), and how often it was restarted after a crash |
| `insthub service logs NAME [-f]` | its output; `-f` follows it live |
| `insthub service restart NAME` | restart, after changing code or config, and wait until it answers |
| `insthub service stop NAME` | stop it; it starts again with `start`, or, if it starts at login, the next time your user's services start (a login after all your sessions ended, or a boot with lingering on) |
| `insthub service start NAME` | start it again, also one that gave up after too many restarts, and wait until it answers |
| `insthub service enable NAME` / `disable NAME` | start it at every login from now on, or no longer |
| `insthub service remove NAME` | stop it and remove it |

The same controls, and the log, are in the manager window (`insthub gui`): open
the server from **Servers**, or the service if it is not running. Its **Add**
button installs a service too (**Keep a server running...**), and **New
server...** writes the config of a new server and starts it.

Nothing here needs administrator rights. Two details:

- **Starting at boot.** User services start at login. To have them start when
  the computer boots, with nobody logged in, turn on *lingering* once:
  `sudo loginctl enable-linger $USER`. `insthub service install` tells you if
  it is off.
- **Which Python.** The service runs with the Python that the server file
  needs, by the rule that `insthub check --import` and the window follow
  too: that of its devices' [extensions](guide.md#extensions) when they
  agree on one, else a virtualenv's (`.venv`, `venv` or `env`) next to the
  file or next to a device's code, else the Python interpreter that ran
  `insthub service install`. `--python` names another. If a server needs
  the packages of a virtualenv that is not next to its files, install it
  with that virtualenv's `insthub` (or pass `--python`):

  ```bash
  ~/Code/mydriver/.venv/bin/insthub service install ~/lab/server.toml
  ```

  Before it writes anything, `install` checks that this Python can import
  insthub and every driver the config names, from the folder the service
  will run in, and that every key of the config fits its driver. If
  something is missing or wrong, it stops with the error (every problem of
  the file at once) instead of installing a service that would fail at every
  start, and says what would fix it: the file, the Python environment, or
  both. A warning, such as a driver that imports Qt, which a service
  without a screen may not have, is printed, and the install goes on. It
  refuses the Python
  of [the insthub command for yourself](#the-insthub-command-for-yourself),
  which has insthub but none of your drivers.

  `insthub check server.toml --import` makes the same check without
  installing anything, in the same Python, and also checks each device's
  `extension` against the manifest of its folder.

This needs Linux with systemd. On other systems, start servers with the
system's own autostart mechanism.

**Servers with a window** are different. A program that shows a GUI belongs to
your desktop session, so start it from a launcher or the desktop's autostart,
not as a service.

## After changing code

A server loads its code once, when it starts. After you edit a driver, pull a
new version of an extension or update insthub, the servers that were running
still run the old code. insthub says which ones:

- `insthub list` adds a line under such a server:

  ```
  lab  (pid 4242)  ipc:///run/user/1000/insthub/lab.sock
      out of date: hp8562a_insthub/device.py changed since it started; a restart loads the new code
      analyzer         SpectrumAnalyzer connected; ...
  ```

- the window marks it **OUT OF DATE** on its card and above its devices, and
  its page says what changed and how to restart it.

Restart the servers marked so: a service with `insthub service restart NAME`
(or **Restart** on its page in the window), a server you started in a
terminal where you started it. Programs using it get an error for calls under
way, then reconnect by themselves.

How it knows: a server writes into its `.json` file in the runtime folder
the files of the code it loaded, and when each was last changed at that
time. Those are insthub's own modules and the modules in its devices'
folders, but not the libraries of a virtualenv in such a folder (a project's
`.venv`). A module that a driver imports later, inside a command say, is
added within about a second. A file changed or deleted since then is code
the server does not run. It compares times, not contents, so a file saved
without a change counts too.

When the manifest of a device's extension changed after the server started
(a `git pull`), the `version` it holds is also compared with the
`extension_version` the device states ("the folder has demo-laser 0.2, it
runs 0.1"). Versions that disagreed already when the server started are not
a sign of old code, since a restart would not change them: `insthub check`
warns about a class and a manifest that disagree.

Only servers on this computer are judged: one on another computer, or one
started by an older insthub, which writes no such list, is never marked for
its files.

## Reading a server's log

Every server keeps the last 1000 lines it logged, and `insthub logs` shows
them from any computer that can reach it. This works where there is no
journal to read: for a server started in a terminal, a program that hosts
its own server, or a server on another computer.

```bash
insthub logs lab                     # by the server's name, as insthub list shows it, or one of its devices' (this computer's first)
insthub logs lab -f                  # and what it logs from now on, until Ctrl-C (also across a restart)
insthub logs lab --level WARNING     # only warnings and errors
insthub -a tcp://labpc:8700 logs     # a server on another computer, by its address
```

| The server is | It keeps |
|---|---|
| `insthub serve`, `insthub sim`, or a script that calls `insthub.serve(...)` | everything the program logs, from INFO up, and exceptions that nothing caught, in any thread |
| a program that makes a `Server` itself (a GUI that hosts its own) | insthub's own lines, at the level the program's logging lets through: warnings and errors unless it sets up logging |

Keeping them changes nothing about where the lines are printed: a program
that leaves logging alone still shows its warnings on stderr, and so in a
service's journal. The lines are kept in the server's memory, so after a
restart only the new run's are there; a service's journal (`insthub service
logs`) has the earlier runs too. A server of an older insthub keeps none and says so: read
its output where it runs. In the window, a server's **Logs** button shows a
service's journal, the output of a server the window started, and this log
for any other server.

## Using insthub from an instrument's project

insthub is a library that other projects depend on. It is not copied into
them. Each instrument's project keeps three things of its own: the driver, the
hardware setup, and one small file that puts the driver on the bus (a server
script or a `.toml`). insthub itself contains nothing about any instrument.

Install insthub into the Python environment of each project that uses it:

```bash
pip install -e ../insthub              # from the project's folder, with insthub in the folder next to it
```

A project can record this in a requirements file next to its own:

```
# requirements-bus.txt
-r requirements.txt
-e ../insthub
```

Why not copy the `insthub` folder into each project: every program on the bus
has to speak the same message format. With one installed package, a fix or a
format change reaches every project at once. With copies, they drift apart,
and the first sign is two programs that can no longer talk to each other.

Without a local checkout next to the project, or on a build server, the same
line can name the repository instead of the folder:

```
insthub @ git+https://github.com/tim4431/insthub
```

## The insthub command for yourself

Using the instruments, as opposed to serving them, needs no driver.
`insthub list`, `call`, `watch`, `view`, `gui` and `mcp` get everything they
show from the servers. So besides the copy in each instrument's project,
install insthub once as a command of your own:

```bash
uv tool install --editable "/path/to/insthub[gui,mcp]"     # pipx install --editable works the same
```

`uv tool` gives it a private environment and puts the `insthub` command in
`~/.local/bin`, which is on the `PATH` of most Linux desktops. It then works
in every folder without activating anything, and AI agents can start it by
name (see [AI agents](agents.md#setting-it-up)). `--editable` makes it run the
code of your checkout, like the projects that installed insthub with `-e`.
After a `git pull` every program runs the same version. Servers started
before the pull still run the old one until they restart (`insthub list` and
the window mark them: see [after changing code](#after-changing-code)); that
works, since
each server says which version it runs and what it supports, and skips
messages it does not know ([what travels on a
connection](architecture.md#what-travels-on-a-connection)). An older server
just describes its devices with less: no units or limits, nothing marked
dangerous or simulated, until it restarts. To remove it:
`uv tool uninstall insthub`.

Servers do not use it. A server needs its driver's packages, and those are
in its project's environment, not in this one. That is why
`insthub service install` takes the Python of the server's extensions, or of
a virtualenv next to its file or its drivers, when there is one, and else
refuses this command's Python and asks you to run the project's `insthub` or
to pass `--python` (see
[keeping a server running](#keeping-a-server-running)). Run `insthub serve`
from the project's environment as well; from this command it stops at the
first driver it cannot import.

## Who sets up what

Getting an instrument to work involves several jobs. Each has one natural
owner, and keeping them apart is what lets insthub stay general.

| Job | Examples | Owner |
|---|---|---|
| Let the operating system reach the hardware | a udev rule that gives your account access to a USB serial port and a fixed name; a kernel driver for a GPIB adapter; membership of a group; a vendor SDK | **the driver project**, in its setup script and README |
| Put the driver on the bus | a `bus_server.py` or `bus.toml` of a few lines | **the driver project** |
| Speak the instrument's language | SCPI strings, binary protocols, vendor library calls | **the driver** |
| Hold the session, order the requests, reconnect | one thread per device, waiting line, link errors | **insthub** (`Device`, `wrap`) |
| Move commands and data between programs | calls, futures, streams | **insthub** |
| Keep servers running in the background | start at login, restart on crash, logs | **insthub** (`insthub service`) |
| A desktop icon for an application with a window | a `.desktop` launcher | **that application** (insthub's own window: `insthub gui --install`) |
| Decide what the experiment does | sequences, scans, analysis | **your scripts** |

The reasoning behind the first and the fifth row, which are the two that put
something into the system:

**Hardware access stays with the driver.** A udev rule or a kernel module is
about one piece of hardware. It is needed even when you use the driver alone,
without any bus. It needs administrator rights. And it is different for every
instrument. If insthub installed such things it would have to know about every
instrument, and it would stop being general.

**Running in the background is done once, by insthub.** Keeping a server alive
is the same job for every server: run this command, restart it if it dies,
keep its output. If every driver project ships its own service file, you end
up with as many conventions as projects, and nothing notices when two services
are both set up to open the same instrument. One command gives one convention:
every service is called `insthub-NAME`, they are all listed by
`insthub service list`, and they all behave the same.

## How many servers?

One server can host any number of devices, and one computer can run any number
of servers. Each device gets its own thread either way, so this choice is
about practical matters, not speed.

Put devices in **the same server** when they share a Python environment and
you are happy to restart them together.

Use **separate servers** when:

- the drivers need different virtualenvs or different Python versions,
- a driver or its vendor library is prone to crashing, and you do not want it
  to take the others down,
- one of them has its own window,
- you want to restart one without interrupting the others,
- their drivers' folders each have a module of the same name, such as two
  `bus_server.py`: Python keeps one module per name, so insthub refuses to
  load the second into the same server. It checks only the module that
  `driver` names: two drivers that each import a helper of the same name
  (two `utils.py`) silently share the first one's.

Device names must be unique across the servers of one computer, because that
is how clients find them.

## Reaching a server from another computer

By default a server can only be reached from its own computer, and only by
your user account. To reach it from elsewhere, give it a TCP port:

```toml
[server]
tcp = "0.0.0.0:8765"
```

or `insthub.serve(..., tcp="0.0.0.0:8765")` in a script. On the other computer,
either give the address:

```python
awg = insthub.device("awg", "tcp://192.168.1.20:8765")
```

or write it once into `~/.config/insthub/devices.toml`, after which the name
alone is enough:

```toml
[devices]
awg = "tcp://192.168.1.20:8765"
```

To use every device of a server there without listing each one, name the
server instead:

```toml
[servers]
labpc = "tcp://192.168.1.20:8765"
```

When a name is not found on this computer or in `[devices]`, insthub asks
the servers of `devices.toml` which devices they have (each gets about a
second to answer, and the answers are kept for 5 seconds, or until
`devices.toml` changes). The name `labpc` is for you: insthub calls a server
by the name it gives itself. `insthub list`, `insthub states`, the window and
AI agents show these servers and their devices too, and say which ones do
not answer. A server is shown once, however many addresses reach it, so
every computer of the lab can share one `devices.toml`: a computer's own
server, named there by its network address, is shown as this computer's.
If `devices.toml` cannot be read, they say so, naming the file, and go on
with this computer's servers.

There is no password and no encryption. Anyone who can reach the port can use
the instruments, so open a TCP port only on a network you trust.

## Checklist for a new instrument

1. Make the instrument work with a plain driver class and a test script. Do
   the operating-system setup it needs (permissions, kernel driver) in the
   driver's own project.
2. Put it on the bus: a few lines of TOML, `insthub.wrap(Driver, "name")`, or a
   `Device` subclass if you want streams and state.
3. Run the server in a terminal and try it: `insthub info name`,
   `insthub call name ...`. Give its settings units and limits (`meta`), and
   mark what is dangerous (`dangerous`), so that the window and AI agents
   handle it well ([every key](adding-a-device.md#every-key)).
4. When it works, `insthub service install` it (with `--no-login` if it is not
   always connected).
5. Use it from experiment scripts with `insthub.device("name")`.
6. To offer it to others, give its folder an `insthub-extension.toml` and
   check it with `insthub check FOLDER --import`
   ([share it as an extension](adding-a-device.md#8-share-it-as-an-extension);
   `examples/extension/` is a complete one to start from).

[Adding an instrument](adding-a-device.md) walks through each step, with every
key of a config file and what to do when something does not work.

## Where things are kept

| What | Where |
|---|---|
| Sockets and announcements of running servers | `$XDG_RUNTIME_DIR/insthub/` (for example `/run/user/1000/insthub/`); without `XDG_RUNTIME_DIR`, `/tmp/insthub-UID`, used only if it is your account's alone; override with `INSTHUB_DIR` |
| Addresses of devices and servers on other computers | `~/.config/insthub/devices.toml`; override with `INSTHUB_DEVICES` |
| A server's last 1000 log lines | in the server's memory; `insthub logs SERVER` reads them |
| Your list of extension folders | `~/.config/insthub/extensions.toml`, written by `insthub ext add`; `INSTHUB_EXTENSIONS` (folders) looks only there instead |
| The configs of the servers the window makes | `~/.config/insthub/servers/`, unless you choose another folder |
| Output of what the window or `insthub open` started | in the runtime folder (the first row): `NAME.log` for a server, `ui/DEVICE/UI.log` for a UI of a device's extension, `ui/DEVICE/_view.log` for its live view. Each run adds to its log, after a line that says when it started and what ran |
| Service definitions | `~/.config/systemd/user/insthub-NAME.service` |
| The insthub command for yourself | `~/.local/bin/insthub`; its environment in `~/.local/share/uv/tools/insthub/` |
| Results saved for AI agents by `insthub mcp` | `.insthub/` in the folder the agent was started in; override with `INSTHUB_DATA` or `--data` |
| (`~/.config` in the rows above) | `$XDG_CONFIG_HOME`, if it is set |
| The launcher of `insthub gui` | `~/.local/share/applications/insthub.desktop`, its icon under `~/.local/share/icons/`, a copy on the desktop, and `~/.config/autostart/insthub.desktop` with Start at login |

A server and its clients must use the same first folder to find each other by
name. They do when both run under your account in a normal login session or as
user services. A program started in some other way, for example from `cron` or
as a system-wide service, may not have `XDG_RUNTIME_DIR` set; give both sides
the same `INSTHUB_DIR` in that case. Without either, insthub uses
`/tmp/insthub-UID`. Any account can make a folder in `/tmp`, so insthub
refuses that one (`insthub will not use /tmp/insthub-1000: ...`) if another
account made it or may write to it.

## Windows

insthub is written to run on Windows but has not been run there. The
differences: programs on one computer are connected through a local TCP port
instead of a Unix socket, which any user of that computer can reach, and
`insthub service` is not available.
