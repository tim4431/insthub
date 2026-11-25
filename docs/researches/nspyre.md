# nspyre

nspyre ("networked scientific python research environment") is a Python
toolkit for lab experiments from the Awschalom group at the University of
Chicago ([docs][doc-who]), under the BSD-3 license. It has three parts: an
*instrument server* that hosts plain Python driver classes for other programs,
a separate *data server* that passes experiment results from a script to any
number of viewers, and a kit of PyQt6 widgets. A second repository,
`template`, shows how a lab puts its own drivers, GUIs and experiments into
one package. nspyre is on PyPI and conda-forge, and its `pyproject.toml` says
version 0.6.1. It is small, because a library (RPyC) does the remote calls:
about 7,500 lines of Python (instrument server 762, data server 1,763, GUI
3,734) and about 480 lines of tests. We studied [nspyre at
`f029358`][nspyre-tree] (a dependency bump written in January 2024 and merged
on 30 January 2025) and [template at `f9d4296`][template-tree] (23 May 2023)
in November 2026, from shallow clones. Nothing from them was run.

On this page, *the study* is the first reading by one agent, and *the check*
is a second agent's verification of it. Where they disagree, the check wins
(see [what we learned from other projects](../design.md#what-we-learned-from-other-projects)).

| Word | Meaning |
|---|---|
| **RPyC** | A Python library for calling into another program. It makes objects in the other program look local. |
| **netref** | RPyC's stand-in for an object that stays in the other program. Each use of it is a trip over the network. |
| **broker** | A middleman program. Producers send data to it, and it forwards the data to readers. |
| **pickle** | Python's own format for saving objects. Loading pickled bytes can run arbitrary code. |

## How it is built

| Area | What nspyre does | Evidence |
|---|---|---|
| Process model | Instrument servers (a script calls `add()` for each driver), the data server `nspyre-dataserv`, and clients. The GUI runs each experiment in a child process. The template runs two instrument servers. | [template][t-local], [second server][t-remote], [data server][ds-class], [runner][misc-runner] |
| Transport | Instruments: RPyC's "classic" mode over TCP, port 42068, with pickle and full attribute access switched on. Data: a protocol of its own on port 30101, each message an 8-byte length and a pickled payload. | [port][srv-port], [RPyC config][srv-rpyc], [data port][ds-port], [framing][ds-frame], [pickle][pk-wire] |
| Discovery and naming | None. A client names a host and port. `InstrumentManager` merges several servers into one namespace, with `exclude` and renaming; the template hard-codes both ports. Data sets are free strings on the one data server. | [manager][mgr-register], [template][t-insmgr] |
| Concurrency | One thread per client connection, and no lock or queue per device, so two clients can be inside one driver at once. The data server is one `asyncio` loop with a queue of 5 per reader. Widgets update from Qt threads of their own. | [threads][srv-rpyc], [queue][ds-queue], [Qt thread][ts-obj], [update loop][upd-loop] |
| Live data | Only through the data server; devices have no streams. A script pushes a whole object, usually a dict. The docs ask that each push hold all data so far, so a lost push does no harm. The broker keeps the merged history and sends it first to each new reader. | [docs][doc-push], [seeding][ds-seed], [history][ds-squash] |
| Device abstraction | Any Python class: no base class, and no declared commands, properties or streams. The server creates it and calls `__enter__` if it has one. Clients reach every attribute through netrefs. | [add][srv-create], [client wrapper][gw-device] |
| Extension mechanism | None. A driver loads from a file path (its folder appended to `sys.path`, imported by file name) or from a dotted module path, and is reloaded if a module of that name is already loaded. A lab lists its widgets by hand in its own `app.py`. | [file][misc-file], [module][misc-str], [widget item][main-item], [template app][t-app-items] |
| UI | A PyQt6 and pyqtgraph kit inside the package: a dock launcher that creates a widget only when the user clicks **Load**, line plots, heat maps, a parameter form, and an experiment widget with Run, Stop and Kill. No generic device page. | [launcher][main-load], [flexible plot][flex-conv], [experiment][exp-run] |
| Configuration | Python only: a script calls `add()` per driver, or passes a list of `add()` arguments to `start_instrument_server`. Command-line flags set port and logging. | [template][t-local], [start][inserv-start] |
| Errors and reconnect | A client retries connecting every 0.5 s until a timeout, and reconnects once when the connection drops. Nothing handles a lost instrument: the remedy is a manual `restart` (remove, then add again with the module reloaded). | [connect][gw-connect], [reconnect][gw-getattr], [restart][srv-restart] |
| Observability | Each program logs to the console and to a rotating file. The instrument server's shell offers `list`, `del`, `restart` and `py`, which opens the Python debugger inside the server. No status call and no list of clients. | [logger][log-init], [rotation][log-rotate], [shell][inserv-shell] |
| Security | None, by design. RPyC classic mode lets any client run code, and the broker and every reader unpickle bytes from the network. Both servers bind to localhost only, although the docs show a reader on another computer. | [RPyC config][srv-rpyc], [broker][ds-unpickle], [reader][sink-unpickle], [bind][ds-bind], [remote example][sink-remote] |
| Footprint | Hard dependencies: numpy, rpyc, PyQt6 and pyqtgraph. `import nspyre` imports every widget, and fails where pyqtgraph is missing. | [dependencies][pyproj-deps], [`__init__`][init-gui], [`gui/__init__`][gui-init] |

## What insthub takes

These lessons survived the check. Its verdict is *adopt* (take it as
proposed), *adapt* (take a narrower form) or *consider later* (only when a
real need shows up). The text gives the proposal as it stood after the check,
and links the place where insthub decided it. Lessons 2, 3 and 4 came from the
check itself: the study had missed them.

| Lesson | Verdict | Where it stands |
|---|---|---|
| 1. A UI is named by a string and built only when opened | adapt | [plan](../design.md#the-plan-in-order) step 4 done; 7 and 8 to do |
| 2. A UI runs as a program of its own | adopt | step 8 |
| 3. "Light" is a test, not a habit | adopt | done (steps 1 and 2) |
| 4. A worked example, not more framework | adopt | step 9 |
| 5. Forms come from what devices already say | adapt | done for properties (step 5); commands show hints |
| 6. Write down the frame convention | adapt | a guide section, not yet written |
| 7. Results from a script: a recipe, not a broker | adapt | a guide section, not yet written |
| 8. Stopping a long run | consider later | possible today; pattern not yet written |
| 9. Context-manager drivers | consider later | when a driver needs it |
| 10. Swapping one device in a running server | consider later | when a server needs it |

**1. A UI is named by a string and built only when opened.** nspyre's dock
launcher holds `MainWidgetItem(module, "ClassName")` and creates the widget
only on **Load** ([item][main-item], [load][main-load]). But each lab writes
that list by hand in its `app.py`, which imports every widget module at start
([template][t-app-imports]). The study proposed a registry that matched a
panel to the device's `module:qualname`. The check showed that this fails (a
project run as `python bus_server.py` has the module `__main__`) and put a
string on the device instead. The design went one step further: a device
sends only its extension id, and the manifest on this computer names the UI,
because insthub never runs a module name that came from another computer. See [the
manifest](../design.md#the-manifest) and [how a device finds its
extension](../design.md#how-a-device-finds-its-extension). Entry points come
later, as one more [place to look](../design.md#where-insthub-looks-for-extensions).

**2. A UI runs as a program of its own.** nspyre runs each experiment in a
child process with Run, Stop and Kill, so a hung experiment cannot freeze the
GUI ([widget][exp-run], [runner][misc-runner]). It does not do this for
widgets: all of them are created in the one GUI process
([load][main-inproc]), so one that blocks freezes every dock. insthub starts
an extension's panel or app with the extension's Python, as the window already
starts `insthub view`. See [two halves, each where its libraries
are](../design.md#two-halves-each-where-its-libraries-are).

**3. "Light" is a test, not a habit.** `import nspyre` re-exports every widget
([`__init__`][init-gui]). The GUI module has a fallback for a missing
pyqtgraph, but defines the widgets only when pyqtgraph imports
([`gui/__init__`][gui-init]), so the package's own `__init__` fails without
it. Qt also reaches the headless instrument server: `Subsystem`, sequencing
logic, is a `QObject` ([subsystem][sub-qobject]), and the template loads a
driver built on it into the server ([import][t-subs-import],
[add][t-local-subs]). No test catches any of this. insthub now has
`tests/test_footprint.py`, and `insthub service install` warns when a driver
imports Qt. See [keeping the core light](../design.md#keeping-the-core-light)
and rule 2 of [rules inside an
extension](../design.md#rules-inside-an-extension).

**4. A worked example, not more framework.** Most of what makes nspyre usable
is its template: one package that depends on nspyre, with `drivers/`, `gui/`
and `experiments/` side by side and a console script
([pyproject][t-pyproject]). Its flaws are that it packages per lab, not per
instrument, and refers to drivers by file path ([template][t-subs-path]). In
insthub the unit is one instrument: a folder with a manifest, a skeleton in
`examples/extension/`, and `hp_8562a_gpib` converted as the reference. See
[what an extension is](../design.md#what-an-extension-is).

**5. Forms come from what devices already say.** Each nspyre experiment GUI
builds its form by hand, with units and limits as spin-box arguments
([template][t-elements]), read back through a table of getters per widget
type ([params][params-get]). The driver checks the same range again
([driver][t-odmr-range]). The form holds an experiment's keyword arguments,
not driver settings ([experiment][exp-kwargs]); applying it to device commands
is insthub's own step. Units and limits that live only in GUI code are
invisible to scripts and agents. insthub puts them in `describe()`, for
properties and for command parameters, with choices taken from `Literal` and
`Enum` annotations. The device page picks a control for each property (a list,
a spin box with the unit, a switch, the text field as the fallback), and
shows a command's parameter hints beside its signature; its arguments are
still typed as text. The check would have stopped at what signatures say; the
design added a fixed vocabulary (`unit`, `min`, `max`, `step`, `choices`) on
the evidence of ScopeFoundry and instrumentserver. See [a generic UI good
enough for most devices](../design.md#a-generic-ui-good-enough-for-most-devices).

**6. Write down the frame convention.** nspyre's flexible plot serves any data
set that follows a dict convention: `title`, `xlabel`, `ylabel`, `datasets`
([docstring][flex-conv]). insthub's viewer, its MCP plots and the MCP tool
description already share one: `t0`/`dt` or `x0`/`dx`, `xlabel`, `xunit` and
`unit`; a dict of 1-D arrays, or a 2-D array with few rows, is several lines;
a larger 2-D array is an image. It belongs in the [guide](../guide.md#streams)
as the contract an extension publishes to.

**7. Results from a script: a recipe, not a broker.** In nspyre a script
pushes results with `DataSource(name).push(obj)` ([push][src-push]), readers
attach by name, and one that joins late first gets the whole history
([seeding][ds-seed]), until a new source resets it ([reset][ds-reset]).
insthub needs no new API: a script hosts a bare `Device` in a `with
insthub.Server(...)` block and calls `publish`; the viewer, the tiles, MCP
`snapshot` and recorders work unchanged, and a late reader gets the retained
last frame. What insthub borrows is nspyre's rule ([docs][doc-push]):
each frame of a results stream is a complete snapshot, or carries its index in
`meta`, and a recorder subscribes with `queue=N`. A helper such as
`insthub.source()` comes only if the recipe is used often.

**8. Stopping a long run.** nspyre's Stop button puts `"stop"` on a queue, and
the experiment checks it between points ([widget][exp-stop],
[template][t-odmr-stop]). insthub can do this today: a command marked
`concurrent=True` is answered at once, even during a sweep
([answering while busy](../guide.md#what-the-base-class-does-for-you)), so it
can set a flag that the long command checks. A generic `$stop` comes only if
the window needs one Stop button for every device, and it must then target one
running command by its id.

**9. Context-manager drivers.** nspyre calls `__enter__` after creating a
driver and `__exit__` when removing it ([enter][srv-enter],
[exit][srv-exit]). insthub's `wrap()` calls the driver's `close` method. Every
lab driver with an `__enter__` also has `close()`, so nothing needs this yet.
When one does: if the class has `__enter__` and `__exit__` and no `close`,
`Wrapped.open` calls `__enter__()` and ignores its result, and `Wrapped.close`
calls `__exit__(None, None, None)`. No new option and no config key. Two
things not to copy: nspyre's call sits in `try`/`except AttributeError`, which
also hides an `AttributeError` raised inside `__enter__`, and its experiment
runner calls `__exit__()` with no arguments ([runner][misc-exit]).

**10. Swapping one device in a running server.** nspyre's `restart` is remove
plus add, with the module reloaded, and any client may call it
([restart][srv-restart], [test][test-restart]). insthub reloads no code in a
server and has no remote restart ([ideas we decided
against](../design.md#ideas-we-decided-against)). One server per instrument
project gives fault isolation and a fast loop: edit, `insthub service restart
NAME`, and clients resume by themselves. If a server with several devices must ever swap
one, the answer is an in-process `Server.remove(name)`, still with no reload.

## What insthub leaves, and why

The rows in quotes are in [ideas we decided
against](../design.md#ideas-we-decided-against).

| nspyre does | Evidence | Why insthub leaves it |
|---|---|---|
| A separate data server that must run first | The broker unpickles every push ([unpickle][ds-unpickle]) and pickles it again for each reader ([repickle][ds-repickle]) | One more program to start and to fail, and arrays copied through a middleman. See "A central broker". |
| Diff streaming with `StreamingList` | Changes inside an item go unseen unless flagged by hand ([docstring][sl-doc]); `clear()` deletes indices in ascending order, so it raises `IndexError` for two or more items ([clear][sl-clear]); the merged history only grows ([squash][pk-squash]) | It exists to make a pickle broker fast enough. insthub sends arrays out of band; a growing data set publishes snapshots, or increments read with `queue=N`. See "Sending changes instead of whole values". |
| Transparent remote objects (netrefs) | Template drivers import rpyc and call `obtain()` on their arguments ([import][t-obtain-5], [one use][t-obtain-60]), as the docs advise ([docs][doc-obtain]); pint needed a patch to RPyC's serializer ([patch][pint-patch]); the client looks a device up again on every attribute ([wrapper][gw-device]) | The network leaks into driver code. insthub copies plain values of a fixed set of types. See "pickle, `eval`, or transparent remote objects". |
| Code execution made safe by binding to localhost | [RPyC config][srv-rpyc]; any client may `add()` a driver from any file path ([add][srv-add-sig]); the docs show a reader on another computer ([example][sink-remote]) | Remote use then needs a tunnel, or a change that opens code execution to the network. insthub never runs a module name that came from another computer ([security](../design.md#how-a-device-finds-its-extension)). |
| No serialization per device | One thread per connection, and no lock or queue in `instrument/` ([server][srv-rpyc]) | Two clients interleave inside one driver. insthub gives each device one thread and one waiting line ([the whole picture](../architecture.md#the-whole-picture)). |
| Drivers loaded by file path, with reload | The folder joins `sys.path` on every add and an existing module of that name is reloaded ([file][misc-file]); dotted paths reload on every add ([module][misc-str]) | Two `driver.py` files collide, and the class changes under a running device. insthub's loader reports collisions (rule 4 of [rules inside an extension](../design.md#rules-inside-an-extension)). See "Reloading driver code inside a running server". |
| A boot graph between devices (`Subsystem`) | Dependencies boot first ([boot][sub-boot]); shutdown refuses while a dependent runs ([shutdown][sub-shutdown]) | Ordering in the core makes one device's thread wait for another. A power-up sequence is a device of its own, in an extension. Two devices must never call each other synchronously, or they deadlock. See "start-up ordering between devices". |
| An experiment runner in the toolkit | [runner][misc-runexp], [widget][exp-run] | Experiment logic belongs to experiments. See "A measurement framework ... or experiment runner". |
| Attribute access by magic | `gateway.<device>` ([gateway][gw-getattr]), `sink.<key>` ([sink][sink-getattr]), `params.<name>` ([params][params-getattr]) | Generic tools cannot tell what exists, and mistakes become puzzling `AttributeError`s. insthub devices describe themselves. |
| The version in three places | `setup.py` says 0.6.0.1 and rpyc below 6 ([25][setup-rpyc], [51][setup-version]); `pyproject.toml` says 0.6.1 and rpyc below 7 ([7][pyproj-version], [38][pyproj-rpyc]); `__version__` says 0.6.0 ([43][init-version]) | Extensions will state which insthub they need, so insthub keeps its version in one place, `insthub/_version.py`. |

## Checked and corrected

The check found no false claim about nspyre or insthub; its corrections of
detail are folded into the lessons above. The line-by-line reading for this
page found two:

- **The studied commit is newer than the study said.** It was written in
  January 2024 but merged on 30 January 2025.
- **A slow reader loses no packets, but can be dropped.** nspyre's docs say
  the data server throws away a slow reader's older packets ([docs][doc-drop]).
  The code merges that reader's queued diffs into one, and drops the reader
  once the merge exceeds 10,000 operations ([server][ds-slow],
  [limit][pk-maxdiff], [check][pk-limit]).

[nspyre-tree]: https://github.com/nspyre-org/nspyre/tree/f029358819049b6e2e071b659016a82ff07b20b5
[template-tree]: https://github.com/nspyre-org/template/tree/f9d429619c0de191bb3d6c7535384792bd54e9a4
[doc-who]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/docs/source/index.rst#L89-L91
[init-gui]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/__init__.py#L8-L31
[init-version]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/__init__.py#L43
[gui-init]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/__init__.py#L6-L43
[pyproj-version]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/pyproject.toml#L7
[pyproj-deps]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/pyproject.toml#L35-L40
[pyproj-rpyc]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/pyproject.toml#L38
[setup-rpyc]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/setup.py#L25
[setup-version]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/setup.py#L51
[srv-port]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/instrument/server.py#L33
[srv-add-sig]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/instrument/server.py#L102-L111
[srv-create]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/instrument/server.py#L187-L200
[srv-enter]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/instrument/server.py#L196-L200
[srv-exit]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/instrument/server.py#L228-L232
[srv-restart]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/instrument/server.py#L236-L268
[srv-rpyc]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/instrument/server.py#L289-L301
[gw-connect]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/instrument/gateway.py#L111-L156
[gw-getattr]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/instrument/gateway.py#L186-L210
[gw-device]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/instrument/gateway.py#L222-L282
[mgr-register]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/instrument/manager.py#L36-L89
[test-restart]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/tests/tests/instrument_server/test_inserv.py#L34-L40
[misc-str]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/misc/misc.py#L94-L107
[misc-file]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/misc/misc.py#L110-L129
[misc-runner]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/misc/misc.py#L179-L224
[misc-runexp]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/misc/misc.py#L227-L260
[misc-exit]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/misc/misc.py#L258-L260
[pint-patch]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/misc/pint.py#L21-L59
[log-init]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/misc/logging.py#L47-L54
[log-rotate]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/misc/logging.py#L126-L129
[inserv-shell]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/cli/inserv.py#L51-L116
[inserv-start]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/cli/inserv.py#L128-L136
[ds-port]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/server.py#L39
[ds-frame]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/server.py#L69-L120
[ds-queue]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/server.py#L181
[ds-seed]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/server.py#L189-L190
[ds-reset]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/server.py#L203-L204
[ds-unpickle]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/server.py#L224
[ds-squash]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/server.py#L226
[ds-slow]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/server.py#L230-L247
[ds-repickle]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/server.py#L285
[ds-class]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/server.py#L308-L391
[ds-bind]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/server.py#L383-L385
[sink-unpickle]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/sink.py#L203
[sink-remote]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/sink.py#L308-L309
[sink-getattr]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/sink.py#L364-L375
[src-push]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/source.py#L214-L239
[pk-maxdiff]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/streaming/_pickle.py#L20
[pk-squash]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/streaming/_pickle.py#L46-L61
[pk-limit]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/streaming/_pickle.py#L94-L95
[pk-wire]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/streaming/_pickle.py#L201-L222
[sl-doc]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/streaming/list.py#L11-L42
[sl-clear]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/data/streaming/list.py#L164-L167
[doc-push]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/docs/source/data_server.rst#L64-L99
[doc-drop]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/docs/source/data_server.rst#L107-L111
[doc-obtain]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/docs/source/instrument_server.rst#L74-L92
[main-item]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/widgets/main.py#L19-L54
[main-load]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/widgets/main.py#L212-L236
[main-inproc]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/widgets/main.py#L226-L231
[exp-run]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/widgets/experiment.py#L129-L180
[exp-kwargs]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/widgets/experiment.py#L144
[exp-stop]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/widgets/experiment.py#L158-L171
[params-get]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/widgets/params.py#L107-L140
[params-getattr]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/widgets/params.py#L191-L207
[flex-conv]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/widgets/flex_line_plot.py#L148-L158
[ts-obj]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/threadsafe.py#L63-L85
[upd-loop]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/widgets/update_loop.py#L62-L75
[sub-qobject]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/extras/subsystem.py#L8-L23
[sub-boot]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/extras/subsystem.py#L162-L172
[sub-shutdown]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/extras/subsystem.py#L198-L205
[t-pyproject]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/pyproject.toml#L18-L26
[t-app-imports]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/gui/app.py#L8-L21
[t-app-items]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/gui/app.py#L40-L59
[t-elements]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/gui/elements.py#L16-L49
[t-local]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/drivers/local_inserv.py#L26-L31
[t-local-subs]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/drivers/local_inserv.py#L27
[t-remote]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/drivers/remote_inserv.py#L26
[t-insmgr]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/drivers/insmgr.py#L3-L7
[t-subs-import]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/drivers/subsystems_driver.py#L5
[t-subs-path]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/drivers/subsystems_driver.py#L21-L28
[t-obtain-5]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/drivers/fake_odmr_driver.py#L5
[t-obtain-60]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/drivers/fake_odmr_driver.py#L60
[t-odmr-range]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/drivers/fake_odmr_driver.py#L58-L63
[t-odmr-stop]: https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/experiments/odmr.py#L117-L119
