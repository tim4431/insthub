# LabRAD

LabRAD is a system for controlling lab instruments over a network, built
around one central program, the *Manager*, that every other program connects
to. It comes from physics labs: its repositories moved from the
`martinisgroup` GitHub organization ([README][pl-readme-moved]), and the
node's docs point to a "Qubit Server" ([node][pl-qubit]). Two repositories
matter here. [pylabrad](https://github.com/labrad/pylabrad) is the Python
library for writing servers and clients, about 14,000 lines including tests.
[scalabrad](https://github.com/labrad/scalabrad) is the Manager, about 15,000
lines of Scala that need Java 8 ([README][sl-readme-java]). It is mature and
quiet: pylabrad's last commit is from September 2024, scalabrad's from June
2021, and pylabrad stopped working with the older Delphi manager in version
0.96 ([README][pl-readme-delphi]). We studied pylabrad at `cd0fea0` (2
September 2024) and scalabrad at `7cb7bcc` (13 June 2021), in November 2026.
One agent read the code; a second checked every
claim against the code and argued against every proposal, and where they
disagree this note follows the second. It backs the LabRAD row of [what we
learned from other projects](../design.md#what-we-learned-from-other-projects).

## Words used here

| Word | Meaning in LabRAD |
|---|---|
| **Manager** | The one central program. Every server and client connects to it, and it forwards every request. |
| **server** | A program that offers settings. A program, not a machine: one PC runs several. |
| **setting** | A command a server offers, with a number, a name, a doc string and type tags. |
| **type tag** | A short string stating a value's type, such as `v[s]` (a number in seconds) or `*v` (a list of numbers). |
| **context** | A label every request carries. A server keeps separate state for each (client, context) pair. |
| **packet** | One message holding several requests to one server, run in order. |
| **signal** | A setting a client calls to sign up for messages the server sends later. |
| **node** | One server per computer that starts and stops the other servers there. |
| **Registry** | A tree of typed keys and values inside the Manager, where servers keep their configuration. |

## How it is built

| Area | How LabRAD does it | Evidence |
|---|---|---|
| Programs | A star around one Manager process, which also hosts the Registry and the password check. Every server and client is its own process with one connection to the Manager; even a server's own client goes through it. | [connectServer][sl-hub-connect], [routing][sl-hub-route], [a server's client][pl-client] |
| Transport | TCP to the Manager on port 7682, encrypted by default (STARTTLS). A packet carries a context, a request number and records of (setting number, type tag, bytes). Positive numbers are requests, negative ones replies, zero messages. numpy arrays go as typed lists, with no separate path for raw buffers. | [CONFIG.md][pl-config], [flattenPacket][pl-flatten-packet], [packetReceived][pl-packet-received], [arrays][pl-numpy] |
| Naming | The Manager owns names, which must be unique across the whole system. It hands out server numbers and gives a restarted server its old one. Clients look settings up by name to get their numbers. | [allocateServerId][sl-hub-alloc], [Servers, Settings, Lookup][sl-handlers-lookup] |
| Threads | Servers run on twisted's event loop, in one thread. Requests in one context run in order; requests in other contexts interleave at every `yield`. `ThreadedServer` uses a pool of up to 10 threads, `SingleThreadedServer` one thread for all devices. | [request_handler][pl-req], [ThreadedServer][pl-threaded] |
| Live data | A client registers a listener for a message number it picks, then calls the signal's setting with that number. The Manager broadcasts named messages such as "Server Connect". No last value is kept, and there is no rule for slow readers. | [Signal][pl-signal], [test_signal.py][pl-test-signal], [named messages][sl-named] |
| Devices | The server is the unit. A setting is a method marked `@setting(ID, name, ...)` that receives the context's dict `c`. `DeviceServer` puts several devices behind one server: a client selects a device in its context, and later calls act on it. | [setting][pl-setting], [selectDevice][pl-select] |
| Extensions | A server is a standalone program, in any language. The node walks configured folders and reads, as text, `.ini` files, a `### BEGIN NODE INFO` block inside any file, or `node.ini` inside a `.jar`. It never imports the server. | [refreshServers][pl-refresh], [find_config_block][pl-find-block] |
| UI | None in the core. GUIs are ordinary clients (pylabrad ships a bridge that runs twisted inside Qt), and the Manager, node and Registry UIs are a separate web app. Generic tools rely on the Manager's `Help`, which returns each setting's doc and types. | [README][pl-readme-web], [qt4reactor][pl-qt4], [Help][sl-help] |
| Configuration | The Registry, with a current directory per context and change notifications; the node's own settings live there too. Environment variables (`LABRADHOST`, `LABRADPORT`, `LABRADPASSWORD`, `LABRAD_TLS`), and command-line options. | [RegistryContext][sl-reg-ctx], [node config][pl-node-config], [CONFIG.md][pl-config] |
| Errors | An exception goes back as an error record "[server] Remote traceback", with an optional number, and a packet stops at its first failure. When a client disconnects, the Manager tells every server to expire its contexts, which frees its locks and signal listeners. | [_getTraceback][pl-traceback], [Hub.disconnect][sl-hub-expire], [_contextExpired][pl-expire] |
| Observability | `Connection Info` on the Manager counts requests, replies and messages per connection. The node keeps the last 1000 output lines of each server it started and serves them. Every server has a `signal: log`. | [Connection Info][sl-conninfo], [LOG_LENGTH][pl-log-length], [node output][pl-node-output], [log signal][pl-log-signal] |
| Security | A password challenge: the client answers `MD5(challenge + password)`. TLS is required by default since scalabrad 0.5; newer versions add user names and OAuth. The node hands the password to the servers it starts through environment variables. | [AuthServiceImpl][sl-auth], [scalabrad README][sl-readme-tls], [node start][pl-node-env] |
| Footprint | Heavy. pylabrad requires twisted, pyOpenSSL, service_identity, requests, pyparsing and numpy, and `import labrad` loads the twisted reactor and the units grammar. `types.py` is 1472 lines, `units.py` 1452. The Manager needs a JVM. | [requirements.txt][pl-reqs], [backend.py][pl-backend], [units.py][pl-units] |

## What insthub takes

| Idea | Verdict | Decided in |
|---|---|---|
| Manifests read as data, never run | adapt | [the manifest](../design.md#the-manifest), plan step 7 |
| A UI is a program started with the extension's Python | adopt | [two halves](../design.md#two-halves-each-where-its-libraries-are), plan step 8 |
| Devices say which extension and version they come from | adopt | [how a device finds its extension](../design.md#how-a-device-finds-its-extension), plan step 4 |
| Describe parameter types, don't enforce them | adapt | [a generic UI](../design.md#a-generic-ui-good-enough-for-most-devices), plan step 5 |
| A test that guards what `import insthub` loads | adopt | [keeping the core light](../design.md#keeping-the-core-light), plan step 1 |
| Wait until a started server answers | adapt | [the plan](../design.md#the-plan-in-order), step 10 |
| A shared secret for TCP | adapt, when needed | [the plan](../design.md#the-plan-in-order), "only when a real need shows up" |
| Hardware matching, a hold on a device, `$batch` | consider later | [the plan](../design.md#the-plan-in-order), "only when a real need shows up" |

### Manifests read as data, never run

The node learns what a server is, and how to start it, from a text manifest
it never runs ([refreshServers][pl-refresh], [find_config_block][pl-find-block]).
The manifest gives a name, a version, a description, an instance name, a
command line per platform, a start timeout and how to stop the server
([from_string][pl-from-string], [example][pl-test-server-block]). Several
versions of one server can sit side by side; all but the newest get
`name-version` names ([versions][pl-versions]).

insthub needs this even more: the window runs in an environment with no
drivers, and importing a driver can load a vendor library or touch hardware.
The second reading narrowed the first proposal. Start with project folders
holding `insthub-extension.toml`, which is how the lab's projects live, and
add one entry-point group only later. Keep the extension id, which names a
kind of device, apart from device names, which name instances, so nothing is
written twice. [The manifest](../design.md#the-manifest), [where insthub
looks](../design.md#where-insthub-looks-for-extensions) and [when code is
loaded](../design.md#when-code-is-loaded) settle the details.

### A UI is a program started with the extension's Python

The node never imports what it runs. It starts each server from the
manifest's command line, filling in `%PYTHON%`, `%FILE%` and `%DIR%`, waits
for it to appear and keeps its output ([cmdline][pl-cmdline],
[ServerProcess.start][pl-proc-start]). LabRAD GUIs were always separate
client programs ([qt4reactor][pl-qt4]).

For extensions that hold both a driver and a UI, this is the choice that
matters most. The lab's real device GUI, `hp8562a_gui`, pins PyQt6 and
imports the driver for its hosting fallback, so importing it into the window
would fail on day one. One difference: LabRAD's `%PYTHON%` is the node's own
interpreter (see [checked and corrected](#checked-and-corrected)). insthub
starts a UI with the extension's Python instead: a `.venv` in the extension's
folder or the one above, or the `python` the manifest names. Decided in [two
halves, each where its libraries are](../design.md#two-halves-each-where-its-libraries-are)
and [writing the UI half](../design.md#writing-the-ui-half).

### Devices say which extension and version they come from

The node's status lists, for every server it knows, the name, description,
version, instance name and running instances ([STATUS_TYPE][pl-status-type],
[status][pl-status]). It compares each running instance's version with the
newest manifest of the same kind, not the same instance name, and reports or
restarts the outdated ones ([_get_outdated][pl-outdated],
[outdated_list, outdated_restart][pl-outdated-settings]).

insthub's `describe()` used to give only a class name as `type`. That name
differs between a driver and its simulator and collides across vendors, and
a window on another computer sees `describe()` but never the server's file.
So devices now carry optional `extension`, `extension_version` and
`simulated` keys (plan step 4, done), and once manifests exist the window
can say "running 0.3, the folder has 0.4: restart". Decided in [how a device
finds its extension](../design.md#how-a-device-finds-its-extension) and
[versions](../design.md#versions).

### Describe parameter types, don't enforce them

Every setting registers its number, name, doc, accepted types and returned
types with the Manager ([getRegistrationInfo][pl-reginfo]). The accepted
list covers every combination, including calls that leave out trailing
optional arguments ([accepts][pl-accepts]). scalabrad works the tags out from
Scala method types ([makeHandler][sl-makehandler],
[inferParamType][sl-infer]). Units are part of the tag, as in
[`v[s]`][pl-tag-s] and [`v[m/s]`][pl-tag-ms]. The Manager serves all of it
through [`Help`][sl-help], and LabRAD's generic tools are built on that.

insthub takes the description and leaves the enforcement (see [what insthub
leaves](#what-insthub-leaves-and-why)): units, limits and choices are advice
for the UI and for agents, and the server converts nothing. The second
reading added two cautions, and both shaped what was built. Drivers written
with `from __future__ import annotations` hand over their annotations as
text, so insthub evaluates them and falls back to the text as written when
that fails. Drivers without annotations, or with `**kwargs`, keep the
free-text argument line. It also shrank the claim: types already showed in
`describe()`'s `signature` string, so this improves the UI and the agents'
view rather than adding a capability. Decided in [a generic UI good enough
for most devices](../design.md#a-generic-ui-good-enough-for-most-devices)
(plan step 5, done).

### A test that guards what `import insthub` loads

`import labrad` loads the twisted reactor through `backend`
([\_\_init\_\_.py][pl-init], [backend.py][pl-backend]), and `units` loads
numpy and a pyparsing grammar ([units.py][pl-units]). Every server and
client pays for it. Nothing in the repository stopped this growth, and parts
of the surface decayed: `util` still calls `inspect.getargspec` (removed in
Python 3.11) and `dict.iteritems` (Python 2 only) ([util][pl-util-decay]).
insthub's answer is a footprint test and moving `asyncio` into the functions
that await. Decided in [keeping the core
light](../design.md#keeping-the-core-light).

### Wait until a started server answers

When the node starts a server, it waits until a server of the expected name
connects, the process exits, or the timeout runs out (20 s by default). On a
timeout it kills the process and returns its captured output as the error
([ServerProcess.start][pl-proc-start], [default timeout][pl-timeout-default]).
Stopping tries a message or a setting first, and kills only after a timeout
([_stop][pl-stop]).

insthub's `service start` and `restart` return as soon as `systemctl` does,
so a server that fails at start looks started. The small part is adopted:
wait for the server to answer, and on failure show the last journal lines
(plan step 10). The larger part, a node-like device that controls servers on
another computer, waits for a real need and for TCP to have a secret,
because it starts processes.

### A shared secret for TCP

The Manager sends a random challenge and the client answers with
`MD5(challenge + password)` ([AuthServiceImpl][sl-auth],
[client side][pl-md5]). LabRAD shows that one shared secret is enough for a
lab network. If insthub needs it: first the zero-code option, a server bound
to `127.0.0.1` and reached through `ssh -L`; then a nonce in the hello,
answered with HMAC-SHA256 and checked with `hmac.compare_digest`, a bumped
protocol version, and Unix sockets left alone. This is authentication, not
encryption, and it must exist before anything can start processes over the
network. Today's stance is in [what insthub does not
do](../architecture.md#what-insthub-does-not-do).

### Later, if a need appears

- **Hardware matching.** A `ManagedDeviceServer` declares which instrument
  identities it handles, and a "Device Manager" server tells it when
  matching instruments appear on the GPIB buses
  ([ManagedDeviceServer][pl-managed], [connectToDeviceManager][pl-devmgr]).
  LabRAD put this in its core package, which tied the core to GPIB. insthub
  reserves a `match` table in [the manifest](../design.md#the-manifest). Any
  bus scan lives outside insthub and runs only when the user asks, because
  probing some instruments with `*IDN?` disturbs them.
- **A hold on a device.** `DeviceServer` lets a context lock a device for
  10 s, released when the context expires; other contexts are refused
  ([lock][pl-lock], [expireContext][pl-dev-expire],
  [Lock Device][pl-lock-setting]). A hold is hidden state that changes what
  other clients' calls do, and a forgotten one is a failure of its own. For
  now an atomic sequence is one device command, and results carry the
  settings they were taken with. If ever built: an immediate error naming
  the holder, no waiting line for the others, no per-client hook. The plan
  calls it "Claims".
- **`$batch`.** A packet runs several records in order and stops at the
  first error ([request_handler][pl-req-loop], [PacketWrapper][pl-packet]).
  insthub already pipelines calls with `.future()`, so only "nothing runs
  between them" would be new, and LabRAD's packets do not give that either
  (see below). If scripts or agents need ad hoc atomic sequences, `$batch`
  is about 30 lines with no new state.

## What insthub leaves, and why

| Idea | Evidence | Why not |
|---|---|---|
| A central Manager for routing, naming, config and auth | Every request goes through it ([routing][sl-hub-route]); a server cannot start without logging in ([startup][pl-startup]). | One more program to start first, a single point of failure, two hops per call, big arrays copied through a middleman. See [ideas we decided against](../design.md#ideas-we-decided-against). |
| The Registry, a central config store | [RegistryContext][sl-reg-ctx], [cd][sl-reg-cd] | It needs a running service and hides config from git, and its per-context current directory is hidden state. An extension that needs saved values keeps its own file. |
| Hand-numbered settings and magic message numbers | Setting numbers checked for clashes at start ([_checkSettingConflicts][pl-conflicts]); 987654321, 55443322 and 66554433 hard-coded ([listeners][pl-magic]); 21436587 for the Device Manager ([messageID][pl-msgid]). | Collisions, unreadable traffic, and a numbering scheme every author must manage. Names cost almost nothing over msgpack, and the `$` prefix already keeps built-ins apart. |
| Hidden per-connection state | "Select Device" changes what later calls in that context act on ([selectDevice][pl-select]); so does the Registry's current directory ([cd][sl-reg-cd]). | The same call does different things depending on earlier calls, and the state is lost after a reconnect. insthub names the device in every call. |
| A strict wire type system with units | Flattening checks and converts types ([flatten][pl-types-flatten]); intricate rules for empty tags ([decorators.py][pl-tag-rules]); the node works around inference bug #342 ([node][pl-342]); a setting that fails only when its result is flattened ([Bad Return Type][pl-bad-return]). | A large core, slow imports, and failures that exist only because of the type system. Describe types; don't enforce them. |
| Heavy required dependencies | [requirements.txt][pl-reqs], [backend.py][pl-backend] | Against [rule 3](../design.md#the-three-rules): every driver and script would inherit them. |
| One event-loop thread running blocking driver code | The code's own comment ([server.py][pl-single-thread]); one pool, or one worker, for all devices ([ThreadedServer][pl-threaded]). | One slow instrument stalls every device on the server, and driver authors must write twisted code. insthub keeps [one thread and waiting line per device](../architecture.md#threads-who-does-what). |
| A name written in two places | The manifest's name "MUST MATCH" the class variable, or the node never sees the server as started ([node][pl-must-match], [template][pl-template-name]). | Configuration that drifts silently. insthub writes the extension id once, in the manifest. |
| Two-step signal sign-up with numbers the client picks | [test_signal.py][pl-test-signal] | Clumsy and fragile. insthub's `subscribe(stream)` by name, with the newest frame kept, is better. |
| A surface too large to keep tested | Python 2 calls in `util` ([util][pl-util-decay]); an integer `cd` kept for the Delphi editor ([Registry][sl-reg-int-cd]). | Every extra concept becomes upkeep. insthub stays small enough that its tests exercise all of it. |

The second reading also dropped parts of the first proposals:

- **Three entry-point groups, and panels imported into the window.** One
  folder manifest now and one entry-point group later are enough, and the
  window runs no extension code ([ideas we decided
  against](../design.md#ideas-we-decided-against)).
- **A `registry.watch()` for servers coming and going.** LabRAD servers
  needed `serverConnected` to set up their listeners again. insthub clients
  reconnect and restore their subscriptions by themselves ([when things go
  wrong](../architecture.md#when-things-go-wrong)).
- **`Device.saved`, a dict saved per device.** Plain files already do this,
  and a new attribute on `Device` costs every program that imports it.
- **An `on_client_gone` hook.** Subscriber counting already stops streaming
  nobody watches, and a device never learns which client started what.

Of what the Manager provided, one small thing is worth having without it: a
qualified name `server/device` in `insthub.device()` and `locate()`, so two
servers with a device of the same name stop being a dead end. It needs
`registry.check_name` to accept the form. design.md does not decide this
yet; it fits step 10's "remote addresses in one place".

## Checked and corrected

- **A packet does not keep other clients out.** The first reading said a
  packet means "nothing else can get in between". `request_handler` takes
  only the context's own lock ([server.py][pl-req-lock]), so requests in
  other contexts interleave at every `yield`. A packet gives order within
  one context, one round trip and stop-at-first-error. The GPIB query
  ([query][pl-gpib-query]) is safe because the timeout is per-context state
  on the GPIB server, set in the device's own context
  ([connect][pl-gpib-connect]).
- **`%PYTHON%` is the node's interpreter, not the server's.** The node sets
  `PYTHON=sys.executable` ([start][pl-node-python]), and the template says
  so ([launchable-server.ini][pl-ini-python]). A per-server environment
  would have to be written into the command line.
- **A lock's lease is renewed only by locking again.** `DeviceWrapper.lock`
  resets the timer ([lock][pl-lock-renew]); ordinary calls do not. Other
  contexts are refused at once with `DeviceLockedError`
  ([selectedDevice][pl-locked-error]), not queued.

[pl-readme-moved]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/README.md#L54
[pl-readme-delphi]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/README.md#L12
[pl-readme-web]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/README.md#L14
[pl-qubit]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L43
[pl-config]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/CONFIG.md#L6-L20
[pl-reqs]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/requirements.txt#L1-L8
[pl-init]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/__init__.py#L20
[pl-backend]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/backend.py#L10-L13
[pl-units]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/units.py#L59-L61
[pl-types-flatten]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/types/types.py#L292-L296
[pl-numpy]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/types/types.py#L1322
[pl-flatten-packet]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/stream.py#L54-L62
[pl-packet-received]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/protocol.py#L231-L238
[pl-md5]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/protocol.py#L420-L430
[pl-packet]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/client.py#L320-L345
[pl-util-decay]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/util/__init__.py#L191-L205
[pl-qt4]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/util/qt4reactor.py#L5-L11
[pl-signal]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L42-L58
[pl-req]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L213-L252
[pl-req-lock]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L221-L229
[pl-single-thread]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L231-L236
[pl-req-loop]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L237-L248
[pl-traceback]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L254-L268
[pl-conflicts]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L278-L284
[pl-startup]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L349-L351
[pl-client]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L368-L385
[pl-magic]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L413-L419
[pl-expire]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L512-L544
[pl-log-signal]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L582-L585
[pl-threaded]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/server.py#L588-L623
[pl-setting]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/decorators.py#L35-L54
[pl-tag-rules]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/decorators.py#L91-L146
[pl-accepts]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/decorators.py#L148-L159
[pl-reginfo]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/decorators.py#L182-L184
[pl-lock]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/devices.py#L30-L73
[pl-lock-renew]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/devices.py#L46-L56
[pl-dev-expire]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/devices.py#L206-L216
[pl-locked-error]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/devices.py#L236-L237
[pl-select]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/devices.py#L240-L271
[pl-lock-setting]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/devices.py#L329-L344
[pl-gpib-connect]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/gpib.py#L41-L50
[pl-gpib-query]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/gpib.py#L66-L76
[pl-managed]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/gpib.py#L199-L243
[pl-msgid]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/gpib.py#L243
[pl-devmgr]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/gpib.py#L312-L331
[pl-node-config]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L29-L38
[pl-must-match]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L49-L54
[pl-log-length]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L94-L95
[pl-status-type]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L102-L103
[pl-proc-start]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L203-L261
[pl-stop]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L272-L304
[pl-node-output]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L332-L342
[pl-status]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L589-L599
[pl-refresh]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L604-L648
[pl-versions]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L663-L668
[pl-342]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L673-L676
[pl-node-python]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L687-L691
[pl-node-env]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L692-L694
[pl-outdated-settings]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L843-L865
[pl-outdated]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/__init__.py#L867-L884
[pl-timeout-default]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/server_config.py#L23
[pl-from-string]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/server_config.py#L28-L74
[pl-cmdline]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/server_config.py#L47-L54
[pl-find-block]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/server_config.py#L77-L94
[pl-ini-python]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/node/launchable-server.ini#L40-L42
[pl-test-server-block]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/servers/test_server.py#L17-L30
[pl-tag-s]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/servers/test_server.py#L103
[pl-tag-ms]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/servers/test_server.py#L120
[pl-bad-return]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/servers/test_server.py#L170-L173
[pl-template-name]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/servers/server_template.py#L25-L29
[pl-test-signal]: https://github.com/labrad/pylabrad/blob/cd0fea07f1b18b8431cff40cd2c348a55205839f/labrad/test/test_signal.py#L32-L46
[sl-readme-java]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/README.md#L12
[sl-readme-tls]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/README.md#L23-L24
[sl-hub-alloc]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/manager/src/main/scala/org/labrad/manager/Hub.scala#L78-L88
[sl-hub-connect]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/manager/src/main/scala/org/labrad/manager/Hub.scala#L103-L118
[sl-hub-expire]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/manager/src/main/scala/org/labrad/manager/Hub.scala#L186-L190
[sl-hub-route]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/manager/src/main/scala/org/labrad/manager/Hub.scala#L199-L218
[sl-handlers-lookup]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/manager/src/main/scala/org/labrad/manager/Handlers.scala#L324-L357
[sl-help]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/manager/src/main/scala/org/labrad/manager/Handlers.scala#L359-L387
[sl-named]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/manager/src/main/scala/org/labrad/manager/Handlers.scala#L426-L459
[sl-conninfo]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/manager/src/main/scala/org/labrad/manager/Handlers.scala#L575-L597
[sl-auth]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/manager/src/main/scala/org/labrad/manager/Manager.scala#L34-L44
[sl-reg-ctx]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/manager/src/main/scala/org/labrad/registry/Registry.scala#L136-L140
[sl-reg-cd]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/manager/src/main/scala/org/labrad/registry/Registry.scala#L175-L186
[sl-reg-int-cd]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/manager/src/main/scala/org/labrad/registry/Registry.scala#L188-L191
[sl-makehandler]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/core/src/main/scala/org/labrad/SettingHandler.scala#L54
[sl-infer]: https://github.com/labrad/scalabrad/blob/7cb7bcc17409d1a164b8c1826839628fa5817442/core/src/main/scala/org/labrad/SettingHandler.scala#L164
