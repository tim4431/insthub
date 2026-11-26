import inspect
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

import insthub
from insthub import mcp, render, sim


class ThroughRun:
    """The tools called the way the protocol calls them: through `Tools.run`,
    which turns exceptions into error replies."""

    def __init__(self, tools):
        self.tools = tools

    def run(self, name, arguments):
        return self.tools.run(name, arguments)

    def __getattr__(self, name):
        signature = inspect.signature(getattr(self.tools, name))
        return lambda *a, **kw: self.tools.run(name, dict(signature.bind(*a, **kw).arguments))


@pytest.fixture
def tools(server, tmp_path):
    return ThroughRun(mcp.Tools(render.Renderer(tmp_path / "data"), timeout=10))


def ok(reply):
    assert not reply.error, reply.text
    return json.loads(reply.text.split("\n(")[0])


def failed(reply):
    assert reply.error, reply.text
    return reply.text


# ---------------------------------------------------------------- rendering
def test_render_inline_and_files(tmp_path):
    r = render.Renderer(tmp_path, inline=8, inline_text=50)
    value = {"n": 3, "x": np.float32(0.3), "small": np.arange(3, dtype=np.float32) / 10, "big": np.arange(100.0),
             "raw": b"\x00" * 100, "tiny": b"OK\n", "c": 1 + 2j, "path": Path("/a"), "set": {2, 1},
             "numbers": list(range(20)), "long": "y" * 60, "nan": np.array([1.0, np.nan] * 10, np.float32)}
    out = r.render(value, "dev-cmd").value
    assert out["n"] == 3 and out["x"] == 0.3 and out["small"] == [0.0, 0.1, 0.2]
    assert out["tiny"] == "b'OK\\n'" and out["c"] == "(1+2j)" and out["path"] == "/a" and out["set"] == [1, 2]
    big = out["big"]
    assert big["array"] == "float64[100]" and big["min"] == 0 and big["max"] == 99
    assert big["file"].endswith("-dev-cmd-big.npy") and np.array_equal(np.load(big["file"]), np.arange(100.0))
    assert out["raw"]["bytes"] == 100 and Path(out["raw"]["file"]).read_bytes() == b"\x00" * 100
    assert out["numbers"]["array"] == "int64[20]" and np.load(out["numbers"]["file"])[-1] == 19
    assert out["long"]["text"] == "str[60]" and Path(out["long"]["file"]).read_text() == "y" * 60
    assert out["nan"] == {**out["nan"], "min": 1.0, "max": 1.0, "non_finite": 10}
    json.dumps(out)  # all of it is plain JSON


def test_render_dataclasses_and_names(tmp_path):
    r = render.Renderer(tmp_path)
    wf = sim.Waveform(2, np.zeros(1000, np.float32), 0.0, 1e-6)
    out = r.render(wf, "scope-get_waveform").value
    assert out["channel"] == 2 and out["dt"] == 1e-6 and out["volts"]["array"] == "float32[1000]"
    again = r.render(wf, "scope-get_waveform").value  # same millisecond, same name: still a new file
    assert again["volts"]["file"] != out["volts"]["file"]
    assert len(list(tmp_path.glob("*.npy"))) == 2


def test_render_plot(tmp_path):
    pytest.importorskip("matplotlib")
    r = render.Renderer(tmp_path)
    t = np.arange(5000) * 1e-6
    got = r.render(sim.Waveform(1, np.sin(2e4 * t), 0.0, 1e-6), "scope", plot=True)
    assert got.image.startswith(b"\x89PNG") and Path(got.image_file).read_bytes() == got.image
    assert r.render({"img": np.zeros((50, 60)), "rows": np.ones((2, 100))}, "cam", plot=True).image
    assert "no numeric array" in r.render("text", "x", plot=True).note


# ---------------------------------------------------------------- tools
def test_list_and_describe(tools):
    listing = ok(tools.list_devices())["devices"]
    assert {"scope", "synth", "camera", "sa", "loopback"} <= set(listing)
    assert listing["synth"]["server"] == "t" and listing["synth"]["connected"] is True
    assert "frequency" in listing["synth"]["properties"] and "peak" in listing["sa"]["commands"]
    d = ok(tools.describe("synth"))
    assert d["commands"]["rf"] == {"signature": "(on: bool = True) -> bool", "doc": "Switch the output on or off."}
    assert d["properties"]["locked"]["readonly"] is True and "readonly" not in d["properties"]["frequency"]
    assert d["state"]["frequency_MHz"] == 1000.0 and "sweep" in d["streams"]
    assert ok(tools.describe("scope"))["tasks"]["_acquire"]["enabled"] is True
    assert "no device 'nope'" in failed(tools.describe("nope"))


