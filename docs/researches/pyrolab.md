# PyroLab

[PyroLab](https://github.com/BYUCamachoLab/pyrolab) turns lab instruments into
[Pyro5](https://github.com/irmen/Pyro5) remote objects. Sequoia Ploeg wrote it
for the CamachoLab at Brigham Young University, and it is used mainly in that
lab. It is also a collection of drivers: Santec and PurePhotonics lasers,
Thorlabs cameras and stages, a Rohde & Schwarz scope, a Keysight SMU, an
Arduino. It is at version 0.5.0, marked Beta, needs Python 3.11 or newer, is
GPL-3.0+ and is on PyPI, with a pytest suite and release candidates. The
framework is about 5.4k lines and the drivers about 8.3k lines in 30 files,
both in one package. We studied commit
[`0dffb50`](https://github.com/BYUCamachoLab/pyrolab/tree/0dffb506e91d7220d811f96235b94f8a942d323f)
(2026-10-23, the 0.5.0 release) in November 2026, by reading the code; nothing
was run. Its one-line summary is in [what we learned from other
projects](../design.md#what-we-learned-from-other-projects).

In short: of all the projects studied, PyroLab shows most clearly what happens
when a framework is also its own driver collection. Its good ideas are
policies, not machinery. It refuses a misspelled config key before anything
starts, it says "ready" only once a server serves, and it stops restarting a
server that keeps crashing.

## Words used here

| Word | Meaning |
|---|---|
| **Pyro5** | A Python library for remote objects. A client gets a *proxy*, and each method call on it runs on an object in another program. |
| **Daemon** | In Pyro5 and PyroLab, a server program that hosts objects. PyroLab also has a background manager, `pyrolabd`, that starts the daemons. |
| **Nameserver** | A separate program that maps a name such as `asgard.wolverine` to an object's address (its *URI*). Clients ask it first, then connect. |
| **Service, Instrument** | PyroLab's base classes for hosted objects. `Instrument` adds `connect()`, `close()`, `autoconnect()` and `detect_devices()`. |
| **Instance mode** | How Pyro5 creates hosted objects: one for every client (`single`), one per client connection (`session`) or one per call (`percall`). |
| **serpent** | Pyro5's default serializer. It carries only Python's built-in types. |

## How it is built

| Area | What PyroLab does |
|---|---|
| Process model | `pyrolab up` starts `pyrolabd` as a detached process ([cli.py:192-224](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/cli.py#L192-L224)). Its process manager ([manager.py:655-715](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L655-L715)) runs one OS process per nameserver ([250](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L250)) and one per daemon ([375](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L375)), so one crashing daemon does not take down the others. |
| Transport | Pyro5 calls over TCP or a Unix socket, encoded with serpent. Only built-in types travel; the README says "not NumPy arrays" ([README.md:175-182](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/README.md#L175-L182)). |
| Discovery | Pyro5 nameservers. Each service registers a dotted name and a free-text description, in the background, retrying every 30 s and warning once ([manager.py:168-243](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L168-L243), [516-530](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L516-L530)). |
| Concurrency | Per daemon, either a thread pool, where a `single` object is called from several threads at once, or a multiplexer that runs one call at a time across the whole daemon ([configure.py:15-31](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L15-L31)). The docs recommend `single` plus a lockable daemon so you need not think about thread safety ([servers.rst:56-71](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/docs/user_guide/servers.rst#L56-L71)), yet the config's default mode is `session` ([configure.py:635](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L635)). |
| Live data | None in the framework; clients poll. A camera driver opens its own TCP socket and sends JPEG frames, waiting for an acknowledgement after each ([thorcam.py:264-302](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/cameras/thorcam.py#L264-L302)). |
| Device abstraction | `Instrument` keeps its constructor free of hardware and adds `connect(**kwargs)`, `close()`, `autoconnect()` and a static `detect_devices()` ([drivers/\_\_init\_\_.py:21-186](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/__init__.py#L21-L186)). A daemon registers classes and connects nothing until a client calls `autoconnect()` ([manager.py:460-468](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L460-L468)). |
| Extension mechanism | Drivers live inside the package, with pip extras per family ([pyproject.toml:79-87](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyproject.toml#L79-L87)). A config entry may name any importable `module` and `classname`, loaded with `importlib` ([configure.py:639-674](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L639-L674)). There is no manifest, no discovery and no place for a UI. |
| UI | None in the core. A separate Flask app, pyromonitor, shows nameservers. Device GUIs are example scripts. `pyrolab ps` gets one preformatted text table from the daemon ([pyrolabd.py:214-249](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/pyrolabd.py#L214-L249)). |
| Configuration | One YAML file per user, checked by pydantic models that forbid unknown keys ([configure.py:247-253](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L247-L253)) and dangling references ([708-736](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L708-L736)). It is read with PyYAML's full loader ([55-58](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L55-L58), [301](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L301)). |
| Errors and restarts | A dead child is restarted after 5, 15, 60, then 300 s; after 5 crashes in a row it is marked failed; 600 s of uptime resets the count ([manager.py:78-87](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L78-L87)). Stopping escalates from a message to terminate to kill ([1054-1114](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L1054-L1114)). An instrument never reconnects by itself. |
| Observability | One log file of JSON lines, written only by `pyrolabd`. Each child sends its records over a pipe of its own, so a killed child cannot block logging ([logs.py:9-25](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/logs.py#L9-L25)). |
| Security | No authentication or encryption; hosts default to `localhost` ([configure.py:535](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L535)). Anyone may force a lock open ([server.py:416-447](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/server.py#L416-L447)). An example GUI unpickles data from a socket ([gui_dev.py:170](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/examples/906.uc480-camera/gui_dev.py#L170)). |
| Footprint | Twelve hard dependencies, among them scipy, pydantic, PyYAML and requests ([pyproject.toml:61-74](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyproject.toml#L61-L74)). Loading any driver runs `pyrolab/drivers/__init__.py`, which imports `pyrolab.api` ([line 15](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/__init__.py#L15)); that loads Pyro5, pydantic and YAML and reads the user's config ([api.py:12-26](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/api.py#L12-L26), [55-66](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/api.py#L55-L66)). |

## What insthub takes

One agent studied PyroLab and made nine proposals. A second agent, *the
check*, tested each against both codebases, argued against it, and added
three the first one missed. The check's verdicts are final. Where
[design.md](../design.md) later decided differently, the design wins, and the
table says so. "Rule 1, 2, 3" are [the three rules](../design.md#the-three-rules).

| Lesson | The check | Where insthub decided it |
|---|---|---|
| Check a server file against the driver's own signature | keep, adapt | [plan step 2](../design.md#the-plan-in-order), done |
| Report "ready", not "started" | modify, adapt | [plan step 10](../design.md#the-plan-in-order) |
| Restart limits from systemd, not a supervisor | modify, adapt | [plan step 10](../design.md#the-plan-in-order) |
| Say when a server runs an older file | modify, adapt | [installing, updating and broken extensions](../design.md#installing-updating-and-broken-extensions) |
| Drivers live outside the framework | modify, adopt (no code) | [where code lives](../design.md#where-code-lives), [ideas we decided against](../design.md#ideas-we-decided-against) |
| A cheap, pure `import insthub` | modify, adapt | [keeping the core light](../design.md#keeping-the-core-light), plan step 1, done |
| Every device fact can be given from outside the driver class | added, adopt | [how a device finds its extension](../design.md#how-a-device-finds-its-extension), plan steps 2 and 4, done |
| A stable id that says what a device is | added, adopt | the `extension` key, same section, plan step 4, done |
| Claims: one program changes a device, others read | modify, adapt | deferred: [only when a real need shows up](../design.md#the-plan-in-order) |
| Attach to a running device or host it in-process | added, adapt | [writing the UI half](../design.md#writing-the-ui-half); no core helper planned |
| A `detect()` hook | modify, consider later | the manifest's reserved `match` ([the manifest](../design.md#the-manifest)) |
| A description per device | modify, consider later | not planned |

**Check a server file against the driver's signature.** PyroLab rejects an
unknown key ([configure.py:247-253](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L247-L253)),
prints one line per problem with a dotted path such as `daemons.lab.port: ...`
([754-781](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L754-L781)),
and checks before it starts anything ([pyrolabd.py:151-158](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/pyrolabd.py#L151-L158),
[444-449](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/pyrolabd.py#L444-L449)).
Its strictness stops at its own keys: `parameters`, the dict passed to a
driver's `connect()`, is `Dict[str, Any]` ([configure.py:633](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L633)).
insthub needs the check more than PyroLab does. A wrapped driver is built only
when its device opens, so `prot = "/dev/ttyUSB0"` becomes a permanent "not
connected: cannot open: TypeError" while the service looks healthy. The
check's form: one helper in `config.py` binds the table to
`inspect.signature(cls)` (a full bind, so missing arguments show too), collects
every problem into one error ("`devices.psu.prot`: not a parameter of
`PowerSupply(port, baud=9600)`; did you mean `port`?"), and checks the types of
the reserved keys. `config.check()`, and so `service install`, runs it. If the
signature cannot be read it skips; with `**kwargs` it checks only reserved
keys. Standard library only: no pydantic, no YAML. insthub built this in
plan step 2, together with checks for reserved names and for two modules of
the same name.

**Report "ready", not "started".** A PyroLab child sets `ready` only once it
serves, or `error` with the reason
([manager.py:567](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L567),
[577](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L577),
[913-923](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L913-L923)).
`pyrolabd` writes a lockfile with its pid and address only after its own
object is registered, and `pyrolab up` waits until that file exists and the
daemon it names answers a call. If the process exits first, `up` prints why
([cli.py:273-297](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/cli.py#L273-L297),
[pyrolabd.py:450-451](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/pyrolabd.py#L450-L451)).
insthub has the same kind of file: a server writes its registry `.json` only
once it listens, and the file holds its pid. So `service install` replaces its
one-second sleep with a wait of up to 60 s: ready when a registry entry's pid
equals the unit's `MainPID`, failed when the unit is `failed` or restarting,
and then it prints the last lines of the journal. `server.py` does not change.
`sd_notify` (`Type=notify`) waits until boot ordering between units is needed;
it would kill any `.py` script that never reaches `Server.start()`.

**Restart limits from systemd, not a supervisor.** PyroLab's policy numbers are
sensible ([manager.py:78-87](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L78-L87),
[788-818](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L788-L818)).
insthub takes the policy and leaves the machinery. In the unit:
`StartLimitIntervalSec=300`, `StartLimitBurst=5`, `RestartSec=5` (now 2),
and `Restart=on-failure` as now. Leave out `RestartSteps`: with five starts per
300 s its long delays are never reached. Run `systemctl --user reset-failed`
before every start and restart; otherwise, after a crash loop, systemd refuses
even a manual start and the window's buttons fail. Show `NRestarts` and
`Result` ("restarted 3×, last: exit-code"). An insthub server crashes only from
a code or config bug, because a missing instrument just marks its device "not
connected", so failing fast is right.

**Say when a server runs an older file.** PyroLab copies the config when it
starts ([pyrolabd.py:155-157](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/pyrolabd.py#L155-L157)),
and every CLI command compares the two files' times and prints "Run 'pyrolab
reload'" ([cli.py:89-98](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/cli.py#L89-L98)).
It checks the config only, not code. In insthub the first step needs only the
window, which already knows the file each server runs
(`inventory.server_file()`) and when the server started. If the file is newer,
it can say "file changed since start" next to the Restart button. A later
step adds the driver's own source files, so that editing `hp8562a.py` is
flagged too; design.md puts each device's folder in the
server's `.json` file (plan step 7).

**Drivers live outside the framework.** PyroLab hard-depends on scipy
([pyproject.toml:61-74](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyproject.toml#L61-L74))
for one constant, the speed of light
([tsl550.py:29](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/lasers/tsl550.py#L29),
[ppcl55x.py:33](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/lasers/ppcl55x.py#L33)).
Its Thorlabs extra is commented out because a git dependency cannot be
published ([pyproject.toml:82](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyproject.toml#L82)),
and its import test has to skip drivers whose vendor library is missing
([tests/test_imports.py:1-9](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/tests/test_imports.py#L1-L9)).
insthub already keeps drivers out (rule 1). This lesson needs no code, only
the written rule and a layout, which design.md now has: a plain driver with no
insthub in it, a thin device, an optional UI and simulator ([rules inside an
extension](../design.md#rules-inside-an-extension)).

**A cheap, pure `import insthub`.** Fourteen PyroLab driver modules import
`pyrolab.api` for `@expose` and `@behavior`, and so does the drivers package
itself, so loading any driver loads it. That module loads
Pyro5, pydantic and YAML ([api.py:12-26](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/api.py#L12-L26)),
and on import reads the user's config and writes a nameserver host into
Pyro5's process-wide settings ([api.py:55-66](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/api.py#L55-L66)).
insthub is already light and has no import side effects. It keeps that with a
footprint test and by importing `asyncio` only where it is awaited, but no lazy
`__init__.py`, which would save about 1 ms (rule 3).

**Every device fact can be given from outside the driver class.** PyroLab needs
the framework inside every driver: decorators from `pyrolab.api` and hooks as
methods of the `Instrument` base class
([drivers/\_\_init\_\_.py:87-170](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/__init__.py#L87-L170)).
insthub's `wrap()` hosts an unchanged class, and the lab's drivers import no
insthub. New facts about a device (its extension, `simulated`, metadata, a
dangerous flag) must therefore also be settable through `wrap()` arguments, a
`.toml` key or the manifest, with class attributes only as a convenience.

**A stable id that says what a device is.** PyroLab has no way to match a remote
object to its client helper. The user pairs `ThorCamClient`
([thorcam.py:398](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/cameras/thorcam.py#L398))
with the right name by hand, and the nameserver holds only free text
([manager.py:521](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L521)).
The check called it `kind`; design.md chose the `extension` key.

**Claims.** PyroLab's `LockableDaemon` gives the right outline
([server.py:274-501](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/server.py#L274-L501)):
a client calls `lock(user)`, other connections are refused
([449-482](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/server.py#L449-L482)),
the lock goes when its holder disconnects
([484-501](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/server.py#L484-L501)),
and anyone can force it open
([416-447](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/server.py#L416-L447)).
The check's form for insthub, opt-in: built-ins `$claim(label, steal=False)` and
`$unclaim(force=False)`. While a device is claimed, other programs may still
read it (`$describe`, `$status`, `$get`, `$subscribe`, concurrent commands) but
not change it; they get `DeviceClaimed`, naming the label, pid and program. A
claim ends when its connection closes. After a reconnect the client claims
again, or raises, so a script never carries on unprotected. One claim covers a
whole program or notebook kernel, because the client keeps one connection per
server. design.md defers claims: nobody has hit a conflict yet, a forgotten
claim is a failure of its own, and pausing a device covers handing it to vendor
software. This is the shape to build if a need shows up.

**Considered later.** `detect_devices()` returns `connect()` arguments for the
instruments attached
([drivers/\_\_init\_\_.py:87-111](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/__init__.py#L87-L111)).
Several drivers implement it, but nothing calls it, and it drifted: the
TSL-550 returns `{"address": ...}` while its `connect()` takes `port`
([tsl550.py:133-148](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/lasers/tsl550.py#L133-L148)).
If insthub adds one, it is a manifest entry with a consumer and a contract
test. Until then the New server dialog could suggest serial ports and VISA
resources for any argument named `port`, `address` or `resource`. A
per-device description (PyroLab's
[configure.py:634](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L634))
waits until descriptive device names prove too little. PyroLab makes users
choose by hand between a local driver and a remote proxy
([README.md:89-146](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/README.md#L89-L146)).
An `attach_or_host()` helper would make that choice once for every extension
UI, but design.md keeps it in the apps for now.

## What insthub leaves, and why

| PyroLab does | Evidence | Why insthub does not |
|---|---|---|
| Drivers and their dependencies inside the framework | [pyproject.toml:61-87](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyproject.toml#L61-L87) | The core would grow with every instrument's needs (rules 1 and 3; [ideas we decided against](../design.md#ideas-we-decided-against)). |
| A nameserver that clients ask first | [nameserver.py:43-133](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/nameserver.py#L43-L133) | One more program to start first and to fail. Most of PyroLab's registration code (retries, timeouts, cleanup for dead daemons) exists only because the broker does ([why no central server](../architecture.md#why-it-is-built-this-way)). |
| Its own supervisor: about 2.1k lines in `pyrolabd.py`, `manager.py` and `logs.py` | [manager.py:24-41](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/manager.py#L24-L41), [utils.py:144-145](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/utils.py#L144-L145) | It relies on pids that get reused, and still needs Task Scheduler ([deployment.rst:51-54](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/docs/user_guide/deployment.rst#L51-L54)) or cron ([97-108](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/docs/user_guide/deployment.rst#L97-L108)) to start at boot. systemd does this already. |
| Side effects when a module is imported | [api.py:55-66](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/api.py#L55-L66) | Importing must not read files or change global settings ([keeping the core light](../design.md#keeping-the-core-light)). |
| The lock owner taken from shared "last requestor" state | [server.py:472](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/server.py#L472), [114](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/server.py#L114) | Under the thread pool another request can overwrite it in between, so the lock may go to the wrong client. insthub's server already hands the connection to each request. |
| Framework methods mixed into the driver's class | [server.py:338-345](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/server.py#L338-L345), [service.py:67-87](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/service.py#L67-L87) | An injected `lock()` collides with an instrument's own keypad lock, and method order silently decides. insthub keeps bus operations in `$` built-ins and `dev.bus`. |
| A lock as the answer to thread safety | [servers.rst:56-71](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/docs/user_guide/servers.rst#L56-L71) | It shuts out every other reader and still lets one client's threads call at once. insthub gives each device one thread and a waiting line ([why](../architecture.md#why-it-is-built-this-way)). |
| Settings stored on the class, so each device gets a generated subclass | [configure.py:68-98](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L68-L98), [667-672](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L667-L672) | Class names with a uuid in tracebacks. Settings belong to the instance: constructor arguments. |
| Clients must connect the hardware | [drivers/\_\_init\_\_.py:147-170](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/__init__.py#L147-L170), [examples/autoconnect/client.py:22-25](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/examples/autoconnect/client.py#L22-L25) | Every script must remember it, and two clients can race. An insthub server owns the session ([when things go wrong](../architecture.md#when-things-go-wrong)). |
| A config loader that can run code | [configure.py:108-111](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L108-L111) | insthub reads `.toml` with `tomllib`, which cannot. |
| Each driver its own streaming protocol | [thorcam.py:264-316](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/drivers/cameras/thorcam.py#L264-L316) | Without streams in the core, every extension invents one, here with `pickle` in an example. insthub's core has streams; pickle is [ruled out](../design.md#ideas-we-decided-against). |
| The server returns formatted text | [pyrolabd.py:214-249](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/pyrolabd.py#L214-L249) | A window or agent would have to parse it. Return data; format it in the client. |
| Transport settings global to the process | [configure.py:220-224](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/configure.py#L220-L224) | Two servers with different settings cannot share a program. insthub keeps settings on each `Server` and client. |
| Contracts and examples nothing checks | [examples/autoconnect/server.py:29-31](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/examples/autoconnect/server.py#L29-L31), [services.rst:89-94](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/docs/user_guide/services.rst#L89-L94) | The example passes `connect_params=`, which `Daemon.register` does not take ([server.py:180](https://github.com/BYUCamachoLab/pyrolab/blob/0dffb506e91d7220d811f96235b94f8a942d323f/pyrolab/server.py#L180)), and its client looks up another name. insthub adds a manifest table only with the code that reads it and a test ([the manifest](../design.md#the-manifest)). |

Also dropped: a lazy `__init__.py` for insthub (about 1 ms saved, see
[keeping the core light](../design.md#keeping-the-core-light)), and pydantic
for config checks.

## Checked and corrected

The check found three places where the first reading misjudged insthub:

- **"What insthub lacks is a template for device projects."** insthub already
  does what the lesson asks. The lab's real drivers (`hp8562a.py`, `synthhd.py`, the
  M8195A driver) import no insthub at all; a thin `bus_server.py` or a
  `.toml` puts them on the bus. The proposed template was off too: it let
  `driver.py` be a `Device` class. The driver stays plain, and the adapter is
  separate.
- **"Nothing can notice a stale server, because the server does not know its
  file."** The window already finds that file, from the unit's `ExecStart` or
  from `/proc/<pid>/cmdline` (`inventory.server_file()`), and the server page
  shows it with Restart and Open buttons. The first step needs no change to
  the server or the protocol.
- **"Take PyroLab's description key."** insthub is broker-free already, and a
  per-device description has no shown need, so it waits.

Smaller corrections:

- The first reading expected a lazy `__init__.py` and a deferred `asyncio` to
  save about a third of the import time. Measured, `asyncio` adds about 6 ms
  of 37-39 ms, because its main parts load anyway, and laziness about 1 ms.
- insthub servers are not "never" marked failed: systemd's default start limit
  applies, though unreliably. A start limit without `reset-failed` would break
  the window's Start and Restart buttons.
- Several line numbers were wrong. `drivers/__init__.py` has 186 lines
  (`detect_devices` is at 87-111), `api.py` has 66 (the side effect is at
  55-66), `service.py` has 87 (`ping` and `pyrolab_version` are at 67-87), and
  the Thorlabs extra is at `pyproject.toml` line 82. Every link in this note
  was checked against the clone.
