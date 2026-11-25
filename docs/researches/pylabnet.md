# PyLabNet

PyLabNet is the Python control software of the Lukin group at Harvard, written
for their solid-state quantum optics and quantum network experiments. It is one
large repository: 58.7k lines of Python in 310 files under `pylabnet/`, with 59
driver files, 41 Qt Designer screens and 21.9k lines of experiment scripts. It
is at version 1.0.0 and calls itself *Alpha*
([`setup.py:60`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/setup.py#L60)),
but one group uses it day to day. Each device is a server program that speaks
*rpyc*, a Python library for calling methods on objects in another program. A
central *master* program collects every program's log and is also the list of
running servers. A Qt window called Launch Control starts servers and scripts
from JSON files, on the same PC or over SSH. We studied commit
[`f2d38c3`](https://github.com/lukingroup/pylabnet/tree/f2d38c38c2f884820e412f963597ab69213c92fb)
(26 May 2026) from its code, in November 2026.

For insthub it is mostly a warning. Its two good ideas are about running a lab:
one place to read what every program on every computer says, crashes included,
and one list of everything that runs in the lab.

On this page, *the study* is the first reading by one agent, and *the check* is
the second agent's verification of it. Where they disagree, the check wins (see
[what we learned from other projects](../design.md#what-we-learned-from-other-projects)).
Line references are links to the studied commit.

## How it is built

| Area | What PyLabNet does | Evidence |
|---|---|---|
| Programs | One master Launch Control (PyQt5) runs the log server and a "GUI server" that exposes its own widgets. Other PCs run a proxy Launch Control that mirrors the master. Each device server and each script is a program of its own; a script program connects to the servers it needs, then calls the script's `launch()` with a dict of clients. | [`launch_control.py:224-227`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L224-L227), [`launch_control.py:377-397`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L377-L397), [`setup.py:51-58`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/setup.py#L51-L58), [`launcher.py:387-407`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launcher.py#L387-L407) |
| Transport | rpyc over TCP with `allow_public_attrs: True`. Arrays and dicts are pickled by hand in each device's Service and Client (132 lines of `network/client_server/` mention `pickle`). Ports are picked at random between 1024 and 49151. | [`generic_server.py:45-53`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/core/generic_server.py#L45-L53), [`client_server/external_gui.py:59-74`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/client_server/external_gui.py#L59-L74), [`helper_methods.py:252-279`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L252-L279) |
| Finding servers | The master's log server is the only registry. Each program posts `{ip, timestamp, port}` under a tag such as `<type>_server_<device_id>`. A taken name gets a number appended. Scripts find servers by name prefix plus `device_id`. The README: "all pylabnet communication will require the master logger to be online". | [`logger.py:211-227`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L211-L227), [`logger.py:450-458`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L450-L458), [`launcher.py:240-250`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launcher.py#L240-L250), [`README.md:124`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/README.md#L124) |
| Threads | rpyc's `ThreadedServer` starts a thread per connection, and that thread calls the driver directly. Nothing serializes access: `mutex.py` holds one line, `# TODO: implement mutex`. | [`generic_server.py:66-77`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/core/generic_server.py#L66-L77), [`mutex.py:1`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/core/mutex.py#L1) |
| Live data | No publish and subscribe. Scripts poll getters in loops and push the results into widgets. A proxy Launch Control polls the master's log text every 1 ms. | [`monitor_counts.py:263-267`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/scripts/counter/monitor_counts.py#L263-L267), [`launch_control.py:1100-1111`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L1100-L1111) |
| Devices | Drivers are plain classes that take a logger. Six abstract interfaces exist, and few drivers follow them. A `dummy_wrap` decorator turns driver methods into log-only no-ops, for running without hardware. | [`synthhd.py:5-20`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/hardware/cw_mw/windfreak/synthhd.py#L5-L20), [`mw_src.py:4-31`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/hardware/interface/mw_src.py#L4-L31), [`dummy_wrapper.py:6-24`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/decorators/dummy_wrapper.py#L6-L24) |
| Extensions | None. A device is a type name reused across four to six folders: driver, Service and Client, `launch()` module, configs, GUI. Code is imported by that name. | [`pylabnet_server.py:104`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/pylabnet_server.py#L104), [`launcher.py:204-220`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launcher.py#L204-L220), [`servers/synthhd.py:1-20`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/servers/synthhd.py#L1-L20) |
| UI | Launch Control shows the log, the list of programs (their data as tooltips) and file trees of the configs; a double-click starts one. Device GUIs are scripts that load a `.ui` file from a folder inside the package. Staticline devices get a generated GUI. | [`launch_control.py:781-806`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L781-L806), [`external_gui.py:619-629`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/gui/pyqt/external_gui.py#L619-L629), [`gui_windowbuilder.py:56-111`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/gui/pyqt/gui_windowbuilder.py#L56-L111) |
| Configuration | JSON files in `<package>/configs`, kept out of git by a `.gitignore` there. A script's file lists the servers it needs and the script's path. A device's folder name must equal its module names. Secrets (the SSH password, Slack and Confluence tokens) go in a `.env` file, read with python-decouple. | [`helper_methods.py:496-503`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L496-L503), [`configs/.gitignore:1-2`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/configs/.gitignore#L1-L2), [`launcher.py:15-37`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launcher.py#L15-L37), [`slackbot.py:7`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/slackbot/slackbot.py#L7), [`external_gui.py:1584-1588`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/gui/pyqt/external_gui.py#L1584-L1588) |
| Errors | Swallowed at many layers: every log call sits in a bare `except` that returns -1, and a client that times out prints and carries on. Uncaught exceptions do reach the central log, through `sys.excepthook`. | [`logger.py:41-75`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L41-L75), [`client_base.py:83-116`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/core/client_base.py#L83-L116), [`pylabnet_server.py:95-100`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/pylabnet_server.py#L95-L100) |
| Observability | One central log; each line carries the program's tag and `LAB:<name>`. The master starts a new dated log file each day and at 50 MB. A key-value metadata store on the master is saved as JSON next to data files. Slack messages. | [`logger.py:288-299`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L288-L299), [`launch_control.py:181`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L181), [`launch_control.py:510-521`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L510-L521), [`helper_methods.py:414-433`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L414-L433), [`logger.py:253-260`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L253-L260) |
| Security | TLS on by default, with one self-signed `pylabnet.pem` on every PC in a fixed system folder. Received data is unpickled. One SSH password serves every lab PC. Any client can call `close_server` and kill a server. | [`generic_server.py:57-82`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/core/generic_server.py#L57-L82), [`logger.py:340-345`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L340-L345), [`helper_methods.py:790-794`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L790-L794), [`service_base.py:30-40`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/core/service_base.py#L30-L40) |
| Footprint | 23 required packages, among them PyQt5, pyqtgraph, paramiko, nidaqmx, slack-sdk and the Confluence API; `requirements.txt` pins 134. Python is capped below 3.9 because of one vendor library, `zhinst`. Importing a typical driver loads Qt, SSH and Slack through shared helpers. | [`setup.py:50`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/setup.py#L50), [`setup.py:67-90`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/setup.py#L67-L90), [`helper_methods.py:12-18`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L12-L18) |

## What insthub takes

These lessons survived the check. Its verdict is *adopt* (take it as
proposed), *adapt* (take a narrower form) or *consider later* (only when a real
need shows up). The last column links the place where the decision is written
down; this page gives only the evidence.

| Lesson | Verdict | Decided in |
|---|---|---|
| 1. Extension UIs run as programs, with their extension's Python | adopt | [two halves](../design.md#two-halves-each-where-its-libraries-are) |
| 2. A test guards what `import insthub` loads | adopt | [keeping the core light](../design.md#keeping-the-core-light), plan step 1 (done) |
| 3. A device names its extension; its UI is found on this computer | adapt | [how a device finds its extension](../design.md#how-a-device-finds-its-extension), plan steps 4 (done), 7, 8 |
| 4. A server keeps its last log lines, readable from any computer | adapt | [plan step 10](../design.md#the-plan-in-order) |
| 5. Remote server addresses kept in one place | adapt | [plan step 10](../design.md#the-plan-in-order) |
| 6. Metadata for properties, so simple devices need no UI code | adapt | [a generic UI](../design.md#a-generic-ui-good-enough-for-most-devices), plan step 5 (done) |
| 7. Every device's settings saved next to a data file | adapt | [plan step 10](../design.md#the-plan-in-order) |
| 8. A lock on the hardware resource | adapt, then deferred | [the plan](../design.md#the-plan-in-order), "only when a real need shows up" |
| 9. Authenticated access from other computers | consider later | [the plan](../design.md#the-plan-in-order), "passwords for TCP" |

**1. Extension UIs run as programs, with their extension's Python.** PyLabNet
runs everything in one Python environment. Launch Control starts local servers
and scripts with its own interpreter
([`helper_methods.py:827`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L827),
[`helper_methods.py:910-912`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L910-L912)).
So one vendor library sets the Python version for every user:
`python_requires='>=3.7, <3.9'`, "3.9 and above requires newer version of
zhinst"
([`setup.py:50`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/setup.py#L50)).
insthub's parts already live in different environments: each project's `.venv`
and the window's driver-less `uv tool` environment. The study missed this and
proposed scanning entry points inside the window and embedding panels there.
In the documented setup that scan finds nothing, and a crashing panel would
take the window down. The check made "a UI is a program, started with its
extension's Python" the main contract.

**2. A test guards what `import insthub` loads.** PyLabNet's weight comes from
one chain of shared helpers. `helper_methods.py` imports paramiko,
python-decouple and pyqtgraph at the top
([`helper_methods.py:12-18`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L12-L18)).
The logger imports it and the Slack bot
([`logger.py:11-12`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L11-L12),
[`slackbot.py:1-3`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/slackbot/slackbot.py#L1-L3)),
and 48 Python files under `hardware/` import the logger
([`synthhd.py:2`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/hardware/cw_mw/windfreak/synthhd.py#L2)
is one). Nothing in the repository notices. The study named this; the check
turned it into a change for insthub: a footprint test, and `asyncio` imported
only where it is awaited. Both are done (`tests/test_footprint.py`).

**3. A device names its extension; its UI is found on this computer.**
PyLabNet's `LogClient` takes a `ui` argument and stores it in the registry
([`logger.py:132`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L132),
[`logger.py:217-218`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L217-L218)),
but screens are loaded by name from a folder inside the package
([`external_gui.py:68`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/gui/pyqt/external_gui.py#L68)).
So a device's GUI cannot be installed or versioned with its driver. The field
turned out to be a stub (see [checked and corrected](#checked-and-corrected)),
so PyLabNet shows the gap, not a solution; the case for the change rests on
insthub's [rule 2](../design.md#the-three-rules). The check kept the design: an
explicit `extension` name on the device, published in `describe()`, matched only
against extensions found on this computer. It must be explicit, because a
class's module is `__main__` when a project runs `python bus_server.py`.
That part is done: `describe()` now carries `extension`, `extension_version`
and `simulated`. design.md then chose a manifest file over entry points,
which come later
([where insthub looks](../design.md#where-insthub-looks-for-extensions)).

**4. A server keeps its last log lines, readable from any computer.**
PyLabNet's most useful feature for running a lab is one window that shows what
every server and script on every PC says, filtered by text and by lab
([`launch_control.py:540-590`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L540-L590)).
Crashes reach it through `sys.excepthook`
([`launcher.py:96-103`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launcher.py#L96-L103)).
How it is built is the warning: each log line is a blocking network call to the
master, retried once
([`logger.py:288-323`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L288-L323)),
and the master must be running. The check narrowed the proposal to a *ring
buffer* (a fixed-size list that drops its oldest entries) of about 1000 records
in each server, a `logs()` server call, `insthub logs SERVER`, and excepthooks
in `insthub serve`. It dropped a live `log` stream: stream locks are not
reentrant, so a log handler that publishes could deadlock inside a publish, and
the window already shows `last_error`.

**5. Remote server addresses kept in one place.** In PyLabNet every program
registers with the master, so every PC sees every server in the lab with its
address, `device_id` and lab
([`pylabnet_server.py:76-79`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/pylabnet_server.py#L76-L79)).
insthub already gets most of this without a master (see [checked and
corrected](#checked-and-corrected)). What is left is one helper for the
addresses in `devices.toml`, used by `locate()`, `discover()`, `insthub list`,
the window and MCP. Lab tags were dropped: a small lab has no use for them.

**6. Metadata for properties, so simple devices need no UI code.** For
*staticlines* (shutters, switches, RF on and off, analog setpoints) PyLabNet
builds the window from the config: on and off buttons, or a value and Apply,
per declared type
([`gui_windowbuilder.py:95-107`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/gui/pyqt/gui_windowbuilder.py#L95-L107),
[`CHANGELOG.md:11`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/CHANGELOG.md#L11)).
One driver offers `up`, `down`, `set_value` and `set_dig_value`
([`staticline.py:54-72`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/hardware/staticline/staticline.py#L54-L72)).
But each instrument needs an adapter class listed in a table in the core: 22
names for 20 classes so far
([`staticline_devices.py:650-673`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/hardware/staticline/staticline_devices.py#L650-L673)).
The lesson: declared meaning replaces much hand-built UI, but the device must
declare it, not a table in the core. The check proposed a free-form hints dict;
design.md chose a small fixed vocabulary (`unit`, `min`, `max`, `step`,
`choices`) for properties and command parameters, and it is built.

**7. Every device's settings saved next to a data file.** The master also keeps
a key-value metadata store
([`logger.py:529-537`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L529-L537)).
Scripts push values into it, for example a laser setpoint
([`wlm_monitor.py:650-653`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/scripts/lasers/wlm_monitor.py#L650-L653)),
and `save_metadata` writes it as JSON beside each dataset
([`helper_methods.py:414-433`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L414-L433)).
It holds what scripts remembered to push, not what the instruments report.
insthub has the better source, the state each device publishes with
`set_state()`; missing is one call, `insthub.states()`, that collects them all.

**8. A lock on the hardware resource (deferred).** PyLabNet names a server by
its hardware address (`device_id`, such as a GPIB address), and a script reuses
a running server with the same type and `device_id` instead of opening the
instrument again
([`launcher.py:240-250`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launcher.py#L240-L250)).
It does not stop two owners (see below). The check proposed an opt-in
`resource` string per device and an operating-system file lock (`fcntl.flock`)
in insthub's runtime folder, taken just before `open()`. The kernel releases it
when the program dies, and a plain script could join in with
`insthub.claim(resource)`. design.md waits until someone actually hits the
conflict.

**9. Authenticated access from other computers.** PyLabNet made TLS the default
in 0.3.0
([`CHANGELOG.md:27`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/CHANGELOG.md#L27)),
which forced a setup step on every PC and still checks no client (see below).
The check proposed no code first: reach the server through an SSH tunnel,
which authenticates with the user's own keys and encrypts. Built-in protection
comes only on real need: opt-in, loaded only when used, and checking every
client, which PyLabNet's does not. design.md documents the tunnel and lists the later step as
"passwords for TCP", a shared key checked once per connection. See also
[what insthub does not do](../architecture.md#what-insthub-does-not-do).

## What insthub leaves, and why

Proposals the check rejected:

| Proposal | Why not |
|---|---|
| Starting servers on other PCs over SSH, from one controller | PyLabNet logs in with paramiko and one password for all lab PCs ([`helper_methods.py:790-794`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L790-L794)); its README calls this "a big limitation" ([`README.md:92`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/README.md#L92)). It can run `taskkill /F /IM python.exe /T` on the remote PC ([`helper_methods.py:812-819`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L812-L819)) and builds shell strings for `Popen(shell=True)` ([`helper_methods.py:837-873`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L837-L873)). insthub's systemd user services already restart crashed servers and start at boot. From another PC: `ssh labpc ~/Code/proj/.venv/bin/insthub service start NAME`, with your own keys and the full path, since `~/.local/bin` may not be on the PATH of an ssh command. |
| Interfaces per kind of instrument in the core | `hardware/interface/` has six abstract interfaces that few drivers implement. The Windfreak source has `set_power(power, channel)` and `output_on` ([`synthhd.py:22`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/hardware/cw_mw/windfreak/synthhd.py#L22), [`synthhd.py:112`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/hardware/cw_mw/windfreak/synthhd.py#L112)) where `MWSrcInterface` asks for `on`, `off` and `set_pwr` ([`mw_src.py:12-31`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/hardware/interface/mw_src.py#L12-L31)). Interchangeable instruments came from staticline adapters anyway. See [ideas we decided against](../design.md#ideas-we-decided-against); shared contracts stay an [open question](../design.md#open-questions). |
| `insthub serve --debug`, which waits for a debugger | PyLabNet's `--debug 1` waits for ptvsd to attach ([`pylabnet_server.py:84-91`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/pylabnet_server.py#L84-L91)). debugpy's own command line does the same with no insthub code: `python -m debugpy --listen 5678 --wait-for-client -m insthub serve bus.toml`. In a service it would block a unit that systemd restarts. |

Patterns to avoid:

| Pattern | Evidence | insthub instead |
|---|---|---|
| Heavy libraries imported at the top of shared helpers | [`helper_methods.py:12-18`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L12-L18), [`external_gui.py:38-47`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/gui/pyqt/external_gui.py#L38-L47) | [keeping the core light](../design.md#keeping-the-core-light) |
| Every instrument's driver, server, launcher, GUI and config inside the framework, so one vendor library caps Python for all | [`setup.py:50`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/setup.py#L50); 47 files in `network/client_server/`, 46 in `launchers/servers/` | [where code lives](../design.md#where-code-lives), [extensions](../design.md#extensions) |
| Each driver method written out twice more, in a Service and a Client, plus a near-identical `launch()` per device | [`client_server/synthhd.py:5-47`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/client_server/synthhd.py#L5-L47), [`servers/synthhd.py:6-20`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/servers/synthhd.py#L6-L20) | `@command`, `wrap()` and `describe()` with a proxy built from it |
| Nothing serializes access to an instrument | [`mutex.py:1`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/core/mutex.py#L1) | [one thread and one waiting line per device](../architecture.md#threads-who-does-what) |
| One required master that is both log sink and registry; names get numbers appended and are matched by prefix | [`README.md:124`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/README.md#L124), [`logger.py:450-458`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L450-L458), [`launcher.py:245-249`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launcher.py#L245-L249) | [no central server](../architecture.md#why-it-is-built-this-way); [no broker](../design.md#ideas-we-decided-against) |
| GUI widgets as a network protocol: proxies poll the master's log text and parse `!~N~!` markers; scripts push pickled arrays into named widgets | [`launch_control.py:1100-1111`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L1100-L1111), [`client_server/external_gui.py:59-74`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/client_server/external_gui.py#L59-L74) | a UI is a client of commands and streams ([writing the UI half](../design.md#writing-the-ui-half)) |
| pickle on the wire, and `allow_public_attrs: True` | [`logger.py:340-345`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L340-L345), [`generic_server.py:49-52`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/core/generic_server.py#L49-L52) | msgpack with a fixed set of types ([ideas we decided against](../design.md#ideas-we-decided-against)) |
| Errors swallowed: a launch module logs a failed driver, carries on, and fails one line later on the unset `pm` | [`thorlabs_pm320e.py:25-41`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/servers/thorlabs_pm320e.py#L25-L41) | [errors travel back with the remote traceback](../architecture.md#when-things-go-wrong) |
| Configs inside the package folder; a folder name doubles as the module to import | [`helper_methods.py:496-503`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L496-L503), [`pylabnet_server.py:104`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/pylabnet_server.py#L104) | explicit `driver = "module:Class"` and [manifests](../design.md#the-manifest) |
| Any client may kill any server; closing the master's window kills them all | [`service_base.py:30-40`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/core/service_base.py#L30-L40), [`launch_control.py:55-59`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L55-L59), [`launch_control.py:476-487`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L476-L487) | systemd user services own each server's lifetime |
| Random ports and Windows console tricks in shared helpers | [`helper_methods.py:228-279`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L228-L279) | named Unix sockets and one fixed, opt-in TCP port |

## Checked and corrected

The check found these claims wrong. What the code shows:

- **"Each process announces which UI belongs to it."** Only Launch Control's own
  logger passes `ui=`
  ([`launch_control.py:209-215`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L209-L215)).
  Device servers
  ([`pylabnet_server.py:68-74`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/pylabnet_server.py#L68-L74))
  and script launchers
  ([`launcher.py:131`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launcher.py#L131))
  do not. The only code that acted on the field is commented out
  ([`launch_control.py:761-763`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L761-L763));
  the rest copies it out of tooltip text
  ([`launch_control.py:826-827`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L826-L827)).
  It is a stub, not evidence that people wanted a UI bound to each device.
- **"Matching by `device_id` stops two programs from owning one instrument."**
  It is only find-or-launch inside the script launcher. With several matching
  servers it asks the user, and may start one more
  ([`launcher.py:330-378`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launcher.py#L330-L378)).
  A double-click in Launch Control starts a server with no check at all
  ([`launch_control.py:690-701`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L690-L701)).
- **"Having `pylabnet.pem` is the credential."** The server builds rpyc's
  `SSLAuthenticator(key, key)` with no CA file
  ([`generic_server.py:74-76`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/network/core/generic_server.py#L74-L76)).
  rpyc then sets `ssl.CERT_NONE` (we read `rpyc/utils/authenticators.py` in
  rpyc 6.0.2), so the server never checks the client's certificate. It is
  encryption without client authentication.
- **About insthub: "a device added to a remote server later stays invisible
  until someone edits `devices.toml`."** The window groups `devices.toml`
  entries by address into one remote server and shows the devices that server
  reports, and MCP's `list_devices` asks every listed address. The real gaps
  are smaller: `client.locate()` knows only names written in `[devices]`,
  `discover()` ignores `devices.toml`, and `insthub list` says "no insthub
  server is running on this machine" even when `devices.toml` lists remote
  servers.
- **Counts.** `hardware/interface/` has six interfaces, not seven, and
  `requirements.txt` pins 134 packages, not about 150. Only `create_server`
  tries up to 1000 random ports
  ([`helper_methods.py:262-279`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L262-L279));
  a device server makes 10 attempts on one port, random only if none was given
  ([`pylabnet_server.py:109-124`](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/pylabnet_server.py#L109-L124)).
  Counted again for this note, the check's "49 files import the logger" is 48
  Python files and a notebook.
