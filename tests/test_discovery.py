import os
import subprocess
import sys
import tempfile
import time

import numpy as np
import pytest

import insthub
from insthub import registry, sim


def test_device_is_found_in_this_process_first(server):
    scope = insthub.device("scope")
    assert scope.bus.address == "inproc://t" and scope.identify().startswith("insthub")
    assert insthub.locate("synth") == "inproc://t"
    assert [s["server"] for s in insthub.discover()] == ["t"]


def test_device_is_found_in_another_process(spawned):
    spawned.start()
    address = insthub.locate("scope")
    assert address.startswith("ipc://") and address.endswith("child.sock")
    scope, synth = insthub.device("scope"), insthub.device("synth")
    assert scope._t is synth._t                         # one shared connection
    assert scope.get_waveform(1).volts.shape == (1000,)
    info = insthub.discover()[0]
    assert info["server"] == "child" and info["pid"] == spawned.proc.pid and "camera" in info["devices"]
    with pytest.raises(insthub.DeviceNotFound, match="known devices: camera, loopback, sa, scope, synth"):
        insthub.device("laser")


def test_stale_registry_entries_are_cleaned_up(spawned):
    spawned.start()
    spawned.stop(kill=True)                             # no chance to clean up after itself
    assert (registry.runtime_dir() / "child.json").exists()
    assert registry.servers() == []
    assert not (registry.runtime_dir() / "child.json").exists()
    spawned.start()                                     # the stale socket file is not in the way
    assert insthub.device("loopback").echo(3) == 3


def test_two_servers_cannot_share_a_name(spawned):
    spawned.start()
    with pytest.raises(OSError, match="already running"):
        insthub.Server("child").start()


