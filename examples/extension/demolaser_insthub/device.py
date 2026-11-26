"""The demo laser on the bus: its driver, DemoLaser, wrapped, with what insthub's window, AI agents
and the extension's own windows need to know about it (docs/design.md#extensions).

The driver knows nothing of insthub.  This class adds what the bus needs: which extension the
device belongs to, that it is simulated, the units and limits of its settings, a value read every
second, and what is dangerous.  It imports insthub's core and the driver, never Qt: a server often
runs as a background service, without a screen.
"""
from __future__ import annotations

from demolaser import PULSES, WAVELENGTHS, DemoLaser

import insthub

from . import EXTENSION


class Laser(insthub.Wrapped):
    """The DL-100 demo laser: power, wavelength, mode and emission as settings, its temperature
    read every second, and pulses."""

    extension = EXTENSION  # the manifest's id: how insthub finds the windows of this device
    extension_version = "0.1"  # the manifest's version (`insthub check` warns when they differ)
    dangerous = ("emission", "pulse")  # they put out light: agents need call_dangerous, the window asks first

    def __init__(self, name: str | None = None, port: str = "sim", max_power: float = 50.0):
        super().__init__(DemoLaser, name, kwargs={"port": port, "max_power": max_power},
                         accessors=True,  # get_power() with set_power(mw) is the setting power, and so on
                         exclude=["write"])  # its raw protocol could turn the beam on unasked: off the bus
        self.simulated = port == "sim"  # marked SIM everywhere: its output is never taken for a measurement
        # Each read every second, and published on a stream of its name: live values for tiles and the panel.
        self.poll = {"temperature": 1.0, "power": 1.0}
        self.set_meta("power", unit="mW", min=0, max=max_power, step=0.1)  # (its limit is this laser's own)
        self.set_meta("wavelength", unit="nm", min=WAVELENGTHS[0], max=WAVELENGTHS[1], step=0.01)
        self.set_meta("pulse.ms", unit="ms", min=PULSES[0], max=PULSES[1])
