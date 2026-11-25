# ARTIQ

ARTIQ (Advanced Real-Time Infrastructure for Quantum physics) is M-Labs'
control system for trapped-ion and cold-atom experiments, mature and in
production use in many labs ([setup.py:59][a-status]: *Production/Stable*).
Its real-time half is FPGA gateware, Rust firmware and a compiler for timed
code. We studied only its host half, which overlaps with insthub: a central
master, one program per slow instrument, and a Qt dashboard. The `artiq`
package has about 72,000 lines of Python without tests, plus about 24,000
lines of Rust. Its host programs talk through two split-out packages:
sipyco (3,100 lines, needs only numpy) and artiq-comtools (1,100 lines, the
controller manager). We read all three in November 2026:

| Repository | Commit | Date |
|---|---|---|
| [m-labs/artiq](https://github.com/m-labs/artiq) | `7d7b85f377e0c172ae3496f0c03cfb6cb85ba022` | 2026-01-18 |
| [M-Labs/sipyco](https://git.m-labs.hk/M-Labs/sipyco) 1.14 | `75f055c2ee29c09a49912e63ed49c7b7cae953c4` | 2026-09-28 |
| [M-Labs/artiq-comtools](https://git.m-labs.hk/M-Labs/artiq-comtools) | `919fa37475e16079dcfcbb1552bc75e995319acc` | 2026-11-12 |

The GitHub repository ends at the studied commit, "Bye bye GitHub": the code
is complete, and the README says the project moved to git.m-labs.hk
([README.rst:1][a-readme]). sipyco and artiq-comtools came from there, so
their permalinks point there. The decisions are in [the design](../design.md).

## Words used here

| Word | Meaning in ARTIQ |
|---|---|
| **master** | `artiq_master`, the one central program. It holds the device database, the datasets, the list of experiments and the scheduler. |
| **device database** | `device_db.py`, a Python file that names every device and says how to make it. |
| **controller** | A program of its own (`aqctl_*`) that holds one slow instrument and offers its methods as remote calls. Roughly an insthub server with one device. |
| **NDSP** | *Network device support package*: a separate package with one instrument's driver and controller. |
| **controller manager** | `artiq_ctlmgr`, one per PC. It starts and watches the controllers that the device database places on that PC. |
| **dataset** | A named value an experiment writes. The master keeps it and sends changes to whoever subscribed. |
| **applet** | A small program that shows datasets, usually inside a dock of the dashboard. |
| **sipyco** | The library for remote calls, publishing and logging that all of ARTIQ's host programs use. |
| **PYON** | sipyco's message format: JSON with tags for tuples, sets and numpy arrays. |

## How it is built

| Area | What ARTIQ does | Evidence |
|---|---|---|
| Process model | Hub and spoke. One master serves four TCP ports. Each experiment runs in a worker program the master starts; each slow instrument is a controller program. | [artiq_master.py:34-39][a-master-ports], [worker.py:86-92][a-worker] |
| Transport | TCP, optionally with mutual TLS. Each connection starts with a banner line such as `ARTIQ pc_rpc`, then carries one PYON message per line. | [pc_rpc.py:41][s-banner], [pyon.py:1-14][s-pyon] |
| Discovery and naming | None. The master executes `device_db.py`, which maps names to a local driver class or to a controller's host, port and start command. Each controller's port is chosen by hand. | [databases.py:14-31][a-ddb-validate-all], [device_db.py:45-65][a-ddb-example], [developing_a_ndsp.rst:226][a-ndsp-port] |
| Concurrency | One asyncio event loop per program. A controller runs each call on its loop, behind a lock unless `allow_parallel`, so one blocking driver call stops the whole controller. A client has one request in flight. | [pc_rpc.py:546-561][s-server-lock], [pc_rpc.py:583-628][s-dispatch], [pc_rpc.py:306-321][s-aclient-call] |
| Live data | No streams per device. Experiments write datasets; the master sends each change to subscribers (*sync_struct*); applets subscribe by dataset name. Logs and requests to dashboards go on a broadcast channel that drops messages when a queue is full. | [environment.py:392-422][a-set-dataset], [artiq_master.py:156-163][a-master-notify], [broadcast.py:95-105][s-broadcast] |
| Device abstraction | Two kinds. A *local* driver is built inside the experiment's program and can fetch other devices by name. A controller is any Python object; its public methods become remote calls. The protocol has no properties, streams or state. | [worker_db.py:24-62][a-create-device], [ttl.py:34-35][a-ttl], [pc_rpc.py:587-597][s-methodlist] |
| Extension mechanism | Several, unconnected, with no manifest. An NDSP is wired in by hand-written device database entries. Applets are command lines. Dashboard plugins are modules imported with `-p`, which fill a global dict of argument editors. | [developing_a_ndsp.rst:18-23][a-ndsp-parts], [applets.py:247-264][a-applet-tpl], [artiq_dashboard.py:132-135][a-dash-plugin] |
| UI | A Qt dashboard of docks. Applets are separate programs built on `SimpleApplet`. They run alone, or embedded in a dock by taking over their native window. | [simple.py:365-384][a-simple-run], [applets.py:266-274][a-applet-embed] |
| Configuration | The device database is executable Python with only its keys checked. Datasets persist in LMDB, saved every 30 s. Controllers take command-line flags. The dashboard's layout is saved per master. | [databases.py:14-31][a-ddb-validate-all], [databases.py:55-91][a-datasetdb], [artiq_dashboard.py:137-141][a-dash-state] |
| Errors and reconnect | Exceptions are re-raised in the caller. The controller manager restarts a controller that exits, with a growing delay, and kills one that stops answering `ping()`. The dashboard does not reconnect to the master. | [pc_rpc.py:306-321][s-aclient-call], [ctlmgr.py:57-120][c-ping], [artiq_dashboard.py:170-177][a-dash-noreconnect] |
| Observability | Logs are a channel. Child programs log to stderr; parents tag each line with its source; records go to the master, which passes them to every dashboard. | [logs.py:17-28][s-multiline], [logs.py:175-210][s-forwarder], [artiq_master.py:88][a-master-log] |
| Security | Optional mutual TLS, no authorization. Remote execution gives any connected client `exec()` in the controller. The server calls any attribute name a client sends. | [tools.py:289-313][s-ssl], [remote_exec.py:45-83][s-rexec], [pc_rpc.py:618-619][s-getattr] |
| Footprint | Heavy: scipy, h5py, lmdb, pygit2, llvmlite, qasync, pyqtgraph and PyQt6. sipyco and artiq-comtools were split out so small programs and controller PCs need not install ARTIQ. | [setup.py:13-20][a-deps], [using_data_interfaces.rst:98][a-comtools-doc] |

## What insthub takes

Each lesson went through a second reading, *the check*, which tested its
claims against both codebases and argued against it. Where it changed a
proposal, the changed one is given, then what insthub did with it.

### 1. UIs run as programs of their own, on a tiny base class

*Adapt.* Applets are separate programs "for modularity and resilience"
([using_data_interfaces.rst:49][a-applets-doc]). `SimpleApplet.run()` parses
the arguments, runs the loop, builds the widget and subscribes
([simple.py:365-384][a-simple-run]), so the entry point of `plot_xy` is seven
lines ([plot_xy.py:80-86][a-plotxy]). **insthub:** a UI is one function,
`make(device, parent=None) -> QWidget`, that `insthub panel` runs in a window
built by `qt.device_window`, with no embedding and no new client API.
Planned, step 8; [writing the UI half](../design.md#writing-the-ui-half).

### 2. The producer names its UI; the host resolves it, or falls back

*Adapt.* An experiment class may carry a string, `argument_ui`, that the
worker reports ([worker_impl.py:233-236][a-argui-worker]). Dashboard plugins
fill a dict of editors keyed by it ([experiments.py:532-537][a-argui-registry]),
and an unknown name falls back to the built-in editor with a warning
([experiments.py:600-609][a-argui-lookup]). **insthub** goes one step past
the check's `Device.ui` string: a device announces only its extension's id
(done, step 4), and a manifest found on this computer names the UI (steps 7
and 8). Nothing that came from another computer is run. [How a device finds its
extension](../design.md#how-a-device-finds-its-extension).

### 3. Say where extension code runs

*Adopt; found by the check.* ARTIQ runs host-side extension code in the
dashboard's Python: `${python}` in an applet's command is the dashboard's
`sys.executable` ([applets.py:250][a-applet-python]), and plugins are imported
into it ([artiq_dashboard.py:132-135][a-dash-plugin]). That needs one shared
environment. **insthub:** each project has its own `.venv` and the `insthub`
command has no drivers, so extension code runs with the extension's own
Python and folder, and the window only reads manifests and starts programs.
[Two halves](../design.md#two-halves-each-where-its-libraries-are).

### 4. Typed descriptions let one generic UI serve most cases

*Adapt; found by the check.* An argument's `describe()` is plain data, such
as `unit`, `step`, `min` and `max` for a `NumberValue`
([environment.py:202-213][a-numbervalue]). The dashboard picks a widget from
the type tag alone ([entries.py:671-688][a-procdesc]), for example a spin box
with a range and a unit suffix ([entries.py:274-289][a-numentry]).
**insthub:** properties and command parameters take optional `unit`, `min`,
`max`, `step` and `choices`. They are advice, not enforced; they choose the
device page's controls and tell agents the units. Done, step 5; [a generic
UI](../design.md#a-generic-ui-good-enough-for-most-devices).

### 5. A list of features in the first message

*Adopt.* A sipyco server's first message lists `"features": ["pyon_v2"]`; the
client opts in, and the server refuses what it does not know
([pc_rpc.py:649-676][s-server-hello], [pc_rpc.py:123-151][s-client-hello]).
That message stays plain JSON "always (past, present, and future)", which let
sipyco leave the `eval`-decoded PYON v1 without every program upgrading at
once. **insthub:** the hello carries `version` and `features`, clients ask
`client.supports()`, and both sides skip kinds of message they do not know.
Done, step 3; [versions](../design.md#versions).

### 6. An identity string that the author keeps stable

*Adapt.* A sipyco client names the target it wants, and a wrong name raises
`IncompatibleServer` with the valid ones ([pc_rpc.py:29-54][s-target]). The
author chooses the name; it is not derived from code. **insthub:**
`describe()["type"]` is a class name, the driver's for a `Wrapped` device
(`HP8562A`, not `SpectrumAnalyzer`). The check proposed an author-set
`model`; the design chose `extension`, with `extension_version` and
`simulated`. Done, step 4; [how a device finds its
extension](../design.md#how-a-device-finds-its-extension).

### 7. Guard the weight of the core

*Adopt.* `artiq` needs scipy, h5py, lmdb, pygit2, llvmlite and Qt even for
small tools ([setup.py:13-20][a-deps]). So sipyco was "split out to enable
light-weight programs" ([README.rst:1][s-readme]), and the controller manager
moved to artiq-comtools ([using_data_interfaces.rst:98][a-comtools-doc]):
late splits that left two more packages to release. **insthub:** a footprint
test checks what `import insthub` loads in a fresh Python, and `asyncio` is
imported only where it is awaited. Done, step 1; [keeping the core
light](../design.md#keeping-the-core-light).

### 8. Keep the output of every program you start

*Adapt.* Child programs log to stderr with each record's line count, so a
multi-line traceback survives being read line by line
([logs.py:17-28][s-multiline]). Parents tag each child's lines `applet(name)`
or `controller(name)` ([applets.py:237-242][a-applet-log],
[ctlmgr.py:82-105][c-log]). **insthub:** the window still discards the output
of `insthub view`. Programs it starts will log to a file, as servers already
do, and `insthub logs SERVER` will show a server's last lines. Planned, steps
8 and 10; [the plan](../design.md#the-plan-in-order).

### Considered for later

- **One package per instrument, found through entry points.** An NDSP holds a
  driver, a controller and an optional client
  ([developing_a_ndsp.rst:18-23][a-ndsp-parts]), runs without hardware under
  `--simulation` ([developing_a_ndsp.rst:215][a-ndsp-sim]), and lives in a
  repository of its own ([developing_a_ndsp.rst:231][a-ndsp-repo]). insthub's
  extension is the same unit. An entry-point index helps only once extensions
  are pip-installed into shared environments ([where insthub looks for
  extensions](../design.md#where-insthub-looks-for-extensions), item 5).
- **One lab-wide file that says what runs where.** The controller manager on
  each PC starts the controllers whose `host` is local
  ([ctlmgr.py:191-213][c-host]), pings each every 30 s, and stops one with
  `terminate`, SIGTERM, then SIGKILL ([ctlmgr.py:18-29][c-defaults],
  [ctlmgr.py:122-156][c-term]). For insthub, once a lab has several PCs: a
  one-shot `insthub service sync lab.toml`, no daemon, and systemd's
  `Type=notify` without a watchdog. Not in the plan yet.

## What insthub leaves, and why

| ARTIQ does | Evidence | Why insthub does not |
|---|---|---|
| A central master that holds devices, datasets, schedule and logs | [artiq_master.py:34-39][a-master-ports], [artiq_master.py:156-163][a-master-notify] | One more program that must run first. The dashboard cannot even reconnect to it: "restart dashboard to reconnect" ([artiq_dashboard.py:170-177][a-dash-noreconnect]). insthub keeps only the convention that a window takes its device name and address as arguments, so it works against a simulator or the real instrument. See [ideas we decided against](../design.md#ideas-we-decided-against). |
| Shared state sent as a stream of changes, through an unbounded queue per subscriber | [sync_struct.py:319-342][s-syncstruct] | One lost change corrupts the copy, so delivery must be lossless and queues unbounded. insthub already sends "the whole value first, then updates" without changes: a new subscriber gets the retained latest frame. See [ideas we decided against](../design.md#ideas-we-decided-against). |
| Embedding applet windows by taking over their native window | [simple.py:267-296][a-simple-hack], [artiq_dashboard.py:144-148][a-dash-wayland], [artiq_browser.py:143-147][a-browser-wayland] | It needs hacks ("Qt window embedding is ridiculously buggy"), and the dashboard and browser force X11 on Wayland "due to applets not embedding". An insthub UI is a top-level window of its own program. |
| Configuration as executable Python | [databases.py:14-31][a-ddb-validate-all] | It runs code when loaded, a GUI cannot edit it safely, and only its keys are checked. insthub's server files are TOML, and so are its planned manifests, read as data. |
| `eval` and `exec` within reach of the network | [remote_exec.py:45-83][s-rexec], [pyon_v1.py:238-244][s-pyonv1], [device_db.py:54-65][a-ddb-rexec] | With remote execution any client that connects can run code in the controller; TLS is optional and nothing checks who may do what. The example device database turns it on. insthub's codec has a fixed set of types. See [ideas we decided against](../design.md#ideas-we-decided-against). |
| Running command lines that arrive over the network | [ctlmgr.py:191-213][c-host], [ctlmgr.py:96-99][c-exec] | The controller manager runs the `command` of each device database entry it receives from the master. insthub never runs a module name or command line that came from another computer (see the security paragraph in [how a device finds its extension](../design.md#how-a-device-finds-its-extension)). |
| Calling any attribute a client names | [pc_rpc.py:618-619][s-getattr] | The method list hides names that start with `_` ([pc_rpc.py:587-597][s-methodlist]), but a call is `getattr(target, name)` with no check. An insthub server calls only the commands in the device's table and answers anything else with `UnknownCommand`. |
| A client that returns `None` when the network fails | [pc_rpc.py:447-462][s-besteffort-rpc] | A lost result looks like a real `None`. insthub raises `ConnectionLost` or `DeviceNotConnected` ([when things go wrong](../architecture.md#when-things-go-wrong)). |
| One request in flight per connection, and one connection per target | [pc_rpc.py:306-321][s-aclient-call], [artiq_dashboard.py:155-197][a-dash-conns] | insthub numbers its requests, so many are under way on one connection per server ([what happens during a call](../architecture.md#what-happens-during-a-call)). |
| TCP ports picked by hand and listed in the manual | [developing_a_ndsp.rst:226][a-ndsp-port] | Ports collide, and the list is kept by hand. An insthub server listens on a Unix socket named after it, and on TCP only when asked ([finding a device by name](../architecture.md#finding-a-device-by-name)). |
| Plugins loaded by a command-line flag into a global dict | [artiq_dashboard.py:132-135][a-dash-plugin], [experiments.py:532-537][a-argui-registry] | Nothing can list them, and every user must know the flags. insthub finds manifests as data ([where insthub looks for extensions](../design.md#where-insthub-looks-for-extensions)). |
| Health checks through a `ping()` that every driver must remember to write | [developing_a_ndsp.rst:138-142][a-ndsp-ping], [ctlmgr.py:67-80][c-wait-ping] | A forgotten method gets a healthy controller killed again and again. An insthub server answers `ping` itself, and `$status` shows each device. |

## Checked and corrected

The check found these claims of the first reading wrong.

- **The New server form** was said to need a typed driver string. It offers a
  list and a *From a file...* picker that reads a constructor's arguments
  without importing it ([`forms.py`](../../insthub/forms.py)). The entry-point
  index proposed instead would find nothing: the window's environment has no
  drivers, and the lab's projects are folders, not packages.
- **Starting a UI with the server's Python** was credited to ARTIQ, which does
  the opposite ([applets.py:250][a-applet-python]). It is insthub's own idea,
  and it also needs the extension's folder, since `hp_8562a_gpib` is not an
  installed package.
- **A new hello message from the client** was said to be ignored by older
  servers. When we studied it, insthub's server closed the connection on any
  message but a call. Step 3 has since made both sides skip unknown kinds of
message.
- **The class path as identity** reads `__main__` for a server run as
  `python bus_server.py`, and `kind` already means something in
  `describe()["streams"]`.
- **A server-level `$log` stream** was called cheap, but insthub has streams
  only per device; most failures already reach the window as `last_error`.
- **A reconnect banner "as in ARTIQ"**: ARTIQ's dashboard never reconnects
  ([artiq_dashboard.py:170-177][a-dash-noreconnect]). The proposed service
  watchdog would miss the likely hang, a device thread stuck in a vendor call.

<!-- ARTIQ, at the studied commit -->
[a-readme]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/README.rst?plain=1#L1
[a-status]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/setup.py#L59
[a-deps]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/setup.py#L13-L20
[a-master-ports]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/frontend/artiq_master.py#L34-L39
[a-master-log]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/frontend/artiq_master.py#L88
[a-master-notify]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/frontend/artiq_master.py#L156-L163
[a-worker]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/master/worker.py#L86-L92
[a-ddb-validate-all]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/master/databases.py#L14-L31
[a-datasetdb]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/master/databases.py#L55-L91
[a-ddb-example]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/examples/no_hardware/device_db.py#L45-L65
[a-ddb-rexec]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/examples/no_hardware/device_db.py#L54-L65
[a-create-device]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/master/worker_db.py#L24-L62
[a-ttl]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/coredevice/ttl.py#L34-L35
[a-set-dataset]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/language/environment.py#L392-L422
[a-numbervalue]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/language/environment.py#L202-L213
[a-procdesc]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/gui/entries.py#L671-L688
[a-numentry]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/gui/entries.py#L274-L289
[a-ndsp-parts]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/doc/manual/developing_a_ndsp.rst?plain=1#L18-L23
[a-ndsp-ping]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/doc/manual/developing_a_ndsp.rst?plain=1#L138-L142
[a-ndsp-sim]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/doc/manual/developing_a_ndsp.rst?plain=1#L215
[a-ndsp-port]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/doc/manual/developing_a_ndsp.rst?plain=1#L226
[a-ndsp-repo]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/doc/manual/developing_a_ndsp.rst?plain=1#L231
[a-applets-doc]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/doc/manual/using_data_interfaces.rst?plain=1#L49
[a-comtools-doc]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/doc/manual/using_data_interfaces.rst?plain=1#L98
[a-dash-plugin]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/frontend/artiq_dashboard.py#L132-L135
[a-dash-state]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/frontend/artiq_dashboard.py#L137-L141
[a-dash-wayland]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/frontend/artiq_dashboard.py#L144-L148
[a-dash-conns]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/frontend/artiq_dashboard.py#L155-L197
[a-dash-noreconnect]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/frontend/artiq_dashboard.py#L170-L177
[a-browser-wayland]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/frontend/artiq_browser.py#L143-L147
[a-argui-registry]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/dashboard/experiments.py#L532-L537
[a-argui-lookup]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/dashboard/experiments.py#L600-L609
[a-argui-worker]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/master/worker_impl.py#L233-L236
[a-applet-log]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/gui/applets.py#L237-L242
[a-applet-tpl]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/gui/applets.py#L247-L264
[a-applet-python]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/gui/applets.py#L250
[a-applet-embed]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/gui/applets.py#L266-L274
[a-simple-hack]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/applets/simple.py#L267-L296
[a-simple-run]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/applets/simple.py#L365-L384
[a-plotxy]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/applets/plot_xy.py#L80-L86

<!-- sipyco and artiq-comtools, at the studied commits, on git.m-labs.hk -->
[s-readme]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/README.rst?display=source#L1
[s-banner]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/pc_rpc.py#L41
[s-target]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/pc_rpc.py#L29-L54
[s-client-hello]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/pc_rpc.py#L123-L151
[s-aclient-call]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/pc_rpc.py#L306-L321
[s-besteffort-rpc]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/pc_rpc.py#L447-L462
[s-server-lock]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/pc_rpc.py#L546-L561
[s-dispatch]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/pc_rpc.py#L583-L628
[s-methodlist]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/pc_rpc.py#L587-L597
[s-getattr]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/pc_rpc.py#L618-L619
[s-server-hello]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/pc_rpc.py#L649-L676
[s-pyon]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/pyon.py#L1-L14
[s-pyonv1]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/pyon_v1.py#L238-L244
[s-syncstruct]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/sync_struct.py#L319-L342
[s-broadcast]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/broadcast.py#L95-L105
[s-multiline]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/logs.py#L17-L28
[s-forwarder]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/logs.py#L175-L210
[s-ssl]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/tools.py#L289-L313
[s-rexec]: https://git.m-labs.hk/M-Labs/sipyco/src/commit/75f055c2ee29c09a49912e63ed49c7b7cae953c4/sipyco/remote_exec.py#L45-L83
[c-defaults]: https://git.m-labs.hk/M-Labs/artiq-comtools/src/commit/919fa37475e16079dcfcbb1552bc75e995319acc/artiq_comtools/ctlmgr.py#L18-L29
[c-ping]: https://git.m-labs.hk/M-Labs/artiq-comtools/src/commit/919fa37475e16079dcfcbb1552bc75e995319acc/artiq_comtools/ctlmgr.py#L57-L120
[c-wait-ping]: https://git.m-labs.hk/M-Labs/artiq-comtools/src/commit/919fa37475e16079dcfcbb1552bc75e995319acc/artiq_comtools/ctlmgr.py#L67-L80
[c-log]: https://git.m-labs.hk/M-Labs/artiq-comtools/src/commit/919fa37475e16079dcfcbb1552bc75e995319acc/artiq_comtools/ctlmgr.py#L82-L105
[c-exec]: https://git.m-labs.hk/M-Labs/artiq-comtools/src/commit/919fa37475e16079dcfcbb1552bc75e995319acc/artiq_comtools/ctlmgr.py#L96-L99
[c-term]: https://git.m-labs.hk/M-Labs/artiq-comtools/src/commit/919fa37475e16079dcfcbb1552bc75e995319acc/artiq_comtools/ctlmgr.py#L122-L156
[c-host]: https://git.m-labs.hk/M-Labs/artiq-comtools/src/commit/919fa37475e16079dcfcbb1552bc75e995319acc/artiq_comtools/ctlmgr.py#L191-L213
