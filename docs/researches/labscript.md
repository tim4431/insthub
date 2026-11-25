# labscript suite

The [labscript suite](https://github.com/labscript-suite) controls experiments
made of hardware-timed *shots*, mostly in cold-atom labs. A shot is one run of
an experiment in which several devices follow one precise timeline. Monash
University started it in 2013 (the copyright lines in its files), and a
community maintains it now. It is mature, widely used and large: six
repositories with about 65,000 lines of Python. We studied it in November
2026 because its device registry is the closest precedent for insthub's
[extensions](../design.md#extensions): one device is a bundle of parts, named
by import strings and loaded in different programs. One agent read the code;
a second checked every claim against the code and argued against every
proposal. Nothing from the repositories was run.

| Repository | What it does | Python lines | Commit studied |
|---|---|---|---|
| labscript | compiles an experiment script into an HDF5 shot file | 6,022 | [607cfae][c-l] (2026-10-30) |
| runmanager | parameter scans; sends shots to BLACS | 7,598 | [a5ffe31][c-r] (2026-11-09) |
| BLACS | the window that programs the hardware and runs shots | 8,586 | [e7bfb89][c-b] (2026-11-09) |
| lyse | analysis of finished shots | 5,562 | [cbbcb16][c-y] (2026-11-09) |
| labscript-devices | all 36 registered devices, in one package | 28,279 | [ea31611][c-d] (2026-11-04) |
| labscript-utils | shared code: config, device registry, file locks, logging | 8,982 | [795b5d2][c-u] (2026-11-03) |

Words used here:

| Word | Meaning |
|---|---|
| **connection table** | a Python file that lists the lab's devices and how they are wired, compiled to HDF5. BLACS makes one tab per device in it. |
| **tab** | a device's page in the BLACS window: a Qt class the device author writes. |
| **worker** | the device's driver code. It runs in a child process that the tab starts. |
| **labconfig** | one INI file per computer, `~/labscript-suite/labconfig/<hostname>.ini`, read by every program of the suite. |
| **`register_classes.py`** | a small file in each device folder. It tells the registry, as import strings, which tab belongs to which device. |

## How it is built

| Area | How labscript does it |
|---|---|
| Process model | BLACS is one Qt program. Each tab starts one or more worker processes, lazily, when its first state runs ([create_worker][b-cw]). Every program also needs a lock server for HDF5 files ([zlock][u-zlock]) and a log server. |
| Transport | ZeroMQ, through the zprocess library. A job is a pickled tuple of method name, arguments and keyword arguments; the tab test-pickles each job ([job][b-pk-job]) and the worker each result ([result][b-pk-res]). Bulk data travels as HDF5 shot files on a shared drive. |
| Discovery and naming | No discovery at run time. BLACS makes a tab for each device in the compiled connection table that has a BLACS connection ([connections.py][u-conn]), and looks up the tab class by the bare device class name ([get_BLACS_tab][u-get]). A name registered twice raises `ValueError` ([duplicate][u-dup]). |
| Concurrency | Each tab has a queue of *states*: generator methods marked `@define_state` that yield jobs for a worker. A job thread waits for the worker while the generator is paused ([StateMachine][b-sm], [define_state][b-ds]). Four mode bits decide whether a queued state runs, waits or is dropped ([modes][b-modes]). A worker runs one job at a time; a second thread listens for "quit", and a job is interrupted with SIGINT ([mainloop][b-loop]). |
| Live data | No generic streams. Tabs poll, for example the instrument's values every 30 s ([poll][b-poll]). A camera opens a private socket to its tab, and the worker blocks until the window has taken the previous frame ([send][d-cam-send]). |
| Device abstraction | Up to four parts per device: a compile-time class that records its constructor arguments ([set_passed_properties][l-props]), a tab, a worker with fixed method names (`init`, `program_manual`, `transition_to_buffered`, ... [DeviceWorker][b-dw]) and a parser for the timing viewer. A tab declares its channels as dicts of `base_unit`, `min`, `max`, `step` and `decimals`, and BLACS builds the widgets ([property dicts][b-props]). |
| Extension mechanism | A device folder's `register_classes.py` holds only import strings ([Windfreak][d-wf-reg]), so that "imports can be deferred until the classes are actually needed" ([docstring][u-doc]). The tab names its worker by a string too ([tab][d-wf-tab]), imported only in the child process ([string worker][b-cw-str]). Third-party packages are listed in labconfig's `user_devices` ([docs][d-user]); `populate_registry()` walks their folders and runs every `register_classes.py` it finds ([populate][u-pop]). |
| UI | Every tab lives in BLACS's own process. The `Tab` base class supplies a status line, an error area, a restart button and a terminal with the worker's output; the author fills a layout. A tab that fails to build is skipped, and BLACS starts anyway ([tab creation][b-main-tabs]). Optional hooks save and restore per-tab UI state ([save data][b-save]). |
| Configuration | One INI per computer for every program: paths, ports, servers, plugin switches, Qt binding, security ([example.ini][u-ini]). Device settings are Python code in the connection table. Every connection-table property is copied into the worker's arguments ([props][b-cw-props]), and the worker sets each one as an attribute, with only a warning on a clash ([attributes][b-attrs]). |
| Errors and reconnect | A worker exception appears as a traceback in the tab ([job done][b-job-err]). An exception in the tab's own state is fatal: its workers are ended until someone presses restart ([fatal][b-fatal]). After 5 s without progress a "has not responded" banner appears ([check_time][b-notresp]). There is no automatic reconnect; restart ends the worker process and builds the tab again ([restart][b-restart]). |
| Observability | A logger per tab and per worker, sent to the log server ([setup_logging][u-log]). A terminal pane per tab, tab icons for ok, busy and error ([icons][b-tab]), and a status line naming the worker method that is running ([state label][b-label]). No status a program can read. |
| Security | Sockets are encrypted with a shared secret file. Without one, startup fails unless `allow_insecure = True` is set, and the message says that most suite programs allow remote code execution ([message][u-secret], [check][u-secret-check]). Pickle on the wire and `eval` of saved settings remain ([eval][b-eval]). |
| Footprint | labscript-utils hard-depends on PySide6, pyqtgraph, h5py, scipy and zprocess ([pyproject][u-deps]), so even the compile-time package pulls in Qt. labscript-devices hard-depends on blacs, runmanager, PyDAQmx, PyVISA, pyserial and spinapi for all its devices ([pyproject][d-deps]). Imports have side effects: `h5_lock` connects to the lock server and patches h5py ([h5_lock][u-h5-patch]), `tab_base_classes` builds a process tree at import ([line 42][b-tree]), and a `.pth` file runs labscript code at every Python start ([.pth][u-pth]). |

## What insthub takes

These lessons survived the check, most of them narrowed. Where
[design.md](../design.md) went further than the checker's final proposal,
the entry says so. *Status* is the state of the matching step in [the
plan](../design.md#the-plan-in-order) when this note was last checked.

| # | Lesson | Verdict | Recommendation | Decided in | Status |
|---|---|---|---|---|---|
| 1 | A manifest per extension, read as data; each part imported only where it runs | modify | adapt | [the manifest](../design.md#the-manifest) | planned, step 7 |
| 2 | A device says which extension it belongs to | modify | adopt | [how a device finds its extension](../design.md#how-a-device-finds-its-extension) | done, step 4; folders step 7 |
| 3 | An import boundary between driver and UI | modify | adapt | [rules inside an extension](../design.md#rules-inside-an-extension) | rules written; check step 7 |
| 4 | A panel is one function, and its failures stay contained | modify | adapt | [writing the UI half](../design.md#writing-the-ui-half) | planned, step 8 |
| 5 | Units and limits in `describe()`, allowed to depend on the instance | modify | adapt | [a generic UI](../design.md#a-generic-ui-good-enough-for-most-devices) | done, step 5 |
| 6 | insthub's own simulators go through the same mechanism | modify | adopt | [where insthub looks](../design.md#where-insthub-looks-for-extensions) | planned, step 7 |
| 7 | Merge superseded requests from a GUI in the client | modify | adopt | [the plan](../design.md#the-plan-in-order) | planned, step 11 |
| 8 | Family extensions share an interface, a mock and a UI | modify | adapt | [what an extension is](../design.md#what-an-extension-is) | written down; no core change |
| 9 | Module names collide between folder extensions (found by the checker) | new | adopt | [rule 4](../design.md#rules-inside-an-extension) | done, step 2 |
| 10 | Guard what `import insthub` loads (found by the checker) | new | adopt | [keeping the core light](../design.md#keeping-the-core-light) | done, step 1 |
| 11 | A secret for TCP listeners other computers can reach | keep | consider later | [the plan](../design.md#the-plan-in-order), "only when a real need shows up" | later |

**1. A manifest, read as data.** `register_classes.py` holds only import
strings ([Windfreak][d-wf-reg]). BLACS imports only the tab class
([get_BLACS_tab][u-get]), and the worker class is imported only in the child
process ([string worker][b-cw-str]), so BLACS never imports a vendor library.
The weak points: the set of parts is fixed by a function signature
([register_classes][u-reg]), and finding the files means walking folders and
running each one as code ([populate][u-pop]). The first reading proposed a
Python dict reached through an entry point. The checker objected that this
still runs the package's `__init__.py`, and that none of the lab's projects
is installable. Final: a TOML manifest read without running code; folders
first; entry points later, found with `importlib.util.find_spec`, which does
not import, as labscript's own [_get_import_paths][u-imp] does. design.md
adopted this as `insthub-extension.toml`, and goes one step further: the
window never imports extension code at all, because the UI half runs as its
own program with the extension's Python ([two
halves](../design.md#two-halves-each-where-its-libraries-are), [when code is
loaded](../design.md#when-code-is-loaded)).

**2. Devices say what they are.** labscript joins a device's parts by the
bare class name, which must differ from every other package's
([docs][d-unique], [duplicate][u-dup]). It also met the `__main__` problem: a
class defined in a script is keyed by the script's file name instead
([ClassRegister][u-classreg]). Final: an explicit id set by the extension,
matched first, then a `module:qualname` fallback, matched only against
manifests found on this computer. design.md keeps only the explicit
`extension` (with `extension_version` and `simulated`), now implemented
(plan step 4), and drops the class string, because a `Wrapped` device's type
differs between the real driver and its simulator, and the module is
`__main__` for `python bus_server.py`. To find a device's folder, the
server's `.json` file will list it instead (plan step 7).

**3. An import boundary.** `create_worker` takes a string so that a worker's
"import-time behaviour" never runs in the main process ([docstring][b-cw]).
Workers import vendor libraries inside `init()` ([Windfreak][d-wf-imp],
[guidance][b-myworker]); camera backends import theirs in the interface
class ([Pylon][d-pylon-imp]). The checker corrected one point: worker modules
are not import-clean. The Windfreak worker imports `h5_lock` and h5py at the
top, and gives "so docs don't need it" as the reason for its lazy import
([Windfreak][d-wf-imp]). The process boundary, not discipline, keeps BLACS
clean. Final: write the rule down, and offer a check that imports each part
in a fresh Python and reports what it loaded, without blocking anything.
design.md: rules 1 to 3 inside an extension, and `insthub check PATH
--import` (plan step 7). Part of the check exists already: `insthub service
install` warns when a driver named in the server's `.toml` loads Qt, and
installs anyway (plan step 2).

**4. A small panel contract.** The parts worth taking are small. A tab that
fails to build is dropped, its saved settings are kept, and BLACS starts
([tab creation][b-main-tabs]). A plugin that fails to import is logged and
skipped ([plugins][b-plug-skip]). The cost is the rest: `Tab` is about 690
lines ([Tab][b-tab]), a callback without `@define_state` is a silent bug
([example][b-forget]), and all tabs share one process, so one hang freezes
BLACS. Final: a panel is one function that takes a proxy, with no base class;
it runs first as a program of its own, as `insthub view` already does; no
settings store owned by insthub. design.md: `make(device, parent=None) ->
QWidget`, in a window of its own by default (plan step 8). A panel as a tab
inside the window is [open question 3](../design.md#open-questions).

**5. Units and limits.** A tab declares channels as dicts of unit, limits and
step, and BLACS builds the widgets ([property dicts][b-props]). The limits
come from the device instance: the Windfreak tab reads them from the
connection table ([limits][d-wf-limits]), and they differ by model, 15 GHz
for the SynthHD and 24 GHz for the SynthHDPro ([SynthHD][d-wf-15],
[SynthHDPro][d-wf-24]). The checker narrowed the expected gain (see [checked
and corrected](#checked-and-corrected)) and proposed one `Device.meta`
mapping. design.md chose keywords on `prop` and on command parameters, a
fixed vocabulary (`unit`, `min`, `max`, `step`, `choices`) that a device can
change from `open()` with `set_meta`, as the Windfreak case needs. It is
advice for the UI, not enforced, so values like `"auto"` still pass. This is
implemented (plan step 5).

**6. The simulators as an extension.** labscript finds its mainline devices
the same way as third-party ones: the search list is `['labscript_devices']
+ user_devices` ([device dirs][u-dirs]). BLACS's own features are plugins,
switched on by default ([default plugins][b-plug-default]). design.md:
insthub ships a manifest for its simulators, first in the search order, so
every test run exercises the mechanism, and the New server dialog's
hard-coded driver list is built from manifests (plan step 7).

**7. Merging superseded requests.** `delete_stale_states` keeps only the
newest of consecutive identical queued states ([StateQueue][b-stale]), so
dragging a spin box does not queue "a million calls" ([program_device][b-program]).
The checker found a better model in the lab's own `hp8562a_gui`: one request
in flight, with later keyword arguments merged into the pending one. Final:
`qt.coalesce(call, slot, error, merge)` in `qt.py`; the device's waiting
line and "a request is never run twice" stay as they are. design.md plans
it for panels (plan step 11). The generic page needs no merging: its spin
box sends a number only when you press Set, and a list or a switch sends
one value per click.

**8. Family extensions.** `TriggerableCameraWorker` holds the camera logic
and calls an interface class ([CameraInterface][d-cam-if]). `MockCamera`
stands in for hardware ([mock][d-cam-mock]), chosen by a flag on the same
worker ([get_camera][d-cam-get]). A vendor tab only names its worker
([Pylon tab][d-pylon-tab]); a vendor worker only names its interface
([IMAQdx][d-imaqdx]). Final: no core change. A vendor extension depends on a
family extension and points its panel string into it, and the mock is a
constructor flag on the same class, so identity and panel matching stay the
same. design.md: an extension is a folder "for one instrument or one family",
and its simulator may be "the device with `sim=True`". One UI for two
vendors (`implements`) is [open question 4](../design.md#open-questions).

**9. Module names collide.** labscript runs each `register_classes.py`
without entering it in Python's module table, because otherwise "each import
would need to have unique names" ([populate][u-noimp]), and third-party code
must sit in a named package ([docs][d-user]). insthub's loader imports a
bare module name from a folder, so two projects with a `bus_server.py` cannot
share one server; the checker reproduced the failure. design.md: [rule
4](../design.md#rules-inside-an-extension), and the loader now reports the
collision, naming both files (plan step 2, done). It sees only the module a
table's `driver` names. Helper modules that a driver imports in turn are
protected by the rule alone, which is why labscript's answer, a named
package for third-party code, became rule 4.

**10. Guarding `import insthub`.** labscript's `.pth` hook "intentionally
re-implements finding and reading the config file so as to not import
labscript_utils" at every Python start ([profile][u-prof-light]). Elsewhere
it shows the cost of not guarding: `h5_lock` connects to a server and patches
h5py when imported ([h5_lock][u-h5-patch]). design.md: a footprint test
that also checks `import insthub` writes no file, and `asyncio` imported
only where it is awaited (plan step 1, done).

**11. A secret for TCP.** labscript refuses to start without a shared secret
unless insecure mode is chosen on purpose, and says why
([message][u-secret], [check][u-secret-check]). The checker kept this but
noted it is not about extensions, and that a token handshake authenticates
without stopping eavesdropping. design.md lists passwords for TCP under
"only when a real need shows up"; until then, keep TCP on a trusted network
or use an SSH tunnel ([what insthub does not
do](../architecture.md#what-insthub-does-not-do)).

## What insthub leaves, and why

| labscript does | Evidence | Why insthub does not |
|---|---|---|
| A state machine per tab, with shot modes (rejected by the checker) | [modes][b-modes], [define_state][b-ds], [missing decorator][b-forget] | The modes exist for hardware-timed shots. insthub's waiting line per device already gives order, and periodic tasks run between requests ([threads](../architecture.md#threads-who-does-what)). A device with modes keeps them in its own state and raises from commands not allowed in the current one ([decided against](../design.md#ideas-we-decided-against)). |
| Bare class names as the global key | [duplicate][u-dup], [docs][d-unique] | Two independent extensions collide, and install order decides. insthub matches an explicit id ([how a device finds its extension](../design.md#how-a-device-finds-its-extension)). |
| Discovery by walking folders named in a per-computer file, plus a `.pth` hook | [populate][u-pop], [device dirs][u-dirs], [.pth][u-pth], [profile][u-prof] | Every Python start pays, and behaviour depends on a hidden file ([decided against](../design.md#ideas-we-decided-against)). insthub lists folders explicitly and reads manifests as data. |
| Side effects at import | [h5_lock order][u-h5-order], [h5_lock patch][u-h5-patch], [process tree][b-tree] | `import insthub` must not open a connection or write a file ([keeping the core light](../design.md#keeping-the-core-light)). |
| One package with every vendor's libraries; Qt in the shared library | [devices][d-deps], [utils][u-deps] | Each extension declares only its own libraries, the UI's in an extra ([rule 5](../design.md#rules-inside-an-extension)). |
| Plugins only in BLACS's own folder, with nine required methods, reaching the app through `__main__` | [loader][b-plug-load], [interface][b-plug-if], [setup][b-main-plug], [signature guess][b-main-sig], [callbacks][b-plug-cb] | Third parties would have to edit the installed app, and the interface could never change. insthub's extensions live in their own folders, and every manifest table is optional ([the manifest](../design.md#the-manifest)). |
| A fixed set of parts in the registration signature | [register_classes][u-reg] | A new consumer would change the core API. insthub adds a kind of manifest table instead, but only with the code that reads it ([the manifest](../design.md#the-manifest)). |
| Large base classes with unwritten rules | [Tab][b-tab], [restart][b-restart], [attributes][b-attrs] | A panel is one function that takes a proxy ([writing the UI half](../design.md#writing-the-ui-half)). |
| pickle on the wire, `eval` for saved settings | [job][b-pk-job], [result][b-pk-res], [eval][b-eval], [settings][u-settings] | Anyone who can connect could run code ([decided against](../design.md#ideas-we-decided-against), [why msgpack](../architecture.md#why-it-is-built-this-way)). |
| A private image socket per camera that blocks the instrument when the window lags | [send][d-cam-send], [thread][d-cam-thread], [receiver][d-cam-recv] | insthub's streams keep the newest frame per subscriber and never wait ([publishing](../architecture.md#what-happens-when-a-device-publishes)). |
| One config file per computer for every program | [example.ini][u-ini] | Setups and tests collide. insthub keeps small files per purpose, and `insthub serve` reads only its own ([where insthub looks](../design.md#where-insthub-looks-for-extensions)). |
| Tab state saved by the framework (dropped from lesson 4 by the checker) | [save data][b-save] | Preferences belong to whoever owns them ([decided against](../design.md#ideas-we-decided-against), "settings saved automatically"). |
| A module `__getattr__` for lazy imports (an optional idea in lesson 10) | [deprecation.py][d-depr] | It would save about a millisecond and make the package harder to read ([keeping the core light](../design.md#keeping-the-core-light)). |
| Everything about shots: compile-time classes, connection tables in HDF5, buffered mode, shot files, the lock server, the timing viewer, lyse | | Not an instrument bus's job. For timed shots, labscript and ARTIQ remain the tools ([what we learned](../design.md#what-we-learned-from-other-projects)). |

One labscript strength insthub has only as a deployment choice: BLACS can end
a hung worker process ([quit][b-quit], [end workers][b-end]). In insthub a
device thread stuck in a vendor call is freed only by restarting its server
([when things go wrong](../architecture.md#when-things-go-wrong)). So a vendor
library that tends to hang should get a server of its own, which the window
can restart as a service.

## Checked and corrected

- **Short tabs do not show that most UIs are just limits.** The first reading
  said labscript's many short tab files prove that the framework draws most
  device UIs from declared units and limits. At the studied commit, 16 of the
  33 device tab files have 24 lines or fewer: 8 subclass the camera family
  tab, 4 subclass a PulseBlaster tab, and the other 4 have no channels (a test
  stub, an empty GUI, a dummy device, a board without a GUI). None of them
  uses the metadata-driven `create_*_outputs`. The 14 tabs that do are 50 to
  431 lines long ([Windfreak][d-wf-limits] is 80). So short tabs argue for
  family extensions (lesson 8); metadata (lesson 5) is still worth having,
  for a moderate gain. The checker counted 17 and 11; a recount at this
  commit gives 16 and 12.
- **insthub's side of lesson 5.** At the time of the study, `mcp.py` did not
  pass `describe()` through unchanged: it rebuilt each entry field by field,
  so a new key needed an edit there (plan step 5 has since added `meta`).
  The generic page already showed a switch for boolean properties. The
  HP 8562A's settings take a number or `"auto"`, so a spin box cannot
  replace the text field; that is why design.md keeps the metadata as
  advice.
- **Smaller corrections**, where the substance held: `Tab` is about 690 lines,
  not 1,484 (that is the whole file); the first reading's line numbers in
  `_device_registry.py` were about 14 lines off (this page cites the right
  ones); labscript's worker modules are not import-clean (lesson 3).

[c-l]: https://github.com/labscript-suite/labscript/tree/607cfaecd58869b876d19cf30c52566a4f490cc6
[c-r]: https://github.com/labscript-suite/runmanager/tree/a5ffe31863be0e9f3ba92166de95ff1fc8bf35cf
[c-b]: https://github.com/labscript-suite/blacs/tree/e7bfb89d8fab3c491c3d83b2da2397d53046d604
[c-y]: https://github.com/labscript-suite/lyse/tree/cbbcb161a66f4a1c12d5dfba57fa42ccc7dbc6d6
[c-d]: https://github.com/labscript-suite/labscript-devices/tree/ea31611c8ebcccff9445958077f471b7732db669
[c-u]: https://github.com/labscript-suite/labscript-utils/tree/795b5d23937f10738695852279b4c168e69f76da

[l-props]: https://github.com/labscript-suite/labscript/blob/607cfaecd58869b876d19cf30c52566a4f490cc6/labscript/utils.py#L104-L113

[b-tree]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L42
[b-modes]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L56-L59
[b-stale]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L122-L133
[b-sm]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L153-L157
[b-pk-job]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L263-L267
[b-job-err]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L287-L299
[b-fatal]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L301-L315
[b-ds]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L343-L357
[b-tab]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L360-L365
[b-label]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L676
[b-cw]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L680-L728
[b-cw-props]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L693-L698
[b-cw-str]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L714-L724
[b-end]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L825-L831
[b-restart]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L990-L993
[b-notresp]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L1025-L1046
[b-attrs]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L1073-L1080
[b-loop]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L1092-L1104
[b-pk-res]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L1141-L1149
[b-quit]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L1162-L1177
[b-forget]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L1317-L1349
[b-myworker]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L1391-L1404
[b-poll]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/device_base_class.py#L66-L68
[b-props]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/device_base_class.py#L100-L118
[b-save]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/device_base_class.py#L385-L399
[b-program]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/device_base_class.py#L435-L440
[b-dw]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/device_base_class.py#L744-L805
[b-eval]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/front_panel_settings.py#L42-L46
[b-main-tabs]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/__main__.py#L306-L324
[b-main-plug]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/__main__.py#L417-L443
[b-main-sig]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/__main__.py#L456-L472
[b-plug-default]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/plugins/__init__.py#L23
[b-plug-cb]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/plugins/__init__.py#L62-L65
[b-plug-load]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/plugins/__init__.py#L81-L114
[b-plug-skip]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/plugins/__init__.py#L107-L114
[b-plug-if]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/plugins/general/__init__.py#L18-L49

[d-wf-reg]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/Windfreak/register_classes.py#L16-L26
[d-wf-tab]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/Windfreak/blacs_tabs.py#L19-L22
[d-wf-limits]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/Windfreak/blacs_tabs.py#L26-L57
[d-wf-imp]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/Windfreak/blacs_workers.py#L14-L23
[d-wf-15]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/Windfreak/labscript_devices.py#L28
[d-wf-24]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/Windfreak/labscript_devices.py#L179
[d-cam-if]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/common/triggerable_camera/blacs_workers.py#L34-L39
[d-cam-mock]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/common/triggerable_camera/blacs_workers.py#L92-L95
[d-cam-get]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/common/triggerable_camera/blacs_workers.py#L176-L183
[d-cam-send]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/common/triggerable_camera/blacs_workers.py#L218-L225
[d-cam-thread]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/common/triggerable_camera/blacs_workers.py#L242-L251
[d-cam-recv]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/common/triggerable_camera/blacs_tabs.py#L50-L55
[d-pylon-tab]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/PylonCamera/blacs_tabs.py#L16-L19
[d-pylon-imp]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/PylonCamera/blacs_workers.py#L28-L34
[d-imaqdx]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/IMAQdxCamera/blacs_workers.py#L139-L140
[d-depr]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/common/deprecation.py#L35-L45
[d-deps]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/pyproject.toml#L35-L50
[d-user]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/docs/source/user_devices.rst?plain=1#L8-L12
[d-unique]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/docs/source/user_devices.rst?plain=1#L23-L25

[u-doc]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L32-L35
[u-imp]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L62-L70
[u-dirs]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L73-L85
[u-classreg]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L102-L116
[u-get]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L218-L224
[u-reg]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L236-L256
[u-dup]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L248-L253
[u-pop]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L259-L276
[u-noimp]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L271-L276
[u-conn]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/connections.py#L165-L175
[u-zlock]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/ls_zprocess.py#L82-L86
[u-secret]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/ls_zprocess.py#L46-L70
[u-secret-check]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/ls_zprocess.py#L108-L113
[u-h5-order]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/h5_lock.py#L20-L35
[u-h5-patch]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/h5_lock.py#L98-L107
[u-settings]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/settings.py#L58-L59
[u-log]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/setup_logging.py#L57
[u-pth]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript-suite.pth#L1
[u-prof]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_profile/__init__.py#L49-L71
[u-prof-light]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_profile/__init__.py#L49-L54
[u-ini]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_profile/default_profile/labconfig/example.ini#L1-L72
[u-deps]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/pyproject.toml#L38-L48