def test_ambiguous_device_name(spawned, server):
    spawned.start()
    assert insthub.locate("scope") == "inproc://t"       # this process wins
    server.stop()
    other = sim.SimScope()
    with insthub.Server("second", [other], ipc=True):
        pass
    proc = subprocess.Popen([sys.executable, "-m", "insthub", "sim", "--name", "twin"], env=os.environ.copy(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while len(registry.find("scope")) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        with pytest.raises(insthub.DeviceNotFound, match="ambiguous"):
            insthub.device("scope")
        address = next(s for s in registry.servers() if s["server"] == "twin")["addresses"][0]
        assert insthub.device("scope", address).identify().startswith("insthub")
    finally:
        proc.terminate()
        proc.wait(10)


def test_static_table_for_other_machines(server, tmp_path):
    with open(os.environ["INSTHUB_DEVICES"], "w") as f:
        f.write(f'[devices]\nfar = "{server.addresses[1]}"\n')
    assert registry.static_devices() == {"far": server.addresses[1]}
    assert insthub.locate("far") == server.addresses[1]


def test_static_table_is_edited_in_place():
    path = registry.devices_file()
    path.write_text('# mine\n[devices]\nawg = "tcp://h:1"  # the awg\n\n# the rest\n[other]\nx = 1\n')
    registry.edit_static_devices({"psu": "tcp://h:3", "awg": "tcp://h:2", "a.b": "tcp://h:4"})
    assert path.read_text() == ('# mine\n[devices]\nawg = "tcp://h:2"  # the awg\npsu = "tcp://h:3"\n'
                                '"a.b" = "tcp://h:4"\n\n# the rest\n[other]\nx = 1\n')
    assert registry.static_devices() == {"awg": "tcp://h:2", "psu": "tcp://h:3", "a.b": "tcp://h:4"}
    registry.edit_static_devices(remove=["awg", "a.b"])
    assert registry.static_devices() == {"psu": "tcp://h:3"} and "# the awg" not in path.read_text()
    path.unlink()
    registry.edit_static_devices({"far": "tcp://h:5"})                     # no file yet: one is written
    assert registry.static_devices() == {"far": "tcp://h:5"} and path.read_text().startswith("#")
    path.write_text('devices = { awg = "tcp://h:1" }\n')                  # a form it cannot edit: left alone
    with pytest.raises(ValueError, match="edit it by hand"):
        registry.edit_static_devices({"psu": "tcp://h:3"})
    assert path.read_text() == 'devices = { awg = "tcp://h:1" }\n'
    with pytest.raises(ValueError, match="tcp://host:port"):
        registry.edit_static_devices({"psu": "h:3"})


def test_calls_fail_cleanly_when_the_server_dies_and_work_again_after_restart(spawned):
    spawned.start()
    lb = insthub.device("loopback")
    assert lb.echo(1) == 1
    pending = lb.sleep.future(5)
    time.sleep(0.1)
    spawned.stop(kill=True)
    with pytest.raises(insthub.ConnectionLost, match="may or may not have run"):
        pending.result(5)
    with pytest.raises(ConnectionError):
        lb.echo(2)
    spawned.start()
    assert lb.echo(3) == 3                              # same proxy, new server process


def test_subscriptions_resume_after_a_server_restart(spawned):
    spawned.start()
    scope = insthub.device("scope")
    sub = scope.subscribe("ch1")
    assert sub.get(3).data.shape == (1000,)
    spawned.stop(kill=True)
    with pytest.raises(TimeoutError):
        while True:
            sub.get(0.3)
    spawned.start()
    frame = sub.get(5)                                  # nothing was called: the client reconnected by itself
    assert frame.data.shape == (1000,) and not sub.closed
    sub.close()


def test_connecting_to_nothing():
    with pytest.raises(insthub.ConnectionLost, match="cannot connect"):
        insthub.connect("tcp://127.0.0.1:1")
    with pytest.raises(insthub.ConnectionLost, match="no server named"):
        insthub.connect("inproc://ghost")
    with pytest.raises(ValueError, match="bad address"):
        insthub.connect("localhost:1234")


def test_not_an_insthub_server_is_refused():
    import socket
    import threading

    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)

    def http():
        conn, _ = lsock.accept()
        conn.recv(100)
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        conn.close()

    threading.Thread(target=http, daemon=True).start()
    with pytest.raises(insthub.ConnectionLost, match="handshake"):
        insthub.connect(f"tcp://127.0.0.1:{lsock.getsockname()[1]}")
    lsock.close()


def test_garbage_on_the_port_does_not_hurt_the_server(server):
    import socket

    port = int(server.addresses[1].rsplit(":", 1)[1])
    junk = socket.create_connection(("127.0.0.1", port))
    junk.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    junk.settimeout(2)
    junk.recv(1000)                                     # our MAGIC + HELLO; then the server hangs up
    junk.close()
    assert insthub.connect(server.addresses[1]).loopback.echo(np.arange(3)).tolist() == [0, 1, 2]


def test_long_runtime_dir_falls_back_to_tcp(tmp_path, monkeypatch, caplog):
    deep = tmp_path / ("d" * 100)
    monkeypatch.setenv("INSTHUB_DIR", str(deep))
    with insthub.Server("longpath", [sim.Loopback()]) as server:
        assert len(server.addresses) == 1 and server.addresses[0].startswith("tcp://127.0.0.1:")
        assert "too long" in caplog.text
        assert registry.find("loopback")[0]["addresses"] == server.addresses      # still found by name
        assert insthub.connect(server.addresses[0]).loopback.echo(1) == 1
    assert registry.servers() == []


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="Windows: the temporary folder is the user's own")
def test_a_runtime_folder_in_the_shared_temporary_folder_must_be_this_user_s_alone(tmp_path, monkeypatch):
    monkeypatch.delenv("INSTHUB_DIR")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # (what gettempdir() says)
    path = registry.runtime_dir()
    assert path == tmp_path / f"insthub-{os.getuid()}" and path.stat().st_mode & 0o077 == 0
    path.chmod(0o755)  # someone else could have put files in it: refused
    with pytest.raises(PermissionError, match=f"insthub will not use {path}: it is not a folder of this user"):
        registry.servers()
    path.chmod(0o700)
    assert registry.servers() == []
    path.rmdir()
    (tmp_path / "elsewhere").mkdir(mode=0o700)
    path.symlink_to(tmp_path / "elsewhere")  # a link, which another user could have made
    with pytest.raises(PermissionError):
        registry.runtime_dir()
    (tmp_path / "insthub-4242").mkdir(mode=0o700)
    monkeypatch.setattr(os, "getuid", lambda: 4242)  # as if another user made this user's folder
    with pytest.raises(PermissionError):
        registry.runtime_dir()


