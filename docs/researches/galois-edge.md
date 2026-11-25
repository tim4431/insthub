# Galois Edge

[Galois Edge](https://github.com/Galois-Labs/edge) is a daemon from Galois
Labs, a company. It puts every instrument on one computer behind one program.
AI agents reach the instruments over MCP (the Model Context Protocol, which
agents such as Claude Code use to call tools). Other programs use gRPC, and
existing PyVISA scripts use a separate `@galois` backend. The project is young
(version 0.1.0, Apache-2.0) and large: about 32,000 lines of Python, not
counting generated protobuf code, about 12,000 lines of Go in `cmd/` and
`internal/`, about 100 Python test files and 24 Go ones. We studied commit
[`ef87451`](https://github.com/Galois-Labs/edge/tree/ef874511364383366f186d7aa1a5cdc63a64d473)
(5 October 2026) from its code, in November 2026. Two parts were not in the
clone, so we judged them only from the outside: the `galois-profiles`
submodule, which parses the newer (v2) profile format, and `pyvisa-galois`,
which lives in another repository.

Of the projects we studied, it is the one built for AI agents first. Its good
ideas and its mistakes are both about letting an agent near real hardware.
The summary row is in [what we learned from other
projects](../design.md#what-we-learned-from-other-projects).

| Word | Meaning here |
|---|---|
| **Profile** | A YAML file for one instrument model: SCPI command templates, typed parameters with units and limits, flags such as `is_dangerous`. A generic handler in the daemon runs it. |
| **Backend** | A small object that claims instrument addresses and talks to them: real hardware, the demo, or the simulator. |
| **SDK wrapper** | A Python class around a vendor library, plus a list that describes its methods to agents. |
| **`destructiveHint`** | A standard MCP mark on a tool saying it may change or harm something. Agent clients can ask a person before using such a tool. |
| **JWT** | A signed token. Galois's cloud issues one to an agent, listing the tools it may use. |

## How it is built

| Area | What Galois Edge does |
|---|---|
| Process model | Two programs per computer. A Go *supervisor* owns the command line, the system service, configuration, an embedded Tailscale node, the link to Galois's cloud and a gRPC proxy. It restarts the engine when it crashes ([supervisor.go:1-5][sup]). A frozen Python *engine* owns all hardware I/O and runs the gRPC, WebSocket and MCP servers ([README:158][readme-how]). |
| Transport | gRPC with protobuf is the main contract: 25 calls, including raw SCPI (`SendCommand`, [edge.proto:20-21][proto-send]). MCP runs over HTTP. Command parameters travel as strings and are parsed on the server ([execute.py:45-50][ex-cmd]). |
| Discovery and naming | The daemon finds hardware itself: VISA scans, LAN lists with optional mDNS, USB hotplug, and a GPIB scanner that probes one address at a time and steps aside while commands wait ([trickle_scanner.py:4-14][trickle]). An instrument is named by its VISA address. No names are chosen by people. |
| Concurrency | One asyncio loop runs the servers; blocking I/O goes to thread pools. Each instrument has a `threading.Lock` ([command_handler.py:57-58][ch-lock]), not a queue, so callers and background pollers race for it. |
| Live data | Pulled, not pushed. The daemon re-runs a command marked `streamable` at a fixed interval. Each point carries a sequence number per stream, so lost points can be counted ([edge.proto:399-413][proto-point]). Over MCP, points arrive as progress notifications, and waveforms are fetched separately ([stream.py:1-13][stream-doc]). |
| Device abstraction | Three kinds side by side: YAML profiles run by a generic handler ([profile_schema.py:352-370][ps-cmd]), protocol drivers (Modbus, CAN, serial, ...), and vendor SDK wrappers. A *backend* Protocol routes each address: the first backend whose `handles()` says yes owns it ([backends/base.py:25-55][backend]). |
| Extension mechanism | Profiles are data, read from layered folders with written precedence ([profile_loader.py:166-216][pl-load]), and can also be pushed over gRPC. Code is in-tree only: protocols come from a fixed import list ([drivers/registry.py:61-65][reg-list]), SDK wrappers from a walk of the package's own folder ([sdk_tools.py:56-75][sdk-walk]), and backends are wired by hand ([main.py:147-169][main-backends]). |
| UI | No device UI on the computer itself, only a Windows tray icon ([README:201][readme-tray]). The real UI is Galois's browser app in the cloud. |
| Configuration | One `KEY=VALUE` file. The engine warns at start about unknown variables that look like its keys ([config.py:142-159][cfg-warn]), and a test keeps the Python and Go key lists in step ([test_known_vars_drift.py:1-10][drift]). Per-instrument settings live in the profile. |
| Errors | Results are dicts `{success, error}`, not exceptions. A bad parameter reports an SCPI error code and the field at fault ([validation.py:17-32][val]). An unknown name gets up to five suggestions ([navigate.py:20-22][nav-fail]). |
| Observability | `galois-edge doctor` runs health checks. An optional trace writes one JSON-lines file per run, fed by observers on the command handler ([main.py:465-478][main-trace]). It skips SDK and protocol-driver commands ([tracing.py:8-10][trace-scope]) and records no caller. |
| Security | An optional gRPC bearer token, compared in constant time ([grpc_server.py:3467][grpc-cmp]). MCP binds to 127.0.0.1 by default ([config.py:224-227][cfg-host]). The Tailscale network is the boundary. Calls relayed by the cloud carry a JWT, but it is checked only inside tools that call `ctx.authorize`, and a call without a token passes ([context.py:62-81][ctx-auth]). |
| Footprint | 19 hard Python dependencies, among them grpcio, pymodbus, asyncua, uvicorn, pydantic and pyjwt ([pyproject.toml:11-31][deps]). The Go side embeds Tailscale ([go.mod:10][gomod]). Three servers listen on five ports ([README:143-144][readme-ports]). |

## What insthub takes

A second reading checked every claim against both codebases and argued
against every proposal. This section follows its verdicts. Most lessons came
out narrower than first proposed. "Step" means a row of [the plan, in
order](../design.md#the-plan-in-order); the last column is the state of
insthub in late November 2026.

| Lesson | Verdict | Where design.md decides it | Built? |
|---|---|---|---|
| A dangerous flag, and one extra agent tool for flagged commands | adapt | [dangerous commands](../design.md#a-generic-ui-good-enough-for-most-devices); [rules inside an extension](../design.md#rules-inside-an-extension), rule 6 | yes, step 6 |
| Mark simulated devices | adopt | [how a device finds its extension](../design.md#how-a-device-finds-its-extension) | yes, step 4 |
| Contributions declared as data, loaded late, with written precedence | adapt | [the manifest](../design.md#the-manifest); [where insthub looks](../design.md#where-insthub-looks-for-extensions); [when code is loaded](../design.md#when-code-is-loaded) | no, step 7 |
| A stable identity that binds a UI to its device | adopt | [how a device finds its extension](../design.md#how-a-device-finds-its-extension) | yes, step 4 |
| Tests that fail when a boundary drifts | adopt | [keeping the core light](../design.md#keeping-the-core-light) | yes, steps 1 and 2 |
| Each half of an extension runs in its own Python | adopt | [two halves, each where its libraries are](../design.md#two-halves-each-where-its-libraries-are) | no, step 8 |
| "Did you mean", and a short describe for huge devices | adopt | the `difflib` rule in [keeping the core light](../design.md#keeping-the-core-light); the short describe is not in design.md yet | "did you mean" only |
| Long operations as a device pattern, not a sweep subsystem | adapt (docs only) | the experiment-runner row of [ideas we decided against](../design.md#ideas-we-decided-against) | no guide section yet |
| Policy at one entry point; a call observer later | consider later | not in design.md yet | no |
| A TCP password checked once per connection | consider later | "Passwords for TCP" under [the plan, in order](../design.md#the-plan-in-order) | no; SSH forwarding instead |

### A dangerous flag, gated by one extra tool

Galois declares `is_dangerous` once per command: in the profile
([profile_schema.py:362][ps-danger]), or in an SDK wrapper's list, where
`enable_output` is dangerous and `disable_output` is not
([dps150_wrapper.py:27-58][dps]). The flag becomes the `destructiveHint` of
that command's MCP tool ([dynamic_tools.py:352-354][dt-hint]) and the word
"destructive" in its description ([dynamic_tools.py:420-431][dt-desc]). It is
also checked against the JWT's `danger_allow` ([auth.py:214-217][auth-danger]).
Why it matters: agent clients such as Claude Code grant permission per tool
name. A separate tool for dangerous actions lets a person allow everything
else and still be asked.

insthub kept its six generic tools and added a seventh, `call_dangerous`.
Flags come from `@command(dangerous=True)` and `@prop(dangerous=True)`; from
`wrap(..., dangerous=[...])` or the `.toml` for what cannot be decorated; or
from `dangerous = true` for a whole driver nobody has vetted. A config file
only adds marks, never removes one. A name the device does not have raises
when its tables are made, so a typo cannot go unnoticed. The agents' `call`
and `set` check a name after normalising it, so
`call(dev, "$set", ["laser_power", 5])` is refused just like `set`. That is
the hole Galois left open (see the next section). The window asks before
running a flagged command. This guards against mistakes with a person in the
loop. It is not security, and hard limits stay in the driver.

### Mark simulated devices

Galois's backends declare `simulated` as a class attribute
([backends/base.py:28][backend-sim]). With an option on, instrument lists and
status say which instruments are simulated ([discovery.py:77-78][disc-mark]).
Traces record `provenance: sim` ([tracing.py:295-298][trace-sim]), and tools
that exist only for the simulator say "SIMULATION ONLY"
([sim_control.py:42-55][simctl]).

insthub puts the flag on the device and lets each instance set it
(`self.simulated = sim`), because one class can be real or simulated. The HP
8562A project's `SpectrumAnalyzer(sim=True)` is the same class as the real one
and keeps the name `sa` in both modes. A wrapped driver class that has
`simulated = True` makes its device say so by itself. `describe()` carries
`simulated`, the tile shows SIM, and `list_devices` shows it to agents.

### Contributions as data, loaded late, with written precedence

Three Galois habits carry over.

1. **Precedence is written down.** Bundled profiles come first, then each
   backend's folders, then deployed ones. A later copy of a bundled profile is
   logged as "shadowed by" and ignored, so an add-on cannot quietly replace a
   built-in ([profile_loader.py:169-184][pl-doc], [208-216][pl-accept]).
2. **One small object contributes several things.** The backend Protocol gives
   a routing test, a `simulated` flag, a profile hint and profile folders
   ([backends/base.py:25-55][backend]).
3. **Heavy code loads when used.** A vendor SDK is imported at connect time,
   with a `pip install` hint when it is missing
   ([sdk_executor.py:62-69][sdk-import]). The demo backend loads only when
   demo mode asks for it ([main.py:150-154][main-demo]).

Galois did not build loading of code from outside its own package (see the
next section). insthub takes the three habits and adds that: a manifest read
as data, the first one found wins and the others are reported as hidden, and
extension code is imported only by the program that needs it. design.md goes
further than the reviewed proposal in one place: `insthub serve` never looks
for extensions, so a server is always built from its own file.

### A stable identity for binding a UI to its device

A Galois backend can name the profile for an address outright; a regex on
`*IDN?` is only the fallback ([backends/base.py:51-52][backend-hint]). The
lesson: bind on a declared name, not a guess. A class name fails, because the
real and simulated analyzers are `HP8562A` and `SimHP8562A`. A module name
fails, because two projects each have a `bus_server.py`. The reviewer proposed
`Device.kind`; design.md chose `extension`, matched against a manifest's `id`.

### Tests that fail when a boundary drifts

Galois guards its contracts with small tests marked critical. One pins the
backend Protocol, signatures included, to the contract's copy
([test_backend_protocol.py:26-35][tbp-match]). One fails if the runtime ever
mentions the submodule ([test_backend_protocol.py:38-41][tbp-sub]). One fails
when the Go config gains a key the Python list lacks
([test_known_vars_drift.py:1-10][drift]). insthub's lightness used to rest on
discipline alone. Tests of the same kind now make it a rule.
`tests/test_footprint.py` imports insthub in a fresh Python and fails if Qt,
matplotlib, `importlib.metadata`, `asyncio` or an optional insthub module was
loaded. `test_every_option_is_a_reserved_key` in `tests/test_config.py` fails
when a `wrap()` option or `Device` setting is missing from the reserved config
keys, so a new option never slips through to a driver's constructor.

### Each half in its own Python

Galois sidesteps the question. It freezes one environment that holds
everything and ships it as one binary. That works for one vendor, not for a
lab with one virtual environment per instrument and insthub in a tool
environment of its own. Only the late import with an install hint carries
over. insthub's answer, a UI half started as its own program with the
extension's Python, is in design.md.

### "Did you mean", and a short describe for huge devices

Galois's capability listing has three levels of detail and pages, and shows a
summary by default for large instruments ([discovery.py:82-111][disc-cap],
[239-259][disc-for]). An unknown name returns up to five suggestions
([navigate.py:20-22][nav-fail]). Agents do better with a summary first and
details on request.

insthub takes the small part. Built: an unknown command or property gets "did
you mean" from `errors.did_you_mean`, which imports `difflib` only on the
error path. Still to do: `DeviceProxy` should pass `name=` and `obj=` to
`AttributeError`, so Python 3.12 suggests names by itself. MCP `describe`
should gain an optional `command` argument; above about 40 commands it would
return signatures and first doc lines only. No paging and no navigation
tools. This matters here: the lab's M8195A driver, wrapped whole, publishes
over 200 methods.

### Long operations as a device pattern

Galois runs sweeps (magnet and temperature ramps) as daemon tasks:
`start_sweep` returns an id, a task polls until the instrument reports idle,
and a command marked `requires_sweep` cannot be set directly
([execute.py:68-72][ex-req]). insthub needs no subsystem for this. A guide
section will show the pattern with existing pieces: `start_ramp(target, rate)`
checks limits; a disabled `@periodic` step moves one bounded step and
publishes state; `@command(concurrent=True) abort()` sets a flag the step
reads; the setter of a dangerous quantity raises "use start_ramp". The HP
8562A project already handles its sweeps this way, with a serial poll from a
periodic task. Galois's own version is half finished (see the next section).

### Policy at one entry point; a call observer later

Galois's command handler keeps observers. They are called after the lock is
released, and their errors are logged, never raised
([command_handler.py:60-84][ch-obs], [164-176][ch-exec]). But the hook sits
too low: SDK and protocol-driver commands never pass it
([tracing.py:8-10][trace-scope]), and events carry no caller. Its permission
checks have the opposite problem: each tool must remember to call them.

For insthub the part worth keeping now is a placement rule. Interlocks that
apply to every client go in the driver, or at `Server._dispatch`, the one
route every call takes. Confirmation prompts go at each front end's single
entry point (`Tools.run` in `mcp.py`, the window's command action). Neither
goes into individual tools. An observer hook at `_dispatch` waits for a real
user, such as an e-logbook or "who changed the synth overnight?". When it
comes, it receives the arguments, runs on the device thread (so it must only
queue work), and is attached with `[server] observers = ["module:fn"]`.

### A TCP password, checked once per connection

Galois's gRPC token check is one interceptor that wraps every handler and
compares in constant time ([grpc_server.py:3419-3433][grpc-int],
[3467][grpc-cmp]). A blank bind host never reaches the web server, which would
otherwise listen on every interface ([mcp/server.py:79-85][mcp-host]). That
single check is what Galois got right. For insthub: SSH forwarding first,
which also encrypts and needs no code. Later, if needed, a nonce in insthub's
hello and an HMAC answer, checked once per connection. No JWT, and no
permission lists per call.

## What insthub leaves, and why

| Galois does | Evidence | Why insthub does not |
|---|---|---|
| One MCP tool per command, renamed when a second instrument of the same model appears, and none at all above a size limit | [dynamic_tools.py:1-31][dt-doc], [155-176][dt-sib], [190-197][dt-max] | An agent's plan breaks when a device is plugged in, and its context grows with every device. insthub's seven generic tools cost the same for one device as for fifty ([agents.md](../agents.md#why-it-is-built-this-way), [ideas we decided against](../design.md#ideas-we-decided-against)). |
| A declarative profile engine in the core | `CommandConfig` mixes SCPI, SDK, CAN, sweep and waveform fields ([profile_schema.py:352-370][ps-cmd]); the format needs daemon code anyway ([profile_schema.py:90-109][ps-preamble]) | The core would know every protocol (rule 1). insthub drivers are Python, and `describe()` reads them. No-code SCPI devices, if wanted, are an extension that generates ordinary `Device` classes ([ideas we decided against](../design.md#ideas-we-decided-against)). |
| Safety checks inside each tool | `ctx.authorize` is called in four places ([dynamic_tools.py:447][dt-auth1], [540][dt-auth2]; [sdk_tools.py:167][sdk-auth]; [stream.py:72][stream-auth]). `execute_command`, `execute_sequence` and `send_scpi` ([execute.py:45][ex-cmd], [211][ex-seq], [345][ex-scpi]) and both sweep tools ([sweep.py:78][sw-start], [200][sw-stop]) never call it | A caller refused the typed dangerous tool does the same thing through `send_scpi`. Checks belong at one entry point, after names are normalised. |
| Raw passthrough | `send_scpi` "bypasses profile validation" ([execute.py:335-344][ex-scpi-doc]). `ProxySDKCall` calls `getattr(client, name)` on connected clients ([grpc_server.py:2564-2567][grpc-getattr]); the allow-list and underscore check guard only the fallback path ([grpc_server.py:2589-2625][grpc-fallback]) | Every range and danger flag becomes advice. insthub has no call-any-attribute built-in, and wrapped drivers must exclude or flag raw I/O ([rules inside an extension](../design.md#rules-inside-an-extension), rule 6). |
| The same facts stated twice | An SDK wrapper's tool list repeats its methods ([dps150_wrapper.py:27-134][dps-all]). Its bounds are checked only in the MCP handler ([sdk_tools.py:172-189][sdk-bounds]), again for sequences ([dynamic_tools.py:545-557][dt-seqcheck]), and are missing from gRPC ([edge.proto:187-195][proto-param]) | Each front end ends up with different rules. insthub derives descriptions from the code and enforces limits in the device. |
| Safety promised but not done | `stop_sweep(hold=True)` reports "holding" ([sweep.py:220-223][sw-hold]) while the poll loop still sends the stop command ([sweep.py:239-253][sw-loop]). `start_sweep` checks no bounds ([sweep.py:78-164][sw-body]). The README promises the "same audit log" ([README:53][readme-audit]), which does not exist | Wrong safety claims are worse than none. Document only what is enforced, and test it. |
| Plug-ins that need an edit of the package | [drivers/registry.py:61-65][reg-list], [sdk_tools.py:56-75][sdk-walk], [main.py:147-169][main-backends] | A third party must fork, and every wrapper is imported at start. insthub finds extensions in folders, and later through entry points ([where insthub looks](../design.md#where-insthub-looks-for-extensions)). |
| A heavy core | 19 hard dependencies ([pyproject.toml:11-31][deps]); Tailscale, and through it the AWS SDK ([go.mod:10-26][gomod-aws]); Claude Code transcript ingestion in the instrument daemon ([claude.go:19-25][claude]) | Everything imports insthub (rule 3). Optional parts are extras or extensions, and unrelated features stay out ([keeping the core light](../design.md#keeping-the-core-light)). |
| Hardware definitions written from the network without path checks | `DeployProfile` joins request strings into file paths ([grpc_server.py:2989-2992][grpc-deploy1], [3041][grpc-deploy2]) | If insthub ever accepts config over the network: check names, and keep it off by default. Today devices cannot be added over the bus ([ideas we decided against](../design.md#ideas-we-decided-against)). |
| A lock per instrument, raced by pollers | [command_handler.py:164-169][ch-exec2]; sweep polls take the same lock ([sweep.py:255-264][sw-poll]) | No order and no fairness. insthub's per-device waiting line, with periodic tasks between requests, is the better model ([the whole picture](../architecture.md#the-whole-picture)). |
| Permissions per call, from a JWT the cloud issues | [auth.py:205-231][auth-full] | It assumes a cloud identity service. Out of scope. |

## Checked and corrected

- **"insthub has no raw passthrough, because it dispatches only names in its
  command table."** True of the core, false in practice. `wrap()` publishes
  every public method unless told otherwise. The lab's M8195A config wraps
  its driver without `exclude`, which publishes `write`, `query` and
  `attr(name)`, a `getattr` shim. The Red Pitaya driver publishes `tx_txt` and
  `txrx_txt`, and the HP 8562A project needed a `DRIVER_INTERNALS` list. So
  the rule against a call-anything built-in stays, and design.md adds
  exclude-or-flag for wrapped drivers ([the manifest](../design.md#the-manifest),
  [rules inside an extension](../design.md#rules-inside-an-extension), rule 6).
- **"`SDKExecutor.call_method` lets a client call any method."** Overstated.
  It is reached only from typed MCP tools, whose method names come from the
  wrapper's own list ([sdk_tools.py:204-208][sdk-call]). The open path is
  `ProxySDKCall` on a connected client.
- **"Health methods are exempt from the gRPC token."** Only `Ping` is
  ([grpc_server.py:3394-3398][grpc-exempt]).

[sup]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/internal/supervisor/supervisor.go#L1-L5
[readme-how]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/README.md#L158
[readme-ports]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/README.md#L143-L144
[readme-tray]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/README.md#L201
[readme-audit]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/README.md#L53
[proto-send]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/proto/edge/v1/edge.proto#L20-L21
[proto-point]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/proto/edge/v1/edge.proto#L399-L413
[proto-param]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/proto/edge/v1/edge.proto#L187-L195
[ex-cmd]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/execute.py#L45-L50
[ex-req]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/execute.py#L68-L72
[ex-seq]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/execute.py#L211
[ex-scpi]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/execute.py#L345
[ex-scpi-doc]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/execute.py#L335-L344
[trickle]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/trickle_scanner.py#L4-L14
[ch-lock]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/command_handler.py#L57-L58
[ch-obs]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/command_handler.py#L60-L84
[ch-exec]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/command_handler.py#L164-L176
[ch-exec2]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/command_handler.py#L164-L169
[stream-doc]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/stream.py#L1-L13
[stream-auth]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/stream.py#L72
[ps-cmd]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/profile_schema.py#L352-L370
[ps-danger]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/profile_schema.py#L362
[ps-preamble]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/profile_schema.py#L90-L109
[backend]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/backends/base.py#L25-L55
[backend-sim]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/backends/base.py#L28
[backend-hint]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/backends/base.py#L51-L52
[pl-load]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/profile_loader.py#L166-L216
[pl-doc]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/profile_loader.py#L169-L184
[pl-accept]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/profile_loader.py#L208-L216
[reg-list]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/drivers/registry.py#L61-L65
[sdk-walk]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/sdk_tools.py#L56-L75
[sdk-auth]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/sdk_tools.py#L167
[sdk-bounds]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/sdk_tools.py#L172-L189
[sdk-call]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/sdk_tools.py#L204-L208
[sdk-import]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/sdk_executor.py#L62-L69
[main-backends]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/main.py#L147-L169
[main-demo]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/main.py#L150-L154
[main-trace]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/main.py#L465-L478
[cfg-warn]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/config.py#L142-L159
[cfg-host]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/config.py#L224-L227
[drift]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/tests/test_known_vars_drift.py#L1-L10
[tbp-match]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/tests/test_backend_protocol.py#L26-L35
[tbp-sub]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/tests/test_backend_protocol.py#L38-L41
[val]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/validation.py#L17-L32
[nav-fail]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/navigate.py#L20-L22
[trace-scope]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/tracing.py#L8-L10
[trace-sim]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/tracing.py#L295-L298
[grpc-cmp]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/grpc_server.py#L3467
[grpc-int]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/grpc_server.py#L3419-L3433
[grpc-exempt]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/grpc_server.py#L3394-L3398
[grpc-getattr]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/grpc_server.py#L2564-L2567
[grpc-fallback]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/grpc_server.py#L2589-L2625
[grpc-deploy1]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/grpc_server.py#L2989-L2992
[grpc-deploy2]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/grpc_server.py#L3041
[ctx-auth]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/context.py#L62-L81
[mcp-host]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/server.py#L79-L85
[deps]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/pyproject.toml#L11-L31
[gomod]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/go.mod#L10
[gomod-aws]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/go.mod#L10-L26
[claude]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/internal/cli/claude.go#L19-L25
[dps]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/sdk_wrappers/dps150_wrapper.py#L27-L58
[dps-all]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/sdk_wrappers/dps150_wrapper.py#L27-L134
[dt-doc]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/dynamic_tools.py#L1-L31
[dt-sib]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/dynamic_tools.py#L155-L176
[dt-max]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/dynamic_tools.py#L190-L197
[dt-hint]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/dynamic_tools.py#L352-L354
[dt-desc]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/dynamic_tools.py#L420-L431
[dt-auth1]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/dynamic_tools.py#L447
[dt-auth2]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/dynamic_tools.py#L540
[dt-seqcheck]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/dynamic_tools.py#L545-L557
[auth-danger]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/auth.py#L214-L217
[auth-full]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/auth.py#L205-L231
[disc-mark]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/discovery.py#L77-L78
[disc-cap]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/discovery.py#L82-L111
[disc-for]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/discovery.py#L239-L259
[simctl]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/sim_control.py#L42-L55
[sw-start]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/sweep.py#L78
[sw-stop]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/sweep.py#L200
[sw-body]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/sweep.py#L78-L164
[sw-hold]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/sweep.py#L220-L223
[sw-loop]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/sweep.py#L239-L253
[sw-poll]: https://github.com/Galois-Labs/edge/blob/ef874511364383366f186d7aa1a5cdc63a64d473/src/galois_edge/mcp/tools/sweep.py#L255-L264
