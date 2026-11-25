import os
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


class Bench(Device):
    """A device to pause: a slow command, a cached one, a periodic task."""

    reconnect_interval = 0.0

    def __init__(self, name="bench", lazy=False):
        super().__init__(name)
        self.opens = self.ticks = 0
        self.open_on_start = not lazy

    def open(self):
        self.opens += 1

    @command
    def work(self, seconds=0.0):
        time.sleep(seconds)
        return "done"

    @command(concurrent=True)
    def cached(self):
        return 1

    @prop
    def level(self):
        return 0.5

    @periodic(0.01)
    def _tick(self):
        self.ticks += 1


@pytest.mark.parametrize("via", ["inproc", "ipc", "tcp"])
def test_a_paused_device_runs_nothing_until_it_is_resumed(via):
    with insthub.Server("p", [Bench()], tcp=0) as server:
        dev = server.devices["bench"]
        address = {"inproc": None, "ipc": server.addresses[0], "tcp": server.addresses[1]}[via]
        p = insthub.device("bench", address) if address else server.device("bench")
        assert p.work() == "done"
        running = p.work.future(0.3)                       # pausing waits for the command under way ...
        time.sleep(0.05)
        queued = p.work.future()                           # ... and refuses what waits behind it
        status = p.bus.pause()
        assert running.result() == "done" and isinstance(queued.exception(), insthub.DevicePaused)
        assert status["paused"]["since"] <= time.time() and not status["connected"] and status["error"] == "paused"
        if via == "tcp":                                   # who paused it: an address, or a process here
            assert status["paused"]["peer"].startswith("127.0.0.1:")
        else:
            assert status["paused"]["pid"] == os.getpid()
        ticks = dev.ticks
        time.sleep(0.1)
        assert dev.ticks == ticks                          # periodic tasks stop too
        for call in (p.work, p.cached, lambda: p.level, p.bus.reopen, p.bus.release):
            with pytest.raises(insthub.DevicePaused, match="insthub resume bench"):
                call()
        with pytest.raises(insthub.DeviceNotConnected):    # what scripts already catch
            p.work()
        assert p.bus.status()["paused"] and p.bus.state() == {}   # reporting still works
        opens = dev.opens
        status = p.bus.resume()
        assert status["connected"] and status["paused"] is None and dev.opens == opens + 1  # it was open: reopened
        assert p.work() == "done" and p.cached() == 1
        time.sleep(0.05)
        assert dev.ticks > ticks


class Quiet(Device):
    """Lazy, and nothing of its own opens it: only a command."""

    open_on_start = False

    def open(self):
        self.opens = getattr(self, "opens", 0) + 1

    @command
    def work(self, seconds=0.0):
        time.sleep(seconds)
        return "done"


def test_resuming_leaves_a_lazy_device_closed_and_pausing_twice_is_harmless():
    with insthub.Server("p", [Quiet("bench")]) as server:
        p, dev = server.device("bench"), server.devices["bench"]
        dev.opens = 0
        p.bus.pause()
        assert p.bus.pause()["paused"]                      # again: still paused, nothing breaks
        status = p.bus.resume()
        assert not status["connected"] and status["error"] is None and dev.opens == 0  # not opened yet, as before
        assert p.bus.resume()["paused"] is None             # resuming what is not paused: nothing happens
        assert p.work() == "done" and dev.opens == 1        # first use opens it


def test_of_several_pauses_and_resumes_the_last_wins():
    with insthub.Server("p", [Bench()]) as server:
        bus, dev = server.device("bench").bus, server.devices["bench"]
        running = server.device("bench").work.future(0.3)   # while it runs, three requests queue up
        time.sleep(0.05)
        rt = dev._rt
        replies = []
        for method in ("$pause", "$resume", "$pause"):
            fut = bus._t.request("bench", method, ("test",) if method == "$pause" else (), {})
            replies.append(fut)
        assert running.result() == "done"
        assert [bool(f.result(5)["paused"]) for f in replies] == [True, False, True]
        assert rt.paused is not None and not rt.opened          # paused at the end, as the last request asked
        with pytest.raises(insthub.DevicePaused):
            server.device("bench").work()
        bus.resume()


def test_calls_are_refused_at_once_and_whether_it_was_open_is_taken_after_the_running_command():
    with insthub.Server("p", [Quiet("bench")]) as server:
        p, dev = server.device("bench"), server.devices["bench"]
        dev.opens = 0
        running = p.work.future(0.3)                          # its first use opens it ...
        time.sleep(0.1)
        p.bus.pause()                                         # ... and is running when the pause arrives
        assert running.result() == "done" and dev.opens == 1
        start = time.monotonic()
        with pytest.raises(insthub.DevicePaused):
            p.work()
        assert time.monotonic() - start < 0.5
        assert p.bus.resume()["connected"]                    # it was open when it was closed: opened again


