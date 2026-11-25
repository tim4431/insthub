"""Simulated instruments: for trying the bus, for tests and benchmarks, and as
templates for real drivers.  `insthub sim` serves all three.

    SimScope    two channels of noisy sine; live traces on streams "ch1", "ch2"
    SimSynth    signal generator with properties, wired to nothing
    SimCamera   a moving Gaussian spot; frames on stream "image"
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .device import Device, command, periodic, prop


@dataclass
class Waveform:
    """One captured trace.  Time of sample i is t0 + i*dt."""

    channel: int
    volts: np.ndarray
    t0: float
    dt: float

    @property
    def t(self) -> np.ndarray:
        return self.t0 + self.dt * np.arange(len(self.volts))


class SimScope(Device):
    """Two-channel oscilloscope looking at a sine of adjustable frequency."""

    streams = {"ch1": "live trace of channel 1 (V); meta: t0, dt", "ch2": "live trace of channel 2 (V)"}

    def __init__(self, name: str = "scope", points: int = 1000, latency: float = 0.0):
        super().__init__(name)
        self.points = int(points)
        self.latency = float(latency)  # pretend each instrument query takes this long

    def open(self) -> None:
        self._rng = np.random.default_rng(0)
        self._timebase = 1e-3  # s/div, 10 divisions
        self._signal_hz = 1e3
        self._noise = 0.02
        self._t_open = time.perf_counter()
        self.set_state(timebase=self._timebase, signal_hz=self._signal_hz, live=self.tasks["_acquire"].enabled)

    def _capture(self, channel: int) -> Waveform:
        if self.latency:
            time.sleep(self.latency)
        n = self.points
        dt = 10 * self._timebase / n
        t = dt * np.arange(n, dtype=np.float32)
        phase = 2 * np.pi * 0.2 * (time.perf_counter() - self._t_open) + (np.pi / 2) * (channel - 1)
        volts = np.sin(2 * np.pi * self._signal_hz * t + phase, dtype=np.float32) / channel
        volts += self._rng.normal(0, self._noise, n).astype(np.float32)
        return Waveform(channel, volts, 0.0, dt)

    @command
    def identify(self) -> str:
        """Instrument identification string."""
        return "insthub,SimScope,0001,1.0"

    @command
    def set_timebase(self, seconds_per_div: float) -> float:
        """Horizontal scale in s/div.  Returns the value in effect."""
        if not 1e-9 <= seconds_per_div <= 10:
            raise ValueError(f"timebase {seconds_per_div} s/div outside 1 ns .. 10 s")
        self._timebase = float(seconds_per_div)
        self.set_state(timebase=self._timebase)
        return self._timebase

    @command
    def set_signal(self, hz: float, noise: float | None = None) -> None:
        """Frequency (and optionally noise amplitude) of the simulated input."""
        self._signal_hz = float(hz)
        if noise is not None:
            self._noise = float(noise)
        self.set_state(signal_hz=self._signal_hz)

    @command
    def get_waveform(self, channel: int = 1) -> Waveform:
        """Capture one trace now."""
        if channel not in (1, 2):
            raise ValueError(f"channel must be 1 or 2, got {channel!r}")
        return self._capture(channel)

    @command
    def measure(self, item: str = "VPP", channel: int = 1) -> float:
        """A scalar measurement: VPP, VRMS or FREQ."""
        wf = self._capture(channel)
        item = item.upper()
        if item == "VPP":
            return float(np.ptp(wf.volts))
        if item == "VRMS":
            return float(np.sqrt(np.mean(wf.volts ** 2)))
        if item == "FREQ":
            return self._signal_hz
        raise ValueError(f"unknown measurement {item!r}")

    @command
    def live(self, on: bool = True, interval: float | None = None) -> None:
        """Switch the continuous acquisition (streams "ch1", "ch2") on or off."""
        task = self.tasks["_acquire"]
        if on:
            task.start(interval)
        else:
            task.stop()
        self.set_state(live=bool(on))

    @periodic(0.05)
    def _acquire(self) -> None:
        for channel in (1, 2):
            if self.subscribers(f"ch{channel}"):  # do not read out what nobody watches
                wf = self._capture(channel)
                self.publish(f"ch{channel}", wf.volts, t0=wf.t0, dt=wf.dt, unit="V")


class SimSynth(Device):
    """Signal generator: frequency and power are properties, like a local driver."""

    streams = {"sweep": "frequency (MHz) of each point while sweep() runs"}
    FREQ_RANGE_MHZ = (10.0, 24000.0)

    def __init__(self, name: str = "synth"):
        super().__init__(name)

    def open(self) -> None:
        self._freq, self._power, self._rf = 1000.0, 0.0, False
        self._publish_state()

    def _publish_state(self) -> None:
        self.set_state(frequency_MHz=self._freq, power_dBm=self._power, rf=self._rf)

    @prop
    def frequency(self) -> float:
        """Output frequency in MHz."""
        return self._freq

    @frequency.setter
    def frequency(self, mhz: float) -> None:
        lo, hi = self.FREQ_RANGE_MHZ
        if not lo <= mhz <= hi:
            raise ValueError(f"frequency {mhz} MHz outside {lo}-{hi} MHz")
        self._freq = float(mhz)
        self._publish_state()

    @prop
    def power(self) -> float:
        """Output power in dBm."""
        return self._power

    @power.setter
    def power(self, dbm: float) -> None:
        self._power = float(dbm)
        self._publish_state()

    @prop
    def locked(self) -> bool:
        """PLL lock (read-only)."""
        return self._rf

    @command
    def rf(self, on: bool = True) -> bool:
        """Switch the output on or off."""
        self._rf = bool(on)
        self._publish_state()
        return self._rf

    @command
    def sweep(self, start_mhz: float, stop_mhz: float, steps: int, dwell: float = 0.0) -> int:
        """Step the frequency from start to stop; publishes each point on "sweep"."""
        for i, f in enumerate(np.linspace(start_mhz, stop_mhz, int(steps))):
            self.frequency = float(f)
            self.publish("sweep", float(f), index=i)
            if dwell:
                time.sleep(dwell)
        return int(steps)

    @command(concurrent=True)
    def temperature(self) -> float:
        """Internal temperature in C (cached: answers even while a sweep runs)."""
        return 42.0


class SimCamera(Device):
    """Camera looking at a slowly circling Gaussian spot."""

    streams = {"image": "live frames, uint16; meta: exposure_us"}

    def __init__(self, name: str = "camera", shape=(480, 640), fps: float = 20.0):
        super().__init__(name)
        self.shape = tuple(shape)
        self.tasks["_grab"].interval = 1.0 / fps

    def open(self) -> None:
        h, w = self.shape
        self._y, self._x = np.mgrid[0:h, 0:w].astype(np.float32)
        self._exposure = 1000.0
        self._t0 = time.perf_counter()
        self._rng = np.random.default_rng(1)
        self.set_state(exposure_us=self._exposure, shape=list(self.shape))

    def _frame(self) -> np.ndarray:
        h, w = self.shape
        a = 0.5 * (time.perf_counter() - self._t0)
        cx, cy = w / 2 + 0.25 * w * np.cos(a), h / 2 + 0.25 * h * np.sin(a)
        sigma = min(h, w) / 12
        spot = np.exp(-((self._x - cx) ** 2 + (self._y - cy) ** 2) / (2 * sigma ** 2))
        img = 3000.0 * (self._exposure / 1000.0) * spot + self._rng.normal(100, 5, self.shape)
        return np.clip(img, 0, 4095).astype(np.uint16)

    @prop
    def exposure(self) -> float:
        """Exposure time in microseconds."""
        return self._exposure

    @exposure.setter
    def exposure(self, us: float) -> None:
        if us <= 0:
            raise ValueError("exposure must be positive")
        self._exposure = float(us)
        self.set_state(exposure_us=self._exposure)

    @command
    def grab(self) -> np.ndarray:
        """One frame (uint16, 12-bit)."""
        return self._frame()

    @periodic(0.05)
    def _grab(self) -> None:
        if self.subscribers("image"):
            self.publish("image", self._frame(), exposure_us=self._exposure)


def devices() -> list[Device]:
    """One of each, as served by `insthub sim`."""
    return [SimScope(), SimSynth(), SimCamera()]


class Loopback(Device):
    """Echoes, sleeps, fails and floods on demand: for tests and `insthub bench`."""

    reconnect_interval = 0.0

    def __init__(self, name: str = "loopback"):
        super().__init__(name)

    @command
    def echo(self, value=None):
        """Return the argument (through the device thread)."""
        return value

    @command(concurrent=True)
    def echo_now(self, value=None):
        """Return the argument without waiting for the device thread."""
        return value

    @command
    def sleep(self, seconds: float) -> float:
        """Keep the device busy for a while."""
        time.sleep(seconds)
        return seconds

    @command
    def fail(self, kind: str = "ValueError", message: str = "boom"):
        """Raise an exception of the named builtin class."""
        raise {"ValueError": ValueError, "RuntimeError": RuntimeError, "TimeoutError": TimeoutError,
               "KeyError": KeyError, "OSError": OSError}[kind](message)

    @command
    def zeros(self, nbytes: int) -> np.ndarray:
        """An array of `nbytes` zero bytes."""
        return np.zeros(int(nbytes), np.uint8)

    @command
    def flood(self, stream: str, count: int, nbytes: int = 8000, interval: float = 0.0) -> float:
        """Publish `count` frames of `nbytes` on `stream`; returns the time it took.
        Each frame's first 8 bytes hold its index."""
        start = time.perf_counter()
        for i in range(int(count)):
            data = np.empty(max(8, int(nbytes)), np.uint8)
            data[8:] = i & 0xFF
            data[:8].view(np.uint64)[0] = i
            self.publish(stream, data, index=i)
            if interval:
                time.sleep(interval)
        return time.perf_counter() - start
