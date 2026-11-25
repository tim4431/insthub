import asyncio
import gc
import threading
import time

import numpy as np
import pytest

import insthub
from insthub import sim


def test_frames_arrive_in_order_with_meta(client):
    scope = client.scope
    with scope.subscribe("ch1") as sub:
        first = sub.get(2)
        second = sub.get(2)
    assert second.seq == first.seq + 1 and first.stream == "ch1"
    assert first.data.dtype == np.float32 and first.data.shape == (1000,)
    assert first.meta == {"t0": 0.0, "dt": pytest.approx(1e-5), "unit": "V"}
    assert 0 <= first.age < 2


def test_nobody_listening_means_no_acquisition(server, client):
    scope = server.devices["scope"]
    time.sleep(0.2)
    assert "ch1" not in scope._rt.streams or scope._rt.streams["ch1"].seq == 0
    with client.scope.subscribe("ch1") as sub:
        sub.get(2)
    time.sleep(0.2)
    assert scope.subscribers("ch1") == 0          # closing detached it


def test_state_stream_and_retained_value(client):
    synth = client.synth
    synth.frequency = 1234.0
    with synth.subscribe("state") as sub:
        assert sub.get(2).data["frequency_MHz"] == 1234.0      # the retained frame, at once
        synth.frequency = 1500.0
        assert sub.get(2).data["frequency_MHz"] == 1500.0
        synth.frequency = 1500.0                               # unchanged: nothing published
        with pytest.raises(TimeoutError):
            sub.get(0.1)
    assert synth.state()["frequency_MHz"] == 1500.0
    assert synth.latest("state").data["frequency_MHz"] == 1500.0
    assert synth.latest("never") is None
    with synth.subscribe("state", retained=False) as sub:
        assert sub.poll() is None


def test_latest_only_subscription_skips_but_ends_on_the_newest(client):
    lb = client.loopback
    with lb.subscribe("burst") as sub:
        done = lb.flood.future("burst", 3000, 4000)
        seen = []
        while not seen or seen[-1] != 2999:
            frame = sub.get(5)
            seen.append(int(frame.data[:8].view(np.uint64)[0]))
            time.sleep(0.002)                                  # a slow reader
        done.result(10)
        assert seen == sorted(seen) and len(seen) < 3000
        assert sub.received + sub.dropped >= 3000 and sub.dropped > 0
        assert all(frame_index == frame_byte for frame_index, frame_byte in [(seen[-1] & 0xFF, frame.data[-1])])


def test_queue_subscription_gets_every_frame(client):
    lb = client.loopback
    with lb.subscribe("rec", queue=5000) as sub:
        lb.flood("rec", 2000, 1000)
        got = [int(sub.get(5).data[:8].view(np.uint64)[0]) for _ in range(2000)]
    assert got == list(range(2000)) and sub.dropped == 0


def test_callback_subscription(client):
    seen, done = [], threading.Event()

    def on_frame(frame):
        seen.append(frame.meta["index"])
        if frame.meta["index"] == 49:
            done.set()

    sub = client.loopback.subscribe("cb", on_frame, queue=100)
    client.loopback.flood("cb", 50, 100)
    assert done.wait(5) and seen == list(range(50))
    sub.close()


def test_several_subscribers_and_several_connections(server, client):
    other = insthub.connect(server.addresses[0])
    a = client.loopback.subscribe("multi", queue=10)
    b = client.loopback.subscribe("multi", queue=10)
    c = other.loopback.subscribe("multi", queue=10)
    client.loopback.flood("multi", 3, 100)
    for sub in (a, b, c):
        assert [sub.get(2).meta["index"] for _ in range(3)] == [0, 1, 2]
    assert server.devices["loopback"].subscribers("multi") == 3
    other.close()
    a.close()
    b.close()
    deadline = time.monotonic() + 2
    while server.devices["loopback"].subscribers("multi") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.devices["loopback"].subscribers("multi") == 0


def test_forgotten_subscription_detaches_itself(server, client):
    lb = server.devices["loopback"]
    client.loopback.subscribe("orphan")            # never closed, reference dropped at once
    gc.collect()
    client.loopback.flood("orphan", 3, 100)
    deadline = time.monotonic() + 2
    while lb.subscribers("orphan") and time.monotonic() < deadline:
        client.loopback.flood("orphan", 1, 100)
        time.sleep(0.01)
    assert lb.subscribers("orphan") == 0


def test_in_process_frames_are_the_same_object(server):
    lb = server.devices["loopback"]
    arr = np.arange(100000)
    with server.device("loopback").subscribe("ref") as sub:
        lb.publish("ref", arr, note="x")
        frame = sub.get(1)
    assert frame.data is arr and frame.meta == {"note": "x"}


def test_publish_copy_protects_a_reused_buffer(server):
    lb = server.devices["loopback"]
    buf = np.zeros(10000)
    with insthub.connect(server.addresses[0]).loopback.subscribe("buf", queue=10) as sub:
        for i in range(5):
            buf[:] = i
            lb.publish("buf", buf, copy=True)
        assert [float(sub.get(2).data[0]) for _ in range(5)] == [0, 1, 2, 3, 4]


def test_a_stalled_subscriber_does_not_stall_the_device(server):
    """A client that subscribed and then stopped reading: publishing must stay
    fast and the device must stay responsive for everyone else."""
    import socket

    from insthub import codec, wire

    lb = server.devices["loopback"]
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.connect(server.addresses[0].removeprefix("ipc://"))
    raw.sendall(wire.MAGIC)
    body, _ = codec.encode(["loopback", "$subscribe", ["heavy", 1, 0, True], {}])
    raw.sendall(wire.frame(wire.CALL, 1, body)[0])              # ... and never read again
    deadline = time.monotonic() + 2
    while not lb.subscribers("heavy") and time.monotonic() < deadline:
        time.sleep(0.01)
    other = insthub.connect(server.addresses[0]).loopback
    took = other.flood("heavy", 40, 8_000_000)                   # 320 MB at a reader that takes none of it
    assert took < 2.0
    start = time.perf_counter()
    assert other.echo(1) == 1
    assert time.perf_counter() - start < 0.2
    raw.close()


def test_async_iteration(client):
    async def main():
        got = []
        sub = client.loopback.subscribe("aio", queue=100)
        task = client.loopback.flood.future("aio", 20, 100, 0.001)
        async for frame in sub:
            got.append(frame.meta["index"])
            if len(got) == 20:
                sub.close()
        await task
        return got

    assert asyncio.run(main()) == list(range(20))


def test_get_on_closed_subscription(client):
    sub = client.loopback.subscribe("x")
    threading.Timer(0.1, sub.close).start()
    with pytest.raises(insthub.SubscriptionClosed):
        sub.get(5)
    assert list(sub) == []


def test_camera_images(client):
    with client.camera.subscribe("image") as sub:
        frame = sub.get(3)
    assert frame.data.shape == (480, 640) and frame.data.dtype == np.uint16
    assert frame.meta["exposure_us"] == 1000.0
    assert isinstance(sim.SimCamera(), insthub.Device)


def test_a_frame_published_right_after_subscribe_arrives(server):
    """subscribe() returning means the server is attached: the very next publish
    must reach the subscriber (subscribe, then trigger, must not lose the frame)."""
    lb = server.devices["loopback"]
    client = insthub.connect(server.addresses[0])
    for i in range(300):
        with client.loopback.subscribe(f"race{i}", retained=False) as sub:
            lb.publish(f"race{i}", i)                    # straight from the server side, no device-thread hop
            assert sub.get(2).data == i