def test_proxy_notices_new_commands_after_a_server_restart():
    class V1(insthub.Device):
        @insthub.command
        def old(self):
            return 1

    class V2(V1):
        @insthub.command
        def new(self):
            return 2

    server = insthub.Server("up", [V1("dev")], tcp=0).start()
    port = server.addresses[1].rsplit(":", 1)[1]
    dev = insthub.device("dev", server.addresses[1])
    assert dev.old() == 1 and insthub.device("dev", server.addresses[1]) is dev      # proxies are cached
    with pytest.raises(AttributeError):
        dev.new()
    server.stop()
    with insthub.Server("up", [V2("dev")], tcp=port):
        time.sleep(1.1)
        assert dev.new() == 2 and dev.old() == 1


def test_reconnect_storm_does_not_deadlock(spawned):
    """Kill and restart the server repeatedly while a subscription and calls are active."""
    spawned.start()
    scope, lb = insthub.device("scope"), insthub.device("loopback")
    sub = scope.subscribe("ch1")
    for _ in range(3):
        assert sub.get(5).data.shape == (1000,)
        spawned.stop(kill=True)
        with pytest.raises(ConnectionError):
            for _ in range(50):
                lb.echo(1)
                time.sleep(0.01)
        spawned.start()
        assert lb.with_timeout(5).echo(2) == 2
    assert sub.get(5).data.shape == (1000,)
    sub.close()


def test_tcp_only_registry_without_pid_checks(monkeypatch):
    """The path taken on Windows: no Unix socket, and a pid cannot be tested, so a
    stale entry is recognised by asking the server itself."""
    monkeypatch.setattr(registry, "_alive", lambda pid: None)
    script = ("import insthub; from insthub import sim; "
              "insthub.Server('w', [sim.Loopback()], ipc=False, tcp=0).serve_forever()")
    proc = subprocess.Popen([sys.executable, "-c", script], env=os.environ.copy(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    entry_file = registry.runtime_dir() / "w.json"
    try:
        deadline = time.monotonic() + 10
        while not entry_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        (entry,) = registry.servers()
        assert len(entry["addresses"]) == 1 and entry["addresses"][0].startswith("tcp://127.0.0.1:")
        assert insthub.locate("loopback") == entry["addresses"][0]
        assert insthub.device("loopback").echo(5) == 5                   # found by name, over loopback TCP
        with pytest.raises(OSError, match="already running"):
            insthub.Server("w", ipc=False, tcp=0).start()
        proc.kill()
        proc.wait(10)
        insthub.close_all()
        assert entry_file.exists()                                      # killed: it could not clean up
        assert registry.servers() == [] and not entry_file.exists()     # ... and is recognised as gone
        with insthub.Server("w", [sim.Loopback()], ipc=False, tcp=0) as again:   # so the name is free again
            assert [s["pid"] for s in registry.servers()] == [os.getpid()]
            # an entry that points at a live port of a *different* server is stale too
            (registry.runtime_dir() / "ghost.json").write_text(
                '{"server": "ghost", "pid": 1, "devices": ["x"], "addresses": ["%s"]}' % again.addresses[0])
            assert [s["server"] for s in registry.servers()] == ["w"]
    finally:
        proc.kill()
        proc.wait(10)
