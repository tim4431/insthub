# Research notes

This folder holds what we learned from eleven other lab-control projects
before designing insthub's extensions. It is for people and AI agents who
change insthub. The decisions are not here: they live in [the
design](../design.md). These notes give the evidence behind them.

## Why we did it

The owner set insthub [three rules](../design.md#the-three-rules): it knows
no instrument, everything about an instrument belongs in an extension, and it
stays light because every lab program imports it. The second rule needs a
framework that finds extensions and runs their parts, and insthub does not
have one yet. Before designing it, we wanted to know how other projects
package instrument code, where their windows run, and what made them heavy.

## How we did it

- **Which projects.** Eight come from [similar-projects.md](similar-projects.md),
  the comparison the owner started from. Three more (qudi, ScopeFoundry, and
  npe2 with VS Code) are marked *extra*: they were chosen not because they
  resemble insthub, but for how they load plug-ins.
- **Two readings of each.** One agent studied each project from its code and
  made proposals for insthub. A second agent, *the check*, verified every
  claim against both that project's code and insthub's, and argued against
  every proposal. Where the two disagree, the notes follow the check.
- **What survived.** 105 proposals: 21 kept as proposed, 83 narrowed, 1
  dropped.
- **When, and on what.** November 2026, on shallow clones (copies with only
  recent history) at the commit each note names. Nothing from them was run.
  VS Code was studied from its documentation only.
- **Evidence.** Each claim links to the lines it rests on, as a permalink
  at the studied commit, so the link keeps showing the same code. Most are
  on GitHub; two ARTIQ packages live on git.m-labs.hk, and VS Code claims
  link to its documentation.

## The notes

| Project | What it is | Main takeaway for insthub | Note |
|---|---|---|---|
| nspyre | RPyC instrument server, a pickle data server and PyQt6 widgets (Awschalom group, Chicago) | A widget named by a string and built only when someone opens it. Leave RPyC proxies in drivers, the data broker, and a GUI imported with the package. | [nspyre.md](nspyre.md) |
| PyLabNet | One 58,000-line repository of drivers, rpyc servers, launchers and GUIs (Lukin group, Harvard) | Mostly a warning. Take one place to read every program's log, and one list of remote servers. Leave a device spread over many folders. | [pylabnet.md](pylabnet.md) |
| instrumentserver | QCoDeS instruments in one server, used over the network through proxies, with Qt windows | Closest in purpose. A UI named by a string with a generic fallback; units, limits and choices in descriptions; polling set in the config. Its loader works only because GUI and drivers share one environment. | [instrumentserver.md](instrumentserver.md) |
| PyroLab | Instruments as Pyro5 remote objects, with a nameserver and drivers inside the package (CamachoLab, BYU) | Policies, not machinery: refuse a misspelled config key before anything starts, say "ready" only once a server serves, stop restarting a server that keeps crashing. | [pyrolab.md](pyrolab.md) |
| LabRAD | A central Manager that every server and client connects to | Manifests read as text, never run; servers started from a command line; servers older than their manifest are shown. Leave the central Manager. | [labrad.md](labrad.md) |
| Galois Edge | A Go and Python daemon that exposes instruments to AI agents over MCP and gRPC | A dangerous flag on commands, gated by one extra agent tool; simulated devices marked as such; contributions declared as data. Leave a profile engine in the core. | [galois-edge.md](galois-edge.md) |
| labscript suite | Hardware-timed shots for cold-atom labs; BLACS tabs and workers | The closest precedent: a device is a bundle of parts named by import strings and loaded in different programs. Leave discovery through a `.pth` file and module names that collide. | [labscript.md](labscript.md) |
| ARTIQ | M-Labs' control system; we read only its host half (master, controllers, dashboard) | UIs as programs of their own on a tiny base class; a list of features in the first message; keep the output of every program you start. Leave embedded foreign windows. | [artiq.md](artiq.md) |
| qudi *extra* | Hardware, logic and GUI modules joined by a config file (Ulm IQO) | A GUI adds nothing its logic cannot do; a simulator for nearly every interface. Leave a Qt base class and one package name shared by every add-on. | [qudi.md](qudi.md) |
| ScopeFoundry *extra* | One-window microscope programs, one `HW_*` repository per instrument | Typed settings with limits generate most of the UI; `get_`/`set_` pairs become settings. Leave a "pure" driver that imports Qt through a helper. | [scopefoundry.md](scopefoundry.md) |
| npe2 and VS Code *extra* | napari's plug-in manifests, and the VS Code extension model they copy | Find a manifest without running code, keep `"module:attr"` strings, import on first use. Run each half of an extension where its libraries are, joined by a protocol. | [npe2-and-vscode.md](npe2-and-vscode.md) |

Most notes share one shape: what the project is and the commit studied, a
short list of the words they use, how it is built, what insthub takes, what
it leaves and why, and what the check corrected.

## Across all eleven

[extension-models.md](extension-models.md) compares how the projects package
instrument code, find it, import it, and where its UI runs. Its main finding:
joining a driver and its UI in one package forces one environment (one
Python installation and its packages) for the program that loads them,
every UI and every driver. nspyre, instrumentserver, labscript's BLACS, qudi and ScopeFoundry
all work this way. insthub cannot, because each instrument's project has its
own environment and the window's has no drivers. That is why an insthub
extension has [two
halves](../design.md#two-halves-each-where-its-libraries-are), joined by the
bus.

## Where the decisions live

| Question | In the design |
|---|---|
| What we took and left from each project | [what we learned from other projects](../design.md#what-we-learned-from-other-projects) |
| What an extension is | [extensions](../design.md#extensions) |
| What we built first, and what comes next | [the plan, in order](../design.md#the-plan-in-order) |
| Ideas we turned down, with reasons | [ideas we decided against](../design.md#ideas-we-decided-against) |
| What is still undecided | [open questions](../design.md#open-questions) |

Steps 1 to 6 of the plan are done. Extensions themselves (manifests, then
starting UIs: steps 7 and 8) are not built yet.

If a note and the design disagree, the design wins: it holds the decisions,
and the notes only the evidence. To add a note, keep the shape above, cite only lines you have opened
in the clone, and put any decision in the design rather than here.
