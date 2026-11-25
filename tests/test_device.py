import threading
import time

import numpy as np
import pytest

import insthub
from insthub import Device, command, periodic, prop, wrap


class Flaky(Device):
    """A device whose 'hardware' can be unplugged."""

    reconnect_interval = 0.15

    def __init__(self, name="flaky"):
        super().__init__(name)
        self.plugged = True
        self.opens = self.closes = 0

    def open(self):
        if not self.plugged:
            raise ConnectionRefusedError("instrument is off")
        self.opens += 1

    def close(self):
        self.closes += 1

    @command
    def read(self):
        if not self.plugged:
            raise TimeoutError("no answer")
        return 1.0

    @command(needs_open=False)
    def note(self):
        return "no hardware needed"


def test_missing_instrument_does_not_stop_the_server_and_recovers():
    dev = Flaky()
    dev.plugged = False
    with insthub.Server("s", [dev]) as server:
        proxy = server.device("flaky")
        with pytest.raises(insthub.DeviceNotConnected, match="instrument is off"):
            proxy.read()
        with pytest.raises(insthub.DeviceNotConnected, match="next attempt in"):
            proxy.read()                              # fails fast, without trying the hardware again
        assert proxy.note() == "no hardware needed"
        assert proxy.status()["connected"] is False and "instrument is off" in proxy.status()["error"]
        dev.plugged = True
        time.sleep(0.2)
        assert proxy.read() == 1.0 and proxy.status()["connected"] is True


def test_link_error_drops_the_session_and_the_next_call_reopens():
    dev = Flaky()
    with insthub.Server("s", [dev]) as server:
        proxy = server.device("flaky")
        assert proxy.read() == 1.0 and dev.opens == 1
        dev.plugged = False
        with pytest.raises(TimeoutError):
            proxy.read()
        assert dev.closes == 1 and proxy.status()["connected"] is False
        dev.plugged = True
        assert proxy.read() == 1.0                    # waits out the reconnect interval, then reopens
        assert dev.opens == 2


def test_release_and_reopen():
    dev = Flaky()
    with insthub.Server("s", [dev]) as server:
        proxy = server.device("flaky")
        assert proxy.release()["connected"] is False and dev.closes == 1
        assert proxy.reopen()["connected"] is True and dev.opens == 2
        proxy.release()
        assert proxy.read() == 1.0 and dev.opens == 3   # a command reopens by itself


def test_stop_closes_devices_and_fails_queued_calls():
    dev = Flaky()
    server = insthub.Server("s", [dev, insthub.sim.Loopback()]).start()
    lb = server.device("loopback")
    running, queued = lb.sleep.future(0.2), lb.echo.future(1)
    time.sleep(0.05)
    server.stop()
    assert running.result(1) == 0.2
    with pytest.raises(insthub.BusError, match="stopped before"):
        queued.result(1)
    assert dev.closes == 1
    with pytest.raises(insthub.BusError):
        lb.echo(1)


class Ticker(Device):
    def __init__(self):
        self.ticks = 0                                 # note: no super().__init__()
        self.fast = 0

    @periodic(0.02)
    def _tick(self):
        self.ticks += 1
        self.publish("tick", self.ticks)

    @periodic(0, enabled=False)
    def _spin(self):
        self.fast += 1
        time.sleep(0.001)

    @periodic(0.01)
    def _broken(self):
        raise RuntimeError("bug in a periodic task")

    @command
    def count(self):
        return self.ticks


def test_periodic_tasks_run_between_commands():
    dev = Ticker()
    with insthub.Server("s", [dev]) as server:
        proxy = server.device("ticker")                # default name: the class name
        time.sleep(0.5)
        assert 15 <= proxy.count() <= 27               # ~25 expected
        assert proxy.describe()["tasks"]["_broken"]["errors"] >= 1      # ... and did not kill the thread
        assert proxy.task("_spin", enabled=True) == {"interval": 0.0, "enabled": True}
        start = time.perf_counter()
        for _ in range(100):                           # commands interleave with a busy loop
            proxy.count()
        assert time.perf_counter() - start < 0.6 and dev.fast > 50
        proxy.task("_spin", enabled=False)
        proxy.task("_tick", interval=0.2)
        before = proxy.count()
        time.sleep(0.3)
        assert proxy.count() - before <= 3
        with pytest.raises(insthub.UnknownCommand):
            proxy.task("nope")


