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

This writes a small *systemd user service*, starts it, and from then on:

- it starts by itself when you log in,
- it is restarted if it crashes,
- its output is kept and can be read with `insthub service logs lab`.

| Command | Effect |
|---|---|
| `insthub service install TARGET [--name NAME]` | register and start; run it again to replace the definition |
| `insthub service list` | what is installed and whether it is running |
| `insthub service logs NAME [-f]` | its output; `-f` follows it live |
| `insthub service remove NAME` | stop it and remove it |
| `systemctl --user restart insthub-NAME` | restart, after changing code or config |
| `systemctl --user stop insthub-NAME` | stop until the next login or `start` |

Nothing here needs administrator rights. Two details:

- **Starting at boot.** User services start at login. To have them start when
  the computer boots, with nobody logged in, turn on *lingering* once:
  `sudo loginctl enable-linger $USER`. `insthub service install` tells you if
  it is off.
- **Which Python.** The service runs with the Python interpreter that ran
  `insthub service install`. If a server needs the packages of a particular
  virtualenv, install it with that virtualenv's `insthub`:

  ```bash
  ~/Code/mydriver/.venv/bin/insthub service install ~/Code/mydriver/server.toml
  ```

This needs Linux with systemd. On other systems, start servers with the
system's own autostart mechanism.

**Servers with a window** are different. A program that shows a GUI belongs to
your desktop session, so start it from a launcher or the desktop's autostart,
not as a service.

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
| A desktop icon for an application with a window | a `.desktop` launcher | **that application** |
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
- you want to restart one without interrupting the others.

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

There is no password and no encryption. Anyone who can reach the port can use
the instruments, so open a TCP port only on a network you trust.

## Checklist for a new instrument

1. Make the instrument work with a plain driver class and a test script. Do
   the operating-system setup it needs (permissions, kernel driver) in the
   driver's own project.
2. Put it on the bus: `insthub.wrap(Driver, "name")`, a few lines of TOML, or a
   `Device` subclass if you want streams and state.
3. Run the server in a terminal and try it: `insthub info name`,
   `insthub call name ...`.
4. When it works, `insthub service install` it.
5. Use it from experiment scripts with `insthub.device("name")`.

## Where things are kept

| What | Where |
|---|---|
| Sockets and announcements of running servers | `$XDG_RUNTIME_DIR/insthub/` (for example `/run/user/1000/insthub/`); override with `INSTHUB_DIR` |
| Addresses of devices on other computers | `~/.config/insthub/devices.toml`; override with `INSTHUB_DEVICES` |
| Service definitions | `~/.config/systemd/user/insthub-NAME.service` |

A server and its clients must use the same first folder to find each other by
name. They do when both run under your account in a normal login session or as
user services. A program started in some other way, for example from `cron` or
as a system-wide service, may not have `XDG_RUNTIME_DIR` set; give both sides
the same `INSTHUB_DIR` in that case.

## Windows

insthub is written to run on Windows but has not been run there. The
differences: programs on one computer are connected through a local TCP port
instead of a Unix socket, which any user of that computer can reach, and
`insthub service` is not available.
