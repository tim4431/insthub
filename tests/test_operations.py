"""Operations: a server's log read from elsewhere, every device's state at once, servers on other
computers found from devices.toml, and connecting with a time limit."""
import json
import logging
import os
import socket
import subprocess
import sys
import textwrap
import time

import pytest

import insthub
from insthub import cli, client, inventory, mcp, registry, server, sim
from insthub.client import log_text


class Chatty(insthub.Device):
    """Logs what it is told to."""

    def open(self):
        pass

    @insthub.command
    def say(self, text: str, level: str = "WARNING"):
        self.log.log(getattr(logging, level), "said %s", text)

    @insthub.command
    def fail(self, text: str):
        try:
            raise ZeroDivisionError(text)
        except ZeroDivisionError:
            self.log.exception("could not divide")


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def wait_for(condition, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def far(tmp_path):
    """A server "on another computer": it announces itself in a folder this test's programs do not
    look in, so it is reached only through its TCP address."""
    elsewhere = tmp_path / "elsewhere"
    proc = subprocess.Popen([sys.executable, "-m", "insthub", "sim", "--name", "far", "--tcp", "0"],
                            env={**os.environ, "INSTHUB_DIR": str(elsewhere)}, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)

    def address():
        try:
            entry = json.loads((elsewhere / "far.json").read_text())
        except (OSError, ValueError):
            return None
        return next((a for a in entry["addresses"] if a.startswith("tcp://")), None)

    try:
        assert wait_for(address), "the far server did not come up"
        yield address()
    finally:
        proc.terminate()
        proc.wait(10)


def test_a_server_keeps_its_log_for_others_to_read():
    with (insthub.Server("chat", [Chatty("chatty")], tcp=0) as srv,
          insthub.connect(srv.addresses[1]) as client):
        dev = client.device("chatty")
        mark = (client.logs(1) or [{"seq": 0}])[-1]["seq"]                 # (the log is the whole process's)
        dev.say("one")
        dev.say("two", "ERROR")
        dev.fail("by nothing")
        records = client.logs(since=mark)
        assert [(r["level"], r["logger"], r["message"]) for r in records] == [
            ("WARNING", "insthub.chatty", "said one"), ("ERROR", "insthub.chatty", "said two"),
            ("ERROR", "insthub.chatty", "could not divide")]
        assert records[0]["exc"] is None and "ZeroDivisionError: by nothing" in records[2]["exc"]
        errors = client.logs(since=mark, level="error")                    # (a name in any case, or a number)
        assert [r["message"] for r in errors] == ["said two", "could not divide"] == [
            r["message"] for r in client.logs(since=mark, level=logging.ERROR)]
        assert [r["message"] for r in client.logs(1, since=mark)] == ["could not divide"]
        assert client.logs(since=records[-1]["seq"]) == []                 # following it: nothing new yet
        text = log_text(records)
        assert " WARNING insthub.chatty: said one\n" in text and "ZeroDivisionError: by nothing" in text
        with pytest.raises(ValueError, match="unknown log level 'LOUD'"):
            client.logs(level="LOUD")
        assert server.LOG_RECORDS == 1000 and client.supports("logs")


class Unplugged(insthub.Device):
    def open(self):
        raise RuntimeError("instrument unplugged")


def test_what_starting_the_devices_logs_is_kept():
    with insthub.Server("plug", [Unplugged("bad")], ipc=False, tcp=0) as srv, insthub.connect(srv.addresses[0]) as c:
        assert wait_for(lambda: any(r["message"].startswith("not connected at start: bad: cannot open")
                                    for r in c.logs(level="WARNING")))


def test_keeping_the_log_hides_nothing_from_stderr(tmp_path):
    script = tmp_path / "host.py"
    for setup, shown in (("", "a warning after start\n"),                       # Python's last resort: once
                         ("import logging; logging.basicConfig(format='%(levelname)s %(message)s')\n",
                          "WARNING a warning after start\n")):                   # its own handler: once
        script.write_text(setup + textwrap.dedent("""\
            import logging, insthub
            from insthub import sim
            with insthub.Server("host", [sim.Loopback()], ipc=False):
                logging.getLogger("insthub.x").warning("a warning after start")
                logging.getLogger("insthub.x").info("news that nobody asked for")
            """))
        done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30,
                              check=False)
        assert done.returncode == 0 and done.stderr == shown, done.stderr


