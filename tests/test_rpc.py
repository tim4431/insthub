import asyncio
import threading
import time

import numpy as np
import pytest

import insthub
from insthub import sim


def test_commands_arguments_and_results(client):
    scope = client.scope
    assert scope.identify() == "insthub,SimScope,0001,1.0"
    assert scope.set_timebase(seconds_per_div=2e-3) == 2e-3
    wf = scope.get_waveform(2)
    assert isinstance(wf, sim.Waveform) and wf.channel == 2
    assert wf.volts.dtype == np.float32 and wf.volts.shape == (1000,) and wf.dt == pytest.approx(2e-5)
    assert 0.5 < scope.measure("vpp", channel=1) < 3


def test_values_keep_their_types(client):
    lb = client.loopback
    value = {"t": (1, 2.5, "x"), "arr": np.arange(6).reshape(2, 3), "set": {1, 2}, "z": 1j, "none": None,
             "blob": b"\x00\x01" * 5000}
    got = lb.echo(value)
    assert got["t"] == (1, 2.5, "x") and got["set"] == {1, 2} and got["z"] == 1j and got["none"] is None
    assert got["blob"] == value["blob"] and np.array_equal(got["arr"], value["arr"])


def test_properties_read_like_attributes(client):
    synth = client.synth
    synth.frequency = 2500.0
    synth.power = -10
    assert synth.frequency == 2500.0 and synth.power == -10.0
    assert synth.bus.get("frequency") == 2500.0
    with pytest.raises(AttributeError, match="read-only"):
        synth.locked = True
    with pytest.raises(AttributeError, match="no property"):
        synth.frequenzy = 1.0          # a typo must not silently create an attribute
    with pytest.raises(ValueError, match="outside"):
        synth.frequency = 1.0


def test_errors_arrive_as_matching_exceptions(client):
    lb = client.loopback
    with pytest.raises(ValueError, match="boom"):
        lb.fail("ValueError", "boom")
    with pytest.raises(TimeoutError, match="late"):
        lb.fail("TimeoutError", "late")
    with pytest.raises(KeyError):
        lb.fail("KeyError", "k")
    with pytest.raises(TypeError):
        lb.sleep()                      # missing argument
    with pytest.raises(AttributeError, match="no command or property"):
        lb.does_not_exist()
    with pytest.raises(insthub.UnknownCommand):
        lb.bus.call("does_not_exist")
    with pytest.raises(insthub.DeviceNotFound, match="no device 'nope'"):
        client.device("nope")
    assert lb.echo(1) == 1              # the connection survived all of it


def test_remote_error_carries_the_remote_traceback(client):
    if client.kind == "inproc":
        pytest.skip("in-process calls raise the original exception")
    with pytest.raises(insthub.RemoteError) as info:
        client.loopback.fail("RuntimeError", "bad")
    e = info.value
    assert isinstance(e, RuntimeError) and str(e) == "bad"
    assert e.remote_type == "RuntimeError" and "in fail" in e.remote_traceback and e.where == "loopback.fail"


def test_unsendable_result_is_an_error_not_a_dead_connection(server, client):
    if client.kind == "inproc":
        pytest.skip("nothing is serialized in-process")

    class Odd(insthub.Device):
        @insthub.command
        def handle(self):
            return open(__file__)

    server.add(Odd("odd"))
    with pytest.raises(TypeError, match="cannot send an object"):
        client.odd.handle()
    with pytest.raises(TypeError, match="cannot send an object"):
        client.loopback.echo(open)
    assert client.loopback.echo("still alive") == "still alive"


def test_futures_notify_and_gather(client):
    lb, synth = client.loopback, client.synth
    futures = [lb.echo.future(i) for i in range(200)]
    assert insthub.gather(*futures, timeout=10) == list(range(200))
    synth.rf.notify(False)
    synth.rf.notify(True)
    assert synth.rf.future(True).result(5) is True
    assert synth.locked is True                     # notifies ran, in order, before the next call


