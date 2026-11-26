"""The driver of the DL-100, an imaginary diode laser controller: plain Python, with no insthub
and no Qt in it (docs/design.md#rules-inside-an-extension), so a script can use it as it is:

    from demolaser import DemoLaser

    laser = DemoLaser()
    laser.set_power(12.5)       # mW
    laser.set_emission(True)    # the beam is on
    print(laser.temperature())  # °C

There is no DL-100: on the port "sim", the driver simulates one, so this example runs anywhere.
A real driver opens its port in the constructor and imports its vendor library there (or guards
the import at the top of the module), so that the module imports cleanly where the library is
missing, as `insthub check --import` and tests need.  Its module name, `demolaser`, is unique, so
it may stay at the top of the extension's folder (rule 4).
"""
from __future__ import annotations

import math
import time
from typing import Literal

WAVELENGTHS = (1530.0, 1565.0)  #: nm: what it can be tuned to
PULSES = (0.1, 1000.0)  #: ms: the shortest and the longest pulse
AMBIENT = 25.0  #: °C: the diode's temperature with the beam off
HEATING = 0.2  #: °C per mW of output: how much warmer the beam makes it
SETTLING = 4.0  #: s: how fast its temperature follows


class DemoLaser:
    """A DL-100 on `port` ("sim": a simulated one), that puts out at most `max_power` mW."""

    def __init__(self, port: str = "sim", max_power: float = 50.0):
        if port != "sim":  # (a real driver opens the port here, and raises OSError if it cannot)
            raise OSError(f"no DL-100 on {port}: this demo driver only simulates one (port 'sim')")
        self.port, self.max_power = port, float(max_power)
        self._power, self._wavelength, self._mode, self._emission = 1.0, 1550.0, "cw", False
        self._temperature, self._since = AMBIENT, time.monotonic()

    def close(self) -> None:
        self.set_emission(False)  # (a real one: the beam off, then the port closed)

    def identify(self) -> str:
        """The controller's model and port."""
        return f"DL-100 demo laser controller on {self.port}"

    def get_power(self) -> float:
        """The output power it is set to, in mW."""
        return self._power

    def set_power(self, mw: float) -> float:
        """Set the output power in mW; returns the power it took (0 to max_power, in 0.1 mW steps)."""
        self._settle()
        self._power = round(min(max(float(mw), 0.0), self.max_power), 1)
        return self._power

    def get_wavelength(self) -> float:
        """The wavelength it is tuned to, in nm."""
        return self._wavelength

    def set_wavelength(self, nm: float) -> None:
        """Tune it, in nm."""
        low, high = WAVELENGTHS
        if not low <= nm <= high:
            raise ValueError(f"{nm} nm is outside {low:g} to {high:g} nm")
        self._wavelength = float(nm)

    def get_mode(self) -> Literal["cw", "pulsed"]:
        """Continuous ("cw"), or pulsed (for pulse())."""
        return self._mode

    def set_mode(self, mode: Literal["cw", "pulsed"]) -> None:
        """Continuous ("cw"), or pulsed (for pulse())."""
        if mode not in ("cw", "pulsed"):
            raise ValueError(f"mode {mode!r}: 'cw' or 'pulsed'")
        self._mode = mode

    def get_emission(self) -> bool:
        """Whether the beam is on."""
        return self._emission

    def set_emission(self, on: bool) -> None:
        """Turn the beam on or off."""
        self._settle()
        self._emission = bool(on)

    def pulse(self, ms: float = 10.0) -> None:
        """Turn the beam on for `ms` milliseconds (0.1 to 1000), in pulsed mode."""
        if self._mode != "pulsed":
            raise RuntimeError("pulse() needs mode 'pulsed'")
        shortest, longest = PULSES
        if not shortest <= ms <= longest:  # before the beam goes on (NaN fails too)
            raise ValueError(f"a pulse of {ms} ms is outside {shortest:g} to {longest:g} ms")
        self.set_emission(True)
        try:
            time.sleep(ms / 1000)
        finally:  # whatever happens meanwhile, the beam goes off again
            self.set_emission(False)

    def temperature(self) -> float:
        """The diode's temperature in °C: it warms with the power while the beam is on."""
        self._settle()
        return round(self._temperature, 2)

    def write(self, line: str) -> None:
        """Send a line of the DL-100's own protocol, such as "POW 12.5" or "EMIT 1"."""
        command, _, value = line.partition(" ")
        setters = {"POW": self.set_power, "WAV": self.set_wavelength, "EMIT": self.set_emission}
        if command not in setters:
            raise ValueError(f"unknown command {command!r}")
        setters[command](float(value))

    def _settle(self) -> None:
        """Bring the simulated temperature up to now: it approaches AMBIENT, warmer by HEATING per mW
        while the beam is on, within a few SETTLING times."""
        now = time.monotonic()
        target = AMBIENT + (HEATING * self._power if self._emission else 0.0)
        self._temperature += (target - self._temperature) * (1 - math.exp(-(now - self._since) / SETTLING))
        self._since = now
