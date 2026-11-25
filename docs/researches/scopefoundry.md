# ScopeFoundry

ScopeFoundry is a Python and Qt framework for building one-window control
programs for microscopes and other lab setups. It comes from the Molecular
Foundry at Lawrence Berkeley National Laboratory (main author Edward S.
Barnard) and is BSD licensed. It is mature and still maintained: version
2.3.0 is dated 2026-09-10 in its
[CHANGELOG](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/CHANGELOG#L1).
The core has about 27,000 lines of Python. Support for each instrument lives
in a separate `HW_*` repository. We read one of them, the plug-in for the
Thorlabs PM100D power meter (515 lines), as a typical example.

| Repository | Commit studied | Commit date |
|---|---|---|
| [ScopeFoundry/ScopeFoundry](https://github.com/ScopeFoundry/ScopeFoundry) | `6375fac0b609c7e9fe11f81f0200bb231fa66586` | 2026-09-10 |
| [ScopeFoundry/HW_thorlabs_powermeter](https://github.com/ScopeFoundry/HW_thorlabs_powermeter) | `d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257` | 2025-10-20 |

We studied it in November 2026, from its code, without running it. It was an
*extra* project: chosen not because it resembles insthub, but for how it
packages one instrument's driver and UI together. Its one-line summary is in
[what we learned from other projects](../design.md#what-we-learned-from-other-projects).

## Words used here

| Word | Meaning in ScopeFoundry | Closest thing in insthub |
|---|---|---|
| **App** | The one program. A `BaseMicroscopeApp` subclass whose `setup()` lists every instrument and measurement in code. It owns the Qt application and the main window. | none: insthub has many programs |
| **HardwareComponent** | One instrument's settings, bound to a driver's read and write functions. | a `Device` |
| **LoggedQuantity** (LQ) | One typed setting: a cached value plus type, unit, limits, choices and a description. Widgets, tooltips, `.ini` files and HDF5 attributes are generated from it. | a `prop` with its metadata, but no cached value |
| **Measurement** | A threaded acquisition plus its Qt window. Plug-ins use one as their custom UI. | a script, or an extension's app |
| **Operation** | A named function with no arguments, shown as a button. | a command |
| **Path** | A setting's address: `hw/<name>/<setting>`, `mm/<name>/<setting>` or `app/<setting>`. | device name plus property name |

## How it is built

| Area | What ScopeFoundry does |
|---|---|
| Process model | One Qt GUI process holds everything. The app creates or reuses the `QApplication` ([base_app.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/base_app/base_app.py#L115-L119)) and keeps all instruments and measurements in two dicts ([base_microscope_app.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/base_app/base_microscope_app.py#L85-L86)). The only way in from outside is an optional, read-only web API on a thread of its own ([flask_web_view.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/flask_web_view/flask_web_view.py#L72-L93)). |
| Transport | None between parts: calls are Python calls, and notifications are Qt signals. Settings are saved to `.ini` files and as HDF5 attributes with a `units` group ([h5_io.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/h5_io.py#L122-L140)). The function meant to serialize setting metadata says "TODO: Complete me" ([collection.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/collection.py#L462-L492)), because metadata never had to leave the process. |
| Discovery and naming | Parts find each other by name inside the process, as in `app.hardware['thorlabs_powermeter']` ([powermeter_optimizer.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/powermeter_optimizer.py#L37)). Every setting has a path ([hardware.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L48-L51)), kept in one map from path to setting ([base_app.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/base_app/base_app.py#L234-L242)). There is no network discovery and no plug-in discovery. |
| Concurrency | Locks and Qt signals. Each setting has a lock, replaced by one lock per instrument unless the plug-in opts out ([hardware.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L77-L82)). The power meter opts out ([L46](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_powermeter.py#L46)) and locks inside its driver instead ([L27](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_pm100d.py#L27)). The GUI thread, a measurement's `QThread` ([measurement.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/measurement.py#L33-L39)) and an instrument's own update thread ([hardware.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L98-L113)) may all talk to one instrument. |
| Live data | No streams. A measurement keeps its arrays as attributes, and a timer calls its `update_display()` every 0.1 s by default ([measurement.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/measurement.py#L726-L738)). A setting's new value reaches its widgets through Qt signals ([logged_quantity.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L312-L344)). Read-only settings change only when something reads them. |
| Device abstraction | A `HardwareComponent` has three methods to override: `setup()` declares settings and operations, `connect()` makes the driver and binds each setting to driver functions, `disconnect()` undoes that ([the power meter](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_powermeter.py#L12-L81)). Every instrument also gets a `connected` checkbox and a `debug_mode` setting ([hardware.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L65-L74)). |
| Extension mechanism | A plug-in is a folder `ScopeFoundryHW/<company_model>/` in the project ([README](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/README.md?plain=1#L96-L112)). `python -m ScopeFoundry new_hardware` writes one from templates ([new_hw.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/tools/features/new_hw.py#L22-L69)), and a publish tool pushes it to GitHub as `HW_<name>` ([publish_hw.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/tools/features/publish_hw.py#L27-L36)). An app loads it by importing it and calling `add_hardware` ([powermeter_test_app.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/powermeter_test_app.py#L13-L15)). Since 2.3.0, parts can be removed at run time and their code reloaded in place ([CHANGELOG](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/CHANGELOG#L8)). |
| UI | Generated from settings. `new_default_widget` picks a list, spin box, checkbox or text field from the type and choices ([logged_quantity.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L1302-L1343)), and `connect_to_widget` binds it both ways with limits, unit and tooltip ([L427-L751](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L427-L751)). A form of any part's settings, filtered by name patterns, can be embedded anywhere ([hardware.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L201-L207)). Each measurement adds one window, usually drawn in Qt Designer, shown inside the main window. |
| Configuration | Code, not files: `setup()` lists the parts. Setting values are saved and loaded as `.ini` files keyed by path ([base_app.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/base_app/base_app.py#L304-L331)). Loading skips settings marked `protected` and reports MISSING, PROTECTED or ERROR per path ([L266-L302](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/base_app/base_app.py#L266-L302)). Loaded values also appear in each widget's right-click menu, beside the last seven values ([logged_quantity.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L1405-L1436)). |
| Errors | A failed `connect()` clears `connected` and shows a red ⚠ ([hardware.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L152-L160)). There is no automatic reconnect. An error in an update loop is printed, then the loop sleeps a second ([L130-L137](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L130-L137)). Importing `base_app` replaces `sys.excepthook`, so errors in Qt slots are logged instead of ending the program ([base_app.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/base_app/base_app.py#L36-L42)). |
| Observability | One log file per session under `./log/` ([base_microscope_app.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/base_app/base_microscope_app.py#L154-L160)), a log widget, connection marks in the tree. Every HDF5 file records an id, the time, the git state and all settings with units ([h5_io.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/h5_io.py#L71-L99)). Much tracing is still `print` ([hardware.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L110)). |
| Security | None, which suits one local program. The web API has no login. A setting marked `is_cmd` runs its value as a shell command when its button is clicked ([logged_quantity.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L1453-L1460)). |
| Footprint | Requires numpy, h5py, uuid7, qtpy and pyqtgraph, plus a Qt binding ([setup.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/setup.py#L71-L72)). `import ScopeFoundry` loads the app, measurements, scanning, sequencer, controlling and sweeping ([`__init__.py`](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/__init__.py#L1-L21)), and the helper module imports pyqtgraph and Qt at the top ([helper_funcs.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/helper_funcs.py#L14-L15)). |

## What insthub takes

These lessons survived the second reading, in which a checker compared each
claim with both codebases and argued against each proposal. Most were
narrowed. The decisions themselves are in [design.md](../design.md); this
section gives the evidence. Lessons 1 to 3 are already in insthub (step 5 of
[the plan](../design.md#the-plan-in-order)); 4 to 7 are planned; 8 and 9 are
not yet decided in design.md.

**1. Settings that describe themselves.** A ScopeFoundry setting carries its
type, unit, limits, choices, step and description
([logged_quantity.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L75-L96)).
From that alone the UI picks and sets up a widget, and data files store units
beside values. The power meter declares its whole control panel in about 25
lines and writes no UI code for it
([thorlabs_powermeter.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_powermeter.py#L15-L41)).
Two faults come with it. The default limits are ±10¹²
([L86-L87](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L86-L87)),
so "no limit" cannot be told from a real limit. And the unfinished serializer
drops the limits altogether. insthub took optional metadata with a fixed
vocabulary, `device.META_KEYS` (`unit`, `min`, `max`, `step`, `choices`),
where a missing key means "unknown". It is advice for UIs and agents, not
enforced, because real instruments accept values like `"auto"` and clamp by
themselves. See [a generic UI good enough for most
devices](../design.md#a-generic-ui-good-enough-for-most-devices). design.md
also gives command parameters the same vocabulary, for drivers like the
Windfreak synthesizer whose settings are methods with a channel argument.
The checker had advised waiting on that.

**2. Limits that depend on the unit connected.** The power meter's driver
asks the sensor head for its wavelength range
([thorlabs_pm100d.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_pm100d.py#L44-L45)),
but the plug-in hard-codes 0 to 2000 nm
([thorlabs_powermeter.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_powermeter.py#L15-L20)),
which is wrong for most heads. To correct such limits, ScopeFoundry has
`change_min_max`, `change_readonly` and `change_unit`, with signals that
widgets follow
([logged_quantity.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L1161-L1190)).
In insthub a device calls `self.set_meta(...)` from `open()`. Pages see the
change the next time they read `describe()`, which the window already
re-reads, so no new signal is needed. Same section of design.md. The first
reading missed this lesson; the checker added it.

**3. `get_X`/`set_X` pairs become properties.** Most of the plug-in's
`connect()` joins driver method pairs by hand: `get_wavelength` with
`set_wavelength`, and so on
([thorlabs_powermeter.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_powermeter.py#L56-L79)).
The driver has pairs, not Python properties
([thorlabs_pm100d.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_pm100d.py#L81-L116)),
the usual shape of vendor and SCPI drivers. insthub's `wrap(...,
accessors=True)` makes a property from a getter with no required arguments
and its matching setter. A getter with no setter is left alone, so a
read-and-clear `get_error()` is never polled. See [a generic
UI](../design.md#a-generic-ui-good-enough-for-most-devices) and the
`m8195a_ripa` row of [existing projects become
extensions](../design.md#existing-projects-become-extensions). Also added by
the checker.

**4. One folder per instrument, with its layers checked.** The scaffold
writes a driver, a binding, a measurement window, a test app, a README and a
license. The driver template says it "SHOULD BE INDEPENDENT OF ANY
SCOPEFOUNDRY FUNCTIONALITY"
([_serial_dev.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/tools/templates/_serial_dev.py#L8-L10)).
The reference plug-in breaks that rule: its driver imports a helper for a
lock class
([thorlabs_pm100d.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_pm100d.py#L6)),
and the helper imports pyqtgraph and Qt. Its `__init__.py` imports the Qt
window too
([`__init__.py`](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/__init__.py#L1-L3)).
A rule kept only in a comment decayed in the plug-in meant to show it.
insthub takes the split (driver, device, optional simulator, optional UI) and
checks it. `insthub service install` already warns when a driver of the
server's `.toml` loads Qt; the planned `insthub check PATH --import` will warn
when importing the device does. There is no scaffold command; a skeleton in
`examples/extension/` will serve instead. See [what an extension
is](../design.md#what-an-extension-is), [rules inside an
extension](../design.md#rules-inside-an-extension) and steps 7 and 9 of [the
plan](../design.md#the-plan-in-order).

**5. A UI matched to the device, with the generic page as fallback.**
ScopeFoundry's data browser has its one real plug-in protocol. Each view
answers `is_file_supported(fname)`, by default by comparing the name of the
measurement that wrote the file
([data_browser_view.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/data_browser/data_browser_view.py#L38-L47)).
The browser takes the last view that accepts the file, or a generic one
([data_browser.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/data_browser/data_browser.py#L234-L239)),
and builds a view's UI the first time it is shown
([L208-L217](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/data_browser/data_browser.py#L208-L217)).
insthub keeps that shape: a cheap match (the device's `extension` against a
manifest's `id`), UI code loaded only on **Open**, and the generic page
always kept. It changes where the code runs: the UI is a program of its own,
started with the extension's Python, so a faulty panel cannot take down the
window. Devices already announce `extension`; manifests and **Open** are
steps 7 and 8. See [two halves, each where its libraries
are](../design.md#two-halves-each-where-its-libraries-are), [how a device
finds its extension](../design.md#how-a-device-finds-its-extension) and
[writing the UI half](../design.md#writing-the-ui-half).

**6. A generic control panel that a custom UI can embed.** The power meter's
own window binds its Designer widgets to settings and embeds the panel
generated for the instrument
([powermeter_optimizer.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/powermeter_optimizer.py#L45-L69)).
insthub takes the device page's settings as a widget a panel can embed,
talking only through the proxy and never blocking the GUI thread. That is a
real refactor: today the page's rows send requests through the window's own
backend, not a proxy. A helper that binds one widget comes later, if at
all. See step 11 of [the plan](../design.md#the-plan-in-order).

**7. Every device's state next to saved data.** Every ScopeFoundry data file
records all settings with units and the git state
([h5_io.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/h5_io.py#L71-L99)).
It is cheap because it saves cached values, not fresh reads
([L131-L140](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/h5_io.py#L131-L140)).
insthub plans `insthub.states()`: each device's state dict, for saving next
to a data file. It reads no properties, so it causes no instrument traffic,
and it needs no h5py; the checker also suggested adding units where metadata
has them. A snapshot is then only as complete as each device keeps its state,
which ties this to lesson 8. See step 10 of [the
plan](../design.md#the-plan-in-order).

**8. One state dict, keyed by property names.** A ScopeFoundry setting *is*
its cached value, and every widget follows it
([logged_quantity.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L51-L59)).
But it stores the new value before writing the instrument
([L299-L305](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L299-L305)),
so a refused write leaves every view showing a value the instrument never
took. insthub does not mirror single properties into the state stream:
setting the span on the HP 8562A also moves start, stop and the coupled
bandwidths. The checker proposed a convention instead: a device keeps its
state dict complete, keyed by property names, and refreshes it from
read-back values after each write; the device page would then show
`state[name]` as a property's live value. Neither the convention nor the
page change is in design.md or the code yet.

**9. An instrument that needs another one goes through the bus.** The power
meter's analog readout imports another plug-in's DAQ driver and opens the
channel itself
([analog_readout.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_powermeter_analog_readout.py#L3-L6),
[L31](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_powermeter_analog_readout.py#L31)).
Nothing declares the dependency or says who owns the DAQ. In insthub, a
device that needs another instrument takes its name and uses
`insthub.device(name)`, never the other extension's driver. From a device's
thread it must pass a timeout or use `.future()`: `insthub.device()` waits
without limit by default, so two devices calling each other would deadlock.
This rule is not yet in design.md; it belongs in [rules inside an
extension](../design.md#rules-inside-an-extension). Also added by the
checker.

**Later.** Two ideas wait for a real need:

- **Finding extensions through installed packages.** ScopeFoundry has no
  discovery at all: plug-ins are folders copied into the project, published
  with no version or declared dependencies (the plug-in's README just says
  [`pip install pyvisa`](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/README.md?plain=1#L33-L36)).
  insthub needs more, but entry points see only one environment, and each
  insthub project has its own `.venv`. So manifests come first and entry
  points later: [where insthub looks for
  extensions](../design.md#where-insthub-looks-for-extensions), item 5.
- **Saving and restoring settings in bulk.** Worth copying: the per-path
  report and the `protected` flag. Not worth copying: a blind replay of every
  path ignores coupled settings, and the faults listed under [checked and
  corrected](#checked-and-corrected). An insthub version would restore only
  the listed properties, in order, and always write. It is not in the plan.

## What insthub leaves, and why

| What ScopeFoundry does | Evidence | Why insthub leaves it |
|---|---|---|
| A measurement framework in the core: run thread, display timer, state machine, file saving, and about 9,300 lines of scanning, sweeping and sequencing, all loaded by `import ScopeFoundry` | [`__init__.py`](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/__init__.py#L10-L21) | Experiment logic belongs to experiments. A device's waiting line, periodic tasks and futures already cover it. [Decided against](../design.md#ideas-we-decided-against). |
| Reloading code in place, adding and removing hardware at run time | [hardware.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L181-L192), which even picks its import by Python version | Fragile. A server restarts in about a second (`insthub service restart`), and clients reconnect by themselves. [Decided against](../design.md#ideas-we-decided-against). |
| Settings, collections, operations and hardware built on Qt objects | [logged_quantity.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L42), [collection.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/collection.py#L19), [operations.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/operations.py#L10), [hardware.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L314) | No server without a screen can be built on it. [Qt stays out of the core](../design.md#ideas-we-decided-against). |
| A "pure" driver that loads Qt through a helper; a package import that loads everything | [thorlabs_pm100d.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_pm100d.py#L6), [helper_funcs.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/helper_funcs.py#L14-L15) | Every program that imports insthub would pay. A test guards it: [keeping the core light](../design.md#keeping-the-core-light). |
| Hardware and measurements need the GUI app object to be built | [hardware.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L36-L50), [measurement.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/measurement.py#L60) | A device must work in a script, a service and a GUI alike. `Device(name)` takes no host. |
| Instrument I/O on the GUI thread: a widget edit calls the driver directly | [L491](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L491) calls `update_value`, which writes at [L304-L305](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L304-L305) | A slow instrument freezes the window. Panels use `qt.deliver`: [writing the UI half](../design.md#writing-the-ui-half). |
| Limits applied only by widgets; `update_value` never checks them | [L249-L310](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L249-L310), [L461-L467](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L461-L467) | Widgets clamp silently while scripts go unchecked. In insthub metadata is the same advice for every client, and the instrument decides. |
| Several locks and threads on one instrument | [hardware.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L61), [L77-L82](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/hardware.py#L77-L82) | Order is undefined and deadlocks are easy. insthub has one thread and one waiting line per device: [threads](../architecture.md#threads-who-does-what). |
| Side effects on import and start-up: replacing `sys.excepthook`, changing the asyncio policy, creating `./data` and `./log` | [base_app.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/base_app/base_app.py#L42-L48), [base_microscope_app.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/base_app/base_microscope_app.py#L116-L118) | A library everything imports must not change process-wide hooks or write files: [keeping the core light](../design.md#keeping-the-core-light). |
| Import failures printed and ignored | [thorlabs_powermeter.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_powermeter.py#L3-L6) | A broken extension is listed with its reason: [installing, updating and broken extensions](../design.md#installing-updating-and-broken-extensions). |
| One setting constructor with about 25 keywords, mixing meaning (unit, limits) with looks (decimals, colors, widget factory) | [collection.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/collection.py#L86-L112) | The list grows without end and cannot be sent to remote UIs. insthub's vocabulary is small, fixed and about meaning only. |
| The same chain of widget-type checks written twice, about 600 lines | [L427-L751](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L427-L751), [L788-L1039](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L788-L1039) | Hard to keep in step. If insthub binds widgets, a few types through one table. |
| A setting whose value runs as a shell command | [L1453-L1460](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L1453-L1460) | Values can come from loaded files, so opening a file can arm a command. insthub never runs code from data. |
| Every instrument and measurement in one process, joined by name lookups | [powermeter_optimizer.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/powermeter_optimizer.py#L89) | One crash stops every instrument, and no other program can share them. See [why it is built this way](../architecture.md#why-it-is-built-this-way). |

## Checked and corrected

No claim of the first reading was judged false outright. The checker did
correct these details, and the lessons above use the corrected versions:

- **"Every real user mirrors properties into state by hand."** Not so. The
  HP 8562A device re-reads its whole coupled settings dict and publishes it
  after each change; the Windfreak device has no properties at all. That is
  why lesson 8 is a convention, not a per-property mirror.
- **"The server should check `min`, `max` and `choices`."** It would reject
  valid input: the HP 8562A accepts `"auto"` for several settings and rounds
  and clamps without an error. Metadata stays advice.
- **Missed in ScopeFoundry:** the ±10¹² default limits are sentinels, and
  the metadata serializer drops limits entirely.
- **Missed in ScopeFoundry's save and restore:** a restore goes through
  `update_value`, which returns early when the value equals the cached one
  ([L292-L295](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/logged_quantity/logged_quantity.py#L292-L295)),
  so a stale cache silently skips the write; and the "don't save read-only values"
  filter runs after the file is written
  ([base_app.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/base_app/base_app.py#L311-L317)),
  so it has no effect on the file.
- **Data browser views are registered in code too**
  ([`add_view`](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/data_browser/data_browser.py#L145)).
  Lesson 5 is a pattern for matching and starting a UI, not for discovery.
- **Entry points in the window's environment would miss most extensions**,
  because servers run in per-project environments and the window
  deliberately never imports driver code.
- **Existing commands already covered two proposals.** `insthub service
  restart` already existed, so no `insthub restart` was needed. The Qt check
  fits the existing check that imports a server file's drivers in a
  subprocess, which `insthub service install` now runs. The planned
  `insthub check` extends it and checks manifests as well.
- **Where the UI runs.** The first reading proposed importing extension
  panels into the window. The checker moved them out of process, and
  design.md then replaced the checker's idea of sending module names in
  `describe()` with a local manifest: a module name that arrives from another
  computer is never run ([how a device finds its
  extension](../design.md#how-a-device-finds-its-extension)).
