"""A device server: owns the instrument, serves it to every program on this machine.

    python examples/01_server.py            # then, in another terminal:
    python examples/02_client.py            # or: insthub list / insthub info lockin / insthub view lockin
"""
import time

import numpy as np

import insthub


class LockIn(insthub.Device):
    """A pretend lock-in amplifier.  Replace the bodies with real instrument I/O."""

    streams = {"xy": "demodulated X and Y, one point per poll"}

    def __init__(self, name="lockin", address="10.0.0.5"):
        super().__init__(name)
        self.address = address

    def open(self):                      # device thread; called again after a link error
        self._tau, self._freq = 0.1, 1000.0
        self.set_state(time_constant=self._tau, frequency=self._freq)

    def close(self):
        pass                             # close the socket / serial port here

    @insthub.command
    def set_time_constant(self, seconds: float) -> float:
        """Filter time constant.  Returns the value the instrument accepted."""
        if seconds <= 0:
            raise ValueError("time constant must be positive")
        self._tau = float(seconds)
        self.set_state(time_constant=self._tau)
        return self._tau

    @insthub.prop
    def frequency(self) -> float:
        """Reference frequency in Hz."""
        return self._freq

    @frequency.setter
    def frequency(self, hz: float):
        self._freq = float(hz)
        self.set_state(frequency=self._freq)

    @insthub.command
    def read(self, samples: int = 1000) -> np.ndarray:
        """Acquire `samples` points of X (takes a moment, like the real thing)."""
        time.sleep(0.02)
        return np.random.default_rng().normal(0.5, 0.01, samples)

    @insthub.periodic(0.1)                # runs between commands, on the same thread
    def _poll(self):
        x, y = np.random.default_rng().normal([0.5, 0.0], 0.01)
        self.publish("xy", {"x": x, "y": y})


if __name__ == "__main__":
    insthub.serve(LockIn())               # Ctrl-C to stop.  Add tcp=8765 to reach it from other machines.
