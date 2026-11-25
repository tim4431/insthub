# Extension models

Every lab-control project puts instrument-specific code somewhere: the driver
that talks to the instrument, and often a window for it. This page compares
how eleven projects package that code, how the *host* (the program that loads
it: a GUI, an editor or a server) finds it, when the code is imported and
where the window runs. It ends with what that means for insthub's
[extensions](../design.md#extensions).

It is for people and AI agents who change insthub. The decisions live in [the
design](../design.md), and each project's note has the details. We read the
code in November 2026, at the commits those notes name, and ran nothing from
it. VS Code was studied from its documentation only. *The study* is one
agent's first reading, *the check* a second agent's verification of it, and
this page follows the check.

| Word | Meaning |
|---|---|
| **Environment** | One Python installation and the packages in it, such as a project's `.venv`. A program can import only what its environment holds. |
| **Manifest** | A small data file that says what an extension holds. It is read, never run. |
| **Entry point** | A name an installed package registers in its metadata. Programs in the *same environment* can look it up without importing the package. |
| **`"module:attr"` string** | A pointer to code, such as `"pkg.ui:make"`, kept as text until the code is needed. |

## The comparison

| Project | Unit of packaging | How it is found | When code is imported | Where the UI runs | What breaks |
|---|---|---|---|---|---|
| [nspyre](nspyre.md) | the lab: one package with its drivers, widgets and experiments | it is not: the lab's `app.py` lists each widget by module and class name ([app.py](https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/gui/app.py#L40-L59)) | the lab's `app.py` imports every widget module at start ([imports](https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/gui/app.py#L8-L21)); a widget is built when someone clicks **Load** ([main.py](https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/widgets/main.py#L212-L236)) | docks in one GUI program | `import nspyre` imports every widget, and fails where pyqtgraph is missing ([`__init__.py`][ns-init], [`gui/__init__.py`](https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/gui/__init__.py#L6-L43)) |
| [PyLabNet](pylabnet.md) | none: one device is spread over four to six folders of a 58,000-line repository, tied by a type name | by convention: the module `servers.<name>` ([pylabnet_server.py](https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/pylabnet_server.py#L104)) | when a launcher starts that server | separate scripts, started like servers with Launch Control's own Python ([servers][pl-exe], [scripts][pl-exe2]) | one vendor library holds everyone below Python 3.9 ([setup.py][pl-py]); a driver's logger imports Slack ([logger.py][pl-log]) |
| [instrumentserver](instrumentserver.md) | none: a driver is any QCoDeS class; its UI is a second class path in the station file, a generic widget by default ([config.py](https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/config.py#L18)) | the station file only | the widget, when its tab opens, by three copies of one loader ([1](https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/application.py#L885-L920), [2](https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/application.py#L996-L1023), [3][is-load3]) | the GUI program, which may also host the server | it works only because GUI and drivers share one environment: PyQt5 and QCoDeS are required ([pyproject.toml](https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/pyproject.toml#L16-L27)), and `import instrumentserver` loads Qt ([`__init__.py`](https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/__init__.py#L4-L6)) |
| [PyroLab](pyrolab.md) | drivers inside the framework's own package, with a pip extra per family ([pyproject.toml](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyproject.toml#L79-L87)) | `module` and `classname` in YAML ([configure.py](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L657-L661)) | when the service starts | none packaged; a camera's client helper sits in the driver's module ([thorcam.py](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/cameras/thorcam.py#L398)), which imports OpenCV ([line 40](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/cameras/thorcam.py#L40)) | scipy is required for one constant ([tsl550.py](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/lasers/tsl550.py#L29)) |
| [LabRAD](labrad.md) | a server: a standalone program in any language, with a text block that describes it | the node (a launcher on each computer) walks folders and reads `.ini` files and embedded blocks as text ([node](https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L613-L640), [find_config_block](https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/server_config.py#L77-L94)) | never by the node: it runs the server's command line | separate client programs, none tied to a server | `%PYTHON%` is the node's own interpreter ([node][lr-python]), so a server in another environment must name its Python in its command line |
| [Galois Edge](galois-edge.md) | YAML instrument profiles; code only inside its own package | profiles from layered folders with written precedence ([profile_loader.py](https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/profile_loader.py#L169-L181)); protocol drivers from a fixed list ([registry.py](https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/drivers/registry.py#L61-L65)) | a vendor library at connect time, with a `pip install` hint ([sdk_executor.py][ge-sdk]) | no device UI on the computer; a browser app in its cloud | adding code means forking the package; one frozen environment holds everything |
| [labscript](labscript.md) | a device folder in an installed package; its `register_classes.py` names the parts by import strings ([Windfreak](https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/Windfreak/register_classes.py#L16-L20)) | packages named in a per-computer config file ([device dirs](https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L73-L85)); a `.pth` file adds the lab's folders to the path at every Python start ([.pth](https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript-suite.pth#L1), [profile](https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_profile/__init__.py#L49-L71)); every `register_classes.py` is run ([populate_registry](https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L259-L276)) | the tab when BLACS builds it; the worker only in its child program ([create_worker][ls-cw]); the vendor library in the worker's `init()` ([worker][ls-sdk]) | every tab inside BLACS; a tab that fails is skipped ([`__main__.py`][ls-tabs]) | one package for all 36 devices requires BLACS and several vendor libraries ([pyproject.toml](https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/pyproject.toml#L35-L50)) |
| [ARTIQ](artiq.md) | an NDSP (network device support package): driver, controller, optional client and mediator, and no UI part ([manual](https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/doc/manual/developing_a_ndsp.rst#L18-L23)) | controllers from the device database; dashboard plug-ins from `-p module` ([dashboard](https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/frontend/artiq_dashboard.py#L133-L135)) | drivers in their controller; plug-ins into the dashboard | applets: separate programs started with the dashboard's own Python ([applets.py][aq-python]) | embedded applet windows fail on Wayland, so the dashboard forces X11 ([dashboard](https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/frontend/artiq_dashboard.py#L144-L148)); applets show datasets, not devices |
| [qudi](qudi.md) | pip add-ons that install modules into one shared `qudi` package | config paths under `qudi.`; a `sitecustomize.py`, run at every Python start, reorders the package's folders so one add-on wins ([sitecustomize.py](https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/sitecustomize.py#L14-L21)) | on activation, but listing modules imports all of them ([module_finder.py](https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/util/module_finder.py#L102-L105)) | GUI modules in the one program; opening one activates its logic and hardware ([modulemanager.py](https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/modulemanager.py#L555-L561)) | the base class imports PySide6, so every driver loads Qt ([module.py][qu-qt]); the add-on requires PySide6 and the NI and Zaber libraries ([pyproject.toml](https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/pyproject.toml#L59-L73)) |
| [ScopeFoundry](scopefoundry.md) | one repository per instrument (`HW_*`), written from a scaffold ([new_hw.py](https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/tools/features/new_hw.py#L22-L69)), with no packaging metadata | an explicit import in the app's `setup()` ([test app](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/powermeter_test_app.py#L13-L15)) | at app start; the folder's `__init__.py` also imports the Qt measurement ([`__init__.py`](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/__init__.py#L2-L3)) | one app program | the "pure" driver imports a helper ([thorlabs_pm100d.py](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/thorlabs_pm100d.py#L6)) that imports pyqtgraph and Qt ([helper_funcs.py][sf-helper]) |
| [npe2](npe2-and-vscode.md) | an installed package with a `napari.yaml` manifest | entry points of the environment it runs in ([schema.py][np-disc]); `find_spec` locates the manifest, so for a top-level package no plug-in code runs ([schema.py](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L387-L425)) | a command's `"module:attr"` string, on first use ([_command_registry.py][np-get]) | widgets inside napari; npe2 itself never imports Qt ([types.py](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/types.py#L13-L16)) | it sees only its own environment, and plug-ins share the host's one process-wide manager, with no isolation ([_plugin_manager.py](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L232-L237)) |
| [VS Code](npe2-and-vscode.md#vs-code-from-its-documentation) | an extension with a `package.json` manifest | the manifest says what the extension adds before any of its code runs ([manifest](https://code.visualstudio.com/api/references/extension-manifest)) | on activation, in an extension host kept apart from the editor's UI ([extension host](https://code.visualstudio.com/api/advanced-topics/extension-host)) | `extensionKind` puts each extension on the UI side or the workspace side ([remote extensions](https://code.visualstudio.com/api/advanced-topics/remote-extensions)); a language server is a program of its own ([language servers](https://code.visualstudio.com/api/language-extensions/language-server-extension-guide)) | studied from documentation, not checked in code |

## Three answers to "where does the UI run?"

| Answer | Projects | What it means |
|---|---|---|
| **In the host, in one environment** | nspyre, instrumentserver, labscript's BLACS, qudi, ScopeFoundry; npe2 for napari | Driver and UI share a package or folder, and the UI loads into one GUI program. Simple, and a UI can call its driver directly, but everything must be installed together (next section). |
| **Never joined** | LabRAD, ARTIQ, Galois Edge, PyroLab, PyLabNet | A UI is a separate client or does not exist, and nothing tells a host which UI belongs to which device. ARTIQ's driver packages have no UI part ([manual](https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/doc/manual/developing_a_ndsp.rst#L18-L23)). |
| **Two halves, joined by a protocol** | VS Code | `extensionKind` runs the part that needs the files where the files are, and the part that draws where the user is ([remote extensions](https://code.visualstudio.com/api/advanced-topics/remote-extensions)). A language server is a program of its own, in any runtime, behind a protocol ([language servers](https://code.visualstudio.com/api/language-extensions/language-server-extension-guide)). insthub takes this model, with the bus as the protocol. |

## The deciding fact: one environment

Joining a driver and its UI in one package forces one environment, in three
steps:

1. A host that imports a device's UI needs the UI's libraries: Qt, plotting,
   whatever the UI uses.
2. The UI brings its driver along, through a shared package, a base class or
   a plain import. So the host needs the driver's libraries too, vendor
   libraries included.
3. So the host, every UI and every driver must be installed into one
   environment, and any one of them can hold back all the others.

| Project | What its one environment must hold | Evidence |
|---|---|---|
| instrumentserver | the GUI, which imports each widget class, with PyQt5 and QCoDeS as hard dependencies | [loader](https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/application.py#L885-L920), [pyproject.toml](https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/pyproject.toml#L16-L27) |
| qudi | every add-on, under one package name, all on PySide6 | [sitecustomize.py](https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/sitecustomize.py#L14-L21), [module.py][qu-qt] |
| labscript | BLACS and the vendor libraries of all its devices | [pyproject.toml](https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/pyproject.toml#L35-L50) |
| ScopeFoundry | the app, its `HW_*` folders, and Qt, which even the driver imports | [test app](https://github.com/ScopeFoundry/HW_thorlabs_powermeter/blob/d4f43bdf02f9d44d56a37c9ccd7c06c4589ec257/powermeter_test_app.py#L13-L15), [helper_funcs.py][sf-helper] |
| nspyre | the lab's package of drivers, widgets and experiments, with PyQt6 | [app.py](https://github.com/nspyre-org/template/blob/f9d429619c0de191bb3d6c7535384792bd54e9a4/src/template/gui/app.py#L8-L21), [`__init__.py`][ns-init] |
| npe2 | napari and every plug-in; discovery sees no other environment | [schema.py][np-disc] |

Separate programs alone do not escape it. ARTIQ starts applets as programs of
their own, but with the dashboard's Python ([applets.py][aq-python]).
PyLabNet starts local servers and scripts with Launch Control's interpreter
([servers][pl-exe], [scripts][pl-exe2]), so one vendor library, zhinst, holds
every user below Python 3.9 ([setup.py][pl-py]). LabRAD's node starts servers
in any language, but its `%PYTHON%` is its own interpreter
([node][lr-python]). Galois Edge freezes one environment that holds
everything. What matters is not which program runs the UI, but which
environment it runs in.

insthub cannot have one environment. Each instrument's project has its own
`.venv`, with its vendor library and `-e ../insthub`. The `insthub` command,
and with it the window (`insthub gui`), runs from a `uv tool` environment
that has no drivers ([the insthub command for
yourself](../running-servers.md#the-insthub-command-for-yourself)). The
projects are folders, not packages. One shared environment would make every
lab install every instrument's libraries, and let any one of them pin the
rest, as zhinst did.

So an insthub extension has two halves, each run where its libraries are. The
server half (driver and device) runs in the server program, in the project's
environment, as today. The UI half (panel or app) runs as a program of its
own, started with the extension's Python. The window only reads manifests and
starts programs, so a crashing vendor UI cannot take it down. The halves meet
over the bus. See [two halves, each where its libraries
are](../design.md#two-halves-each-where-its-libraries-are) and [writing the
UI half](../design.md#writing-the-ui-half).

## Finding extensions

| How | Seen in | Runs extension code to list it? | Catch |
|---|---|---|---|
| The host's own code imports each part | nspyre, ScopeFoundry | yes | nothing is discovered; each app is written by hand |
| A config names a class by import path | instrumentserver, PyroLab, insthub today | no | loads one thing; cannot list what exists |
| Walk folders and run a file in each | labscript ([populate_registry](https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L259-L276)), qudi ([module_finder.py](https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/util/module_finder.py#L102-L105)), Galois Edge's SDK wrappers ([sdk_tools.py](https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/sdk_tools.py#L56-L75)) | yes | slow, and import side effects happen while listing |
| Code Python runs at every start | labscript's `.pth` ([.pth](https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript-suite.pth#L1)), qudi's `sitecustomize.py` ([sitecustomize.py](https://github.com/Ulm-IQO/qudi-iqo-modules/blob/ed5b7940fa956967ea16ff649aa252d65e39ce32/src/sitecustomize.py#L14-L21)) | yes, in every program | hidden effects; import order decides which code wins |
| Text manifests found in folders | LabRAD ([find_config_block](https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/server_config.py#L77-L94)) | no | the node's Python is the default; another one is spelled out by hand |
| Entry points plus a data manifest | npe2 ([schema.py](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L387-L425)) | no | sees only the current environment |
| Layered folders with written precedence | Galois Edge's profiles ([profile_loader.py](https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/profile_loader.py#L169-L181)) | no | the order must be written down, and shadowed copies reported |

insthub combines the rows that run no code. Its manifest,
`insthub-extension.toml`, is read as data and never run, as LabRAD's node
reads its blocks ([the manifest](../design.md#the-manifest)). Folders come
from the device's own folder and from a list you edit; the first manifest
with a given `id` wins and the others are reported as hidden, as in Galois
Edge. Entry points come later, for packaged extensions ([where insthub
looks](../design.md#where-insthub-looks-for-extensions)).

## Loading code late

The projects that stay light name code by a string and import it only in the
program that needs it. labscript names a device's tab and worker by import
strings, so that "imports can be deferred until the classes are actually
needed" ([docstring][ls-doc]). The worker is imported only in its child
program ([create_worker][ls-cw]), and the vendor library only in `init()`
([worker][ls-sdk]). npe2 imports a command the first time something asks for
it ([_command_registry.py][np-get]). Galois Edge imports a vendor library at
connect time, and names the package to install when it is missing
([sdk_executor.py][ge-sdk]). Failures stay local: ARTIQ falls back to its
built-in editor for an unknown argument UI ([experiments.py][aq-argui]),
BLACS skips a tab that fails to build ([`__main__.py`][ls-tabs]), and
instrumentserver's client falls back to its generic widget ([3][is-load3]).

Each project that grew heavy did so through one eager import: nspyre's
package `__init__` ([`__init__.py`][ns-init]), ScopeFoundry's helper module
([helper_funcs.py][sf-helper]), qudi's base class ([module.py][qu-qt]) and
PyLabNet's logger ([logger.py][pl-log]). A rule in a comment does not hold:
ScopeFoundry's template says the driver "SHOULD BE INDEPENDENT OF ANY
SCOPEFOUNDRY FUNCTIONALITY" ([template][sf-tpl]), and its own reference
driver is not. So insthub tests its rules ([when code is
loaded](../design.md#when-code-is-loaded), [keeping the core
light](../design.md#keeping-the-core-light)).

Folders bring one trap: Python keeps one module per name. labscript runs its
registration files outside Python's module table, because "each import would
need to have unique names" ([_device_registry.py][ls-exec]); npe2 avoids the
problem by requiring installed packages. The lab's `hp_8562a_gpib` and
`windfreak_sythHDprov2` both have a top-level `bus_server.py`, so insthub has
[rule 4](../design.md#rules-inside-an-extension) and a clear error from the
loader ([the plan](../design.md#the-plan-in-order), step 2).

## What the check corrected

- The studies of nspyre, instrumentserver, LabRAD, ScopeFoundry and Galois
  Edge proposed importing panels into the window. The check refused: the
  window's environment has no drivers, and the lab's one device GUI,
  `hp8562a_gui`, needs PyQt6 and its project's own modules.
- ARTIQ was first credited with starting UIs in the server's Python. Its
  `${python}` is the dashboard's ([applets.py][aq-python]); starting a UI
  with the extension's Python is insthub's own idea.
- An interpreter is not enough. `hp8562a_gui` imports only from its project
  folder, so a UI starts in its extension's folder, with that folder on the
  module path ([the manifest](../design.md#the-manifest)).
- Entry points as the main way to find extensions were deferred. They list
  only the asking environment's packages ([schema.py][np-disc]), and the
  window's has none. Loading one that names a Python object also runs its
  package's `__init__.py`.
- Matching a UI by class or module name fails. A wrapped device's type
  differs between the instrument and its simulator, and the module is
  `__main__` when a project runs `python bus_server.py`. Hence an explicit
  `extension` id ([how a device finds its
  extension](../design.md#how-a-device-finds-its-extension)).
- PyLabNet was first read as having each process announce its UI. Only
  Launch Control's own logger passes `ui=` ([launch_control.py][pl-ui]), and
  device servers do not ([pylabnet_server.py][pl-srvlog]).

## What this means for insthub

| Finding | insthub's answer |
|---|---|
| Joining driver and UI in one program needs one environment | [two halves](../design.md#two-halves-each-where-its-libraries-are), joined by the bus |
| Manifests read as text list extensions without running code (LabRAD, npe2) | [`insthub-extension.toml`](../design.md#the-manifest) |
| Entry points see only one environment | [folders first, entry points later](../design.md#where-insthub-looks-for-extensions) |
| A UI must find its device without importing the driver | [the device announces its `extension`](../design.md#how-a-device-finds-its-extension), done in step 4 of [the plan](../design.md#the-plan-in-order) |
| LabRAD's node lists servers older than their manifest ([`_get_outdated`](https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L867)) | [`extension_version`](../design.md#versions) |
| A GUI that adds nothing its logic cannot do stays scriptable ([qudi](https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/docs/design_concepts/measurement_modules.md#L47-L49)) | [a panel talks only through a device proxy](../design.md#writing-the-ui-half) |
| Embedded foreign windows break, and panels inside the host share its fate | a panel runs as a program of its own; a tab in the window is an [open question](../design.md#open-questions) |
| One eager import made each framework heavy | [a footprint test and import rules](../design.md#keeping-the-core-light); [the plan](../design.md#the-plan-in-order), steps 1 and 2 |
| Typed settings generate most UIs (ScopeFoundry, labscript's tabs) | [a generic UI good enough for most devices](../design.md#a-generic-ui-good-enough-for-most-devices) |

What insthub leaves behind, and why, is in [ideas we decided
against](../design.md#ideas-we-decided-against). Manifests, finding
extensions and starting UIs are steps 7 and 8 of [the
plan](../design.md#the-plan-in-order).

[ns-init]: https://github.com/nspyre-org/nspyre/blob/f029358819049b6e2e071b659016a82ff07b20b5/src/nspyre/__init__.py#L8-L31
[pl-srvlog]: https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/pylabnet_server.py#L68-L74
[pl-ui]: https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/launchers/launch_control.py#L209-L215
[pl-exe]: https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L827
[pl-exe2]: https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/helper_methods.py#L910-L912
[pl-py]: https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/setup.py#L50
[pl-log]: https://github.com/lukingroup/pylabnet/blob/f2d38c38c2f884820e412f963597ab69213c92fb/pylabnet/utils/logging/logger.py#L12
[is-load3]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/application.py#L190-L230
[lr-python]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L688-L691
[ge-sdk]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/sdk_executor.py#L62-L69
[ls-sdk]: https://github.com/labscript-suite/labscript-devices/blob/ea31611c8ebcccff9445958077f471b7732db669/labscript_devices/Windfreak/blacs_workers.py#L21-L23
[ls-doc]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L30-L35
[ls-exec]: https://github.com/labscript-suite/labscript-utils/blob/795b5d23937f10738695852279b4c168e69f76da/labscript_utils/device_registry/_device_registry.py#L273-L276
[ls-cw]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/tab_base_classes.py#L680-L724
[ls-tabs]: https://github.com/labscript-suite/blacs/blob/e7bfb89d8fab3c491c3d83b2da2397d53046d604/blacs/__main__.py#L306-L323
[aq-python]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/gui/applets.py#L247-L257
[aq-argui]: https://github.com/m-labs/artiq/blob/7d7b85f377e0c172ae3496f0c03cfb6cb85ba022/artiq/dashboard/experiments.py#L600-L609
[qu-qt]: https://github.com/Ulm-IQO/qudi-core/blob/dc69be418843ca9124106aeed3af153e822cec51/src/qudi/core/module.py#L28
[sf-helper]: https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/helper_funcs.py#L14-L15
[sf-tpl]: https://github.com/ScopeFoundry/ScopeFoundry/blob/6375fac0b609c7e9fe11f81f0200bb231fa66586/tools/templates/_serial_dev.py#L8-L9
[np-disc]: https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L364-L385
[np-get]: https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_command_registry.py#L126-L139
