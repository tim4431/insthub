# instrumentserver

[instrumentserver](https://github.com/toolsforexperiments/instrumentserver)
puts the instruments of a QCoDeS *station* inside one server program, and lets
other programs use them over the network through generated proxy objects. It
ships Qt windows for the server and for clients, a data logger, and a large
"Parameter Manager". It belongs to the toolsforexperiments organisation, and
its `pyproject.toml` names Wolfgang Pfaff and Marcos Frenkel as authors and
says version 0.0.1 ([pyproject.toml][pp-authors]). It is actively developed:
its decision records are dated September 2026 ([ADR-0003][adr3]), it has about
16k lines of tests, and some of its docs are still marked "planned, not yet
written" ([custom_widgets.md][doc-widgets]). The source is about 18.8k lines
of Python, 1.9k of them a generated Qt resource file. We studied commit
[`684d7cf`][commit] (10 November 2026) in November 2026, by reading the code;
nothing was run. Its one-line summary is in [what we learned from other
projects](../design.md#what-we-learned-from-other-projects).

In short: of the projects we studied, it is the closest to insthub in purpose.
One program owns the instruments, and everything else asks it. It differs in
almost every mechanism, so it serves both as a model and as a warning. Its
best ideas are a UI named by a string with a generic fallback, polling set in
the config, and proxy methods with real signatures. Its generic window shows
what happens when descriptions carry no units or limits.

## Words used here

| Word | Meaning |
|---|---|
| **QCoDeS** | A Python framework for instrument drivers, common in quantum-device labs. A driver is an `Instrument` with *parameters* and methods. |
| **Parameter** | A QCoDeS value with a getter, a setter, a unit and a *validator*. The nearest thing to an insthub property. |
| **Validator** | A QCoDeS object that checks a parameter's values, for example "a number from 0 to 10". |
| **Submodule, channel** | An object inside an instrument with parameters of its own, such as `dac.ch1`. |
| **Station** | QCoDeS's collection of instrument objects, usually loaded from a YAML file. |
| **Blueprint** | instrumentserver's description of a parameter, a method or a whole instrument, sent as JSON. Like insthub's `describe()`. |
| **Proxy instrument** | The object a client builds from a blueprint. Gets, sets and calls on it go to the server. Like an insthub device proxy. |
| **Broadcast** | A change notice the server sends to every subscriber. |
| **Instrument mutex** | The lock the server holds on one instrument while a request for it runs. |
| **ZMQ** | ZeroMQ, a messaging library with ready-made socket kinds: ROUTER (a server that answers many clients), DEALER (a client), PUB (sends to whoever subscribed). |

## How it is built

| Area | What instrumentserver does |
|---|---|
| Process model | One server program owns the station. The server object runs on a Qt thread ([core.py][c-thread]), usually inside the server's own Qt window ([application.py][a-host]), which talks to it over TCP on localhost ([application.py][a-embedded]). It can also run without a window ([apps.py][apps-headless]). The other windows, the Parameter Manager and the data logger are separate programs: five console scripts in all ([pyproject.toml][pp-scripts]). |
| Transport | ZMQ. Requests go to a ROUTER socket ([core.py][c-router]) from a DEALER socket in each client ([client/core.py][cc-dealer]). Broadcasts go out on a PUB socket on the next port up ([core.py][c-port], [core.py][c-pub]). |
| Encoding | JSON written by hand. Every scalar in a blueprint becomes a string ([blueprints.py][b-todict]), and the receiver guesses numbers and booleans back ([blueprints.py][b-guess]). Arrays travel as lists of strings ([blueprints.py][b-array]). |
| Discovery and naming | None: a client passes host and port. Names are station names. A dotted path such as `dac.ch1.offset` is resolved with plain `getattr`, private names included ([helpers.py][h-getattr]). |
| Concurrency | Each request goes to a thread pool ([core.py][c-pool]). The instrument mutex, keyed on the first part of the path, keeps calls to one instrument apart; station-level calls take no lock ([core.py][c-lock]). No thread owns an instrument. A client sends one request and blocks for its reply ([client/core.py][cc-ask]), 20 s at most by default ([client/core.py][cc-timeout]). |
| Live data | Parameter events only, no data streams. After a set the server broadcasts the requested value; after a get, the value read ([core.py][c-bcast]). A subscriber that joins late misses what came before ([broadcasts.md][doc-quirk]). An instrument can send its own Broadcasts through an opt-in `Broadcaster` mixin ([base.py][base-bcaster]). |
| Device abstraction | QCoDeS instruments. A blueprint describes a parameter ([blueprints.py][b-param]), a method ([blueprints.py][b-method]) or a whole instrument, recursively ([blueprints.py][b-module]). The client builds a proxy from it, with one sub-proxy per submodule ([proxy.py][p-sub]). |
| Extension mechanism | A driver is any importable QCoDeS class, named in the station YAML. Each instrument may name a widget class by dotted path, with keyword arguments; the default is a generic widget ([config.py][cfg-gui]). No manifest, no plug-in discovery. |
| UI | Qt through qtpy, with PyQt5 required. The generic widget is a tree of parameters and a list of methods. It picks each parameter's input from its validator ([parameters.py][p-select]). Every open instrument tab starts its own subscriber thread and socket ([instruments.py][i-sub]). |
| Configuration | One YAML file: the QCoDeS station config plus extra keys (`gui`, `pollingRate`, `gui_defaults`, `networking`). The loader takes the extra keys out ([config.py][cfg-gui]) and writes the rest to a temporary file for QCoDeS ([config.py][cfg-load]). |
| Errors | The server sends an error's type name and message ([blueprints.py][b-err-send]). The client rebuilds a plain `Exception`, so the type is lost ([blueprints.py][b-err-recv]). A timeout resets the client's socket ([client/core.py][cc-reset]). We found no code that reopens a lost instrument. |
| Observability | Every set, get and call is logged ([core.py][c-log]). A separate Listener program writes Broadcasts to CSV ([listener.py][listener-csv]) or InfluxDB ([listener.py][listener-influx]), and Grafana files ship inside the package ([pyproject.toml][pp-data]). Nothing shows an instrument's own status, such as connected or busy. |
| Security | None. Requests are taken on localhost plus configured addresses, but the PUB socket listens on every network interface ([core.py][c-port], [core.py][c-pub]). Any client can make the server import and construct any class ([core.py][c-create]). A received `_class_type` makes the receiver import a class or `eval` a string ([blueprints.py][b-eval]). An init script runs through `exec` ([core.py][c-init]). The code says `TODO: client white list` ([core.py][c-todo]). |
| Footprint | Hard dependencies include PyQt5, pyzmq, QCoDeS, scipy and pandas ([pyproject.toml][pp-deps]). `import instrumentserver` imports Qt ([\_\_init\_\_.py][init-qt]), then reads a JSON schema file and imports the client ([\_\_init\_\_.py][init-schema]), which loads zmq and QCoDeS ([proxy.py][p-imports]). The Parameter Manager alone is about 6.8k lines. |

## What insthub takes

One agent read the project and made nine proposals. A second agent, *the
check*, tested each claim against both codebases, argued against each
proposal, and added three lessons of its own. The check's verdicts are final.
Where [design.md](../design.md) later decided differently, the design wins,
and the text says so.

| Lesson | The check | Where insthub decided it |
|---|---|---|
| A UI named by a string, with a generic fallback | modify, adapt | [two halves](../design.md#two-halves-each-where-its-libraries-are), [how a device finds its extension](../design.md#how-a-device-finds-its-extension), [writing the UI half](../design.md#writing-the-ui-half); [plan](../design.md#the-plan-in-order) steps 4 (done), 7, 8 |
| The UI half runs in the extension's own Python | added, adapt | [two halves](../design.md#two-halves-each-where-its-libraries-are); plan step 8 |
| A manifest that lists what is installed | added, adapt | [the manifest](../design.md#the-manifest), [where insthub looks](../design.md#where-insthub-looks-for-extensions); plan step 7 |
| Units, limits and choices in the description | modify, adapt | [a generic UI good enough for most devices](../design.md#a-generic-ui-good-enough-for-most-devices); plan step 5 (done) |
| Polling declared in the config | modify, adapt | same section, and [existing projects become extensions](../design.md#existing-projects-become-extensions) for the reserved name; plan steps 2 and 5 (done) |
| Proxy methods with a real signature | modify, adopt | named in [what we learned](../design.md#what-we-learned-from-other-projects); no numbered step yet |
| Announce property changes on one stream | modify, adapt | not taken up by design.md |
| Channels as dotted names; a `hidden` list; save and restore; a publish hook for plain drivers | modify or added, consider later | not planned ([below](#later-when-a-need-shows-up)) |

### A UI named by a string, with a generic fallback

**They do.** An instrument's YAML names its widget class; without one it gets
the generic widget ([config.py][cfg-gui]). The detached server window, which
may run on another computer, asks the server which widget belongs to an
instrument ([core.py][c-guiconf]) and imports it there
([application.py][a-detached]). The widget takes either a proxy or the real
driver ([instruments.py][i-generic]); even the server's own window hands it a
proxy ([application.py][a-servertab]).

**It works only because** the windows, the drivers and the widgets share one
Python environment: the server window hosts the server in its own process
([application.py][a-host]), and PyQt5 and QCoDeS are hard dependencies
([pyproject.toml][pp-deps]). insthub's window runs from an environment with
no drivers, and each instrument project has its own. The check made this a
lesson of its own: copying the loader would fail for every real insthub
extension. So insthub takes the idea (a name, a generic fallback, a UI written
against a proxy) but not the loading. An extension's UI runs as a program of
its own, started with the extension's Python.

**Where design.md differs from the check.** The check proposed a
`ui = "module:factory"` string in `describe()`. design.md chose a narrower
signal: a device announces only its extension's name, and only a manifest
found on this computer can cause code to run. Where their server tells a
remote window which module to import, an insthub server never does. The
server's Python, which the check wanted in the server's registry entry, will
go into the server's `.json` file, next to each device's folder.

Their loader is written three times, and each copy passes the widget
different extra arguments ([application.py][a-servertab],
[application.py][a-detached], [client/application.py][cl-tab]). One copy loses
its keyword arguments to a `KeyError` that `except Exception: pass` hides
([application.py][a-detached]). insthub plans one panel contract,
`make(device, parent=None)`, and a log of each started UI, whose last lines
the window shows when the UI exits at once. Their per-instance widget
arguments (`gui.kwargs`) have no counterpart in insthub's manifest yet.

### A manifest, which instrumentserver lacks (added by the check)

instrumentserver declares only console scripts, no plug-in entry points
([pyproject.toml][pp-scripts]). Its "create instrument" panel lists only
instruments preset in the config or added from code
([application.py][a-possible]). So which UI goes with which driver lives only
in each lab's YAML, and no window can show what is installed. The check
proposed entry points, asked of each project's Python. design.md chose a
data-only file per extension, `insthub-extension.toml`, found in folders.
Entry points come later, as one more place to look: they see only the
environment they are read from, and the window's has no drivers.

### Descriptions carry units, limits and choices

**They do.** The generic widget would pick a number box, a text box or a
checkbox from a parameter's validator ([parameters.py][p-select]). But
blueprints carry no validators ([blueprints.py][b-param]), the proxy says
`FIXME: uncomment after implementing serializable validators`
([proxy.py][p-fixme]), and a parameter without a validator counts as "any
value" and gets a plain text field ([params.py][pm-type],
[parameters.py][p-fixme2]). For remote instruments, the usual case, the
generic window never got past a text field. Methods carry only their
signature text and the kind of each parameter ([blueprints.py][b-method]).

**insthub takes** optional metadata from a fixed vocabulary (`unit`, `min`,
`max`, `step`, `choices`), as plain values, never validator objects. It is
advice for the UI, not enforced. The check advised properties only, and
waiting with command parameters until a per-argument command form exists.
design.md went further, because the lab's synthesizer keeps its settings in
methods with arguments; command parameters take the same words. Both are
done.

### Polling declared in the config

**They do.** `pollingRate: {param: seconds}` in the YAML ([config.py][cfg-poll]).
A Qt thread with one timer per parameter reads each through a TCP client
connected to its own server ([pollingWorker.py][poll], [apps.py][apps-poll]).
The read's Broadcast is how the value reaches the windows and the Listener.

**insthub takes** the idea, not the machinery. `poll = {...}` in a
`[devices.x]` table runs as a periodic task on the device's own thread and
publishes on a stream of the same name. Following the check, an entry may
also name a command with no arguments, because the lab's raw drivers report
status through methods. design.md also allows `poll` on the class, which the
check had advised against as a second way to write `@periodic`. A new
reserved key takes its name away from driver constructors: a table that sets
`poll` for a driver whose constructor also takes `poll` is an error until
`kwargs` says what the constructor gets.

### Proxy methods with a real signature

**They do.** Each proxy method gets a `__signature__` whose `str()` is the
server's signature text, annotations and defaults included
([proxy.py][p-sig]). The method itself is generated with `exec`, from
parameter names that default to a sentinel, so only the arguments the caller
passed are sent ([proxy.py][p-names], [proxy.py][p-exec]).

**insthub takes** the signature without `exec`. `RemoteMethod` would parse
the signature text with `ast`, which runs nothing, build `inspect.Parameter`s,
and keep the server's text for `str()`. Completion and `help()` in notebooks
then show real argument names. If parsing fails, it sets nothing. Arguments
are forwarded as today, with no local check: the server already rejects a
wrong call quickly, and a second check could refuse a valid one.

### Not decided yet: announcing property changes

**They do.** After a client sets or gets a parameter, the server broadcasts
the value with the instrument's name as topic ([core.py][c-bcast],
[core.py][c-send]). Windows update their rows without polling. There are
three faults. The bulk-set path calls `param.set` directly, with no notice and
no lock ([core.py][c-setparams], [serialize.py][s-from]). Creating a
parameter was detected by the method's name until ADR-0003 replaced it
([core.py][c-byname], [ADR-0003][adr3]). And a set announces the requested
value, not the one the instrument applied.

**The check's form, if insthub takes it.** In `_b_get` and `_b_set` in
`server.py`, the one place every get and set passes, record the value. Keep
only small plain values, and publish the dict of last-known values on one
reserved stream (`$props`). Its retained last frame gives a late subscriber
every known value, and agents and panels could read values without touching a
slow GPIB instrument. One helper replaces the five places that treat the
`state` stream specially. Changes made inside commands still show only
through `set_state` or `publish`. design.md has not taken this up.

### Later, when a need shows up

| Idea | Their evidence | Why it waits |
|---|---|---|
| Channels as dotted names (`ch1.offset`), flat on the wire | recursive blueprints ([blueprints.py][b-module]), one lock for the root ([core.py][c-lock]) | No lab driver uses channel objects; they take the channel as an argument. `Wrapped` builds its tables from the driver class before the driver exists, so each channel's class would have to be named. |
| A `hidden` glob list, folded under "Advanced" | `gui_defaults`, merged default, then class, then instance ([config.py][cfg-defaults]) | The generic page now builds real controls (plan step 5), so it waits only for someone to ask. Skip pinning and a global default layer. |
| Save and restore settings, client-side, over normal `$set` | `paramsToFile` and `paramsFromFile` ([proxy.py][p-params]), applied in sorted key order ([serialize.py][s-sorted]) | A few lines in any script today. Save settable properties only, in the file's order; devices with ordering rules keep a `configure(**kw)` command. Plan step 10's `insthub.states()` collects each device's `state` for saving next to a data file, which is another thing. |
| One hook name (`insthub_attach`) so a plain wrapped driver can publish without importing insthub (added by the check) | the server registers itself when an instrument has `add_broadcast_sink` ([core.py][c-register], [base.py][base-bcaster]) | The lab writes thin `Wrapped` subclasses without complaint. Revisit if adapter files multiply. |

## What insthub leaves, and why

| What they do | Evidence | Why not in insthub |
|---|---|---|
| ZMQ with hand-written JSON in which values are strings | [core.py][c-router], [blueprints.py][b-todict], [blueprints.py][b-guess], [blueprints.py][b-rewrite] | Values change type silently: a string `"123"` comes back as the number 123. insthub's msgpack codec keeps types and sends arrays without copying ([what travels on a connection](../architecture.md#what-travels-on-a-connection)), with no compiled pyzmq. |
| Wire data and clients can make a program import, construct, `eval` or reach any attribute | [blueprints.py][b-eval], [core.py][c-create], [helpers.py][h-getattr], [core.py][c-init] | Remote code execution by design. insthub dispatches only to declared commands and properties, and devices come only from server code or a `.toml` ([ideas we decided against](../design.md#ideas-we-decided-against)). If opening a device at run time is ever wanted, it stays limited to devices the server's file already names, as `lazy = true` does. |
| Errors arrive as a plain `Exception` | [blueprints.py][b-err-send], [blueprints.py][b-err-recv] | Callers cannot catch specific errors. insthub keeps the exception's type and the server's traceback ([when things go wrong](../architecture.md#when-things-go-wrong)). |
| Fire-and-forget PUB/SUB | [broadcasts.md][doc-quirk] | A late subscriber misses the current state. An insthub subscription starts with the stream's last frame. |
| Notices sent on one PUB socket from many worker threads | [broadcasts.md][doc-order], [base.py][base-send] | ZMQ sockets are not thread-safe. Two instruments hold different locks, so their two-part messages can interleave. insthub's sender is thread-safe; keep it so when adding notices. |
| Notices sent from one request handler, which other paths bypass; events recognised by method name | [core.py][c-bcast], [core.py][c-setparams], [core.py][c-byname] | Windows and loggers drift out of sync, and the server collects one instrument's API. Emit where every path passes, and let devices announce anything else with `publish()`. |
| One request in flight per client; a blind retry of whole functions | [client/core.py][cc-ask], [proxy.py][p-retry], [proxy.py][p-retried] | No concurrency within a program, and a write can be sent twice. insthub multiplexes requests with futures and never runs a request twice ([when things go wrong](../architecture.md#when-things-go-wrong)). |
| A widget loader written three times, with different extra arguments and swallowed errors | [application.py][a-servertab], [application.py][a-detached], [client/application.py][cl-tab] | An author cannot know what a widget will receive, and failures disappear. insthub has one panel contract and shows errors ([writing the UI half](../design.md#writing-the-ui-half)). |
| Qt, QCoDeS, pandas and jsonschema loaded by the package itself | [\_\_init\_\_.py][init-qt], [serialize.py][s-imports], [log.py][log-qt] | Every headless server and script pays for them ([keeping the core light](../design.md#keeping-the-core-light)). |
| A large feature in the core package: the Parameter Manager (about 6.8k lines) and Grafana files | [pyproject.toml][pp-data] | It is a self-contained virtual instrument with its own GUI, an ideal extension, yet everyone installs it ([rule 2](../design.md#the-three-rules)). |
| Polling through a TCP client connected to its own server | [pollingWorker.py][poll] | An extra thread, a network round trip and Qt, for what a task on the device thread does. |
| A subscriber thread and socket per open widget | [instruments.py][i-sub], [proxy.py][p-subloop] | Each tab costs a socket and a thread, and the order of teardown becomes fragile. insthub uses one connection per server and `qt.watch`. |

## Checked and corrected

The check found every claim of the first reading about both codebases
accurate. It corrected these conclusions:

- **A read-back option after a set** is not needed: insthub's device page
  already reads a property again after setting it (`_Properties` in
  `pages.py`).
- **A hook on `prop.__set__`** cannot work for wrapped drivers, whose
  properties are plain `property` objects, and it would publish twice.
- **Better MCP argument schemas** do not follow from richer metadata:
  insthub's agent tools are a few generic ones (`call`, `get`, `set`, ...),
  and agents read the signature text.
- **The names the lab's adapters exclude** (hp8562a, Windfreak) are
  deliberate, not collateral damage: raw protocol methods must stay off the
  bus ([the manifest](../design.md#the-manifest)).
- **"One `$describe` should be enough"** is not insthub's behaviour either:
  its proxy asks again for an unknown name, at most once a second.

Two more, found while writing this note:

- **"Annotations must be quoted"** is out of date at this commit. `base.py`
  asks for it because the proxy `exec`s the signature text
  ([base.py][base-quote]), but the generated source now holds only parameter
  names, each defaulting to a sentinel ([proxy.py][p-defargs],
  [proxy.py][p-names]). The lesson stands: that source still holds the method
  name exactly as the server sent it ([proxy.py][p-exec]).
- **An unknown attribute does not always reach the server.** `__getattr__`
  asks for a blueprint ([proxy.py][p-getattr]), but blueprints are cached on
  the client ([proxy.py][p-cache]).

<!-- Evidence: permalinks at the studied commit. -->
[commit]: https://github.com/toolsforexperiments/instrumentserver/tree/684d7cf59ebf66d662d0ec67944b19382a4c1371
[pp-authors]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/pyproject.toml#L5-L15
[pp-deps]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/pyproject.toml#L16-L27
[pp-scripts]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/pyproject.toml#L35-L40
[pp-data]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/pyproject.toml#L50
[adr3]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/docs/adr/0003-broadcaster-contract.md#L1-L16
[doc-widgets]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/docs/technical_guide/custom_widgets.md#L1-L6
[doc-order]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/docs/technical_guide/broadcasts.md#L22-L25
[doc-quirk]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/docs/technical_guide/broadcasts.md#L126-L130
[init-qt]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/__init__.py#L4-L6
[init-schema]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/__init__.py#L30-L33
[base-send]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/base.py#L62-L72
[base-bcaster]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/base.py#L75-L94
[base-quote]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/base.py#L90-L93
[b-param]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/blueprints.py#L92-L105
[b-method]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/blueprints.py#L160-L209
[b-module]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/blueprints.py#L212-L226
[b-todict]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/blueprints.py#L474-L496
[b-rewrite]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/blueprints.py#L734-L750
[b-err-recv]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/blueprints.py#L751-L754
[b-err-send]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/blueprints.py#L779-L782
[b-eval]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/blueprints.py#L821-L854
[b-array]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/blueprints.py#L908-L911
[b-guess]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/blueprints.py#L974-L1030
[h-getattr]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/helpers.py#L57-L68
[cfg-gui]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/config.py#L87-L100
[cfg-poll]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/config.py#L102-L111
[cfg-defaults]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/config.py#L119-L165
[cfg-load]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/config.py#L180-L200
[s-imports]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/serialize.py#L73-L74
[s-from]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/serialize.py#L151-L188
[s-sorted]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/serialize.py#L162
[log-qt]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/log.py#L12
[pm-type]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/params.py#L52-L61
[apps-headless]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/apps.py#L26-L34
[apps-poll]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/apps.py#L83-L88
[c-todo]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L16
[c-port]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L159-L162
[c-log]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L178-L188
[c-init]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L202-L208
[c-router]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L217-L228
[c-pub]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L230-L241
[c-pool]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L249-L262
[c-create]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L447-L479
[c-bcast]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L487-L513
[c-setparams]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L563-L564
[c-guiconf]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L566-L580
[c-register]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L582-L591
[c-send]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L593-L614
[c-byname]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L616-L640
[c-lock]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L642-L666
[c-thread]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/core.py#L695-L699
[poll]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/pollingWorker.py#L11-L59
[a-possible]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/application.py#L286-L293
[a-host]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/application.py#L746-L767
[a-servertab]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/application.py#L885-L920
[a-detached]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/application.py#L1003-L1015
[a-embedded]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/server/application.py#L1040-L1056
[cl-tab]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/application.py#L190-L230
[cc-timeout]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/core.py#L34
[cc-dealer]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/core.py#L81-L87
[cc-ask]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/core.py#L90-L114
[cc-reset]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/core.py#L100-L123
[p-imports]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L17-L23
[p-defargs]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L78-L101
[p-sig]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L104-L124
[p-fixme]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L242-L243
[p-names]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L432-L447
[p-exec]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L474-L486
[p-sub]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L488-L506
[p-getattr]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L525-L542
[p-cache]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L634-L652
[p-params]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L699-L760
[p-subloop]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L829-L840
[p-retry]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L1013-L1033
[p-retried]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/client/proxy.py#L1081-L1105
[p-select]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/gui/parameters.py#L96-L149
[p-fixme2]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/gui/parameters.py#L105-L106
[i-sub]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/gui/instruments.py#L310-L319
[i-generic]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/gui/instruments.py#L712-L725
[listener-csv]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/monitoring/listener.py#L109-L148
[listener-influx]: https://github.com/toolsforexperiments/instrumentserver/blob/684d7cf59ebf66d662d0ec67944b19382a4c1371/src/instrumentserver/monitoring/listener.py#L151-L185