def test_who_paused_it_and_a_pause_ends_with_its_server():
    server = insthub.Server("p", [Bench()])
    server.start()
    try:
        status = server.device("bench").bus.pause(by="experiment.py")
        assert status["paused"]["by"] == "experiment.py"
        with pytest.raises(insthub.DevicePaused, match="by experiment.py"):
            server.device("bench").work()
    finally:
        server.stop()
    server.start()                                            # the same objects, started again: not paused
    try:
        assert server.device("bench").work() == "done" and server.device("bench").bus.status()["paused"] is None
    finally:
        server.stop()


class Sequencer(Device):
    """A command that runs the device's own commands through its proxy, as a sweep does."""

    open_on_start = False
    reconnect_interval = 0.0

    def open(self):
        pass

    @command
    def measure(self, i):
        return 2 * i

    @command(concurrent=True)
    def progress(self):
        return "busy"

    @command
    def sweep(self, n=5, step=0.06):
        me = insthub.device(self.name, f"inproc://{self.server_name}")
        out = []
        for i in range(n):
            out.append(me.measure(i))
            assert me.progress() == "busy"                      # its concurrent commands too
            time.sleep(step)
        return out


def test_the_command_running_when_the_pause_comes_in_finishes_even_through_its_own_proxy():
    with insthub.Server("p", [Sequencer("seq")]) as server:
        server.devices["seq"].server_name = "p"
        p = server.device("seq")
        assert p.sweep(3, 0) == [0, 2, 4]
        running = p.sweep.future()
        time.sleep(0.1)
        status = p.bus.pause(by="test")
        assert running.result(5) == [0, 2, 4, 6, 8]           # all of it: not cut off at the pause
        assert status["paused"] and not status["connected"]
        with pytest.raises(insthub.DevicePaused):             # what comes after waits for the resume
            p.measure(1)
        p.bus.resume()
        assert p.measure(1) == 2


def test_a_lazy_device_paused_when_its_server_stopped_is_not_shown_broken_after_a_restart():
    server = insthub.Server("p", [Quiet("bench")])
    server.start()
    try:
        assert server.device("bench").work() == "done"
        server.device("bench").bus.pause()
    finally:
        server.stop()
    server.start()
    try:
        st = server.device("bench").bus.status()
        assert st["paused"] is None and st["error"] != "paused"
    finally:
        server.stop()


class TwoTasks(Device):
    """Two periodic tasks due together: a pause during the first must stop the second starting."""

    reconnect_interval = 0.0

    def __init__(self, name):
        super().__init__(name)
        self.ran = []

    def open(self):
        pass

    @periodic(5.0)
    def _a_first(self):
        self.ran.append("a")
        time.sleep(0.4)

    @periodic(5.0)
    def _b_second(self):
        self.ran.append("b")


def test_no_periodic_task_starts_after_a_pause_is_asked_for():
    dev = TwoTasks("two")
    with insthub.Server("p", [dev]) as server:
        deadline = time.monotonic() + 3
        while dev.ran[:1] != ["a"] and time.monotonic() < deadline:   # the first pass: a runs ...
            time.sleep(0.01)
        if "b" in dev.ran:
            pytest.skip("the tasks' first pass was over before the test could pause it")
        server.device("two").bus.pause(by="test")                       # ... and the pause comes in meanwhile
        assert dev.ran == ["a"]                                          # b did not start after it
        server.device("two").bus.resume()


class SlowOpen(Device):
    """It takes a while to open; a periodic task uses it."""

    reconnect_interval = 0.0

    def __init__(self, name, lazy=False):
        super().__init__(name)
        self.open_on_start = not lazy
        self.events = []

    def open(self):
        self.events.append("open")
        time.sleep(0.5)

    @periodic(10.0)
    def _acquire(self):
        self.events.append("task")


@pytest.mark.parametrize("lazy", [False, True], ids=["opened at start", "opened by its task"])
def test_a_pause_while_it_opens_shows_as_pausing_and_nothing_runs_after_it(lazy):
    from insthub import inventory
    dev = SlowOpen("slow", lazy=lazy)
    with insthub.Server("p", [dev]) as server:
        time.sleep(0.2)                                                    # it is opening
        bus = server.device("slow").bus
        st = bus.status()
        assert st["busy"] == "$open" and not st["connected"]
        bus._t.request("slow", "$pause", ("test",), {})                     # (does not wait)
        time.sleep(0.05)
        d = {"status": bus.status()}
        assert inventory.device_state(d)[1] == "pausing"                    # not "paused ... left alone"
        assert "being opened" in inventory.device_state(d)[2]
        deadline = time.monotonic() + 3
        while (bus.status()["connected"] or bus.status()["busy"]) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert inventory.device_state({"status": bus.status()})[1] == "paused"
        time.sleep(0.2)
        assert "task" not in dev.events                                    # its task did not run after the pause
        bus.resume()
