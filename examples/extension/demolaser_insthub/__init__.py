"""The demo laser's part for insthub: its device (device.py) and its windows (ui/).  Its modules have
generic names, so they live in a package whose name is the extension's own (rule 4 of
docs/design.md#rules-inside-an-extension).  It imports nothing, so a server that loads the device
never loads the windows' Qt.
"""

#: The manifest's id (insthub-extension.toml): the device announces it (Laser.extension), so that
#: insthub finds this extension's windows for it, and the app knows a laser of its own by it.
EXTENSION = "demo-laser"