def test_a_server_that_keeps_no_log_says_so(monkeypatch, capsys):
    monkeypatch.setattr(server, "FEATURES", server.FEATURES - {"logs"})   # as an older insthub
    with insthub.Server("old", [sim.Loopback()], tcp=0) as srv, insthub.connect(srv.addresses[1]) as client:
        with pytest.raises(insthub.BusError, match="old runs an older insthub, which keeps no log"):
            client.logs()
        code, _, err = run(capsys, "-a", srv.addresses[1], "logs")
        assert code == 1 and "keeps no log for others to read" in err


def test_insthub_logs_shows_what_another_program_logged(spawned, capsys):
    spawned.start()
    code, out, _ = run(capsys, "logs", "child")                            # by the server's name ...
    assert code == 0 and "INFO insthub: server 'child' up" in out          # (insthub sim keeps INFO too)
    code, out, _ = run(capsys, "logs", "scope", "-n", "1", "--level", "info")   # ... or one of its devices'
    assert code == 0 and len(out.splitlines()) == 1
    code, _, err = run(capsys, "logs", "nobody")
    assert code == 1 and "no server or device named 'nobody'" in err


def test_insthub_logs_follows_a_log(monkeypatch, capsys):
    with insthub.Server("chat", [Chatty("chatty")], tcp=0) as srv:
        dev = srv.device("chatty")
        dev.say("before")
        naps = []

        def nap(seconds):                                                  # each second of following
            naps.append(seconds)
            if len(naps) == 1:
                dev.say("during")
            elif len(naps) == 3:
                raise KeyboardInterrupt                                    # Ctrl-C ends it, well

        monkeypatch.setattr(cli.time, "sleep", nap)
        code, out, _ = run(capsys, "-a", srv.addresses[1], "logs", "-n", "1", "--level", "WARNING", "-f")
    assert code == 0 and [line.split(": ", 1)[1] for line in out.splitlines()] == ["said before",
                                                                                   "said during"]


def test_insthub_logs_follows_a_server_across_a_restart(spawned, monkeypatch, capsys):
    spawned.start()
    old = next(s["pid"] for s in registry.servers() if s["server"] == "child")
    naps = []

    def nap(seconds):                                                      # each second of following
        naps.append(seconds)
        if len(naps) == 1:
            spawned.stop()                                                 # it goes away ...
        elif len(naps) == 2:
            spawned.start()                                                # ... and comes back, another process
        elif len(naps) == 4:
            raise KeyboardInterrupt

    class Clock:                                                           # cli's time, with that sleep
        sleep = staticmethod(nap)

        def __getattr__(self, name):
            return getattr(time, name)

    monkeypatch.setattr(cli, "time", Clock())
    code, out, _ = run(capsys, "logs", "child", "-n", "1", "-f")
    assert code == 0 and naps == [1.0] * 4
    lines = out.splitlines()
    assert len(lines) > 3 and lines[1:3] == ["--- child does not answer; waiting for it", "--- child started again"]
    new = next(s["pid"] for s in registry.servers() if s["server"] == "child")
    assert new != old and any("server 'child' up" in line for line in lines[3:])   # the new run's, from its start