def test_a_paused_device_says_so(tools, server):
    server.device("synth").bus.pause(by="the experimenter")
    assert ok(tools.list_devices())["devices"]["synth"]["paused"] is True
    text = failed(tools.get("synth", "frequency"))
    assert "synth is paused" in text and "the experimenter" in text and "do not resume it" in text
    assert "insthub resume" not in text                                   # not the hint meant for people
    start = time.monotonic()
    text = failed(tools.snapshot("synth", "sweep", timeout=5))                       # no waiting
    assert "publishes nothing" in text and text.count("do not resume it") == 1 and ".." not in text
    assert "insthub resume" not in text
    assert "publishes nothing" in failed(tools.snapshot("synth", "sweep", fresh=0, timeout=5))  # nothing kept
    assert time.monotonic() - start < 2
    server.device("synth").bus.resume()
    assert "paused" not in ok(tools.list_devices())["devices"]["synth"]


def test_a_snapshot_of_a_paused_device(tools, server):
    scope = server.device("scope")
    with scope.bus.subscribe("ch1", retained=False) as sub:
        sub.get(5)                                                       # it has published a frame
    scope.bus.pause(by="the experimenter")
    try:
        frame = ok(tools.snapshot("scope", "ch1", fresh=0))              # the one from before the pause
        assert frame["paused"] is True and frame["seq"] >= 1
        assert "paused" not in ok(tools.snapshot("sa", "trace", fresh=0))    # another device: as before
    finally:
        scope.bus.resume()
    assert "paused" not in ok(tools.snapshot("scope", "ch1"))


def test_a_pause_during_a_snapshot_says_so(tools, server):
    synth = server.device("synth")
    threading.Timer(0.3, lambda: synth.bus.pause(by="the experimenter")).start()
    try:
        text = failed(tools.snapshot("synth", "sweep", timeout=1.5))     # nothing published while waiting
        assert "synth is paused" in text and "the experimenter" in text and "do not resume it" in text
    finally:
        synth.bus.resume()


def test_get_set_call(tools):
    assert tools.set("synth", "frequency", 2500).text == "ok"
    assert ok(tools.get("synth", "frequency")) == 2500.0
    wf = ok(tools.call("scope", "get_waveform", [2]))
    assert wf["channel"] == 2 and np.load(wf["volts"]["file"]).shape == (1000,)
    assert ok(tools.call("scope", "measure", kwargs={"item": "FREQ"})) == 1000.0
    assert ok(tools.call("scope", "measure", '["FREQ"]')) == 1000.0  # the list as JSON text
    assert failed(tools.set("synth", "frequency", 1e6)).startswith("ValueError: frequency 1000000.0 MHz outside")
    assert "no command 'nope'" in failed(tools.call("scope", "nope"))
    assert "unexpected keyword" in failed(tools.run("get", {"device": "synth", "property": "power", "x": 1}))
    assert "unknown tool" in failed(tools.run("rm", {}))
    assert ok(tools.call("scope", "$release"))["connected"] is False
    assert ok(tools.call("scope", "$open"))["connected"] is True


class Laser(insthub.Device):
    """A laser: firing it, or raising its power, is dangerous."""

    def open(self):
        self.fired, self._power = 0, 1.0

    @insthub.prop(unit="mW", max=50, dangerous=True)
    def power(self) -> float:
        return self._power

    @power.setter
    def power(self, mw):
        self._power = mw

    @insthub.command(dangerous=True, params={"ms": {"unit": "ms"}})
    def fire(self, ms=10):
        self.fired += 1
        return ms

    @insthub.command
    def status_word(self):
        return "ok"


