# npe2 and VS Code

npe2 ("napari plugin engine, version 2") is how [napari](https://napari.org),
an image viewer for scientific Python, finds, checks and loads plug-ins that
other people write. The napari project wrote it; Talley Lambert and Nathan
Clack are the listed authors. Every napari install runs it, yet its package
metadata still says "Development Status :: 3 - Alpha"
([`pyproject.toml:22`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/pyproject.toml#L22)).
It is about 7,500 lines of Python. We read its code at commit
[`5bb54e9`](https://github.com/napari/npe2/tree/5bb54e9f8c5a13604f0a9f58802933da7340c375)
(23 November 2026), in November 2026. npe2 copies the VS Code extension model
on purpose, and its source says so
([`schema.py:96`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L96)),
so this note covers that model too. **VS Code was studied from its
documentation, not from its code.**

This is one of the studies behind [the design](../design.md). The decisions
live there; this page gives the evidence for them.

| Word | Meaning |
|---|---|
| **Plug-in, extension** | Code from someone else that a program loads to gain features. napari says plug-in; VS Code and insthub say extension. |
| **Manifest** | A small data file that says what an extension adds, read without running it. npe2's is `napari.yaml`, VS Code's is `package.json`, insthub's will be `insthub-extension.toml`. |
| **Contribution point** | A kind of thing an extension may add: in napari a file reader, a widget or a menu item; in VS Code a command, a language or a view. |
| **Entry point** | A name an installed Python package registers in its metadata, which other programs can look up without importing the package. |
| **`"module:attr"` string** | A pointer to code, such as `"my_plugin:get_reader"`, kept as text until the code is needed. npe2 calls it a `python_name`. |
| **Activation** | The moment an extension's code is first imported and its setup function runs. |

## How npe2 is built

| Area | What npe2 does |
|---|---|
| Process model | A library inside napari's own program. Plug-ins run there with no isolation, and one process-wide manager holds them all ([`_plugin_manager.py:232-237`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L232-L237)). |
| Transport | None: nothing crosses a process. Manifests are YAML, JSON or TOML, or a `[tool.napari]` table in `pyproject.toml` ([`_bases.py:96-144`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/_bases.py#L96-L144)). Duplicate keys are refused while parsing ([`_bases.py:10-54`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/_bases.py#L10-L54)), and pydantic models check the data ([`schema.py:93`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L93)). |
| Discovery | Installed packages only. npe2 walks every installed distribution for the entry point group `napari.manifest` ([`schema.py:39`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L39), [`schema.py:364-385`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L364-L385)), whose value is `package:napari.yaml` ([sample](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/tests/sample/my_plugin-1.2.3.dist-info/entry_points.txt#L1-L2)). It locates the file with `find_spec`, so for a top-level package no plug-in code runs ([`schema.py:387-425`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L387-L425)). |
| Naming | A manifest's `name` must equal its package's name ([`schema.py:416-420`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L416-L420)), and every command id must start with `<name>.` ([`schema.py:274-290`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L274-L290)). |
| Concurrency | None. Synchronous, single-threaded, no locks. Changes are announced with psygnal signals ([`_plugin_manager.py:180-202`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L180-L202), [`_command_registry.py:50-51`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_command_registry.py#L50-L51)). |
| Live data | None. A viewer's plug-in engine has no streams. |
| Device abstraction | None. "Acquisition" (driving cameras and microscopes) is only a category tag ([`schema.py:46-47`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L46-L47)). |
| Extension mechanism | Four steps. `register()` only indexes the manifest ([`_plugin_manager.py:293-339`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L293-L339)). Each command's code is kept as a string ([`_command_registry.py:87-92`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_command_registry.py#L87-L92)). The first `get()` of a command activates its plug-in ([`_command_registry.py:126-139`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_command_registry.py#L126-L139)). `resolve()` imports the string only then ([`_command_registry.py:20-39`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_command_registry.py#L20-L39)). |
| UI | A widget contribution names a command that returns a Qt or magicgui widget. With `autogenerate`, napari builds the widget from the function's signature ([`_widgets.py:39-43`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/contributions/_widgets.py#L39-L43), [`_widgets.py:58-71`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/contributions/_widgets.py#L58-L71)). npe2 itself never imports Qt: those types appear only for type checkers ([`types.py:13-16`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/types.py#L13-L16)). |
| Configuration | Plug-ins declare settings in a subset of JSON Schema ([`_contributions.py:50-61`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/contributions/_contributions.py#L50-L61)). Saving them, and the list of disabled plug-ins, is napari's job ([`_plugin_manager.py:210-213`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L210-L213), [`:451`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L451)). |
| Errors | One broken plug-in never stops discovery: it comes back as a result carrying its error, plus a log line ([`schema.py:364-385`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L364-L385)). A failed activation is re-raised with the plug-in's name ([`_plugin_manager.py:386-391`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L386-L391)). A manifest written for a newer npe2 only warns ([`schema.py:259-272`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L259-L272)). |
| Observability | Python logging, the signals above, `npe2 parse` and `npe2 list` ([`cli.py:168-169`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/cli.py#L168-L169), [`cli.py:236-237`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/cli.py#L236-L237)), and a dump of the manager's state ([`_plugin_manager.py:498-503`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L498-L503)). |
| Security | None: plug-ins are trusted code in the same program. `npe2 validate --imports` warns that it "runs python code!"; plain `validate` only parses ([`cli.py:125-129`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/cli.py#L125-L129)). |
| Footprint | Nine required packages, among them pydantic, PyYAML, psygnal, typer, rich and build ([`pyproject.toml:33-43`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/pyproject.toml#L33-L43)). typer, rich and build serve only the command line and fetching from PyPI ([`cli.py:8`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/cli.py#L8), [`_fetch.py:117`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_inspection/_fetch.py#L117)), yet everyone installs them. `import npe2` loads the fetcher, the manager and the pydantic models at once ([`__init__.py:11-15`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/__init__.py#L11-L15)). |

### VS Code, from its documentation

| Area | What VS Code does |
|---|---|
| Process model | Extensions run in an *extension host*, kept apart from the editor's UI so they cannot slow it down or change it. There are local, web and remote hosts ([extension host](https://code.visualstudio.com/api/advanced-topics/extension-host)). |
| Placement | `extensionKind` says where an extension runs: `ui` on the user's machine, `workspace` on the machine that has the files, which may be remote ([remote extensions](https://code.visualstudio.com/api/advanced-topics/remote-extensions)). |
| Manifest | `package.json`. Its `contributes` section declares what the extension adds before any of its code runs. An extension's id is always `publisher.name` ([extension manifest](https://code.visualstudio.com/api/references/extension-manifest)). |
| Activation | `activationEvents` lists the events that wake an extension. Since VS Code 1.74, a contributed command needs no matching event ([activation events](https://code.visualstudio.com/api/references/activation-events)). |
| Version contract | `engines.vscode` is the range of VS Code versions an extension works with ([extension manifest](https://code.visualstudio.com/api/references/extension-manifest)). |
| Separate programs | A language server is a separate program, written in any language, that talks to the editor over a protocol ([language server guide](https://code.visualstudio.com/api/language-extensions/language-server-extension-guide)). |

## What insthub takes

One agent read npe2 and proposed lessons. A second agent (*the check*) tested
every claim against npe2's code and insthub's and argued against every
proposal. Only what survived is listed, in its narrowed form. *Narrowed*
means the check changed the proposal; the recommendation is the check's.
The status follows [the plan](../design.md#the-plan-in-order) at the time of
writing.

| Lesson | Verdict | Status | Decided in |
|---|---|---|---|
| A manifest is data; code is imported late | narrowed, adopt | step 7 | [The manifest](../design.md#the-manifest), [When code is loaded](../design.md#when-code-is-loaded) |
| Two halves in two programs, no extension host | narrowed, adopt | step 8 | [Two halves](../design.md#two-halves-each-where-its-libraries-are), [Writing the UI half](../design.md#writing-the-ui-half) |
| Find extensions in folders; entry points later | narrowed, adapt | step 7 | [Where insthub looks](../design.md#where-insthub-looks-for-extensions), [Ideas we decided against](../design.md#ideas-we-decided-against) |
| A device names its extension | narrowed, adapt | done (step 4) | [How a device finds its extension](../design.md#how-a-device-finds-its-extension) |
| Namespaced ids, used when a server file is written | narrowed, adapt | step 7 | [The manifest](../design.md#the-manifest), [Where insthub looks](../design.md#where-insthub-looks-for-extensions) |
| An integer contract level, checks that run no code, errors kept per extension | narrowed, adopt | step 7 | [Versions](../design.md#versions), [Broken extensions](../design.md#installing-updating-and-broken-extensions) |
| An off-switch for discovery in tests | narrowed, adopt | step 7 | not spelled out in design.md |
| Unique module names | found by the check, adopt | done (step 2) | [Rules inside an extension](../design.md#rules-inside-an-extension), rule 4 |
| Guard the core's imports | found by the check, adopt | done (step 1) | [Keeping the core light](../design.md#keeping-the-core-light) |
| Disable as a filter at the next start | kept, consider later | later | not in design.md yet |

### A manifest is data; code is imported late

npe2's useful core is the four steps of *Extension mechanism* in the first
table, ending in a plain `import_module` and `getattr`
([`utils.py:246-257`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/utils.py#L246-L257)).

insthub already keeps drivers as `"module:Class"` strings and imports them
only when a server builds the device (`config.load_object`). What it lacked
was one data file that names a device's driver and its UI together. The
check narrowed the proposal in three ways, all now in design.md:

- The reason is the split between programs, not startup speed. The window
  runs in insthub's own `uv tool` environment, which has no drivers, so it
  must read extension facts as data and never import them.
- No separate kind for simulators. The HP 8562A picks its simulator with
  `sim=True` on the same class, which a manifest writes as `sim = true` in a
  `[devices.NAME]` table.
- No activation step and no extra layer. In npe2 a widget names a command id,
  which names a `python_name`. An insthub manifest entry points straight at
  its code, or at a command line.

### Two halves in two programs, no extension host

npe2 runs every plug-in inside napari. VS Code runs extensions in a host
apart from the UI, and `extensionKind` decides which machine each runs on.
insthub takes `extensionKind`, not the host: a server half and a UI half,
each loaded in a program that has its libraries, with the bus between them.
Device servers already keep hardware code out of the window.

The check dropped a proposed `insthub open --app` helper that would call a
Python function with `(address, device)`. The lab's one real device GUI,
`hp8562a_gui`, already takes `--name` and finds its own server; a new entry
signature would force a rewrite. So an app is a command line run with the
extension's Python, with `{device}` and `{address}` filled in (`[ui.NAME] run
= [...]`). VS Code starts a language server the same way: as a separate
program, in whatever runtime it needs.

### Find extensions in folders; entry points later

npe2 finds manifests only through installed packages
([`schema.py:364-365`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L364-L365)),
which means only in the environment the program itself runs in. The check
measured the cost as small (5 to 8 ms to import `importlib.metadata`, 1 to
4 ms to scan 24 packages) but found the real problem: the window runs from
the driverless `uv tool` environment, so an `insthub.extensions` entry point
group would be empty exactly where discovery matters. The lab's instrument
projects are also plain folders, not packages.

So insthub looks in folders: the ones you add, and the ones its services and
running servers already point at. A folder also tells insthub which Python
can run the extension's code (a `.venv` in it or above it). Entry points come
later, for extensions installed into the environment that uses them.

### A device names its extension

npe2 matches in two stages. Readers declare filename patterns, which are
checked without importing anything
([`_plugin_manager.py:105-140`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L105-L140)).
The imported reader may then still refuse a file by returning `None`
([`io_utils.py:182-189`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/io_utils.py#L182-L189)).
The first reading proposed the same for insthub: match by type name, then
check that the device has the required commands.

insthub kept the static stage and made it exact. This part is built: a
device's `describe()` carries an optional `extension` name, set by a class
attribute or a server-file key, which the window will compare with a
manifest's `id`. Matching by commands, as `hp8562a_gui` does today with
`REQUIRED_COMMANDS`, was dropped because it breaks when a command is
renamed. The class name cannot do the job either: for a `Wrapped` device it
differs between instrument and simulator, and the module is `__main__` when
a project runs `python bus_server.py`.

The check deferred panels inside the window after confirming four obstacles:
the window's environment has no drivers; `insthub.qt` accepts four Qt
bindings, so a PySide6 panel cannot live in a PyQt6 window; the one real
device GUI sets application-wide styles; and folder modules collide. So a
panel runs in a window of its own ([open question
3](../design.md#open-questions) is about tabs).

### Namespaced ids, used when a server file is written

npe2 ties every contribution to its owner. Command ids must start with the
plug-in's name ([`schema.py:274-290`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L274-L290)),
each contribution records its plug-in
([`schema.py:219-225`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L219-L225)),
and the source cites VS Code's `publisher.name` ids. insthub takes the idea of
a name that says where something came from: extension ids follow
`registry.check_name`, and menus and errors name the extension.

The check rejected letting `insthub serve` look drivers up by extension name.
A service often starts at login; it would then depend on one user's list of
folders and break when a folder is renamed. So the catalogue is used when a
server file is *written*: the New server dialog offers the manifests' devices
and writes an ordinary, self-contained `driver = "module:Class"` with `path`,
plus an `extension` label. `insthub serve` never looks for extensions.

### An integer contract level, checks that run no code, errors kept per extension

npe2 versions its "contract" with a semver `schema_version`
([`schema.py:147-164`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L147-L164)),
compared with a vendored semver class
([`utils.py:99-106`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/utils.py#L99-L106)),
and a manifest newer than npe2 only warns. `npe2 validate` parses without
running code, and `--imports` also imports every pointer
([`cli.py:122-155`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/cli.py#L122-L155),
[`schema.py:474-508`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L474-L508)).
Discovery keeps each failure as data instead of raising.

insthub takes all three, more strictly. `api = 1` is a plain integer, so no
version parser is needed. A manifest with a higher `api` is refused, not
warned about, because these entries end up driving hardware. One command,
`insthub check PATH [--import]`, checks a server file or an extension folder;
the check proposed running `--import` with the extension's own Python, as
`service install` already does for server files. A broken manifest is listed
with its error and never hides the others.

### An off-switch for discovery in tests

npe2 keeps tests away from installed plug-ins with a test subclass of its
manager that refuses to discover
([`_pytest_plugin.py:14-23`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_pytest_plugin.py#L14-L23)).
The check proposed building the switch in instead: an `INSTHUB_EXTENSIONS`
variable (`none`, or a list of folders), set by the test fixture that already
sets `INSTHUB_DIR`. It dropped a proposed `register()` for adding an
extension from code, because nothing needs it. design.md does not spell this
out; it belongs to step 7 of the plan.

### Unique module names (found by the check)

npe2 never meets two plug-ins with the same module name, because packaging
rules it out: an entry point names a unique top-level package. insthub's
folder extensions have no such guarantee, and Python keeps only the first
module of a name. The check reproduced the failure with two dummy folders:
after `bus_server:SpectrumAnalyzer` loaded from one, `bus_server:Synth` from
the other raised `AttributeError`. The lab's `hp_8562a_gpib` and
`windfreak_sythHDprov2` both have a `bus_server.py`.

insthub's answer is rule 4 of [rules inside an
extension](../design.md#rules-inside-an-extension), and a loader that now
reports the collision, naming both files (plan step 2, done). The loader sees
only the module a table's `driver` names, not the modules that one imports in
turn; for those, only the rule helps.

### Guard the core's imports (found by the check)

`import npe2` loads its PyPI fetcher, which imports `urllib`, `zipfile` and
`subprocess` at the top
([`_fetch.py:1-19`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_inspection/_fetch.py#L1-L19)),
whether or not the caller fetches anything. The check found the same mistake
in insthub: every `import insthub` loaded `asyncio`, which only `await` paths
use. That is fixed: `asyncio` is now imported where it is awaited, and
`tests/test_footprint.py` keeps the list of what `import insthub` may load
([keeping the core light](../design.md#keeping-the-core-light), plan step 1).

### Disable as a filter at the next start (later)

npe2's `disable()` removes a plug-in's contributions from the index and
`enable()` puts them back
([`_plugin_manager.py:429-466`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L429-L466)).
When insthub needs a mute switch (say, for an extension found through a
service you never listed), it will be a `disabled = [...]` list in
`~/.config/insthub/extensions.toml`, applied when a program next starts.

## What insthub leaves, and why

| What npe2 or VS Code does | Evidence | Why insthub leaves it |
|---|---|---|
| Activation callbacks, a context object per plug-in, and clean-up functions run on deactivation, modelled on VS Code's `activate(context)` | [`schema.py:171-189`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L171-L189), [`_plugin_manager.py:361-396`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L361-L396), [`:713-746`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L713-L746); the code's own `# FIXME: who should control activation?` ([`_command_registry.py:128`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_command_registry.py#L128)) | insthub's contributions are constructors and command lines, not registrations into a long-running editor. "Activating" means importing or starting it when first used ([decided against](../design.md#ideas-we-decided-against)). |
| Unloading a running plug-in | `deactivate()` leaves contributions indexed, and imported modules stay loaded ([`_plugin_manager.py:404-427`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L404-L427)) | Python cannot really unload a module. A change takes effect when a program next starts. |
| An extension host program | VS Code documentation | Device servers already keep hardware code out of the window. |
| A hidden global manager, reached from deep inside | [`_command_registry.py:130-136`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_command_registry.py#L130-L136), [`utils.py:59-67`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/utils.py#L59-L67), [`_plugin_manager.py:723`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L723) | Discovery is a plain function that returns data, which callers pass along. Tests need no patching. |
| Importing every old-style (npe1, pluggy-like) plug-in to learn what it adds, then caching the result | [`_npe1_adapter.py:85-122`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/_npe1_adapter.py#L85-L122) | Listing extensions would import vendor libraries, which fail in the driverless window. insthub has no hooks that every extension answers. |
| Manifests compiled from decorators, and a merge step | `npe2 compile` ([`_compile.py:21`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_inspection/_compile.py#L21)); "TODO: refactor this ugly thing" ([`utils.py:305`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/utils.py#L305)) | `describe()` already reports commands, properties and streams at run time. A copy in the manifest would drift. |
| Heavy dependencies and an `__init__` that loads every part | [`pyproject.toml:33-43`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/pyproject.toml#L33-L43), [`__init__.py:11-15`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/__init__.py#L11-L15) | Everything imports insthub ([rule 3](../design.md#the-three-rules)). `tomllib` and hand-written key checks replace pydantic, and `import insthub` never loads `insthub.extensions` ([keeping the core light](../design.md#keeping-the-core-light)). |
| Identity tied to pip packaging | [`schema.py:416-420`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L416-L420) | The lab's projects are folders. |
| Old plug-ins supported by code that runs on attribute access | [`_npe1_adapter.py:85-88`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/_npe1_adapter.py#L85-L88) | Version the contract from the first day (`api = 1`) and refuse what does not fit. |
| A list of napari categories inside the core | [`schema.py:43-84`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/manifest/schema.py#L43-L84) | [Rule 1](../design.md#the-three-rules): insthub knows no instrument. Tags, if ever needed, are free text in the manifest. |
| Parsing and validating every manifest at every start | [`_plugin_manager.py:245-284`](https://github.com/napari/npe2/blob/5bb54e9f8c5a13604f0a9f58802933da7340c375/src/npe2/_plugin_manager.py#L245-L284) | insthub's manifests will be small TOML files, read only by programs that need them, so no cache is needed. |

## Checked and corrected

- **There was no `insthub check` command.** The first reading said
  `insthub check` imports drivers without creating devices. `config.check()`
  exists, but the command line does not offer it: only `service install`
  reaches it, through `service.check_python`, which runs it in the service's
  Python. design.md plans `insthub check PATH [--import]` (plan step 7).
- **Line numbers.** The two readings disagree on a few lines. At the studied
  commit, the "refactor this ugly thing" comment is on line 305 of
  `manifest/utils.py`, and a reader's refusal by returning `None` is on lines
  182-189 of `io_utils.py`. This note cites those.