class Driver:
    """An ordinary driver class, written without insthub in mind."""

    instances = 0

    def __init__(self, port, baud=9600):
        type(self).instances += 1
        self.port, self.baud = port, baud
        self._center = 1e9
        self.closed = False
        self.broken = False

    def idn(self) -> str:
        """Identify."""
        return f"driver on {self.port}@{self.baud} #{self.instances}"

    @property
    def center(self) -> float:
        """Center frequency in Hz."""
        return self._center

    @center.setter
    def center(self, hz):
        self._center = float(hz)

    @property
    def span(self):
        return 1e6

    def trace(self, which="A"):
        if self.broken:
            raise OSError("bus error")
        return np.arange(601.0), which

    def secret(self):
        return "hidden"

    @staticmethod
    def scan(board=0):
        return [18]

    def _private(self):
        return 1

    def close(self):
        self.closed = True


def test_wrap_hosts_an_existing_driver_class():
    Driver.instances = 0
    dev = wrap(Driver, "sa", args=("/dev/ttyUSB0",), kwargs={"baud": 115200}, exclude=["secret"], attrs=["port"])
    with insthub.Server("s", [dev], tcp=0) as server:
        sa = insthub.connect(server.addresses[1]).sa
        assert sa.idn() == "driver on /dev/ttyUSB0@115200 #1"
        sa.center = 2.5e9
        assert sa.center == 2.5e9 and sa.span == 1e6 and sa.port == "/dev/ttyUSB0"
        levels, which = sa.trace("B")
        assert levels.shape == (601,) and which == "B" and sa.scan() == [18]
        info = sa.describe()
        assert info["type"] == "Driver" and set(info["commands"]) == {"idn", "trace", "scan"}
        assert info["commands"]["trace"]["signature"] == "(which='A')"
        assert info["properties"]["span"]["settable"] is False and info["commands"]["idn"]["doc"] == "Identify."
        with pytest.raises(AttributeError):
            sa.secret()
        first = dev.target
        first.broken = True
        with pytest.raises(OSError, match="bus error"):
            sa.trace()
        assert first.closed and dev.target is None
        dev.reconnect_interval = 0
        assert sa.idn().endswith("#2")                 # a link error re-creates the driver object
    assert dev.target is None


def test_wrap_include_and_live_instance():
    obj = Driver("x")
    with insthub.Server("s") as server:
        server.add(wrap(obj, "only", include=["idn", "center"]))
        server.add(Driver, "auto")                     # a class given to add() is wrapped, opened lazily on failure
        only = server.device("only")
        assert set(only.describe()["commands"]) == {"idn"} and only.center == 1e9
        with pytest.raises(insthub.DeviceNotConnected, match="missing 1 required positional argument"):
            server.device("auto").idn()
    assert obj.closed is False                         # an instance you made stays yours
    with pytest.raises(insthub.UnknownCommand, match="nope"):
        wrap(Driver, include=["nope"])._rt


def test_duplicate_and_bad_names():
    with insthub.Server("s", [Flaky("a")]) as server:
        with pytest.raises(ValueError, match="already has a device"):
            server.add(Flaky("a"))
        with pytest.raises(ValueError, match="bad device name"):
            server.add(Flaky("has space"))
        with pytest.raises(ValueError, match="already running in this process"):
            insthub.Server("s").start()


def test_publish_from_any_thread():
    dev = Flaky()
    with insthub.Server("s", [dev]) as server:
        with server.device("flaky").subscribe("x", queue=1000) as sub:
            threads = [threading.Thread(target=lambda: [dev.publish("x", i) for i in range(100)]) for _ in range(4)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            seqs = [sub.get(1).seq for _ in range(400)]
    assert seqs == list(range(1, 401))


def test_device_class_is_usable_without_a_server():
    class Local(Device):
        @command
        def double(self, x):
            return 2 * x

        @prop
        def level(self):
            return 3

    dev = Local()
    assert dev.double(4) == 8 and dev.level == 3
    dev.publish("x", 1)                                # no subscribers, no server: a no-op
    dev.set_state(a=1)
    assert dev.state == {"a": 1}