def test_what_is_dangerous_needs_its_own_tool(tools):
    with insthub.Server("l", [Laser("laser")]) as server:
        laser = server.devices["laser"]
        d = ok(tools.describe("laser"))
        assert d["commands"]["fire"] == {"signature": "(ms=10)", "params": {"ms": {"unit": "ms"}}, "dangerous": True}
        assert d["properties"]["power"] == {"meta": {"unit": "mW", "max": 50}, "dangerous": True}
        assert ok(tools.call("laser", "status_word")) == "ok" and ok(tools.get("laser", "power")) == 1.0
        for refused in (tools.call("laser", "fire", [5]), tools.set("laser", "power", 40),
                        tools.call("laser", "$set", ["power", 40])):
            text = failed(refused)
            assert text.startswith("PermissionError: laser: ") and "is marked dangerous" in text
            assert "call_dangerous('laser', " in text
        assert "$set\", ['power', value]" in failed(tools.set("laser", "power", 40))
        assert laser.fired == 0 and laser.power == 1.0                      # nothing ran
        assert ok(tools.call_dangerous("laser", "fire", kwargs={"ms": 5})) == 5 and laser.fired == 1
        assert ok(tools.call_dangerous("laser", "$set", ["power", 40])) is None and laser.power == 40
        assert ok(tools.call("laser", "$release"))["connected"] is False       # managing the session is not


class Shutter:
    """A driver whose output is an accessor pair: get_output() and set_output(on)."""

    def __init__(self):
        self.on = False

    def get_output(self):
        return self.on

    def set_output(self, on):
        self.on = on


def test_either_half_of_a_dangerous_accessor_pair_needs_its_own_tool(tools):
    for i, marked in enumerate(("set_output", "output")):  # the command and the property write one setting
        shutter = insthub.wrap(Shutter, "shutter", accessors=True, dangerous=[marked])
        with insthub.Server(f"s{i}", [shutter]):
            for refused in (tools.set("shutter", "output", True), tools.call("shutter", "set_output", [True]),
                            tools.call("shutter", "$set", ["output", True])):
                assert "is marked dangerous" in failed(refused), marked
            assert shutter.target.on is False and ok(tools.call("shutter", "get_output")) is False  # reading is not
            assert ok(tools.call_dangerous("shutter", "set_output", [True])) is None and shutter.target.on is True


def test_tools_say_what_a_device_is(tools):
    listing = ok(tools.list_devices())["devices"]
    assert listing["synth"]["simulated"] is True and listing["synth"]["extension"] == "insthub-sim"
    d = ok(tools.describe("synth"))
    assert d["simulated"] is True and d["properties"]["frequency"]["meta"] == {"unit": "MHz", "min": 10, "max": 24000}
    assert ok(tools.describe("scope"))["commands"]["measure"]["params"]["item"] == {"choices": ["VPP", "VRMS", "FREQ"]}
    dangerous = next(t for t in mcp.TOOLS if t["name"] == "call_dangerous")
    assert dangerous["annotations"]["destructiveHint"] is True and "dangerous" in mcp.INSTRUCTIONS


def test_call_timeout_does_not_block_other_devices(tools):
    start = time.monotonic()
    assert "no reply within 0.2 s" in failed(tools.call("loopback", "sleep", [1.0], timeout=0.2))
    assert ok(tools.get("synth", "power")) == 0.0
    assert time.monotonic() - start < 0.8


def test_file_arguments(tools, tmp_path):
    np.save(tmp_path / "a.npy", np.arange(1000))
    np.savez(tmp_path / "b.npz", x=np.arange(3), y=np.ones(2))
    (tmp_path / "c.bin").write_bytes(b"abc")
    echoed = ok(tools.call("loopback", "echo", [{"$file": str(tmp_path / "a.npy")}]))
    assert echoed["array"] == "int64[1000]" and np.array_equal(np.load(echoed["file"]), np.arange(1000))
    assert ok(tools.call("loopback", "echo", kwargs={"value": {"$file": str(tmp_path / "b.npz"), "key": "x"}})) \
        == [0, 1, 2]
    assert ok(tools.call("loopback", "echo", [[{"$file": str(tmp_path / "c.bin")}]])) == ["b'abc'"]
    assert "add \"key\"" in failed(tools.call("loopback", "echo", [{"$file": str(tmp_path / "b.npz")}]))


def test_snapshot(tools):
    snap = ok(tools.snapshot("scope", "ch1"))
    assert snap["data"]["array"] == "float32[1000]" and snap["meta"]["dt"] == 1e-5 and snap["age_s"] < 1
    state = ok(tools.snapshot("synth", "state", fresh=0))
    assert state["data"]["frequency_MHz"] == 1000.0
    assert "has no stream 'ch3' (it has: ch1, ch2, state" in failed(tools.snapshot("scope", "ch3"))
    text = failed(tools.snapshot("synth", "sweep", timeout=0.2))
    assert "no new frame on synth.sweep within 0.2 s (nothing has been published" in text
    first, second = ok(tools.snapshot("sa", "trace")), ok(tools.snapshot("sa", "trace", fresh=2))
    assert second["seq"] >= first["seq"] + 2


