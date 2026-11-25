# qudi

qudi is the Python framework the Ulm IQO group uses to run its measurements
on color centers: confocal scans, ODMR, pulsed experiments. It was first
published in 2017 ([the SoftwareX paper][paper]) and is mature: qudi-core
marks itself "Production/Stable" ([pyproject.toml][core-status]). It comes in
two repositories. **qudi-core** (version 1.8.1.dev0, LGPL-3) is the
application: module manager, threads, configuration, logging, remote modules,
a Jupyter kernel and a manager window, in about 25,000 lines of Python plus
6.8 MB of artwork. **qudi-iqo-modules** is one large add-on: 195 Python files
and about 93,000 lines, with 18 interfaces, 95 hardware files (15 of them
simulators), 19 logic modules and 14 GUI packages.

| Repository | Commit studied | Commit date |
|---|---|---|
| [Ulm-IQO/qudi-core](https://github.com/Ulm-IQO/qudi-core/tree/dc69be418843ca9124106aeed3af153e822cec51) | `dc69be418843ca9124106aeed3af153e822cec51` | 2026-11-03 |
| [Ulm-IQO/qudi-iqo-modules](https://github.com/Ulm-IQO/qudi-iqo-modules/tree/ed5b7940fa956967ea16ff649aa252d65e39ce32) | `ed5b7940fa956967ea16ff649aa252d65e39ce32` | 2026-10-19 |

We studied it in November 2026, from its code, without running it. It is one
of the three *extra* projects, chosen for how it loads plug-ins rather than
for resembling insthub. Its one-line summary is in [what we learned from
other projects](../design.md#what-we-learned-from-other-projects).

qudi's main idea is three tiers of modules (words in the table below): a
driver, measurement code, and a window. The config file joins them, and each
link names an interface that the target must implement. A lab can therefore
swap one vendor's driver for another's, or for a simulator, by changing one
line of config, without touching the measurement code or the window. The
costs are a core built on Qt that every driver imports, one process for
everything, and packaging held together by path tricks.

A second reading, *the check*, verified every claim on this page against the
code and argued against every proposal. All the claims held. What changed
were the proposals, and this page gives the checked versions.

## Words used here

| Word | Meaning in qudi | Closest thing in insthub |
|---|---|---|
| **Hardware module** | A driver, as a subclass of qudi's `Base` class. It has no thread of its own. | a `Device` |
| **Logic module** | Measurement code that drives hardware modules, on its own Qt thread. | a script, or a device that uses other devices |
| **GUI module** | A Qt window bound to one logic module. | an extension's app or panel |
| **Interface** | An abstract class a hardware module implements, such as `SwitchInterface`. | none; `implements` tags are an open question |
| **Connector** | A named slot on a module, filled in the config with another module's name and checked against an interface. | a device name passed as a constructor argument |
| **Interfuse** | A hardware module built from others, such as two switches shown as one. | a device that uses other devices |
| **Dummy** | A simulator for one interface. | a simulated device (`simulated`) |
| **ConfigOption**, **StatusVar** | A value read from the config file; a value saved to disk when a module stops and restored when it starts. | a constructor argument; nothing |
| **netref** | rpyc's network reference: an object that forwards every attribute access to another computer. | a proxy, which sends fixed kinds of message |

## How it is built

| Area | What qudi does |
|---|---|
| Process model | Everything runs in one program. `Qudi` allows one instance per process ([application.py:91-99][app-singleton]) and needs a Qt application object even with no window ([application.py:308-312][app-qcore]). A parent launcher restarts it when it exits with code 42 ([runnable.py:59-64][runnable]). |
| Transport | Inside the program, a connector hands out a proxy to the live object, so calls are plain Python calls or Qt signals ([connector.py:114-123][conn-check]). Between computers, modules are rpyc netrefs, with every attribute and pickle allowed ([servers.py:58-63][srv-client]). |
| Discovery and naming | A module is named `module.Class` under a fixed package, `qudi.<base>`, and imported by that name ([modulemanager.py:763-764][mm-import]). Remote modules are typed in by host and port. Nothing is discovered at run time. |
| Concurrency | Qt threads. Each logic module gets its own thread ([module.py:444-447][mod-logic], [modulemanager.py:566-572][mm-thread]). Hardware modules get none ([module.py:115][mod-unthreaded]) and run on whichever logic thread calls them, so 36 of the 95 hardware files use a mutex of their own. |
| Live data | Qt signals declared on logic modules, such as `sigSwitchesChanged` ([switch_logic.py:54][sw-signal]). No frames, sequence numbers or drop policy. Hardware that changes by itself is polled by a timer in the logic ([switch_logic.py:191-206][sw-watchdog]). |
| Device abstraction | A driver subclasses an interface, an abstract class that is also a Qt `QObject` ([switch_interface.py:29][sw-iface]). An interface can define a constraints object, for example frequency and power limits ([microwave_interface.py:239-262][mw-constraints]). Every module carries a state machine with three states: deactivated, idle, locked ([module.py:58-64][mod-fsm]). |
| Extension mechanism | Add-ons install into one shared package name, `qudi`; qudi-core's [`src/qudi`](https://github.com/Ulm-IQO/qudi-core/tree/dc69be418843ca9124106aeed3af153e822cec51/src/qudi) has no `__init__.py`. When two add-ons ship a module of the same name, a `sitecustomize.py` (a file Python runs at every start) reorders the package's search path by looking for the text "qudi-iqo-modules" in it ([sitecustomize.py][sitecustomize], [pyproject.toml:104-106][iqo-pymodules]). |
| UI | A GUI module builds a Qt window and binds to a logic module through a connector ([switch_gui.py:107][sw-gui]). Opening a window activates the logic and hardware below it ([modulemanager.py:555-561][mm-up]); stopping a module stops everything that depends on it ([modulemanager.py:671-678][mm-down]). The manager window lists modules in GUI, Logic and Hardware tabs ([modulewidget.py:318-326][modwidget]). |
| Configuration | One YAML file per session, checked against a JSON Schema when it loads ([validator.py:68-72][validator]). Each `ConfigOption` says what happens when it is missing, and can carry a checker and a converter ([configoption.py:52-61][configoption]). |
| Errors and reconnect | An exception during activation is logged and leaves the module off ([module.py:297-306][mod-activate]). Logic modules often log and carry on, returning `None` or `-1` ([switch_logic.py:148-154][sw-except], [laser_scanner_logic.py:157-160][laser-locked]). A lost remote module is noticed by polling once a second, and the user must re-activate it by hand ([modulemanager.py:293][mm-poll], [modulemanager.py:617-623][mm-reconnect]). |
| Observability | One logger per module, a log table in the window, a pop-up for each error, and a rotating log file of up to 1 GiB with five old copies ([logger/\_\_init\_\_.py:168][log-rotate]). The Jupyter kernel gives live access to running modules. |
| Security | The rpyc servers allow every attribute and pickle, so any client that connects can run code ([servers.py:94-99][srv-server]). The docs call SSL "very experimental" ([configuration.md:85-90][doc-ssl]). A module is reachable from outside only if its config sets `allow_remote`, which is off by default ([modulemanager.py:322][mm-remote]). |
| Footprint | qudi-core has 16 hard dependencies, among them PySide6, Jupyter, qtconsole, scipy, and an `entrypoints` package that nothing imports ([pyproject.toml:63-80][core-deps]). The module base class imports PySide6's QtCore, QtGui and QtWidgets ([module.py:27-28][mod-imports]), and `qudi.core` reads `importlib.metadata` when imported ([core/\_\_init\_\_.py:24-29][core-init]). Even a plain driver pays for all of it. |

## What insthub takes

Each lesson gives qudi's evidence, the proposal as the check left it, and
where [the design page](../design.md) decided it. Where the two differ, the
design page is the authority.

**1. A UI adds nothing the API cannot do, and runs as a program of its own**
(adopt). qudi imports a GUI module only when someone opens it
([modulemanager.py:744-787][mm-load]), and its docs say GUI modules "MUST NOT
facilitate any functionality that could not be achieved by using the bare
logic module(s)" ([measurement_modules.md:45-49][doc-gui-rule]). The checked
proposal keeps the rule and drops qudi's one process: the UI starts as a
program of its own, with the server's Python and working directory, and its
output goes to a log whose last line is shown if it dies within seconds.
Without those, the lab's one real app (`hp8562a_gui`) would not start, or
would fail silently. Decided in [two halves, each where its libraries
are](../design.md#two-halves-each-where-its-libraries-are) and [writing the
UI half](../design.md#writing-the-ui-half); step 8 of [the
plan](../design.md#the-plan-in-order), not built yet. The design names the
app in the manifest's `[ui.NAME]` table rather than on the device, and keeps
the server's Python and each device's folder in the server's `.json` file
rather than in the hello, so they never leave the computer.

**2. Find extensions by reading data, never by importing code** (adapt).
qudi's config editor lists modules by importing every one of them
([config_editor/module_finder.py:50-70][finder-editor],
[util/module_finder.py:102-113][finder-ns]), and its docs warn of "a long
series of warnings and errors" ([configuration.md:290-298][doc-editor]). The
checked proposal reads `"module:Class"` strings as data, from installed
packages and from project folders, and never imports a manifest module:
importing any module of a package first runs its `__init__.py`, which often
imports the driver and its vendor library. Decided in [the
manifest](../design.md#the-manifest) (`insthub-extension.toml`, read with
`tomllib`) and [where insthub looks for
extensions](../design.md#where-insthub-looks-for-extensions), with entry
points as a later fifth place; step 7 of the plan, not built yet.

**3. Limits come from the instrument, at run time** (adapt; built). An
interface defines a constraints object
([microwave_interface.py:239-262][mw-constraints]); the driver checks
arguments against it ([microwave_interface.py:188-199][mw-assert]) and the
window sets its widget ranges from it ([odmrgui.py:88-100][odmr-gui]). But
drivers build these limits when they start, from what the instrument
reports: the SRS driver from its `*IDN?` model
([mw_source_srssg.py:88-116][mw-srssg]), the R&S driver from its model string
([mw_source_smbv.py:82-94][mw-smbv]), even the dummy in `on_activate`
([microwave_dummy.py:54-62][mw-dummy]). So metadata must be settable from
`open()`, as advice for the UI, not validated by the core. Built in step 5:
see [a generic UI good enough for most
devices](../design.md#a-generic-ui-good-enough-for-most-devices) (`unit`,
`min`, `max`, `step`, `choices`, and `self.set_meta(...)` from `open()`).
insthub's `prop` overrides `getter`, `setter` and `deleter`, because
`property.setter` builds a new object and would drop the metadata. The design
also gave command parameters the same vocabulary, since the Windfreak
synthesizer's settings are methods with arguments.

**4. A stable name for what a device is** (adapt; built). qudi logic asks
for an interface, not a class: `Connector(interface=SwitchInterface)`
([switch_logic.py:49][sw-connector]). The sample config wires the switch
logic to a combiner of two dummies ([default.cfg:163-169][default-switch],
[default.cfg:323-340][default-interfuse]); a real driver would replace them in
one line. But qudi decides a match by comparing class-name strings
([connector.py:114-123][conn-check], [meta.py:107-108][meta-mro]), and
interface changes have broken custom modules with no version marker
([changelog.md:82-84][changelog-1], [changelog.md:166-169][changelog-2]). In
insthub, `describe()["type"]` of a `Wrapped` device is the driver's class
name, `HP8562A` or `SimHP8562A`, so nothing stable said what a device is.
The checked proposal: a versioned tag in `describe()` only. Built in step 4
as `extension` and `extension_version` ([how a device finds its
extension](../design.md#how-a-device-finds-its-extension)). Shared
`implements` tags stay an [open question](../design.md#open-questions) until
two vendors' devices share one UI.

**5. The UI's libraries are optional, and a failed launch is loud** (adopt;
found by the check). qudi-iqo-modules hard-depends on PySide6, pyqtgraph,
nidaqmx, zaber_motion and PyVisa ([pyproject.toml:59-73][iqo-deps]), and its
only extras are for development ([pyproject.toml:84-86][iqo-extras]), so a
headless PC that hosts one switch installs Qt and the NI and Zaber libraries.
Decided as rule 5 of [rules inside an
extension](../design.md#rules-inside-an-extension) and in [writing the UI
half](../design.md#writing-the-ui-half): an app's output goes to a log, and
an early exit shows its last lines. The log comes with step 8.

**6. Check a config against the constructor before anything starts** (adopt;
found by the check; built). qudi validates its whole config when it loads
([validator.py:68-72][validator]) and lets each option say what a missing
value means ([configoption.py:52-61][configoption]). insthub needs neither a
schema nor descriptors: the constructor's signature is the schema. Built in
step 2 of [the plan](../design.md#the-plan-in-order): loading a `.toml`
checks each table's keys against its driver's constructor, so a misspelled
key is reported before a service crash-loops on it.

**7. Measurement code is just another device** (adapt, documentation only).
qudi needs a logic tier, and modules that combine others
([switch_combiner_interfuse.py:48-49][interfuse]), because its hardware
modules have no thread. An insthub device already has a thread, a queue,
periodic tasks and streams. The pattern: a composite device takes its peers'
names as constructor arguments, resolves them with `insthub.device(name)` in
`open()`, drives them with `.future()`, and publishes results as streams. One
trap: a peer's `OSError` comes back as `RemoteOSError`, a subclass of
`OSError`, so with the default `link_errors = (OSError, EOFError)` the
composite closes and reopens itself whenever a peer times out. It should set
`link_errors` itself, usually `()` or `(insthub.ConnectionLost,)`. Two
devices that make blocking calls to each other deadlock. The design rejects a
measurement framework and start-up ordering ([ideas we decided
against](../design.md#ideas-we-decided-against)), but neither it nor the
guide describes this pattern or its trap yet; the guide is the place.

**8. Simulators beside their drivers** (consider later). qudi has a dummy for
nearly every interface, its sample config runs whole toolchains on them, and
its tests load a test config ([conftest.py:37][conftest]). A reserved `sim`
key in insthub's `.toml` would break `hp8562a`'s `sim=True` constructor
argument, and a second `.toml` with the same device names already simulates
a whole lab. So no core switch. Devices announce `simulated`, shown as SIM
(step 4), and a manifest lists a simulator as a `[devices.NAME]` table whose
`sim = true` reaches the constructor unchanged ([the
manifest](../design.md#the-manifest)).

**9. An "in use" claim** (consider later). qudi's `locked` state
([module.py:58-64][mod-fsm]) is set by about 38 files around measurements and
checked by others, for example a laser scanner that refuses to move while
locked ([laser_scanner_logic.py:157-160][laser-locked]). No conflict has
shown up in the lab, and the server's `info` already lists each client. It
waits under "only when a real need shows up" in [the
plan](../design.md#the-plan-in-order), which describes a stricter claim
(others may read but not change) than the advisory one proposed here.

## What insthub leaves, and why

| What qudi does | Evidence | Why insthub does not | Decided in |
|---|---|---|---|
| A Qt object as the base class of every module, with a state-machine library and cascading activation | [module.py:102][mod-base], [module.py:27-28][mod-imports], [meta.py:25][meta-qt], [modulemanager.py:304-305][mm-mainthread] | Every driver would import Qt and be tied to one binding. insthub's `Device` is a plain class with one thread and one queue; its lifecycle is open, close and status. `tests/test_footprint.py` keeps Qt out of `import insthub`. | [ideas](../design.md#ideas-we-decided-against), [keeping the core light](../design.md#keeping-the-core-light) |
| Transparent remote objects: rpyc with pickle and full attribute access, polled liveness | [servers.py:58-63][srv-client], [services.py:245-273][rpyc-proxy], [remote_modules.md:22-43][rm-doc] | Any client could run code, arrays arrive as references, and a restart needs manual re-activation. insthub sends explicit messages with a fixed set of types. | [ideas](../design.md#ideas-we-decided-against), [what travels on a connection](../architecture.md#what-travels-on-a-connection) |
| State saved automatically per module, even when deactivation fails | [module.py:308-360][mod-statusvar], [module.py:450-455][mod-gui-geom] | A file format and hidden state in every device. Preferences stay with their owner. | [ideas](../design.md#ideas-we-decided-against) |
| One package name all add-ons install into, held together by `sitecustomize.py` | [sitecustomize.py][sitecustomize], [configuration.md:153-160][doc-extpaths] | Hidden effects on every Python start; import order decides which code runs. | [ideas](../design.md#ideas-we-decided-against) |
| Listing modules by importing all of them | [util/module_finder.py:102-113][finder-ns] | Listing would load every vendor library and Qt. | [when code is loaded](../design.md#when-code-is-loaded) |
| One add-on with the interfaces and every vendor's drivers and libraries | [pyproject.toml:59-73][iqo-deps] | Using one switch means installing the NI and Zaber libraries. Each extension declares only its own. | [ideas](../design.md#ideas-we-decided-against), [rules inside an extension](../design.md#rules-inside-an-extension) |
| A heavy core, including a dependency nothing uses | [pyproject.toml:63-80][core-deps] | Every program that imports insthub would pay for it. | [keeping the core light](../design.md#keeping-the-core-light) |
| Interface checks by class-name string, plus proxy machinery for name clashes | [connector.py:114-123][conn-check], [meta.py:107-108][meta-mro] | Two unrelated classes with one name pass; a driver must inherit framework classes. insthub compares a plain name and never interprets it. | [how a device finds its extension](../design.md#how-a-device-finds-its-extension) |
| Thread safety left to each driver author | [module.py:115][mod-unthreaded], [switch_logic.py:63][sw-lock] | Every driver repeats locking. In insthub only the device's own thread talks to the instrument. | [threads](../architecture.md#threads-who-does-what) |
| Errors swallowed into logs and `-1` (33 bare `except:` in the top-level logic files) | [switch_logic.py:148-154][sw-except], [laser_scanner_logic.py:157-160][laser-locked] | Scripts, windows and agents cannot react to a failure they never see. insthub sends the exception back to the caller. | [when things go wrong](../architecture.md#when-things-go-wrong) |
| One process for everything, restarted by an exit code | [application.py:91-99][app-singleton], [runnable.py:59-64][runnable] | One crashing vendor library takes every instrument and window down, and every add-on shares one environment. | [two halves](../design.md#two-halves-each-where-its-libraries-are) |
| Driver code that cannot exist without a running qudi | [module.py:117-119][mod-init] | A qudi hardware module cannot be used from a plain script. An insthub driver needs no insthub. | [rules inside an extension](../design.md#rules-inside-an-extension) |

The check also dropped parts of the first reading's proposals:

- **A registry key for tags, a `discover(implements=...)` filter and an
  `insthub.conforms()` helper.** The window already fetches every device's
  `describe()` with one `devices` call per server, scripts look for devices
  rather than servers, and a set comparison does what `conforms()` would.
- **Opt-in validation of limits in the core.** Limits often depend on the
  mode, and instruments accept values like `"auto"` or clamp by themselves.
- **A reserved `sim` key and `insthub serve --sim`.** See lesson 8.
- **An `insthub.paths.config_dir()` helper** for extensions' settings. No
  extension needs one.

## Checked and corrected

No claim of the first reading was judged false. The check corrected details,
and the lessons above use the corrected versions:

- qudi's limits are built by each driver at start-up, not declared once
  (lesson 3).
- `REQUIRED_COMMANDS` in `hp8562a_gui` asks "is this my analyzer?", not "can
  I drive any analyzer?". The better evidence for a tag is the type name,
  which differs between instrument and simulator (lesson 4).
- The lab's projects are folders, not packages, so an entry-point scan finds
  none of them (lesson 2).
- `hp8562a_gui` needs its working directory and takes `--name`, not
  `--device`; the live view throws its child's output away (lesson 1).
- A `sim` key collides with `hp8562a`'s constructor argument (lesson 8). A
  composite device's default `link_errors` catch its peers' errors (lesson
  7). `property.setter` drops a subclass's metadata, which the check
  confirmed with a ten-line test (lesson 3).
- The interface check is at [connector.py:114-123][conn-check], not lines
  104-110 as first cited.

[paper]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/README.md#L37
[core-status]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/pyproject.toml#L30
[core-deps]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/pyproject.toml#L63-L80
[core-init]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/__init__.py#L24-L29
[app-singleton]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/application.py#L91-L99
[app-qcore]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/application.py#L308-L312
[runnable]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/runnable.py#L59-L64
[conn-check]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/connector.py#L114-L123
[meta-qt]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/meta.py#L25
[meta-mro]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/meta.py#L107-L108
[srv-client]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/servers.py#L58-L63
[srv-server]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/servers.py#L94-L99
[rpyc-proxy]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/services.py#L245-L273
[mm-poll]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/modulemanager.py#L293
[mm-mainthread]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/modulemanager.py#L304-L305
[mm-remote]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/modulemanager.py#L322
[mm-up]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/modulemanager.py#L555-L561
[mm-thread]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/modulemanager.py#L566-L572
[mm-reconnect]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/modulemanager.py#L617-L623
[mm-down]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/modulemanager.py#L671-L678
[mm-load]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/modulemanager.py#L744-L787
[mm-import]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/modulemanager.py#L763-L764
[mod-imports]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/module.py#L27-L28
[mod-fsm]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/module.py#L58-L64
[mod-base]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/module.py#L102
[mod-unthreaded]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/module.py#L115
[mod-init]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/module.py#L117-L119
[mod-activate]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/module.py#L297-L306
[mod-statusvar]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/module.py#L308-L360
[mod-logic]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/module.py#L444-L447
[mod-gui-geom]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/module.py#L450-L455
[log-rotate]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/logger/__init__.py#L168
[configoption]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/configoption.py#L52-L61
[validator]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/config/validator.py#L68-L72
[finder-ns]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/util/module_finder.py#L102-L113
[finder-editor]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/tools/config_editor/module_finder.py#L50-L70
[modwidget]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/gui/main_gui/modulewidget.py#L318-L326
[doc-gui-rule]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/docs/design_concepts/measurement_modules.md#L45-L49
[doc-ssl]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/docs/design_concepts/configuration.md#L85-L90
[doc-extpaths]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/docs/design_concepts/configuration.md#L153-L160
[doc-editor]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/docs/design_concepts/configuration.md#L290-L298
[rm-doc]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/docs/design_concepts/remote_modules.md#L22-L43
[sitecustomize]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/sitecustomize.py#L7-L23
[iqo-deps]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/pyproject.toml#L59-L73
[iqo-extras]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/pyproject.toml#L84-L86
[iqo-pymodules]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/pyproject.toml#L104-L106
[default-switch]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/default.cfg#L163-L169
[default-interfuse]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/default.cfg#L323-L340
[sw-connector]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/logic/switch_logic.py#L49
[sw-signal]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/logic/switch_logic.py#L54
[sw-lock]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/logic/switch_logic.py#L63
[sw-except]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/logic/switch_logic.py#L148-L154
[sw-watchdog]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/logic/switch_logic.py#L191-L206
[sw-gui]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/gui/switch/switch_gui.py#L107
[sw-iface]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/interface/switch_interface.py#L29
[interfuse]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/hardware/interfuse/switch_combiner_interfuse.py#L48-L49
[mw-assert]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/interface/microwave_interface.py#L188-L199
[mw-constraints]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/interface/microwave_interface.py#L239-L262
[odmr-gui]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/gui/odmr/odmrgui.py#L88-L100
[mw-srssg]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/hardware/microwave/mw_source_srssg.py#L88-L116
[mw-smbv]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/hardware/microwave/mw_source_smbv.py#L82-L94
[mw-dummy]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/hardware/dummy/microwave_dummy.py#L54-L62
[laser-locked]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/qudi/logic/laser_scanner_logic.py#L157-L160
[changelog-1]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/docs/changelog.md#L82-L84
[changelog-2]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/docs/changelog.md#L166-L169
[conftest]: https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/tests/conftest.py#L37
