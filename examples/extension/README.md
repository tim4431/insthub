# A reference extension: the demo laser

This folder is a complete insthub extension, small enough to read in a few
minutes. It puts the DL-100, an imaginary diode laser, on the bus, with a panel
and an app for it. Its driver simulates the laser, so it runs without
hardware. Copy it to start an extension of your own.

[The design](../../docs/design.md#extensions) explains what an extension is
and why it is built this way; [the guide](../../docs/guide.md#extensions)
says how to use one.

## What is in it

| File | What it is | What it shows |
|---|---|---|
| `insthub-extension.toml` | the *manifest*: what the folder adds to insthub, as data | the extension's `id`, the device the window's **New server** dialog offers, the two UIs |
| `demolaser.py` | the driver: plain Python, with no insthub and no Qt | a module name of its own (`demolaser`), so it may stay at the top of the folder |
| `demolaser_insthub/device.py` | `Laser`, the driver wrapped for the bus | which extension it belongs to (`extension`, the manifest's `id`) and its version; that it is simulated; units and limits (`set_meta`), and choices from the driver's `Literal` annotation; `get_X`/`set_X` pairs as settings (`accessors`); values read every second (`poll`); what is dangerous; the raw protocol kept off the bus (`exclude`) |
| `demolaser_insthub/ui/panel.py` | the panel: `make(device, parent=None)` returns its widget, and insthub puts it in a window | a slider sent through `qt.coalesce`, live values through `qt.watch`, the rest as insthub's ready-made `Controls` |
| `demolaser_insthub/ui/__main__.py` | the app: a whole program, `python -m demolaser_insthub.ui --address ADDRESS --name NAME` | given an address, it only attaches; without one, it hosts a laser itself when none runs |
| `laser.toml` | a server for the laser, as **New server** writes one | `path`, the folder the device's modules are in, and its constructor's arguments |

The modules with generic names (`device`, `ui`) are inside a package named
after the extension, `demolaser_insthub`: Python keeps one module of a name
per program, so two extensions that each had a top-level `device.py` could
not be used together.

## Try it

From this folder:

```bash
uv venv && uv pip install -e "../..[gui]"    # its Python: a .venv here, with insthub and Qt
insthub check . --import                     # what is wrong with it: nothing, it should say
insthub ext add .                            # insthub now knows it
.venv/bin/insthub serve laser.toml           # terminal 1: the laser's server, until Ctrl-C
```

(In insthub's own checkout, `ln -s ../../.venv .venv` borrows the environment
insthub's tests run in, instead of making one.)

Then, in another terminal:

```bash
insthub info laser                           # units, limits, choices, SIM, what is dangerous
insthub open laser panel                     # the panel, in a window of its own
insthub open laser app                       # the app
```

In the window (`insthub gui`), the laser's tile is marked SIM, its page has
**Open Laser panel** and **Open Laser app**, and **Add → New server...** offers
"Demo laser: a simulated diode laser, no hardware needed". `insthub ext remove .`
takes the extension off your list again.

In `laser.toml`, `path = "."` means the file's own folder (a relative `path`
counts from there), so the server runs from any folder, as a service does.

## Make it yours

1. Choose an `id` for your extension, and give your modules names of their
   own: rename `demolaser.py` and `demolaser_insthub/`, and update the names
   in the manifest, in `laser.toml` and in the imports. Put the `id` in the
   manifest and in `EXTENSION` in `demolaser_insthub/__init__.py`: the device
   announces it, and the app attaches only to a device that does.
2. Put your driver in place of `demolaser.py`. Keep insthub and Qt out of
   it, and import its vendor library in its constructor (or guard the import),
   so that it imports cleanly where that library is missing.
3. In `device.py`, say what your instrument takes: units and limits, what is
   read periodically, what is dangerous. Keep `extension_version` equal to
   the manifest's `version`: `insthub check` warns when they differ.
4. Keep the panel, the app, both or neither, and remove the `[ui.*]` table of
   what you drop. A device without a UI of its own still gets the window's
   device page, which is made from what it says about itself.
5. Run `insthub check . --import` again.

A packaged extension keeps its UI's libraries in an extra, so that a server's
environment installs only the server half: `dependencies = ["insthub @
git+...", ...]` and `gui = ["insthub[gui] @ git+..."]` under
`[project.optional-dependencies]`.
