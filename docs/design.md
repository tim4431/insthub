# Design

This page is for whoever changes insthub, people and AI agents alike. It
states the three rules every change is judged by, the extension model that
follows from them, what we took from eleven other lab-control projects, and
the ideas we decided against, with the reasons.

For how insthub works today, read [how insthub works](architecture.md). Much
of this page is a plan: sections marked *proposed* describe plans, and the
rest describe insthub as it is.

- [The three rules](#the-three-rules)
- [Where code lives](#where-code-lives)
- [Extensions](#extensions) (manifests; finding, checking and starting them)
- [Recipes](#recipes) (snippets kept as Python functions, run by name)
- [A generic UI good enough for most devices](#a-generic-ui-good-enough-for-most-devices)
- [Keeping the core light](#keeping-the-core-light)
- [The plan, in order](#the-plan-in-order)
- [What we learned from other projects](#what-we-learned-from-other-projects)
- [Ideas we decided against](#ideas-we-decided-against)
- [Open questions](#open-questions)

## The three rules

**1. insthub knows no instrument.** Nothing in insthub is about a particular
scope, synthesizer or camera: no command strings, no vendor libraries, no
"if this is a Rigol". insthub knows *kinds* of things: devices, commands,
properties, streams, units. It never knows *which* instrument it is talking
to. The one exception is the simulated instruments of `insthub sim`: generic
test instruments with no vendor code, loaded only by `insthub sim` and the
tests.

**2. Everything about an instrument belongs in an extension.** An extension
holds a device's driver and, if it has one, its user interface, the way a VS
Code extension can hold both a language server and the views that show its
results. insthub provides the framework that finds extensions, joins their
parts and runs each part in the right place (see
[Extensions](#extensions)). The smallest extension is a driver class and four
lines of config.

**3. insthub stays light, because everything imports it.** Every driver
server, experiment script, notebook and GUI in the lab runs `import insthub`.
So each dependency, each millisecond of import time and each new concept is
paid in every one of them. Heavy things (Qt, plotting, and
`importlib.metadata`, which reads what packages are installed) stay optional
and load only in the programs that use them.

What these rules mean in practice:

| When you are about to... | Ask |
|---|---|
| add a feature | Could an extension do this instead? If only one instrument needs it, it belongs in that instrument's extension. |
| add a dependency | Would `import insthub` load it? Then don't add it: put it in an extra (`gui`, `mcp`) or in a module imported only where it is used. |
| add a key to `describe()` | Is it generic (a unit, a range) rather than about one instrument? Is it optional, so older programs ignore it? |
| add a concept | Will most users meet it? If not, it must cost nothing until someone uses it. |
| import something at module level in the core | Does every lab program need it? If not, import it inside the function that uses it. |

When a change conflicts with these rules, change this page first, on purpose,
rather than letting the code drift.

## Where code lives

There are three places. Code may use code in its own place and in the places
below it, never above. An arrow means "uses".

```mermaid
flowchart TB
    subgraph ext["Extensions: one folder per instrument, outside insthub"]
        ui["panel or app<br/>Qt"]
        dev["device<br/>Device or Wrapped subclass"]
        drv["driver<br/>plain Python + vendor library"]
    end
    subgraph opt["Optional parts of insthub: loaded only by the programs that use them"]
        gui["qt, controls, viewer, sim_ui, manager,<br/>pages, forms, widgets, inventory"]
        rest["cli, config, tomlfile, service, launcher, sim,<br/>bench, mcp, render, extensions, recipes"]
    end
    subgraph core["The core: what import insthub loads"]
        c["device, server, client, codec,<br/>wire, registry, errors<br/>needs only numpy and msgpack"]
    end
    ui --> gui
    ui --> c
    dev --> drv
    dev --> c
    gui --> c
    rest --> c
```

| Place | Loaded by | May depend on |
|---|---|---|
| **Core** | every lab program | the standard library, numpy, msgpack |
| **Optional parts of insthub** | the `insthub` command, the window (`insthub gui`), the MCP server, servers started from a `.toml` | the core, plus their extra (`gui`: Qt and pyqtgraph; `mcp`: matplotlib) |
| **Extensions** | the server that hosts the device, and the program that shows its UI | insthub, plus whatever their instrument needs |

An extension mechanism must keep this true.

## Extensions

*Status: implemented, except entry points. A device says which
extension it belongs to and whether it is simulated (step 4). Manifests, finding
extensions, `insthub ext` and `insthub check`, the folders in the servers'
`.json` files and the New server dialog's list are step 7
(`insthub/extensions.py`). Starting an extension's UIs (`insthub open`,
`insthub panel`, `qt.device_window`, the device page's **Open** buttons) is
step 8. A complete extension to copy, `examples/extension/` (a simulated
laser with a panel and an app, which runs without hardware), is step 9. The
"out of date" notice, which says which servers run older code than their
folders hold and need a restart (see [versions](#versions)), is step 12, and
bringing them up to date (`insthub service update`, the window's **Update**
and **Update all**) is step 13. The
lab's own instrument projects, folders beside insthub, are the real ones:
`hp_8562a_gpib` (which has its manifest), `windfreak_sythHDprov2`,
`m8195a_ripa` and `oscilloscopes`.*

### What an extension is

An extension is a folder for one instrument or one family of instruments. It
may also be an installable Python package, but it does not have to be. It
holds:

- a **driver**: plain Python and the vendor library, with no insthub in it
  (see [rules inside an extension](#rules-inside-an-extension)).
- a **device**: a `Device` or `Wrapped` subclass that puts the driver on the
  bus. Or no class at all: the driver wrapped from a `.toml`.
- optionally a **simulator**: a fake driver, or the device with `sim=True`.
- optionally a **UI**, in one of two forms. A *panel* is one widget, and
  insthub puts it in a window. An *app* is a whole program with its own
  window.
- optionally **recipes**: files of Python functions that use its devices,
  which insthub runs by name (see [Recipes](#recipes)). A folder whose
  manifest names only recipes is an experiment's, not an instrument's.
- one small file, **`insthub-extension.toml`**, the *manifest*, that names all
  of the above.

Other projects say plug-in or add-on. This page says *extension* for
insthub's.

```
hp8562a/                          the project folder, with its own .venv
    insthub-extension.toml        the manifest: names everything below, by strings
    hp8562a.py                    the driver (its name is unique, so it may stay at the top)
    hp8562a_sim.py                a simulated analyzer, for tests and demos
    hp8562a_insthub/              everything with a generic name goes in a package of its own
        __init__.py               imports nothing heavy
        device.py                 class SpectrumAnalyzer(insthub.Wrapped), with extension = "hp8562a"
        ui/
            __main__.py           the app:   python -m hp8562a_insthub.ui --address ADDR --name NAME
            panel.py              the panel: make(device, parent=None) -> QWidget
    pyproject.toml                optional (rule 5 below)
```

`examples/extension/` in insthub's repository is laid out this way: a
simulated laser (`demolaser.py`, `demolaser_insthub/`) with a panel, an app
and a server file. It runs without hardware, the tests run it from `insthub
check` to `insthub open`, and its README says how to make it your own.

### Two halves, each where its libraries are

The deciding fact is that **the parts of a lab run in different Python
environments**. Each instrument's project has its own `.venv`, with its vendor
library and `-e ../insthub`. The `insthub` command, and with it the window
(`insthub gui`), runs from its own `uv tool` environment, which has no
drivers at all (see [the insthub command for
yourself](running-servers.md#the-insthub-command-for-yourself)).

Every project we studied that joins a driver and its UI in one package
(qudi, labscript's BLACS, instrumentserver, nspyre, ScopeFoundry) needs the
UI and the driver installed in one environment, and most load them into one
process. insthub cannot do that without making every lab install every
instrument's libraries into one environment.

So an extension has two halves, and each runs where its libraries are. They
meet over the bus, which is what the bus is for:

```mermaid
flowchart LR
    subgraph proj["the project's environment (.venv)"]
        server["server program<br/>imports driver and device"]
        app["app or panel program<br/>imports the UI"]
    end
    subgraph tool["insthub's own environment (uv tool)"]
        window["the window (insthub gui)<br/>reads manifests,<br/>never imports extension code"]
    end
    manifest[("insthub-extension.toml")]
    server <-->|bus| app
    window <-->|bus| server
    window -.->|reads| manifest
    window -->|"starts it with the<br/>extension's Python"| app
```

- The **server half** (driver, device) runs in the server program, as today.
- The **UI half** (panel, app) runs as a program of its own, started with the
  extension's Python (`extensions.launch`), as the window starts `insthub
  view` for a live view. Its output goes to a log file, as that of a server
  the window starts does (`inventory.spawn`).
- The **window** reads manifests as data and starts programs. It never
  imports an extension's code. A crashing or hanging vendor UI therefore
  cannot take down the window, its tray icon or its probing of other servers.

VS Code extensions do the same with language servers: the extension starts a
separate program, in whatever runtime that program needs, and talks to it
over a protocol. In insthub the protocol is the bus.

The UI half reaches its device only through a device proxy, never by calling
the driver. So it can do nothing a script could not do, and it works on any
computer with `insthub[gui]` and the extension installed.

### The manifest

*Status: implemented: `extensions.load()` reads one, and the New server
dialog offers its devices (step 7); `insthub open` and the device page's
**Open** buttons start its UIs (step 8).*

`insthub-extension.toml` is data. insthub reads it with `tomllib` and never
runs it, so listing extensions never imports a vendor library. A manifest
lists what the extension adds to insthub, in two kinds of table, `devices`
and `ui`; its `[extension]` table can also name files of recipes.

```toml
[extension]
id = "hp8562a"                        # what this extension's devices announce (see below)
title = "HP 8562A spectrum analyzer"
version = "0.3"
api = 1                               # the version of this file's format
# python = ".venv/bin/python"         # default: a .venv in this folder or the one above
# recipes = ["recipes.py", "more/"]   # files (and folders of files) of recipes: Python functions

[devices.analyzer]                    # offered by the window's New server dialog
driver = "hp8562a_insthub.device:SpectrumAnalyzer"
doc = "HP 8562A over GPIB (linux-gpib)"

[devices.analyzer-sim]
driver = "hp8562a_insthub.device:SpectrumAnalyzer"
doc = "a simulated analyzer, no hardware needed"
sim = true                            # every other key goes into the server file as it is

[ui.analyzer]                         # an app: a whole program, started as <python> <run...>
title = "Spectrum analyzer"
run = ["-m", "hp8562a_insthub.ui", "--address", "{address}", "--name", "{device}"]

[ui.quick]                            # a panel: one widget, in a window insthub provides
title = "Quick look"
panel = "hp8562a_insthub.ui.panel:make"
types = ["HP8562A", "SimHP8562A"]     # only for devices of these types (default: every device of it)
```

| Table | Used by | What it does |
|---|---|---|
| `[devices.NAME]` | the window's **New server** dialog, `insthub ext list`, `insthub check` | a ready-made `[devices.x]` table for a server file. `driver` names a `Device` subclass or a plain driver class to wrap, exactly as in a server's `.toml`. `doc` describes it. Every other key (constructor arguments, `exclude`, `link_errors`, `lazy`, ...) is copied into the server file as it is; insthub's own keys among them are checked as in a server file (what `link_errors` names is imported, so it is loaded by `insthub check --import`, in the extension's Python). The dialog reads the constructor's arguments from the source without importing it, as it already does, and adds `path` and `extension`, so a manifest never gives those (nor `extension_version`, see [versions](#versions)). The server it creates runs with the extension's Python. A server runs in one Python, so the dialog says so when the extensions of two of its devices run in different ones (as `insthub check` warns about a server file), and keeps a Python you chose yourself. Typing another driver in place of the one chosen takes away the keys the choice brought, `extension` among them. |
| `[ui.NAME]` | the device page's **Open** buttons and its tile's menu, `insthub open DEVICE [NAME]` | with `run`, an app, started as `<python> <run...>`; with `panel`, a panel, put in a window of its own (see [writing the UI half](#writing-the-ui-half)). Offered for every device that belongs to this extension, or with `types` only for the devices whose `type` (in `describe()`: the class name, which for a wrapped driver is the driver's) is listed. insthub's own manifest offers its scope panel this way, for its simulated scope only. |
| `[extension] recipes` | the window's **Recipes** page and the device pages' **Recipes** tabs, `insthub run`, `insthub recipes`, the agents' `describe` and `run_recipe` | files of [recipes](#recipes), relative to the manifest's folder; a folder gives its `.py` files. They are read without running them, and run in the extension's Python. An entry that is neither a `.py` file nor a folder is a warning, and a recipe that cannot be used is listed with its error: neither makes the extension unusable. A manifest with only recipes needs no Python of its own: they run in a virtualenv in its folder, else in insthub's Python. |

Each module name in a manifest is found with the manifest's folder first on
the module search path. An app or panel starts with that folder as its
working directory and on `PYTHONPATH`, so an extension works as a plain
folder, without being installed. `{device}` and `{address}` in `run` are
replaced by the device's name and its server's address; another `{...}` is
left as it is. Its output goes to `ui/DEVICE/NAME.log` in insthub's runtime
folder.

insthub runs nothing of an extension that another user may change: its
manifest, which says what is run, and its folder, whose modules come first,
must belong to you (or root) and be writable by no one else. So must its
Python: the folder that holds its virtualenv, the virtualenv, its `bin`
folder and the `python` in it (a link: the link itself, which only its owner
may change). A group of your own alone counts as you, since a umask of 002
makes files writable for it. This matters because a manifest, and a
virtualenv, are also looked for in the folders above, and one of them, such
as `/tmp`, may be anyone's. An extension that fails this check says why in
its error, and offers no UI; a server file's virtualenv that fails it is not
used.

A driver class wrapped from a manifest keeps its safety options in its
table. For example, the Windfreak synthesizer's raw protocol methods must
stay off the bus:

```toml
[devices.synth]
driver = "synthhd:SynthHD"
doc = "Windfreak SynthHD over USB"
exclude = ["write", "query", "read_all", "ch_write", "ch_query"]   # "m1" would move control to UART
```

Further kinds of table are reserved, but nothing reads them yet: `match` (how
to recognise the hardware on a scan), `docs` (for people and agents), and
`implements` (a tag saying a device offers a standard set of commands, so one
UI can serve two vendors' instruments). A new kind is added only together
with the code that reads it and a test (in `extensions.py` it is one entry of
`_ADDS` and an attribute of `Extension`). A field nothing reads drifts out of
date, as PyroLab's `detect_devices` did.

A manifest is unusable, and listed with the reason, when something insthub
reads is wrong: it is not valid TOML, its `api` is too new, its `id` or a
name is not a valid name, a table lacks what it needs, it has no Python. A
key or kind of table this insthub does not read is left alone, since a newer
insthub may read it; `insthub check` warns about it. insthub's own manifest,
`insthub/insthub-extension.toml`, lists the simulated instruments; its
version is insthub's, and it runs in any Python that has insthub.

### How a device finds its extension

*Status: implemented: a device saying what it is (step 4); the folders and
the Python in the server's `.json` file, and `extensions.for_device()`, which
finds the manifest from them (step 7); `extensions.device_uis()`, which
`insthub open` and the window use to find a device's UIs (step 8).*

A running device says which extension it belongs to:

```python
class SpectrumAnalyzer(insthub.Wrapped):
    extension = "hp8562a"             # just a name: insthub compares it, never interprets it
    extension_version = "0.3"         # optional

    def __init__(self, name=None, board=0, pad=18, sim=False):
        super().__init__(SimHP8562A if sim else HP8562A, name, kwargs={"board": board, "pad": pad})
        self.simulated = sim          # shown as SIM, so simulator output is never taken for a measurement
```

For a driver wrapped from a `.toml`, the keys `extension = "hp8562a"` (and
`simulated = true`) go in its `[devices.x]` table. The New server dialog
writes `extension` for a device chosen from a manifest, and `simulated` when
the manifest's table has it. A driver class that has `simulated = True` makes its wrapped
device say so by itself. `describe()` then carries `extension`,
`extension_version` and `simulated` (each only when set), so any client on
any computer can match a device to its UI without importing anything. The
window marks a simulated device SIM on its tile and page, `insthub info` and
the agents' `list_devices` say it, and the device page shows the extension.

These are attributes of `Device`, and insthub reads them without running
anything: a subclass that already uses one of the names for a command or a
property of its own keeps it, and then just declares nothing under it.

Neither the class name nor the module name can do this job. For a `Wrapped`
device the type is the driver's class name, which differs between the real
instrument and its simulator (`HP8562A`, `SimHP8562A`). The module is
`__main__` when a project runs `python bus_server.py`. And matching by the
commands a device happens to have, as `hp8562a_gui` does today with its
`REQUIRED_COMMANDS`, breaks when one is renamed.

To go from a running device to its folder, each server's `.json` file in
insthub's runtime folder (see [finding a device by
name](architecture.md#finding-a-device-by-name)) lists, for each device,
the folder of the file its class comes from (for a `Wrapped` device, the
driver's too), and the server's Python. The window looks for the nearest
`insthub-extension.toml` in that folder or a folder above it. This works
however the server was started: from a `bus_server.py` in another folder,
from a `.toml` the New server dialog wrote into `~/.config/insthub/servers`,
or with devices from two extensions in one server. These two facts go only
into the `.json` file, which only your user account can read. They are never
sent to other computers. Without `XDG_RUNTIME_DIR` that folder is
`/tmp/insthub-UID`, a name any account could take first, so insthub refuses
it unless it belongs to your account and no other may use it.

**Security.** A UI is offered when `describe()["extension"]` equals a
manifest's `id`, but only a manifest found on *this* computer can cause code
to run. A remote server sends nothing but the name. Names that arrive over
the network are checked (`registry.check_name`) before they fill a
placeholder, and insthub never runs a module name or command line that came
from another computer.

### Where insthub looks for extensions

*Status: implemented (step 7, `extensions.discover()` and `for_device()`;
the hint for a device without its extension here, step 8), except entry
points (5.).*

For a device on this computer, insthub uses the manifest found from the
device's own folder, as above. **Open** therefore starts the copy of the
extension that its server runs, even when you have two copies (a working copy
and the one a service runs).

Everywhere else (a device on another computer, the New server dialog,
`insthub ext list`), it looks in this order. The first manifest with a given
`id` that can be used wins, and `insthub ext list` reports the others as
hidden by it. One that cannot be used hides nothing, so a fresh clone without
its `.venv` yet does not hide the copy a service runs:

1. insthub's own manifest, shipped with it. It lists the simulated
   instruments of `insthub sim` and runs with the Python insthub runs in, so
   every run of the test suite exercises the mechanism. Another copy of
   insthub has the same manifest: in a lab, the window runs from its own `uv
   tool` environment while the projects' servers run the checkout beside
   them (`-e ../insthub`). insthub knows such a copy by where its manifest is
   (in a package `insthub`, next to its `_version.py`) and counts it as its
   own: it is listed once, and a simulated device of such a server gets the
   extension of the insthub that asks, whose code runs here.
2. folders you add with `insthub ext add PATH` or **Add folder...** on the
   window's Settings page, kept in `~/.config/insthub/extensions.toml`.
   `insthub ext remove PATH` and a **Remove** button take them out again.
3. the folders of the installed services' code: for a server file, each
   device's `path`, or the folder the server runs in for a device whose
   module is there (`insthub check lab.toml` looks in the same folders); for
   a `.py` server, its folder.
4. the folders of the devices of the servers running on this computer, as
   above.
5. later, installed packages that declare an `insthub.extensions` *entry
   point* (a name a package registers in its `pyproject.toml`, which other
   programs can look up without importing the package). This is for when
   extensions are pip-installed into shared environments.

Setting `INSTHUB_EXTENSIONS` to folders (separated by `:`, or `;` on
Windows) replaces 2 to 4 with them, and `none` leaves only insthub's own: for
trying an extension without adding it, and for the tests, which never see
this computer's extensions.

`insthub serve` never looks for extensions. A server is built from its own
file, so a service does not depend on what one user has listed.

**A device on another computer** gets its UI from a copy of the same
extension on this computer, run with that copy's Python. If there is none,
its page stays generic and says "extension hp8562a is not installed on this
computer: `insthub ext add FOLDER`", and so does `insthub open`. A device on
this computer whose folders lead to no manifest of its extension gets the
same.

### Installing, updating and broken extensions

insthub installs nothing. An extension is a folder: clone it, make its
environment (for a packaged one, `uv sync --extra gui`), then `insthub ext
add` it. Updating is `git pull`; the window and `insthub list` then say which
servers run older code and need a restart (see [versions](#versions)), and
`insthub service update` (or **Update all** in the window) restarts the
services among them. A
manifest that cannot be used (it is not
valid TOML, its `api` is too new, its `id` is not a valid name, its Python or
its folder is missing) is listed with its error by `insthub ext list` and on
the window's Settings page. It never hides the other extensions, and `insthub
check FOLDER` says what to do about it. `insthub check FOLDER --import` also
loads what its entries name, in the extension's Python, in a program of its
own that needs nothing but the standard library there: each of its drivers,
the exception classes their `link_errors` name, each panel's function and
each app's `-m` module (looked for, not run). It reports those that fail,
drivers that load Qt, and a device class whose `extension` is not the
manifest's `id` (its devices would find no UI; the device page says so too).
For a server file, `insthub check lab.toml --import` makes the check
`insthub service install` makes, in the Python that serves the file: that of
its devices' extensions when they agree on one, else a virtualenv next to the
file or next to a device's code, else insthub's own (one rule,
`extensions.python_for`, which the service and the window's Keep a server
running dialog follow too).

### When code is loaded

| What | Imported by | When |
|---|---|---|
| the manifest | the window, `insthub ext`, `insthub check`, `insthub list` | to list extensions, and to compare a device's version with its folder's (out of date); it is data, nothing runs |
| the driver and device | the server that hosts the device | when the server builds it (`config.load_object`, as today) |
| the app or panel | the program started for it, with the extension's Python | when someone clicks **Open** or runs `insthub open` |
| a file of recipes | the program started to run one of its recipes (`recipes.start()`), with its Python, or a script that calls `recipes.run()` or `load()`; the window, `insthub recipes`, `insthub check` and `insthub mcp` only read it, with `ast` | when someone runs a recipe of it |
| `insthub.extensions` itself | the window and the command line | never by `import insthub` |

Unlike VS Code, insthub has no list of events that wake an extension up, and
no long-running program that hosts extensions. "Activating" an extension just
means importing it in the program that needs it.

### Writing the UI half

*Status: implemented: `qt.device_window`, `insthub panel`, `insthub open`
(step 8), `qt.start`, `qt.coalesce`, `qt.follow` and the controls a panel can embed,
`insthub.controls` (step 11). Examples to copy: insthub's own panel for its
simulated scope, `insthub/sim_ui.py`, which uses them all, and the panel and
the app of the reference extension, `examples/extension/demolaser_insthub/ui/`
(step 9). [Writing a panel](guide.md#writing-a-panel) in the guide is the
how-to.*

A panel is one function:

```python
def make(device, parent=None) -> QWidget:
    ...
```

`device` is an ordinary proxy, the same thing `insthub.device(name)` returns.
The panel uses `qt.watch` for streams, `qt.start` to start calls and
`qt.deliver` for results, so it never blocks the GUI thread, not even while
the proxy reconnects to a server that has gone away. A setting that changes
faster than the instrument takes it (a slider, a spin box's arrows held down)
goes out through `qt.coalesce`: one call on its way at a time, and the values
sent meanwhile merged into the next one, the newest value of each key winning
(or as a `merge` function says, for keys that go together, such as a center
and a span), so the instrument never works through a backlog. A control that
also shows the device's value goes through `qt.follow`, which builds on
`qt.coalesce`: it shows what the device reports, but not an old report over a
value on its way, and once a call has ended, what the device took. What the panel does
not show in a way of its own, `controls.Controls(device, include, exclude)`
shows: the settings and commands of any device, made from its `describe()`,
the same widgets as the window's device page (see [a generic
UI](#a-generic-ui-good-enough-for-most-devices)). It stops its watchers in
its widget's `closeEvent`, which the window around it sends when it closes.
It talks only
through commands, properties and streams, so everything it does can also be
done from a script or by an agent. qudi has the same rule: a GUI adds
nothing that the logic's API cannot do.

The same panel can run in two places without change:

- in a window of its own. The window (or `insthub open`) runs `python -m
  insthub -a ADDRESS panel DEVICE MODULE:FUNCTION --title TITLE` with the
  extension's Python, where the `insthub` command itself may not be
  installed. That imports the panel's module before `insthub.qt`, so the
  panel's own Qt binding (PyQt6, PySide6, ...) is the one used, and then a
  small helper, `qt.device_window`, builds the window around the panel. This
  is the default.
- inside an app that hosts its own server in the same process, where frames
  arrive without being copied (as in `examples/04_server_with_gui.py`).

Showing a panel as a tab inside the window is an [open
question](#open-questions).

An app is a whole program, for a device whose UI is a full application. It
takes the device's address and name, and when it is given an address it only
attaches to that device. Otherwise a name that does not resolve at that
moment could make it start a second server for the same instrument.

The window writes the output of an app or a panel to a log file in insthub's
runtime folder, as it does for the live view and the servers it starts. If
one ends with an error, at once or later, the window shows the log's last
lines, for example "cannot load the panel 'x:make': insthub.qt needs a Qt
binding: pip install 'insthub[gui]' (pyqtgraph + PyQt6) (this is
.../python; does it have the libraries of the extension's UI, such as its gui
extra?)". A program that Qt or a vendor library ends by a signal (Qt aborts
when it cannot load its platform plugin) has ended with an error too; one
stopped on purpose (SIGTERM, SIGINT, SIGKILL, SIGHUP) has not. `insthub open` waits three seconds and prints them if it
ended with an error by then.

The launched program runs the extension's own insthub, which may be older
than the window's. If that insthub has no `panel` command yet, the log ends
with argparse's `invalid choice: 'panel'`: insthub needs an update in that
environment. (insthub's version has not changed since `panel` arrived, so
there is no version number to name.)

### Rules inside an extension

1. **The driver does not need insthub or Qt, and imports cleanly where the
   vendor library is missing.** Import the vendor library in the constructor,
   or guard the import at the top (`try: import gpib` / `except ImportError:
   gpib = None`), as `hp8562a.py` does. A pure-Python library such as pyserial
   can be imported normally. Then the driver can be used from a plain script,
   and tests and `insthub check --import` run without the hardware stack.
2. **The device imports only insthub's core, the driver and the extension's
   own modules.** A server often runs as a background service with no screen,
   so it must not need Qt. `insthub service install` already warns when a
   driver of the server's `.toml` loads Qt, and `insthub check PATH
   --import` warns when importing the device does. Vendor exception classes for `link_errors` come
   through the driver (for example a `LINK_ERRORS` tuple defined in the
   driver module), or as `"module:Class"` strings in the `.toml`. A `Wrapped` subclass that a
   manifest or `.toml` names defines its own `__init__(self, name=None,
   <the driver's arguments>)` and calls `super().__init__(Driver, name,
   kwargs=...)`, as in the example above. `insthub serve` calls it with the
   table's keys, and the New server dialog shows its arguments.
3. **The UI is the only place for Qt, and it reaches the instrument only
   through a device proxy.** It never calls the driver. An app may also host
   the server half in its own process when no server runs, as `hp8562a_gui`
   does. It then imports the device module only at that point, and it needs
   the project's environment.
4. **Every top-level module name an extension uses is its own.** Generic
   names (`bus_server`, `driver`, `device`, `gui`, `sim`) go inside a package
   such as `hp8562a_insthub`. A uniquely named driver such as `hp8562a.py` may
   stay where it is. Python keeps only the first module of a name, so two
   folders that each have a `bus_server.py` cannot both be loaded into one
   program. For the module that a table's `driver` names, insthub's loader
   reports the collision, naming both files (also for a module inside a
   namespace package, a folder without `__init__.py`, whose folders Python
   merges), instead of failing with a
   misleading "cannot load driver" error or silently using the other
   folder's class when both use the same class name. It cannot see the
   modules a driver imports in turn: two folders whose drivers each import
   a helper `driver.py` or `sim.py` silently share the first folder's
   helper. Only this rule prevents that.
5. **A packaged extension keeps its UI's libraries in an extra**, for
   example `dependencies = ["insthub @ git+...", "pyserial"]` and
   `[project.optional-dependencies] gui = ["insthub[gui] @ git+..."]`. A
   server's environment then installs only the server half. (Libraries that
   come from the system, like linux-gpib's `python3-gpib`, need a venv made
   with `--system-site-packages` instead.)
6. **Mark what is dangerous.** A wrapped driver's raw I/O methods (`write`,
   `query`, ...) are excluded (`exclude`) or marked `dangerous`, and so is
   anything that can harm the instrument, the sample or a person. Agents
   then need `call_dangerous` for it, and the window asks first.

### Versions

| What | How |
|---|---|
| the manifest's format | `api = 1`. insthub refuses a manifest with a higher number and accepts lower ones until one is deliberately dropped. |
| the extension's code | `version` in the manifest, `extension_version` on the device (the device page shows it), and the files a server has loaded. A server that runs older code than its folders hold is *out of date*, as LabRAD's node marks a server started from an older manifest. One rule, `extensions.outdated(entry, devices)`, says so from two signals. **Code changed since it started:** a server lists in its `.json` file (never in its hello) the source files of its loaded modules with their modification times: every module of insthub's own package, and every module in one of its devices' folders but not in a Python environment below that folder (a `.venv`, `venv`, `site-packages` or `dist-packages` folder, or the Python installation), so a project's numpy is not listed. Each file keeps the time it had when the server first listed it; one whose time differs now, or that is gone, is code the server does not run. Times are compared, not contents, so a file saved without a change counts too. A module imported after the server started (a helper a driver imports inside a command) is listed within about a second: the server writes its `.json` file again when its process has loaded more modules. A module run again with `importlib.reload` (IPython's `%autoreload`) is timed again. **The version differs:** a device that states `extension_version`, whose extension's manifest, found from the device's own folders (not another copy), has another `version`, and that manifest changed since the server started: "the folder has demo-laser 0.2, it runs 0.1". Versions that already disagreed when it started (a class and its manifest that disagree on disk, or an `extension_version` in a `.toml`) would disagree again after a restart, so they are left to `insthub check`, which warns about the first. `insthub list` adds a line under such a server; the window marks it OUT OF DATE on its card and above its devices, and its page says why in a Code row under its state, with a **Restart** link when the window can restart it (its devices' pages say it too); the window finds this out on a worker thread with each probe. A server on another computer has no `.json` file here and is never judged, nor is one of an older insthub on its files: never a false notice. The New server dialog never writes `extension_version` into a `.toml`, because it would freeze there, and `insthub check` warns when a class's `extension_version` (read from its source, without running it) differs from its manifest's `version` (with `--import`, read from the imported class, also when its source does not say it plainly). |
| the bus between two programs | Every connection starts with the bytes `INSTHUB1`, so a program that speaks another protocol is refused. The server's first message about itself, the *hello*, carries `protocol`, insthub's `version` and a list of `features` (`server.FEATURES`, today `["dangerous", "logs", "meta", "pause"]`), so that a client can ask (`client.supports("pause")`) before it relies on something newer. Both sides skip kinds of message they do not know instead of closing the connection. Together these let a feature arrive without everyone upgrading at once. |
| `describe()` | Keys are only ever added, and they are optional, so older programs ignore them. |
| a service's unit | Its first line says that insthub wrote it, and in which format (`(unit format 2)`, `service.UNIT_FORMAT`; units of format 1 did not say theirs). `service.units()` says what is old about a unit (`"stale"`, such as `RestartSec=2, now 5`) by comparing the file with the unit this insthub writes (`unit_text`) for the same name, server file and Python, read back from the unit itself. Only a unit insthub wrote is judged: its first line says so, in a format no newer than this insthub's, its server file is still there, its command is the one insthub makes of that file and Python, and each of its settings is today's or a value an older insthub wrote instead (`service._OLDER`, which gets the old values whenever `unit_text` changes, along with a new format number). Any other unit was written by hand, changed by hand (a setting insthub does not write, a value no insthub wrote) or written by a newer insthub; it is never judged, and `"unchecked"` says why, so writing it anew can never undo what somebody chose, and an older insthub (a long-running window, a user-wide command) never undoes a newer one's units. One rule, `service.plan(unit, reasons)`, says what a service needs: its unit written anew when it is stale, then a restart when it runs and its unit or its code changed (`service.outdated(unit)`: `extensions.outdated` on the registry entry of the unit's process); never a start. `service.update(names, check=, wait=)` carries it out for each service, looking at each one again just before its turn: the unit is written in place, for the same Python (if that Python is gone, it says so and changes nothing), then systemd reloads it; the restart is systemd's `try-restart`, which never starts a service stopped meanwhile, and waits as `restart` does. One service failing never stops the others; each gets a record, `service.Update`, which `insthub service update` prints (`--json`: as it is; `--check`: only what it would do, with exit code 1 if anything would change) and the window shows. The window asks `service.plan` too, to offer **Update** and to say what restarts, and runs `service.update` on a worker. |
| a program's own code | The window and `insthub mcp` are clients with no `.json` file, but run insthub's code all the same. `extensions.outdated_here()` applies the same rule to the files of insthub's own modules as their process loaded them (`server._code`): the window looks with each scan, on a worker, and says so in its sidebar and on its Settings page; `list_devices` adds a note, so that the agent can ask its user to reconnect it. No MCP tool restarts a server, since a restart drops an instrument for a few seconds. The window has no button to restart itself: it would lose the servers it started (it restarts them by their process) and whatever its workers were doing; quitting and starting it again does the same, knowingly. |

Servers started before a `git pull`, other computers, and projects that
install insthub from git instead of `-e ../insthub` all run other versions.
So a lab must expect several insthub versions at once, and these rules allow
it.

### Existing projects become extensions

Nothing breaks: `driver = "module:Class"` with `path` keeps working, and
every new key is optional. New reserved keys (`extension`,
`extension_version`, `simulated`, `accessors`, `poll`, `meta`, `dangerous`)
do take those names away from driver constructors. A table that has such a
key while its constructor takes a parameter of that name is an error until
`kwargs` says what the constructor gets, so a file that used to pass
`simulated = true` to its driver never silently starts the real instrument.
When the table does not have the key, the loader's error says why it does not
reach a required parameter; `insthub service install` warns about an optional
one.

| Project | Becomes |
|---|---|
| `hp_8562a_gpib` | The first real one, converted in its own repository. The first step, with today's files, is done there: a manifest that `insthub check` passes, with `driver = "bus_server:SpectrumAnalyzer"`, its simulator, and `[ui.analyzer] run = ["-m", "hp8562a_gui", "--address", "{address}", "--name", "{device}"]`; `SpectrumAnalyzer` says `extension = "hp8562a"` and `self.simulated = sim`; `hp8562a_gui` takes `--address` and then only attaches, and knows its device by `extension` (by `REQUIRED_COMMANDS` only on a server older than that). Still to do there: `bus_server.py`, a generic module name (rule 4), moves with `SpectrumAnalyzer` into `hp8562a_insthub/`, and is kept as a two-line shim, so services and tests keep working; a panel may be offered. `examples/extension/` shows each of these parts. |
| `windfreak_sythHDprov2` | A manifest with one device and no UI. The `Synth` class moves into a package of its own (`bus_server.py` stays as a shim). Its settings are per-channel methods (`frequency(ch)`, `set_frequency(ch, mhz)`), not properties, so the generic page serves it through its commands, with units and ranges once command parameters carry metadata. |
| `m8195a_ripa` | Two manifests, one per instrument: `m8195a` in the project folder and `redpitaya` in `tools/redpitaya/`. Each device in `bus.toml` gets its `extension` key. `accessors = true` turns the AWG driver's ~40 argument-free `get_X`/`set_X` pairs into properties; its ~30 per-channel pairs stay commands. Its raw I/O methods are excluded or marked dangerous. |
| `oscilloscopes` | Already a package. It ships its manifest inside the installed package and is the first candidate for finding extensions through entry points. |

## Recipes

*Status: implemented (step 14): `insthub/recipes.py`; your recipes folder
and `[extension] recipes` in manifests; `insthub run`, `insthub recipes` and
`insthub check FILE.py`; the window's **Recipes** page, the device page's
**Recipes** tab and **Save as recipe...**; recipes in the agents' `describe`,
and `run_recipe`. [Recipes](guide.md#recipes) in the guide is the how-to.*

A lab keeps snippets: set the Red Pitaya to a square wave; arm it, wait for a
trigger and read its buffer; start the SynthHD's comb and take a trace at
each step. They lived in a notes file and were pasted into a terminal. A
*recipe* is such a snippet as a Python function in a file, so that the
window, the command line, a script and an agent all find it by name and run
it the same way:

```python
# ~/.config/insthub/recipes/comb_scan.py
import insthub

def comb(start: float = 4000, stop: float = 9000, step: float = 1, dbm: float = 0):
    """SynthHD frequency comb, stepped by the trigger."""
    return insthub.device("synthhd").arm_comb("A", start, stop, step, dbm=dbm)
```

### Python functions, not data

A data format came first: `[recipes.NAME]` tables of steps (call a command,
set a property, wait) with `{placeholder}` parameters, in a `recipes.toml`
and in manifests. It was dropped before it was released. It was a small
language of its own, and too small for the snippets people keep: they loop,
wait for a trigger, and read a value back to decide what to do next (an
armed Red Pitaya is asked until it answers `TD`, triggered). Each such
need would have added a keyword, toward a scripting language worse than
Python. A function:

- is the code people already have, with its values turned into parameters.
  There is nothing new to learn.
- can loop, wait, read back, use numpy, and return data.
- says its parameters in its signature: defaults, annotations, and the
  choices of a `Literal[...]`. `@recipe(params=...)` adds units and limits
  for the window and for agents, in the vocabulary of a device's metadata,
  and `@recipe(dangerous=True)` asks first.

What the data format gave (a check before running, dangers in sight,
nothing of the user's code in the window) is kept as far as reading code
allows. The next sections say how, and what it cannot see.

It is not the experiment runner we [decided
against](#ideas-we-decided-against): there is no queue, no schedule and no
datasets. A recipe runs when someone asks, and what it returns goes back to
whoever asked. It is not called an *app*, which a manifest already uses for a
whole program with its own window. A *preset* suggests values without logic,
a *macro* recorded keystrokes, and *task* is taken by periodic tasks.

### Read without running

insthub reads a file of recipes with `ast` and never imports it to list or
check its recipes. Listing therefore runs none of the user's code, so the
window can list recipes in its own process, and a file whose top cannot run
now (a device that is off, a library missing from insthub's environment) is
still listed. From each public top-level function it reads:

- its name, and its docstring (the first line is its title);
- its parameters: each default as written, the annotation, the values of a
  `Literal[...]` as choices, and what `@recipe(params=..., dangerous=...)`
  says, read as literals;
- the devices it uses: `insthub.device("name")` with the name written out, in
  the function, in a function of the same file that it calls, or in a
  function of a `.py` file beside it that it imports (`import redpitaya`,
  `from redpitaya import acquire`) and calls;
- the devices the top of its file names, and the top of each file beside it
  that runs when it does (one it imports): that code runs whenever any
  recipe of the file starts, so every recipe of the file needs them
  (`at_import`), and the check asks for them too ("the top of redpitaya.py
  asks for it"). A device named there and imported from that file (`from
  _devices import laser`) is followed;
- what it uses of them (`Use(device, name, kind, line, file)`): a command or
  method called, a property read, a property written. `dev.call("cmd")`,
  `dev.bus.call("cmd")` and `dev.bus.set("prop", v)` count when the name is
  written out, and so do uses through `dev.with_timeout(s)` and through
  other names for the device (`a, b = ...` included). Uses at the top of the
  file count for every recipe of the file. A block under `if __name__ ==
  "__main__":` is not read: it never runs when a recipe does.

A recipe that cannot be used (an `async def`, a decorator not written as
literals, anything else insthub cannot read in it) has its own error and
leaves the other recipes of its file usable. What each file says is kept
until it changes, so the window can ask again cheaply; what a recipe uses
across the files beside it is put together each time, so a change there, or
a file that appears, is seen at once.

### Checked before running

`recipes.check(recipe)` asks each device the recipe uses, and each the top
of its file names, for its `describe()`, and finds what would stop it: a
device not on the bus or paused, a command or property it does not have
(with a "did you mean"), a read-only property written. It also lists what
the recipe uses that is marked dangerous, and which of its devices are
simulated. Nothing runs. The window, `insthub run` (and `--dry-run`, which
stops there) and `run_recipe` check before they start a recipe, and refuse
one with a problem.

Reading code has limits, and the check is a guard against the usual
mistakes (the wrong server, a misspelled command, a renamed property), not a
proof. It does not see a device named only when the recipe runs
(`insthub.device(name)` with a variable, in the function or at the top of
the file), or a command or property named so (`dev.call(name)` with a
variable): the check says so, as `named_later`. Nor does it see a name
reached through `getattr`, a function of a module that is not a `.py` file
beside the recipe's file, what a function returns (`rp = connect()`), or a
device handed to a helper as an argument. It does not judge values either:
whether 30000 MHz is too high is for the driver to say when it runs, as for
any client. What it does not see fails where it is, as in a script. There
is no undo: what a recipe did before it failed stays done, and its `finally`
blocks are where it puts things back.

### A process of its own

The window and the agents' MCP server never run the user's code in their own
process. It is the rule that keeps extension UIs out of the window (see [two
halves](#two-halves-each-where-its-libraries-are)), and for the same
reasons: a recipe can wait for a trigger that never comes, crash, or import
a library that insthub's environment does not have.

So `recipes.start(recipe, values)` runs a recipe in a program of its own,
with the recipe's Python: a virtualenv in your recipes folder, or the
extension's Python, else the one running insthub. That program is `python
-c` with a line that calls `insthub.recipes._child(FILE, FUNCTION, VALUES,
RESULT)`. A recipe reaches its instruments over the bus, so that Python
needs no driver. `start()` returns a `Run`: what the recipe has printed so
far (the window streams it), `stop()`, and `wait()`, which returns what the
recipe returned, packed with the bus's own codec (numpy arrays, dicts,
dataclasses), or raises `RecipeError`, whose `output` holds what it printed,
the traceback too, starting in the recipe's own code. `insthub run` uses the
same, with the output going to its terminal.

The program has the recipe's folder on `sys.path`, so the recipe imports the
files beside it, but not the folder insthub was started from: a `json.py` or
an `insthub/` there would otherwise replace what the recipe and insthub
import. The recipe's file is compiled from its source each time it runs,
never from bytecode, so an edit made a moment before is never missed, and
the program writes no bytecode.

**Stop** sends the program SIGTERM once, which it turns into a
`KeyboardInterrupt` in the recipe, so its `finally` blocks run, as after
Ctrl-C. If it is still running 3 seconds later, it is killed. Stop pressed
again sends nothing: a second `KeyboardInterrupt` would land in the
`finally` blocks it is running. A recipe that returns all the same (it
caught the interrupt, or had just finished) gives what it returned. A
command the recipe was waiting for keeps running on its device: Stop ends
the recipe, not the instrument's work, as a timeout does. The agents'
`run_recipe` stops its recipe the same way when its timeout passes, when the
agent's request is cancelled and when `insthub mcp` exits, so none is left
running with nobody to stop it.

Scripts and notebooks call `recipes.run(name, **values)` or `load(name)`
instead, which run the recipe in their own process with no check and no
question: the same as calling the function, which they could do anyway.
They add the recipe's folder to the end of `sys.path` and leave it there, so
the recipe can import the files beside it and never hides a module of the
script's.

### Dangerous

The marks of the devices reach recipes as far as insthub can read them. The
window asks before running a recipe whose check found something marked
dangerous, or that says `@recipe(dangerous=True)`. `insthub run` asks on a
terminal, and needs `--yes` where there is nobody to ask. The agents'
`run_recipe` refuses such a recipe: only the user runs it.

A recipe can still reach a dangerous command through code insthub cannot
read, and the instrument will do it. As for scripts, the marks prevent
accidents; they do not stop code written to get around them. So allowing an
agent to run recipes without asking means trusting them with any values the
agent picks, and [agents](agents.md#the-tools) says so.

### Where recipes live

| Place | For |
|---|---|
| your folder, `~/.config/insthub/recipes/*.py` (`$INSTHUB_RECIPES`) | your snippets, and what **Save as recipe...** writes (into `DEVICE.py`, replacing a function of that name and keeping the rest). Files whose names start with `_` or `.` are skipped, so helpers can live beside them. |
| `[extension] recipes = ["recipes.py", "more/"]` in a manifest | the recipes of one experiment, kept in git beside its code and added with `insthub ext add`. Such a manifest needs no Python of its own: its recipes run in a virtualenv in its folder, else in insthub's Python. An instrument's extension can name recipes too. |

A recipe is stored in one place and shown wherever it is used: it belongs to
no device, since one often uses several. The window's **Recipes** page lists
them all, and each device's page those that use it, as read from the file.

This is not the central store of settings we decided against: nothing is
saved unless someone saves it, nothing is shared through a service, and a
file of recipes is its owner's, edited in an editor and kept in git if they
like, as `devices.toml` is.

### Names

A recipe is called by its function's name (`square`); by its file's name and
its own (`redpitaya.square`) when two files have one; and by a path
(`path/to/file.py:square`) for one in any file. Of several recipes with one
name, yours is used before an extension's; otherwise the error lists each
by the shortest name that runs it (`recipes.runs_as()`). Two folders can
each hold a file of one name (two experiments' `recipes.py`): their recipes
then share a full name too, and only the path says which file. Short names
for typing, files for grouping, and no list of names to keep up to date.

`find(name, any_file=False)` accepts a path only to a file `available()`
lists (your folder, a usable extension's). The agents' `run_recipe` finds
recipes that way, so an agent can tell two such recipes apart, but cannot
run a function of any other file: a script in its project, or a module of
Python's own.

### Recipe, command or app

| | A driver command | A recipe | An app or a panel |
|---|---|---|---|
| for | what anyone with the instrument would want: load a bitfile, a square wave, wait for a trigger | what is about your setup: this comb, with this trigger and this acquisition, often across devices | something you work in while it stays open: live traces while you adjust the trigger |
| lives in | the device class, in its extension | your recipes folder, or an experiment's folder | an extension's `[ui.NAME]` |
| runs | in the server, on the device's thread | in a program of its own, as a client, and finishes | in a program of its own, until you close it |
| found as | the **Commands** tab, `insthub info`, `call` | the **Recipes** tab and page, `insthub run`, `run_recipe` | **Open ...** on the device's page, `insthub open` |

What should happen by itself, such as loading a bitfile whenever the server
starts, belongs in the driver's `open()` or in a periodic task, not in a
recipe.

### Later, when a need shows up

- A recipe that **saves the current settings** of a device (its settable
  properties) to run again later, next to `insthub.states()` (step 10).
- **Save as recipe** for several calls in a row, kept as one function.

## A generic UI good enough for most devices

*Status: implemented (plan steps 5 and 6), as described below. Its controls
are a widget of their own, `insthub.controls` (step 11): the window's device
page and any panel use the same ones.*

The cheapest extension UI is the one nobody has to write. If devices describe
themselves well enough, the generic device page and the AI agent tools cover
most simple instruments. An extension then needs a UI only for what a
generic page cannot show, such as a spectrum with markers or a camera image
with a region of interest. ScopeFoundry builds most of its UIs from typed
settings, and ARTIQ builds its experiment forms from typed arguments.
instrumentserver shows the converse: its descriptions carried no ranges or
choices, and its generic widget never got past a text field.

- **Property metadata.** `@insthub.prop(unit="Hz", min=0, max=26.5e9)`, with a
  fixed vocabulary, `device.META_KEYS`: `unit`, `min`, `max`, `step`,
  `choices`. A misspelled word is a `TypeError` that names the right one. A
  device can change it from `open()` with `self.set_meta("center", max=...)`,
  because limits often depend on which model or option is connected; pages
  see a later change the next time they read `describe()`. `choices` can come
  from a `Literal` or `Enum` annotation (a `bool` needs none: the page shows
  a true or false value as a switch anyway). Wrapped drivers get it from
  `wrap(meta=...)` or the `.toml`. The device page picks a list, a spin box
  with the unit, or a switch, with the text field as the fallback, and agents
  and `insthub info` get the units. It is advice for the UI, not enforced:
  instruments accept values like `"auto"`, and they clamp by themselves.
- **Command parameters** take the same vocabulary
  (`@command(params={"mhz": {...}})`, or `"set_frequency.mhz"` in `meta`), for
  drivers whose settings are methods with arguments, such as a synthesizer's
  `set_frequency(channel, mhz)`. A name that is not a parameter is an error
  when the device's tables are made.
- **Accessor pairs.** `wrap(..., accessors=True)` turns a driver's getter with
  no required arguments (`get_X()`) and its matching `set_X(value)` into a
  property `X`. A getter without a matching setter is left alone, so a
  read-and-clear method like `get_error()` is never read by the device page.
  Both stay commands, and a driver that has a public `X` already keeps it.
- **Polling from the config.** `poll = {temperature = 1.0}` in a `[devices.x]`
  table (or `poll` on the class) reads the property `temperature` (or calls a
  command of that name with no arguments) every 1.0 s, on the device's thread,
  and publishes the value on a stream of the same name. It is a periodic task
  named `poll:temperature`, switchable like any other. An unmodified driver
  then gets live values, tile previews and `insthub view` without a subclass.
- **Dangerous commands.** `@command(dangerous=True)`, `@prop(dangerous=True)`
  (writing it is, so only a property with a setter), or `dangerous = [...]` /
  `true` for a wrapped driver as a whole. The agents' `set` and `call` refuse
  them, including through `$set`, and `call_dangerous` runs them; the window
  asks before running one. A config file's `dangerous` adds to the class's
  marks and never removes one, and the two halves of an accessor pair (`X`
  and `set_X`) are marked together, since they write one setting.

All of these reach clients only through `describe()`, as optional keys
(`meta`, `params`, `dangerous`), and the server announces them as the
features `"meta"` and `"dangerous"`. The tables they live in are made when the
device's runtime is made and again when it is added to a server, so what a
config file sets after the constructor ran counts: a new `dangerous` mark, a
new `poll` entry, and a new interval for one the class already polls (a task
keeps an interval changed at run time only while its declaration stays the
same). A name that the device does not have is reported then: for a wrapped
driver already when the file is checked, since making its device runs none
of the driver's code.

## Keeping the core light

*Status: implemented. `tests/test_footprint.py` enforces the limits below,
and `asyncio` is imported only where it is awaited.*

What `import insthub` may load:

- the standard library, numpy and msgpack;
- `insthub.device`, `server`, `client`, `codec`, `wire`, `registry` and
  `errors`, and `_version`, the one line that holds insthub's version.

What it must not load: Qt in any binding, pyqtgraph, matplotlib,
`importlib.metadata`, `asyncio`, or any of insthub's optional modules
(`config`, `extensions`, `recipes`, `qt`, `controls`, `viewer`, `sim_ui`, `manager`, `pages`,
`forms`, `widgets`, `inventory`, `render`, `mcp`, `sim`, `bench`, `service`,
`launcher`, `cli`, `tomlfile`). It must not write a file or open a connection
either.

`import insthub` used to add about 210 modules to a bare Python and take
about 40 ms on the lab PC, half of that numpy. About 6 ms of it was
`asyncio`, which only the asynchronous paths of `client.py` use: `await` on a
call, `aio()`, `aget()` and `async for` on a subscription. That import now
happens in them, and `import insthub` adds about 170 modules.

**A test makes this a rule, not a habit.** `tests/test_footprint.py` imports
insthub in a fresh Python, with its runtime and config folders pointed at an
empty one, and checks the lists above and that the folder stays empty. It
prints the import time rather than failing on it. Several
frameworks we studied got heavier through imports nobody guarded:

- nspyre imports its GUI along with the package.
- PyLabNet's logger pulls in Slack and Qt.
- ScopeFoundry's "pure" driver imports pyqtgraph through a helper.
- labscript connects to a lock server when one of its modules is imported.

ARTIQ later had to split its remote-call layer (sipyco) into a package of its
own, so that small programs could use it without the rest.

More rules:

- New features arrive as optional keys and as modules imported where they are
  used, never as new hard dependencies. For example, `config.py` imports
  `difflib`, for its "did you mean" suggestions, only when there is an error
  to report.
- No lazy imports in `__init__.py` (a module-level `__getattr__` that imports
  a submodule the first time it is used). The core modules import each other
  anyway, so it would save about a millisecond and make the package harder to
  read.
- The window and the MCP server should use only insthub's public API, so they
  could move to a separate package one day. They connect through
  `insthub.connect(address, connect_timeout=...)`, which gives the window the
  short wait it needs, and no longer through `client._SocketTransport`, and
  take a server's address from the `"address"` of its `discover()` entry.
- Extensions carry the rule on: their driver and device modules do not import
  Qt (see [rules inside an extension](#rules-inside-an-extension)).

## The plan, in order

The first rows are small and protect what exists. Extensions come after them,
so they are built on guard rails.

| # | Change | Where | Size |
|---|---|---|---|
| 1 | **Done.** The footprint test. `asyncio` imported only where it is awaited. Imports that only the window needs moved into the functions that use them (`registry.py` imports `tomlfile` only for editing `devices.toml`). | `tests/`, `client.py`, `registry.py` | small |
| 2 | **Done.** Loader hygiene: report two modules of the same name from different folders; check each table's keys against its driver's constructor whenever a `.toml` is loaded, and so before a service is installed ("`prot`: not a parameter of ...; did you mean `port`?"); every problem of the file at once, drivers that cannot be loaded among them, from `insthub serve` (an error, not a traceback) as from `insthub service install`, which says whether fixing the file or the Python environment would help (or both); a driver's own error (its module failing for another reason than a missing module, a `Device` subclass's constructor) is no problem of the file: it names the table and keeps its traceback; a key that a constructor taking `**kwargs` gets but that is close to a reserved key (`lazzy`) is warned about; a reserved key that the table has and the constructor also takes is an error until `kwargs` says what the constructor gets, and a constructor parameter with a reserved name that the table does not have is explained in the error when it is required and warned about by `insthub service install` when it is optional, and so is a driver that imports Qt (`insthub service install` shows the warnings and goes on); the collision check covers the module a table's `driver` names, also inside a namespace package, not the modules it imports; a test that every `wrap()` option and `Device` setting is a reserved config key | `config.py`, `service.py` | small |
| 3 | **Done.** A tolerant bus: `version` and `features` in the hello (`client.supports()`); both sides skip kinds of message they do not know | `server.py`, `client.py`, `wire.py` | small |
| 4 | **Done.** Devices say what they are: `extension`, `extension_version`, `simulated` (a SIM badge on the tile and the device page, in `insthub info`, and in `list_devices` for agents), as `Device` attributes, `wrap()` options and config keys | `device.py`, `config.py`, window, `mcp.py` | small |
| 5 | **Done.** Metadata for properties and command parameters (`META_KEYS`, `set_meta`, choices from `Literal` and `Enum`), accessor pairs, polling from the config; the names that a wrapped driver's `meta`, `poll` and `dangerous` use are checked with the file (so by `insthub service install`); the device page's controls follow the metadata, and `insthub info` shows it | `device.py`, `config.py`, `pages.py`, `mcp.py` | small to medium |
| 6 | **Done.** Dangerous commands: `@command(dangerous=True)`, also for a whole wrapped driver at once. The MCP tools refuse them, including through `$set`, and a seventh tool, `call_dangerous`, runs them, so an agent can be allowed everything else without asking. The window asks before running one. A config file can only add marks, and an accessor pair is marked as a whole. | `device.py`, `mcp.py`, `pages.py` | small |
| 7 | **Done.** Extension manifests: `insthub/extensions.py` (no Qt, never imported by `import insthub`); `insthub ext list`, `add` and `remove`, and the same on the window's Settings page, with what is wrong with each; `insthub check PATH [--import]` (with `--import`, what a manifest's entries name is loaded in the extension's Python: drivers, `link_errors`, panels and apps' modules, and a class whose `extension` is not the manifest's `id` is reported; a server file gets the check of `insthub service install`, in the Python that serves it, `extensions.python_for`, the one rule that the service and the window's dialogs follow too); an extension's Python, like its manifest, is run only if no other user may change it; insthub's own manifest for its simulators; each device's folders and the server's Python in the server's `.json` file (not in the hello); the New server dialog offers the manifests' devices, fills in their keys and runs them with the extension's Python | new module, `cli.py`, `server.py`, `forms.py` | medium |
| 8 | **Done.** Starting UIs: `extensions.launch` (an app or a panel, in the extension's folder and Python) and `device_uis` (a device's UIs, from its own folders when its server runs here, or why there are none); `insthub open`, `insthub panel`, `qt.device_window`; the device page's **Open** buttons and its tile's menu, and the hint for an extension not installed here; `types` in a `[ui.NAME]` table; insthub's own scope panel (`sim_ui.py`); everything the window starts (servers, live views, UIs) writes a log file through one helper (`inventory.spawn`), and the window shows its last lines when one ends with an error; a stop asked before a Qt program's event loop runs is no longer lost | `extensions.py`, `cli.py`, `qt.py`, `manager.py`, `pages.py`, `inventory.py` | medium |
| 9 | **Done** for insthub's part. The reference extension, `examples/extension/`: a simulated laser that runs without hardware, with a manifest; a driver with a module name of its own and no insthub in it; a `Wrapped` device that says its extension and version, that it is simulated, the units and limits of its settings, what it polls and what is dangerous, and keeps its raw protocol off the bus; a panel made of `qt.follow`, `qt.watch` and `Controls`; an app that only attaches when it is given an address, and else hosts a laser itself if none runs; a server file; and a README on trying it and making it your own. The tests run it from `insthub check --import` to `insthub open`. Converting `hp_8562a_gpib` belongs to that project, and its first step is done there (see [existing projects](#existing-projects-become-extensions)) | `examples/extension/` | medium |
| 10 | **Done.** Operations: a server's process keeps its last 1000 log records (`server.keep_logs`; all of them, and exceptions nothing caught, for `insthub serve`, `insthub sim` and `serve()`; insthub's own for a program that hosts a server, from before its devices start, and still printed where they were: Python's last resort for a program that leaves logging alone), and the server call `logs` (the feature `"logs"`) gives them to `Client.logs()`, `insthub logs SERVER [-f]` and the window's **Logs** for a server with no journal or log file; an older server says it keeps none; `service install`, `start` and `restart` wait until the service's process has announced its server, and show the journal's last lines if it does not; a limit on restart loops (5 s apart, 5 starts in 5 minutes; `start` resets it), and the restart count in `insthub service list` and the window; `insthub.states()` and `insthub states` collect every device's state at once for saving next to a data file (each name read where `locate` finds it, by one rule, `client._place`, which the agents' `list_devices` follows too, so a name two servers have is an error, not a guess; `address=` / `-a` for one server; exit code 1 when what was asked for is missing); remote addresses in one place, `registry.remote_addresses()` (`devices.toml`'s `[devices]` and a new `[servers]` table), for `locate` (which asks those servers for their devices on a miss, cached for 5 s or until `devices.toml` changes), `discover(remote=True)`, `insthub list`, the window and MCP, each server once however many addresses reach it (`registry.identity`: name, pid and start time), and a `devices.toml` that cannot be read named instead of stopping them; looking at a server elsewhere keeps no connection open to it; every `discover()` entry has the `"address"` to use; `insthub logs NAME` looks at this computer first; `insthub.connect(address, connect_timeout=...)` | `server.py`, `service.py`, `client.py`, `registry.py` | small each |
| 11 | **Done.** Qt helpers for panels: `qt.start` (a call started off the GUI thread, in order per connection, so a server that has gone away never freezes the window); `qt.coalesce` (one call on its way at a time, later values merged into the next one; `send.busy` says whether one is on its way); `qt.follow` (the same for a control that also shows the device's value: never an old report over a value on its way, and what the device took once a call has ended); `qt.deliver(..., owner=widget)` hands nothing to a widget that was deleted; the device page's settings and commands as widgets of their own, `insthub.controls` (`Controls`, `Settings`, `Commands`, with `include` and `exclude`), which the window's device page is made of (it replaces how they reach the device, through its workers) and insthub's scope panel embeds | `qt.py`, `controls.py`, `pages.py`, `sim_ui.py` | medium |
| 12 | **Done.** Which servers run older code: each server lists the files of the code it loaded, with their times, in its `.json` file (`server._code`: insthub's own modules and those of its devices' folders, not a Python environment's); one rule, `extensions.outdated`, compares them with the disk and each device's `extension_version` with its manifest's `version`, for `insthub list` and the window (the OUT OF DATE mark on a server's card and above its devices, and why, in a Code row under its state on its page, with a Restart link when the window can restart it). A config whose `[server]`, `[devices]` or device entry is not a table is a problem of the file, said with its others, not an `AttributeError` | `server.py`, `extensions.py`, `cli.py`, `inventory.py`, `manager.py`, `pages.py`, `config.py` | small |
| 13 | **Done.** Bringing services up to date, for programs and people alike: a unit an older insthub wrote is recognised (`service.units()`' `"stale"`, what is old about it; a unit's first line says its format), and one written or changed by hand, or by a newer insthub, is left alone and says why (`"unchecked"`); one rule, `service.plan`, says what each service needs, and `service.update` does it (a stale unit written anew for the same file and Python, keeping whether it starts at login; a running service whose unit or code changed restarted, waiting until it answers; one that does not run never started; one failing never stops the others), with a record per service (`service.Update`); `insthub service update [NAME ...] [--check] [--json]`; `insthub service list` says what is out of date, and `insthub list --json` gives each server's reasons; the window's **Update** (a server's page and card) and **Update all** (the Servers page, one question that says what restarts), and a Unit row on a service's page; the window and `insthub mcp` say when their own code changed since they started (`extensions.outdated_here`) | `service.py`, `cli.py`, `extensions.py`, `manager.py`, `pages.py`, `mcp.py` | small |
| 14 | **Done.** Recipes as Python functions (a TOML format of steps was tried first and dropped): your recipes folder and `[extension] recipes` in manifests (a recipe that cannot be used leaves the rest usable; a manifest with only recipes needs no Python), read with `ast` and never imported to list or check them; every device asked before a recipe runs; each run in a program of its own, with its Python, its output streamed, and Stop; `insthub run` (with `--dry-run`, and asking before what is dangerous), `insthub recipes`, and `insthub check FILE.py`; the window's **Recipes** page, the device page's **Recipes** tab and **Save as recipe...**; recipes in the agents' `describe`, and an eighth tool, `run_recipe`, which refuses a recipe with a problem or something dangerous; `examples/sim_recipes.py` for the simulated instruments | new module, `extensions.py`, `cli.py`, `pages.py`, `manager.py`, `mcp.py` | medium |

Only when a real need shows up:

- **Claims**: one program takes a device for a while, and the others may read
  but not change it. Nobody has hit a conflict yet, and a forgotten claim is
  a failure of its own. Pausing a device (`dev.bus.pause()`) already covers
  handing an instrument to the vendor's software for a while.
- **`$batch`**: several calls that wait in the device's line as one request,
  so nothing runs between them.
- **A lock on the hardware itself** (a serial port, a GPIB address), so that
  a GUI hosting its own server and a service cannot both open one instrument.
- **Passwords for TCP** (a shared key checked once per connection). Until
  then, keep TCP on a trusted network (see [what insthub does not
  do](architecture.md#what-insthub-does-not-do)), or forward the server's
  port through SSH, which also encrypts: `ssh -L 8765:localhost:8765 labpc`.
- **Entry points**, **panels inside the window**, **shared contracts**
  (`implements = ["spectrum_analyzer/1"]`, once two vendors share one UI) and
  **hardware matching** for the New server dialog.

## What we learned from other projects

In November 2026 we studied eleven projects: eight lab-control systems like
insthub, from a comparison of projects similar to it, and three chosen for
how they load plug-ins (marked *extra*). VS Code was studied from its
documentation; the others from their code. One agent studied each project. A
second agent then checked every claim against both that project's code and
insthub's, and argued against every proposal. Of 105 proposals, one was
dropped and most were narrowed. The table keeps what survived.

| Project | What it is | Take | Leave |
|---|---|---|---|
| [nspyre](https://github.com/nspyre-org/nspyre) | RPyC instrument server, a pickle data broker, Qt widgets | a widget is created only when someone opens it | RPyC proxies leaking into drivers (`obtain()`), the broker, a GUI imported with the package |
| [PyLabNet](https://github.com/lukingroup/pylabnet) | a 58k-line repository: drivers, servers, launchers, GUIs | seeing every process's log from one place; one list of remote servers | a device spread over five folders; a central log server everything depends on; one vendor library (`zhinst`) pinning Python below 3.9 for everyone |
| [instrumentserver](https://github.com/toolsforexperiments/instrumentserver) | QCoDeS instruments behind ZMQ, with Qt GUIs | a UI named by a string, with a generic fallback; real signatures on proxies; polling set in the config | a loader that works only because GUI and drivers share one environment; a generic widget that stalled for lack of metadata |
| [PyroLab](https://github.com/BYUCamachoLab/pyrolab) | Pyro5 remote objects, a nameserver, its own supervisor | rejecting misspelled config keys before anything starts; services that report when they are ready; every new device fact settable from outside the driver class | drivers inside the framework package (scipy as a hard dependency for one constant); lock methods injected into driver classes; a home-made supervisor |
| [LabRAD](https://github.com/labrad/pylabrad) | a central Manager, typed settings, a registry, nodes | the node reads server manifests as text and starts servers from a command line; it remembers which manifest version started each server, so outdated ones show | the central Manager, numeric setting IDs, the wire type system |
| [Galois Edge](https://github.com/Galois-Labs/edge) | a Go and Python daemon exposing devices over MCP and gRPC | contributions declared as data, with stated precedence; a dangerous flag on commands; marking simulators | safety checks scattered over tools that raw paths get around; a profile engine in the core |
| [labscript suite](https://github.com/labscript-suite) | hardware-timed shots; BLACS tabs and workers | **the closest precedent:** a device is a bundle of parts named by import strings and loaded in different processes; vendor libraries imported when the device starts | discovery through a `.pth` file (code Python runs at every start) and a config file; bare module names that collide (it runs its registration files outside Python's module table to dodge this) |
| [ARTIQ](https://github.com/m-labs/artiq) | an experiment master, controllers, dashboard applets | applets as separate programs with a tiny base class (hence `qt.device_window`); feature lists in the hello (sipyco); drivers as separate packages (NDSPs) | embedding other programs' windows, which fails on Wayland; a master the dashboard cannot reconnect to |
| [qudi](https://github.com/Ulm-IQO/qudi-core) *extra* | hardware, logic and GUI modules joined by config | a GUI adds nothing its logic cannot do; a dummy for nearly every interface | a Qt object as the base class; one package name every add-on installs into, held together by a `sitecustomize.py` that Python runs at every start; one add-on that hard-depends on PySide6, Jupyter and every vendor library |
| [ScopeFoundry](https://github.com/ScopeFoundry/ScopeFoundry) *extra* | one-window microscope apps, with `HW_*` plug-in repositories | one folder per instrument; typed settings generate most of the UI, with limits that can change at run time; `get_`/`set_` pairs as settings | the "pure" driver importing Qt through a helper; limits hard-coded in a plug-in although its driver can ask the instrument |
| [npe2](https://github.com/napari/npe2) and VS Code *extra* | napari's plug-in manifests; VS Code's extension model | npe2's four steps: find a manifest without running code, index it, keep `"module:attr"` strings, import on first use. From VS Code: code runs where its libraries are (`extensionKind`), and a language server is a separate program behind a protocol | pydantic, code that runs when a plug-in is switched on or off, pluggy hooks, unloading plug-ins from a running program |

For runs of precisely timed shots (DDS, TTL, camera triggers on one
timeline), labscript and ARTIQ remain the tools. insthub configures
instruments and streams their data around them; it does not try to be a
pulse sequencer.

## Ideas we decided against

| Idea | Seen in | Why not |
|---|---|---|
| A central broker, master or nameserver | nspyre, PyLabNet, LabRAD, ARTIQ, PyroLab | One more program that must run first and can fail; two hops per call; large arrays copied through a middleman. The servers' `.json` files and direct connections already do the naming. |
| pickle, `eval`, or transparent remote objects | nspyre, PyLabNet, qudi, instrumentserver, ARTIQ | Any program that can connect could run code. Remote objects leak the network into driver code. The msgpack codec with a fixed set of types is safer and faster. |
| Qt in the core | qudi, ScopeFoundry, nspyre, instrumentserver | Every server and script would pay for it, and every extension would be tied to one Qt binding. |
| Drivers inside insthub, or one add-on with every vendor's drivers | PyroLab, PyLabNet, labscript, qudi | The core would need every instrument's libraries. Each extension declares only its own. |
| Discovery through one package name all extensions share, code Python runs at every start (`.pth` files, `sitecustomize.py`), or importing everything | qudi, labscript, napari's old plug-ins | Hidden effects on every Python start; import order decides which code runs; listing extensions loads vendor libraries. |
| Importing extension UIs into the window by default, or embedding other programs' windows | ARTIQ, instrumentserver, qudi, labscript, ScopeFoundry | The window has no drivers, the projects have their own environments, and one faulty panel would take down the window and its tray icon. A panel runs as a program of its own; a tab in the window, when you choose it, is an [open question](#open-questions). |
| Code that runs when an extension is switched on or off; unloading an extension from a running program | npe2, VS Code | insthub's extensions provide constructors and programs, not registrations in a long-running editor. Python cannot really unload a module. |
| A declarative instrument-profile engine in the core, or one agent tool per command | Galois Edge | The core would end up knowing every protocol. One tool per command makes an agent's context grow with the number of devices. If no-code SCPI devices are ever wanted, that is an extension that makes ordinary `Device` classes. |
| Reloading driver code inside a running server; adding devices over the bus | nspyre, ScopeFoundry | Stale classes and half-reloaded modules; the bus becomes a way to run code. Restarting a server takes about a second, and clients reconnect by themselves. |
| Sending changes instead of whole values | ARTIQ, nspyre | Every subscriber would need every change, in order, without loss, which contradicts "a device never waits for a subscriber". Whole frames, of which the newest is kept, are simpler and safer. |
| A measurement framework, mode state machine, or experiment runner | labscript, ScopeFoundry, nspyre | Experiment logic belongs to experiments. The device's waiting line, periodic tasks, futures and an abort command marked `concurrent=True` already cover ordering, background work and stopping. |
| Settings saved automatically per device, or a central config store | qudi, LabRAD | Automatic saving would put a file format and hidden state into `Device`, which every program imports; a central store is one more service to start first. Preferences belong to whoever owns them: the instrument, the UI or the experiment. |
| Interfaces per kind of instrument in the core; start-up ordering between devices | PyLabNet, nspyre | Interfaces are instrument knowledge (rule 1). Ordering would make one device's thread wait for another. A power-up sequence is a device of its own, in an extension. |
| Entry points as the only way to find extensions, for now | npe2 | They see only the current environment, which for the window has no drivers, and the lab's projects are folders, not packages. They come later, as one more place to look. |

## Open questions

1. **Packaged extensions**: should the manifest stay a file of its own
   (shipped inside the package), or become a `[tool.insthub]` table in
   `pyproject.toml`? The separate file works for folders today.
2. **One id per extension, or per kind of device?** A UI can already say
   which types of device it is for (`types`, as insthub's own scope panel
   does), so one manifest serves devices that need different UIs. An id per
   kind of device (`"hp8562a/analyzer"`) would be needed only where the type
   cannot tell them apart.
3. **Panels inside the window**: when an extension's UI half is installed in
   the window's own environment, should its panel be offered as a tab? Only
   as an opt-in, built inside `try`/`except` so the generic tabs keep working
   if it fails, and only once a manifest can say which Qt binding the panel
   uses.
4. **Shared contracts**: `implements` tags, and perhaps small packages that
   define a standard set of commands, once two vendors' devices share one UI.
5. **Agents**: should a manifest point to documentation or hints for agents,
   and how would `insthub mcp` show them without knowing any device?
6. **Windows and macOS**: finding extensions and starting programs is
   portable, but services are systemd only. What should `insthub service` use
   there (see [Windows](running-servers.md#windows))?