def test_an_exception_of_the_main_thread_is_logged_unless_the_program_has_its_own_hook(tmp_path):
    script = tmp_path / "main.py"
    for own, shown in (("", "CRITICAL root: uncaught exception\nTraceback"),
                       ("sys.excepthook = lambda *a: print('its own')\n", "")):
        script.write_text(f"import sys\nfrom insthub import server\n{own}server.keep_logs(everything=True)\n"
                          "raise ZeroDivisionError('in the main thread')\n")
        done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30,
                              check=False)
        assert done.returncode == 1 and shown in done.stderr
        assert done.stderr.count("ZeroDivisionError: in the main thread") == (1 if not own else 0)
        assert done.stdout == ("its own\n" if own else "")


def test_what_nothing_caught_goes_to_the_log(tmp_path):
    script = tmp_path / "hooked.py"
    script.write_text(textwrap.dedent("""\
        import threading
        import insthub
        from insthub import sim

        threading.Timer(0.3, lambda: 1 / 0).start()   # an exception nothing catches, in a thread
        insthub.serve(sim.Loopback(), name="hooked")
        """))
    proc = subprocess.Popen([sys.executable, str(script)], env=os.environ.copy(), stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    try:
        assert wait_for(lambda: any(s["server"] == "hooked" for s in registry.servers()))
        client = insthub.connect(insthub.locate("loopback"))
        assert wait_for(lambda: client.logs(level="ERROR"))
        (record,) = client.logs(level="ERROR")
        assert record["message"].startswith("uncaught exception in thread")
        assert "ZeroDivisionError" in record["exc"]
        client.close()
    finally:
        proc.terminate()
        _, err = proc.communicate(timeout=10)
    assert err.decode().count("ZeroDivisionError") == 1                    # in its output too, once


def test_the_state_of_every_device_at_once(server, monkeypatch, capsys):
    registry.devices_file().write_text('[devices]\nghost = "tcp://127.0.0.1:9"\n')    # nothing listens there
    found = insthub.states()
    assert {"scope", "synth", "camera", "ghost"} <= set(found)
    scope = found["scope"]
    assert (scope["server"], scope["type"], scope["extension"], scope["connected"]) == ("t", "SimScope",
                                                                                         "insthub-sim", True)
    assert scope["state"]["timebase"] == 1e-3 and abs(scope["time"] - time.time()) < 5
    assert found["ghost"]["server"] is None and "cannot connect" in found["ghost"]["error"]
    assert set(insthub.states(["synth", "nope"])) == {"synth", "nope"}
    (nope,) = insthub.states("nope").values()
    assert set(nope) == {"error"} and nope["error"].startswith("no device 'nope' on the bus (known devices: ")
    code, out, _ = run(capsys, "states", "synth", "--json")
    assert code == 0 and json.loads(out)["synth"]["state"]["frequency_MHz"] == 1000.0
    code, out, _ = run(capsys, "states")                                   # every device: most are read
    assert code == 0 and "scope  (SimScope on t, connected)\n    timebase: 0.001" in out
    assert "ghost: cannot connect" in out
    for named in (["scope", "ghost"], ["nosuch"], ["nosuch", "--json"]):  # one you named is not read: 1
        code, out, _ = run(capsys, "states", *named)
        assert code == 1 and ("ghost: cannot connect" in out or "no device 'nosuch'" in out)
    code, out, _ = run(capsys, "-a", server.addresses[1], "states")        # one server's devices only
    assert code == 0 and "scope  (SimScope on t" in out and "ghost" not in out
    assert set(insthub.states(address=server.addresses[1])) == set(server.devices)
    code, out, _ = run(capsys, "-a", server.addresses[1], "states", "synth", "nope")
    assert code == 1 and "synth  (SimSynth on t" in out and "nope  (on t): no device 'nope' on server 't'" in out
    code, _, err = run(capsys, "-a", "tcp://127.0.0.1:9", "states")
    assert code == 1 and err.startswith("error: cannot connect to tcp://127.0.0.1:9")


def test_a_device_name_two_servers_have_is_not_guessed():
    for name, devices in (("a", ["scope", "only_a"]), ("b", ["scope"])):  # (they need not answer for this)
        registry.write(name, {"server": name, "pid": os.getpid(), "devices": devices,
                              "addresses": [f"tcp://127.0.0.1:9{len(name)}"]})
    found = insthub.states()
    assert found["scope"] == {"error": "device name 'scope' is ambiguous: servers ['a', 'b'] all have one; "
                                       "pass address="}
    assert found["only_a"]["server"] is None and "cannot connect" in found["only_a"]["error"]


def test_devices_toml_names_the_servers_on_other_computers():
    registry.devices_file().write_text('[servers]\nlabpc = "tcp://h:1"\nrig = "tcp://h:2"\n\n'
                                       '[devices]\nawg = "tcp://h:2"\npsu = "tcp://h:3"\nsa = "tcp://h:3"\n')
    assert registry.remote_addresses() == {"tcp://h:1": [], "tcp://h:2": ["awg"], "tcp://h:3": ["psu", "sa"]}
    assert registry.static_devices() == {"awg": "tcp://h:2", "psu": "tcp://h:3", "sa": "tcp://h:3"}
    registry.devices_file().unlink()
    assert registry.remote_addresses() == {}


def test_servers_on_other_computers_are_asked_for_their_devices(far, capsys):
    also = far.replace("127.0.0.1", "localhost")                           # the same server, by another name
    registry.devices_file().write_text(f'[servers]\nlabpc = "{far}"\nagain = "{also}"\n\n'
                                       '[devices]\nghost = "tcp://127.0.0.1:9"\n')
    assert insthub.discover() == []                                        # this computer's: none
    there, ghost = insthub.discover(remote=True)                           # (far once)
    assert (there["server"], there["address"], there["addresses"], there["remote"]) == ("far", far, [far], True)
    assert "scope" in there["devices"] and "error" not in there
    assert (ghost["server"], ghost["devices"], ghost["address"]) == (None, ["ghost"], "tcp://127.0.0.1:9")
    assert "cannot connect" in ghost["error"]
    assert insthub.locate("scope") == far                                  # not in devices.toml: asked
    assert insthub.device("scope").identify().startswith("insthub")
    with pytest.raises(insthub.DeviceNotFound, match="known devices: camera, ghost, loopback, sa, scope"):
        insthub.locate("laser")
    code, out, _ = run(capsys, "list")
    assert code == 0 and "far  (pid " in out and f" on another computer)  {far}" in out
    assert "tcp://127.0.0.1:9  (on another computer)\n    unreachable: cannot connect" in out
    code, out, _ = run(capsys, "logs", "far", "-n", "1")                   # by its name, from here
    assert code == 0 and out.count("\n") == 1


def test_looking_at_a_server_elsewhere_keeps_no_connection_to_it(far):
    registry.devices_file().write_text(f'[servers]\nlabpc = "{far}"\n')
    tools = mcp.Tools()
    assert "scope" in json.loads(tools.list_devices().text)["devices"]
    with insthub.connect(far) as look:                                     # the agent's one, and this one
        assert wait_for(lambda: len(look.info()["clients"]) == 2)


def test_a_server_here_named_by_another_address_is_listed_once(capsys):
    with insthub.Server("lan", [sim.Loopback()], ipc=False, tcp=0) as srv:
        port = srv.addresses[0].rpartition(":")[2]
        registry.devices_file().write_text(f'[servers]\nlabpc = "tcp://localhost:{port}"\n')  # (not announced)
        (entry,) = insthub.discover(remote=True)
        assert (entry["server"], entry["address"], "remote" in entry) == ("lan", "inproc://lan", False)
        code, out, _ = run(capsys, "list")
        assert code == 0 and out.count("lan  (pid") == 1 and "another computer" not in out
        notes = json.loads(mcp.Tools().list_devices().text).get("notes", [])
        assert not any("lan" in note for note in notes)


def test_a_broken_devices_toml_is_named_and_the_rest_still_works(server, capsys):
    registry.devices_file().write_text("[servers\n")
    path = str(registry.devices_file())
    *_, unread = insthub.discover(remote=True)
    assert unread["address"] is None and unread["error"].startswith(f"{path}: Expected ']'")
    code, out, err = run(capsys, "list")
    assert code == 0 and "t  (pid" in out and err.startswith(f"error: {path}: Expected ']'")
    code, out, _ = run(capsys, "states", "scope")
    assert code == 0 and "scope  (SimScope on t" in out
    code, out, _ = run(capsys, "logs", "t", "-n", "1")
    assert code == 0 and out
    code, _, err = run(capsys, "logs", "nobody")                           # looked for elsewhere: it says why not
    assert code == 1 and err.startswith(f"error: {path}: Expected ']'")
    assert f"{path}: Expected ']'" in json.loads(mcp.Tools().list_devices().text)["notes"][0]
    assert inventory.Backend().scan()["remote_error"].startswith(f"{path}: Expected ']'")
    with pytest.raises(insthub.BusError, match="Expected"):
        insthub.locate("nobody")


def test_this_computer_is_looked_at_first(server, monkeypatch, capsys):
    registry.devices_file().write_text('[servers]\nlabpc = "tcp://h:1"\n')
    asked = []
    monkeypatch.setattr(client, "_ask_remote", lambda address, listed: asked.append(address) or (
        client._unanswered(address, listed, "no answer")))
    for name in ("t", "scope"):                                            # a server here, a device here
        code, out, _ = run(capsys, "logs", name, "-n", "1")
        assert code == 0 and out
    assert asked == []
    code, _, err = run(capsys, "logs", "labpc")
    assert code == 1 and "no server or device named 'labpc'" in err and asked == ["tcp://h:1"]   # asked once


def test_a_server_elsewhere_that_says_nothing_is_given_up_on(monkeypatch):
    monkeypatch.setattr(client, "_REMOTE_TIMEOUT", 0.2)
    with socket.create_server(("127.0.0.1", 0)) as silent:                 # accepts, and never says hello
        registry.devices_file().write_text(f'[servers]\nmute = "tcp://127.0.0.1:{silent.getsockname()[1]}"\n')
        start = time.monotonic()
        (entry,) = insthub.discover(remote=True)
        assert time.monotonic() - start < 1.5 and "handshake" in entry["error"]


def test_locate_believes_what_servers_elsewhere_said_for_a_few_seconds(monkeypatch):
    registry.devices_file().write_text('[servers]\nlabpc = "tcp://h:1"\n')
    asked = []

    def ask(address, listed):
        asked.append(address)
        return {"server": "labpc", "pid": 1, "started": 1.0, "devices": ["laser"], "addresses": [address],
                "address": address, "remote": True}

    monkeypatch.setattr(client, "_ask_remote", ask)
    assert insthub.locate("laser") == "tcp://h:1" and len(asked) == 1
    assert insthub.locate("laser") == "tcp://h:1" and len(asked) == 1     # within 5 s: what it said
    with pytest.raises(insthub.DeviceNotFound, match="known devices: laser"):
        insthub.locate("nothing")                                          # a miss too
    assert len(asked) == 1
    client._remote_seen[0] -= client._REMOTE_FRESH                         # 5 s later
    assert insthub.locate("laser") == "tcp://h:1" and len(asked) == 2
    registry.devices_file().write_text('[servers]\nlabpc = "tcp://h:2"\n')  # devices.toml changed: asked at once
    assert insthub.locate("laser") == "tcp://h:2" and asked[-1] == "tcp://h:2"
    insthub.discover(remote=True)                                          # discover() asks every time
    assert len(asked) == 4


def test_connect_gives_up_after_its_connect_timeout():
    with socket.create_server(("127.0.0.1", 0)) as silent:                 # accepts, and never says hello
        start = time.monotonic()
        with pytest.raises(insthub.ConnectionLost, match="handshake"):
            insthub.connect(f"tcp://127.0.0.1:{silent.getsockname()[1]}", connect_timeout=0.3)
        assert time.monotonic() - start < 2