def test_synth_into_analyzer(tools):
    """The debugging loop the MCP server is for: make a tone, look at it."""
    assert ok(tools.call("sa", "peak"))["level_dBm"] < -80  # output off: noise only
    tools.set("synth", "frequency", 1002.5)
    tools.set("synth", "power", -5)
    tools.call("synth", "rf", [True])
    peak = ok(tools.call("sa", "peak"))
    assert abs(peak["frequency_Hz"] - 1.0025e9) < 20e3 and abs(peak["level_dBm"] + 25) < 1
    snap = ok(tools.snapshot("sa", "trace"))
    trace = np.load(snap["data"]["file"])
    x = snap["meta"]["x0"] + snap["meta"]["dx"] * np.arange(len(trace))
    assert abs(x[np.argmax(trace)] - 1.0025e9) < 20e3 and snap["meta"]["unit"] == "dBm"


def test_snapshot_plot(tools):
    pytest.importorskip("matplotlib")
    reply = tools.snapshot("sa", "trace", plot=True)
    assert not reply.error and reply.image.startswith(b"\x89PNG") and "plot saved to" in reply.text
    content = reply.to_mcp()["content"]
    assert [c["type"] for c in content] == ["text", "image"] and content[1]["mimeType"] == "image/png"


# ---------------------------------------------------------------- protocol
class Agent:
    """Talks to `insthub mcp` in a child process, the way Claude Code does."""

    def __init__(self, data):
        self.proc = subprocess.Popen([sys.executable, "-m", "insthub", "mcp", "--data", str(data)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=os.environ.copy(), text=True)
        self.lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(line)

    def send(self, **msg):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        self.proc.stdin.flush()

    def receive(self):
        return json.loads(self.lines.get(timeout=10))

    def close(self):
        self.proc.stdin.close()
        return self.proc.wait(10)


def test_stdio_protocol(spawned, tmp_path):
    spawned.start()
    agent = Agent(tmp_path)
    try:
        agent.send(id=1, method="initialize", params={"protocolVersion": "2025-06-18", "capabilities": {},
                                                      "clientInfo": {"name": "test", "version": "0"}})
        init = agent.receive()
        assert init["id"] == 1 and init["result"]["protocolVersion"] == "2025-06-18"
        assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
        assert "list_devices" in init["result"]["instructions"]
        agent.send(method="notifications/initialized")
        agent.send(id=2, method="tools/list")
        names = [t["name"] for t in agent.receive()["result"]["tools"]]
        assert names == ["list_devices", "describe", "get", "set", "call", "snapshot", "call_dangerous"]
        agent.send(id=3, method="tools/call", params={"name": "call", "arguments": {
            "device": "loopback", "command": "sleep", "args": [0.5]}})
        agent.send(id=4, method="tools/call", params={"name": "get", "arguments": {
            "device": "synth", "property": "frequency"}})
        agent.send(id=5, method="ping")
        agent.send(id=6, method="resources/list")
        got = {m["id"]: m for m in (agent.receive() for _ in range(4))}
        assert got[4]["result"] == {"content": [{"type": "text", "text": "1000.0"}], "isError": False}
        assert got[5]["result"] == {} and got[6]["error"]["code"] == -32601
        assert json.loads(got[3]["result"]["content"][0]["text"]) == 0.5
        agent.send(id=7, method="initialize", params={"protocolVersion": "1999-01-01"})
        assert agent.receive()["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]
        agent.send(id=8, method="tools/call", params={"name": "describe", "arguments": {"device": "nope"}})
        reply = agent.receive()["result"]
        assert reply["isError"] is True and "no device 'nope'" in reply["content"][0]["text"]
        agent.send(id=9, method="tools/call", params={"name": "set", "arguments": {
            "device": "synth", "property": "frequency", "value": 1e6}})
        text = agent.receive()["result"]["content"][0]["text"]  # the driver's error, and where it was raised
        assert text.startswith("ValueError: frequency 1000000.0 MHz outside") and "remote traceback" in text
        assert "sim.py" in text
    finally:
        assert agent.close() == 0


def test_an_agent_that_paused_a_device_is_not_told_a_person_did(tools, server):
    ok(tools.call("synth", "$pause"))
    try:
        assert server.device("synth").bus.status()["paused"]["by"] == "an AI agent (insthub mcp)"
        text = failed(tools.get("synth", "frequency"))
        assert "by an AI agent" in text and "A person paused it" not in text and "$resume" in text
    finally:
        ok(tools.call("synth", "$resume"))


def test_an_agents_pause_behind_a_long_command_is_not_reported_as_failed(server, tmp_path):
    tools = ThroughRun(mcp.Tools(render.Renderer(tmp_path / "data"), timeout=10))
    slow = server.device("loopback").sleep.future(2.0)
    time.sleep(0.1)
    try:
        reply = ok(tools.call("loopback", "$pause", timeout=0.5))
        assert reply["paused"] and "refuses every call already" in reply["note"] and "sleep" in reply["note"]
    finally:
        slow.result(5)
        server.device("loopback").bus.resume()


def test_agents_see_where_a_device_is_and_what_it_is(tmp_path):
    class Scpi:
        def __init__(self, host, port=5000):
            pass

    rp = insthub.wrap(Scpi, "rp", kwargs={"host": "10.0.0.5"})
    rp.description = "the one on the optics table"
    with insthub.Server("w", [rp]):
        tools = ThroughRun(mcp.Tools(render.Renderer(tmp_path / "data"), timeout=10))
        listed = ok(tools.list_devices())["devices"]["rp"]
        assert listed["address"] == "10.0.0.5" and listed["description"] == "the one on the optics table"
        assert ok(tools.describe("rp"))["address"] == "10.0.0.5"


def test_a_device_is_listed_where_the_other_tools_find_it(tmp_path):
    """Of two servers that have a device of one name, list_devices shows the one that describe and call
    reach (as insthub.locate places it), or, if they refuse it as ambiguous, says so and shows none."""
    from insthub import registry

    def far(name):  # a server "on another computer": it announces itself where this test does not look
        folder = tmp_path / name
        proc = subprocess.Popen([sys.executable, "-m", "insthub", "sim", "--name", name, "--tcp", "0"],
                                env={**os.environ, "INSTHUB_DIR": str(folder)}, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        end = time.monotonic() + 15
        while time.monotonic() < end:
            try:
                entry = json.loads((folder / f"{name}.json").read_text())
                return proc, next(a for a in entry["addresses"] if a.startswith("tcp://"))
            except (OSError, ValueError, StopIteration):
                time.sleep(0.05)
        proc.kill()
        raise TimeoutError(f"{name} did not come up")

    started = [far("srva"), far("srvb")]
    try:
        (_, a), (_, b) = started
        tools = ThroughRun(mcp.Tools(render.Renderer(tmp_path / "data"), timeout=10))
        registry.devices_file().write_text(f'[servers]\nlabpc = "{a}"\nrig = "{b}"\n')
        listed = ok(tools.list_devices())
        assert "scope" not in listed["devices"]                              # both have one: neither is guessed
        assert any("device name 'scope' is ambiguous: servers on other computers" in n for n in listed["notes"])
        assert "is ambiguous" in failed(tools.describe("scope"))             # as the other tools say
        registry.devices_file().write_text(f'[servers]\nlabpc = "{a}"\n\n[devices]\nscope = "{b}"\n')
        insthub.client._remote_seen[0] = float("-inf")                       # (devices.toml changed)
        listed = ok(tools.list_devices())
        assert listed["devices"]["scope"]["server"] == "srvb"                 # [devices] places it: no question
        assert not any("'scope'" in n for n in listed["notes"]) and "synth" not in listed["devices"]
        assert any("device name 'synth' is ambiguous" in n for n in listed["notes"])  # (the others are on both)
        assert tools.tools._bus("scope").address == b                       # where describe and call go
    finally:
        for proc, _ in started:
            proc.terminate()
            proc.wait(10)


def test_list_devices_says_when_this_program_runs_older_code(tools, monkeypatch):
    """So that the agent can ask its user to reconnect it; there is no tool that restarts anything."""
    assert "notes" not in ok(tools.list_devices())
    monkeypatch.setattr(mcp.extensions, "outdated_here", lambda: ["insthub/mcp.py changed since it started"])
    [note] = ok(tools.list_devices())["notes"]
    assert note == ("this insthub mcp runs older insthub code than the disk holds (insthub/mcp.py changed since it "
                    "started): ask the user to reconnect it, which starts it anew (in Claude Code: /mcp)")
    assert not any("restart" in t["name"] or "update" in t["name"] for t in mcp.TOOLS)