def test_one_device_is_serial_different_devices_are_parallel(server, client):
    server.add(sim.Loopback("other"))
    a, b = client.loopback, client.other
    start = time.perf_counter()
    insthub.gather(a.sleep.future(0.15), b.sleep.future(0.15))
    parallel = time.perf_counter() - start
    start = time.perf_counter()
    insthub.gather(a.sleep.future(0.15), a.sleep.future(0.15))
    serial = time.perf_counter() - start
    assert parallel < 0.25 and 0.29 < serial < 0.5


def test_concurrent_commands_answer_while_the_device_is_busy(client):
    lb = client.loopback
    busy = lb.sleep.future(0.3)
    time.sleep(0.02)                                # let the device thread pick it up
    start = time.perf_counter()
    assert lb.echo_now("x") == "x"
    assert lb.bus.status()["busy"] == "sleep"
    assert time.perf_counter() - start < 0.1
    assert busy.result(5) == 0.3


def test_timeout_does_not_break_later_calls(client):
    lb = client.loopback.bus.with_timeout(0.05)
    with pytest.raises(TimeoutError, match="no reply within"):
        lb.sleep(0.3)
    assert client.loopback.echo(5) == 5             # waits for the sleep to finish, then runs


@pytest.mark.parametrize("nbytes", [1, 5000, 3_000_000, 150_000_000])
def test_bulk_data_both_ways(client, nbytes):
    data = np.random.default_rng(0).integers(0, 255, nbytes, dtype=np.uint8)
    back = client.loopback.echo(data)
    assert back.dtype == np.uint8 and np.array_equal(back, data)
    assert client.loopback.zeros(nbytes).nbytes == nbytes


def test_many_threads_share_one_connection(client):
    lb = client.loopback
    errors = []

    def worker(k):
        try:
            for i in range(300):
                value = (k, i, "x" * (i % 50))
                if lb.echo(value) != value:
                    errors.append(value)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors


def test_asyncio(client):
    async def main():
        lb, synth = client.loopback, client.synth
        a, b = await asyncio.gather(lb.echo.aio(1), synth.rf.aio(True))
        c = await lb.echo.future("awaited")
        with pytest.raises(ValueError):
            await lb.fail.aio()
        return a, b, c

    assert asyncio.run(main()) == (1, True, "awaited")


def test_introspection(client):
    scope = client.scope
    info = scope.bus.describe()
    assert info["type"] == "SimScope" and "oscilloscope" in info["doc"]
    assert info["commands"]["measure"]["signature"] == "(item: str = 'VPP', channel: int = 1) -> float"
    assert "measure" in dir(scope) and "subscribe" in dir(scope)
    assert scope.measure.__doc__.startswith("measure(item: str = 'VPP', channel: int = 1) -> float")
    text = client.synth.bus.help()
    assert "frequency" in text and "locked  (read-only)" in text and "rf(on: bool = True) -> bool" in text
    assert sorted(client.devices()) == ["camera", "loopback", "sa", "scope", "synth"]
    assert client.ping() < 0.5


def test_device_can_call_itself_and_other_devices(server):
    class Chain(insthub.Device):
        @insthub.command
        def twice(self, x):
            me = insthub.device(self.name)
            return me.once(x) + insthub.device("loopback").echo(x)     # own proxy: runs inline, no deadlock

        @insthub.command
        def once(self, x):
            return x

    server.add(Chain("chain"))
    assert server.device("chain").with_timeout(5).twice(21) == 42


def test_shortcuts_yield_to_device_commands(server):
    class Clash(insthub.Device):
        @insthub.command
        def subscribe(self, what):
            return f"device subscribe {what}"

    server.add(Clash("clash"))
    dev = server.device("clash")
    assert dev.subscribe("x") == "device subscribe x"
    dev.bus.subscribe("state").close()               # the bus version is always there
